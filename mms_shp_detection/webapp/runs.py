from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mms_shp_detection.config import (
    PipelineConfig,
    config_file_sha256,
    config_sha256,
)
from mms_shp_detection.domain.models import JobStatus, PipelineErrorInfo
from mms_shp_detection.infrastructure.manifest_writer import (
    RunManifestStore,
    validate_manifest_document,
    validate_published_outputs,
)

from .datasets import catalog_path as dataset_catalog_path
from .datasets import require_ready_dataset, seed_catalog_cache, utc_now
from .optimizer import resolve_run_parameters
from .security import UnsafePath, normalize_relative_path, resolve_under_root

router = APIRouter(prefix="/api", tags=["runs"])

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
PUBLIC_STATUS = {
    "starting": "preparing",
    "interrupted": "failed",
    "error": "failed",
}
MAX_RUN_MANIFEST_BYTES = 5_000_000
MAX_PUBLIC_STRUCTURED_ARTIFACT_BYTES = 25_000_000
RUN_EXECUTION_CONTRACT_VERSION = 1
_INLINE_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>]+"
)
_INLINE_FILE_URI = re.compile(
    r"(?i)\bfile:(?:(?:/{1,3}|\\\\{1,2})[^\s\"'<>]+|[A-Z]:[\\/][^\s\"'<>]+)"
)
_INLINE_FORWARD_UNC_PATH = re.compile(
    r"(?<![A-Za-z0-9:/])//[^/\s\"'<>]+(?:/[^/\s\"'<>]+)+"
)
_INLINE_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9:/>])/(?:[^/\s\"'<>]+(?:/[^/\s\"'<>]+)*)"
)
PATH_OPTION_NAMES = {
    "data_root",
    "calibration_path",
    "model_path",
    "model_dir",
    "output_dir",
    "pointcloud_cache_path",
    "pcdb_cache_path",
    "crs_wkt_path",
}
SAFE_RESULT_SUFFIXES = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qpj",
    ".wkt2",
    ".json",
    ".csv",
    ".txt",
    ".las",
    ".laz",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
}
SHAPEFILE_BUNDLE_SUFFIXES = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qpj",
    ".wkt2",
}
RESULT_MANIFEST_NAMES = {"run_manifest.json", "models_manifest.json"}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dataset_id: str
    track_ids: list[str] = Field(default_factory=list)
    frame_range: tuple[int, int] | list[int] | None = None
    frame_from: int | None = None
    frame_to: int | None = None
    mode: str | None = None
    parameter_mode: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    auto: dict[str, Any] = Field(default_factory=dict)
    profile: str | None = None


def _set_config_value(
    document: dict[str, Any],
    key: str,
    value: Any,
    *,
    include_model_filters: bool = False,
) -> None:
    matches: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], *, inside_model_filters: bool = False) -> None:
        for child_key, child in node.items():
            normalized = child_key.replace("-", "_")
            child_inside_filters = inside_model_filters or normalized == "model_filters"
            if normalized == key and (
                include_model_filters or not inside_model_filters
            ):
                matches.append(node)
            if isinstance(child, dict) and (
                include_model_filters or not child_inside_filters
            ):
                visit(child, inside_model_filters=child_inside_filters)

    visit(document)
    if matches:
        for parent in matches:
            for existing in list(parent):
                if existing.replace("-", "_") == key:
                    parent[existing] = value
        return
    overrides = document.setdefault("web_run", {})
    if not isinstance(overrides, dict):
        raise ValueError("Base configuration has an incompatible web_run section.")
    overrides[key] = value


def _absolutize_config_paths(document: dict[str, Any], config_dir: Path) -> None:
    def visit(node: dict[str, Any]) -> None:
        for key, value in list(node.items()):
            normalized = key.replace("-", "_")
            if isinstance(value, dict):
                visit(value)
            elif normalized in PATH_OPTION_NAMES and value not in {None, ""}:
                path = Path(str(value)).expanduser()
                if not path.is_absolute():
                    path = config_dir / path
                node[key] = str(path.resolve(strict=False))

    visit(document)


def _dataset_root(app: Any, dataset: dict[str, Any]) -> Path:
    root = app.state.storage_roots_by_id.get(dataset["root_id"])
    if root is None:
        raise ValueError("Dataset storage is no longer configured.")
    return resolve_under_root(
        root.path,
        dataset["relative_path"],
        must_exist=True,
        expect_directory=True,
    )


def _frame_selection(
    dataset: dict[str, Any],
    frames: list[dict[str, Any]],
    payload: RunRequest,
) -> tuple[list[str], tuple[int, int], int, int]:
    known_tracks = {track["id"]: track for track in dataset.get("tracks", [])}
    unknown = sorted(set(payload.track_ids) - set(known_tracks))
    if unknown:
        raise ValueError("One or more selected tracks do not belong to this dataset.")
    track_ids = list(dict.fromkeys(payload.track_ids))

    if payload.frame_range is not None:
        if len(payload.frame_range) != 2:
            raise ValueError("frame_range must contain [first, last].")
        first, last = int(payload.frame_range[0]), int(payload.frame_range[1])
    else:
        first = int(payload.frame_from) if payload.frame_from is not None else 0
        last = (
            int(payload.frame_to)
            if payload.frame_to is not None
            else max(0, len(frames) - 1)
        )
    if not frames or first < 0 or last < first or last >= len(frames):
        raise ValueError("Frame range is outside this dataset.")

    selected_by_track = (
        [frame for frame in frames if frame["track_id"] in set(track_ids)]
        if track_ids
        else frames
    )
    selected = [
        frame for frame in selected_by_track if first <= int(frame["ordinal"]) <= last
    ]
    if not selected:
        raise ValueError("Track and frame selections contain no frames.")
    selected_positions = [
        index
        for index, frame in enumerate(selected_by_track)
        if first <= int(frame["ordinal"]) <= last
    ]
    start_index = selected_positions[0]
    limit_images = selected_positions[-1] - selected_positions[0] + 1
    if limit_images != len(selected):
        raise ValueError(
            "Frame selection is not contiguous after applying track filters."
        )
    return track_ids, (first, last), start_index, limit_images


def _build_job_config(
    app: Any,
    *,
    run_id: str,
    dataset: dict[str, Any],
    frames: list[dict[str, Any]],
    payload: RunRequest,
    core_parameters: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    base_path = app.state.config.pipeline_config_path
    if base_path.is_file():
        from mms_shp_detection.config import _Yaml12SafeLoader

        document = (
            yaml.load(
                base_path.read_text(encoding="utf-8-sig"),
                Loader=_Yaml12SafeLoader,
            )
            or {}
        )
        if not isinstance(document, dict):
            raise ValueError("Pipeline configuration root must be an object.")
        _absolutize_config_paths(document, base_path.parent)
    else:
        document = {"config_version": 1}

    work_dir = app.state.config.state_dir / "runs" / run_id
    output_dir = work_dir / "output"
    cache_file = work_dir / "cache" / "pointcloud_catalog.json"
    work_dir.mkdir(parents=True, exist_ok=False)
    try:
        output_dir.mkdir(parents=True)
        cache_file.parent.mkdir(parents=True)

        dataset_root = _dataset_root(app, dataset)
        track_ids, frame_range, start_index, limit_images = _frame_selection(
            dataset, frames, payload
        )
        selected_track_set = set(track_ids)
        selected_tracks = [
            track
            for track in dataset.get("tracks", [])
            if track["id"] in selected_track_set
        ]
        track_names = [str(track["name"]) for track in selected_tracks]
        record_names = [
            str(track["record_name"])
            for track in selected_tracks
            if track.get("record_name")
        ]
        job_names = [
            str(track["job_name"]) for track in selected_tracks if track.get("job_name")
        ]
        cache_seed = seed_catalog_cache(
            app,
            dataset_root,
            cache_file,
            preferred=dataset_catalog_path(app, dataset["id"]),
        )

        _set_config_value(document, "data_root", str(dataset_root))
        _set_config_value(document, "output_dir", str(output_dir))
        _set_config_value(document, "pointcloud_cache_path", str(cache_file))
        # The web worker captures stderr/stdout into process.log.  Keep tqdm
        # enabled there so the existing ``current/total`` parser can report
        # real progress to SSE clients.  Disabling it made healthy, long runs
        # appear permanently stuck at 0% in the operator UI.
        _set_config_value(document, "disable_console_progress", False)
        # A server-side base config may itself be scoped for a previous batch.
        # Clear every persistent selector before applying this request's opaque
        # track selection, otherwise the two independent scopes are intersected.
        for selector_name in (
            "include_record_names",
            "include_job_names",
            "include_track_names",
            "frame_id_from",
            "frame_id_to",
        ):
            _set_config_value(document, selector_name, None)
        if selected_tracks:
            if record_names:
                _set_config_value(document, "include_record_names", record_names)
        # Numeric UI bounds are global ordinals.  Apply them only after the
        # exact record filter, where start/limit remains unambiguous even when
        # image stems repeat in multiple tracks.
        filtered_count = sum(
            not selected_track_set or frame["track_id"] in selected_track_set
            for frame in frames
        )
        if start_index != 0 or limit_images != filtered_count:
            _set_config_value(document, "start_index", start_index)
            _set_config_value(document, "limit_images", limit_images)
        else:
            _set_config_value(document, "start_index", 0)
            _set_config_value(document, "limit_images", 0)
        manual_mode = str(payload.mode or payload.parameter_mode or "").casefold() in {
            "manual",
            "numeric",
            "number",
        }
        for key, value in core_parameters.items():
            _set_config_value(
                document,
                key,
                value,
                include_model_filters=manual_mode,
            )

        config_path = work_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        pipeline_config = PipelineConfig(
            values=document,
            config_hash=config_sha256(document),
            source_path=config_path,
        )
        RunManifestStore(output_dir / "run_manifest.json").create(
            job_id=run_id,
            config=pipeline_config,
            input_root=dataset_root,
            dataset_job=(job_names[0] if len(set(job_names)) == 1 else "multiple"),
            track=(track_names[0] if len(set(track_names)) == 1 else "multiple"),
            request_file_hash=config_file_sha256(config_path),
            config_is_effective=False,
        )
        resolved = {
            "run_execution_contract_version": RUN_EXECUTION_CONTRACT_VERSION,
            "track_ids": track_ids,
            "track_names": track_names,
            "record_names": record_names,
            "job_names": job_names,
            "frame_range": list(frame_range),
            "start_index": start_index,
            "limit_images": limit_images,
            "parameters": core_parameters,
            "config_file": "config.yaml",
            "output_directory": "output",
            "cache_file": "cache/pointcloud_catalog.json",
            "cache_seed": cache_seed,
        }
        return config_path, resolved
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _run_work_dir(app: Any, run: dict[str, Any]) -> Path:
    runs_root = app.state.config.state_dir / "runs"
    return resolve_under_root(
        runs_root,
        run["work_relative"],
        must_exist=True,
        expect_directory=True,
        reject_symlinks=True,
    )


def _run_manifest_path(
    app: Any,
    run: dict[str, Any],
    *,
    must_exist: bool,
) -> Path:
    return resolve_under_root(
        _run_work_dir(app, run),
        "output/run_manifest.json",
        must_exist=must_exist,
        expect_directory=False,
        reject_symlinks=True,
    )


def _read_run_manifest(
    app: Any,
    run: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one trusted-size manifest and verify that it belongs to the run."""

    try:
        path = _run_manifest_path(app, run, must_exist=True)
        if path.is_symlink():
            return None, "manifest is a symbolic link"
        with path.open("rb") as handle:
            size = int(os.fstat(handle.fileno()).st_size)
            if size <= 0:
                return None, "manifest is empty"
            if size > MAX_RUN_MANIFEST_BYTES:
                return None, "manifest exceeds the size limit"
            payload = handle.read(MAX_RUN_MANIFEST_BYTES + 1)
        if len(payload) > MAX_RUN_MANIFEST_BYTES:
            return None, "manifest exceeds the size limit"
        document = json.loads(payload.decode("utf-8"))
    except FileNotFoundError:
        return None, "manifest is missing"
    except (OSError, UnicodeError, json.JSONDecodeError, UnsafePath, ValueError):
        return None, "manifest cannot be read"
    if not isinstance(document, dict):
        return None, "manifest root is not an object"
    errors = validate_manifest_document(document)
    if errors:
        return None, "; ".join(errors[:8])
    if document.get("job_id") != run.get("id"):
        return None, "manifest job_id does not match the run"
    return document, None


def _run_output_validation_problem(
    app: Any,
    run: dict[str, Any],
    manifest: dict[str, Any],
) -> str | None:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        return "manifest outputs is not an object"
    try:
        output_root = resolve_under_root(
            _run_work_dir(app, run),
            "output",
            must_exist=True,
            expect_directory=True,
            reject_symlinks=True,
        )
    except (OSError, UnsafePath, ValueError):
        return "run output directory is missing or unsafe"
    errors = validate_published_outputs(output_root, outputs)
    return "; ".join(errors[:8]) if errors else None


def _succeeded_manifest_contract_problem(
    app: Any,
    run: dict[str, Any],
    manifest: dict[str, Any] | None,
    manifest_problem: str | None,
) -> tuple[str, str] | None:
    """Return the public error code and reason when success is not trustworthy."""

    if manifest is None:
        return "RUN_MANIFEST_INVALID", manifest_problem or "manifest is unavailable"
    if manifest.get("status") != JobStatus.SUCCEEDED.value:
        return (
            "RUN_MANIFEST_NOT_SUCCEEDED",
            f"manifest status is {manifest.get('status')!r}",
        )
    output_problem = _run_output_validation_problem(app, run, manifest)
    if output_problem is not None:
        return "RUN_OUTPUT_INVALID", output_problem
    return None


def _requires_run_execution_contract(run: dict[str, Any]) -> bool:
    resolved = run.get("resolved")
    return (
        isinstance(resolved, dict)
        and resolved.get("run_execution_contract_version")
        == RUN_EXECUTION_CONTRACT_VERSION
    )


def _output_path_identity(value: str) -> str:
    """Match filesystem case semantics while keeping manifest paths portable."""

    return os.path.normcase(Path(value).as_posix())


def _published_shapefile_paths(
    app: Any,
    run: dict[str, Any],
) -> frozenset[str] | None:
    """Return declared paths, an empty denied set, or ``None`` for legacy runs."""

    manifest, manifest_problem = _read_run_manifest(app, run)
    if manifest is None:
        return frozenset() if _requires_run_execution_contract(run) else None
    if (
        _succeeded_manifest_contract_problem(app, run, manifest, manifest_problem)
        is not None
    ):
        return frozenset()
    outputs = manifest.get("outputs", {})
    shapefiles = outputs.get("shapefiles", [])
    return frozenset(
        _output_path_identity(value) for value in shapefiles if isinstance(value, str)
    )


def _is_published_shapefile_component(
    relative: str,
    published_paths: frozenset[str] | None,
) -> bool:
    if published_paths is None:
        return True
    path = Path(relative)
    if path.suffix.casefold() not in SHAPEFILE_BUNDLE_SUFFIXES:
        return True
    return _output_path_identity(path.with_suffix(".shp").as_posix()) in published_paths


def _published_models_manifest_policy(
    app: Any,
    run: dict[str, Any],
) -> tuple[bool, str | None]:
    """Return whether models-manifest membership is enforced and its identity."""

    manifest, manifest_problem = _read_run_manifest(app, run)
    if manifest is None:
        return _requires_run_execution_contract(run), None
    if (
        _succeeded_manifest_contract_problem(app, run, manifest, manifest_problem)
        is not None
    ):
        return True, None
    models_manifest = manifest.get("outputs", {}).get("models_manifest")
    return (
        True,
        _output_path_identity(models_manifest)
        if isinstance(models_manifest, str)
        else None,
    )


def _is_published_models_manifest(
    relative: str,
    policy: tuple[bool, str | None],
) -> bool:
    if Path(relative).name.casefold() != "models_manifest.json":
        return True
    enforced, published_path = policy
    return not enforced or (
        published_path is not None and _output_path_identity(relative) == published_path
    )


def _redact(app: Any, value: str, run: dict[str, Any] | None = None) -> str:
    text = value
    sensitive = [
        app.state.config.project_root,
        app.state.config.state_dir,
        *(root.path for root in app.state.storage_roots),
    ]
    if run is not None:
        try:
            sensitive.append(_run_work_dir(app, run))
        except Exception:
            pass
    for path in sensitive:
        raw = str(path.resolve(strict=False))
        for variant in {raw, raw.replace("\\", "/")}:
            text = re.sub(re.escape(variant), "<server>", text, flags=re.IGNORECASE)
    text = _INLINE_FILE_URI.sub("file:<server-path>", text)
    text = _INLINE_FORWARD_UNC_PATH.sub("<server-path>", text)
    text = _INLINE_WINDOWS_PATH.sub("<server-path>", text)
    return _INLINE_POSIX_PATH.sub("<server-path>", text)


def _redact_manifest_value(
    app: Any,
    value: Any,
    run: dict[str, Any],
) -> Any:
    """Recursively remove private server paths from public manifest projections."""

    if isinstance(value, dict):
        return {
            str(_redact_manifest_value(app, str(key), run)): _redact_manifest_value(
                app, item, run
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_manifest_value(app, item, run) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _redact(app, value, run)
    stripped = redacted.strip()
    if redacted == value and (
        re.match(r"^[A-Za-z]:[\\/]", stripped) or stripped.startswith(("\\\\", "/"))
    ):
        basename = re.split(r"[\\/]", stripped.rstrip("\\/"))[-1]
        return f"<server>/{basename}" if basename else "<server>"
    return redacted


def _sync_manifest_terminal(
    app: Any,
    run: dict[str, Any],
    target: JobStatus,
    *,
    error: PipelineErrorInfo | None = None,
) -> bool:
    """Best-effort synchronization after the child stops or cannot launch."""

    document, _reason = _read_run_manifest(app, run)
    if document is None:
        return False
    current = JobStatus(document["status"])
    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
    if current in terminal and current != target:
        return False
    store = RunManifestStore(_run_manifest_path(app, run, must_exist=True))
    try:
        store.transition_terminal(target, error=error)
        return True
    except (OSError, ValueError):
        app.state.logger.warning(
            "Could not synchronize run manifest %s to %s.",
            run.get("id"),
            target.value,
        )
        return False


def _tail(path: Path, max_bytes: int = 32_768) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        length = handle.tell()
        handle.seek(max(0, length - max_bytes))
        payload = handle.read(max_bytes)
    return payload.decode("utf-8", errors="replace")


def _runtime_log_text(app: Any, item: dict[str, Any]) -> str:
    try:
        work = _run_work_dir(app, item)
    except Exception:
        return ""
    paths = [work / "logs" / "process.log", work / "output" / "logs" / "run.log"]
    try:
        paths.extend(sorted((work / "output").glob("*/logs/run.log"))[:8])
    except OSError:
        pass
    return "\n".join(_tail(path, max_bytes=16_384) for path in paths if path.is_file())


def _progress_from_log(status_value: str, log_text: str) -> float:
    if status_value == "completed":
        return 100.0
    if status_value in {"queued", "preparing"}:
        return 0.0
    matches = re.findall(r"(?<!\d)(\d{1,9})\s*/\s*(\d{1,9})(?!\d)", log_text)
    ratios = [
        int(current) / int(total)
        for current, total in matches
        if int(total) > 0 and int(current) <= int(total)
    ]
    return max(ratios, default=0.0) * 100.0


def _process_failure_message(app: Any, run: dict[str, Any], return_code: int) -> str:
    """Return a concise operator-facing reason from bounded pipeline logs."""

    log_text = _runtime_log_text(app, run)
    if re.search(r"forrtl:\s*error\s*\(200\).*window-CLOSE", log_text, re.IGNORECASE):
        return (
            "The Windows host console closed and terminated the pipeline's native "
            f"runtime (exit code {return_code})."
        )
    for line in reversed(log_text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if re.search(
            r"(?:\|\s*ERROR\s*\||Traceback|(?:Error|Exception):)",
            candidate,
            re.IGNORECASE,
        ):
            return (
                f"Pipeline process exited with code {return_code}. "
                f"Last error: {candidate[:600]}"
            )
    return f"Pipeline process exited with code {return_code}."


def _bounded_result_files(
    output: Path,
    *,
    max_files: int,
) -> tuple[list[Path], bool]:
    """Collect a bounded set of real result files without materializing a tree."""

    files: list[Path] = []
    pending = [output]
    # Unsupported files and nested directories must not defeat the response
    # bound. This is deliberately a scan budget, not an assertion about how
    # many files the pipeline is allowed to write.
    max_entries = max(10_000, max_files * 20)
    visited_entries = 0
    truncated = False

    while pending and len(files) < max_files and visited_entries < max_entries:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited_entries += 1
                    if visited_entries > max_entries or len(files) >= max_files:
                        truncated = True
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() == "logs":
                                continue
                            pending.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if path.suffix.casefold() in SAFE_RESULT_SUFFIXES:
                        files.append(path)
        except OSError:
            continue

    if pending or visited_entries >= max_entries or len(files) >= max_files:
        truncated = True
    return files, truncated


def _priority_result_files(
    output: Path,
    *,
    max_shapefiles: int,
    max_bundle_files: int,
    max_entries: int,
) -> tuple[list[Path], bool]:
    """Find delivery-critical SHP bundles independently of artifact pagination.

    Pipeline output has a stable shallow layout: a single-model run writes
    ``output/shp`` and a multi-model run writes ``output/{model}/shp``. We do
    not recurse into image, point-crop, or log trees, so thousands of ordinary
    artifacts cannot consume the SHP/result-manifest discovery budget.
    """

    found: dict[str, Path] = {}
    shp_directories: dict[str, Path] = {}
    inspected = 0
    truncated = False

    def real_file(path: Path) -> bool:
        try:
            is_junction = bool(getattr(path, "is_junction", lambda: False)())
            return path.is_file() and not path.is_symlink() and not is_junction
        except OSError:
            return False

    def real_directory(path: Path) -> bool:
        try:
            is_junction = bool(getattr(path, "is_junction", lambda: False)())
            return path.is_dir() and not path.is_symlink() and not is_junction
        except OSError:
            return False

    def add_file(path: Path) -> None:
        nonlocal truncated
        if not real_file(path):
            return
        try:
            key = path.relative_to(output).as_posix().casefold()
        except ValueError:
            return
        if key in found:
            return
        if len(found) >= max_bundle_files:
            truncated = True
            return
        found[key] = path

    direct_shp = output / "shp"
    if real_directory(direct_shp):
        shp_directories[str(direct_shp).casefold()] = direct_shp
    for name in RESULT_MANIFEST_NAMES:
        add_file(output / name)

    # Multi-model manifests name each shallow model directory. Reading only
    # those opaque keys avoids depending on root directory enumeration order
    # when a run also has thousands of root-level artifacts.
    models_manifest = output / "models_manifest.json"
    if real_file(models_manifest):
        document: Any = None
        try:
            if models_manifest.stat().st_size <= 5_000_000:
                document = json.loads(models_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            document = None
        models = document.get("models", []) if isinstance(document, dict) else []
        if isinstance(models, list):
            for model in models[: max_shapefiles * 4]:
                if not isinstance(model, dict):
                    continue
                model_key = str(model.get("model_key") or "")
                if (
                    not model_key
                    or model_key != model_key.strip()
                    or "/" in model_key
                    or "\\" in model_key
                    or model_key in {".", ".."}
                ):
                    continue
                try:
                    normalized_model_key = normalize_relative_path(
                        model_key, allow_empty=False
                    )
                    model_directory = resolve_under_root(
                        output,
                        normalized_model_key,
                        must_exist=True,
                        expect_directory=True,
                        reject_symlinks=True,
                    )
                except (UnsafePath, FileNotFoundError, NotADirectoryError):
                    continue
                candidate = model_directory / "shp"
                if real_directory(candidate):
                    shp_directories[str(candidate).casefold()] = candidate

    try:
        with os.scandir(output) as entries:
            for entry in entries:
                inspected += 1
                if inspected > max_entries:
                    truncated = True
                    break
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child = Path(entry.path)
                try:
                    is_junction = bool(getattr(child, "is_junction", lambda: False)())
                except OSError:
                    continue
                if is_junction:
                    continue
                candidate = child / "shp"
                if real_directory(candidate):
                    shp_directories[str(candidate).casefold()] = candidate
                for name in RESULT_MANIFEST_NAMES:
                    add_file(child / name)
    except OSError:
        pass

    primary_count = 0
    for shp_directory in shp_directories.values():
        candidates: list[Path] = []
        # Pipeline-standard names remain discoverable even if an unexpected
        # directory contains enough unrelated files to exhaust the scan bound.
        for stem in ("detected_signs", "pole_bottoms"):
            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2"):
                candidate = shp_directory / f"{stem}{suffix}"
                if real_file(candidate):
                    candidates.append(candidate)
        try:
            with os.scandir(shp_directory) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > max_entries:
                        truncated = True
                        break
                    try:
                        if entry.is_symlink() or not entry.is_file(
                            follow_symlinks=False
                        ):
                            continue
                    except OSError:
                        continue
                    candidate = Path(entry.path)
                    if candidate.suffix.casefold() in SHAPEFILE_BUNDLE_SUFFIXES:
                        candidates.append(candidate)
        except OSError:
            continue

        unique_candidates = {
            (path.stem.casefold(), path.suffix.casefold()): path for path in candidates
        }
        primary_stems = sorted(
            stem for stem, suffix in unique_candidates if suffix == ".shp"
        )
        for stem in primary_stems:
            if primary_count >= max_shapefiles:
                truncated = True
                break
            primary = unique_candidates[(stem, ".shp")]
            before = len(found)
            add_file(primary)
            if len(found) == before and not any(
                path == primary for path in found.values()
            ):
                continue
            primary_count += 1
            for suffix in sorted(SHAPEFILE_BUNDLE_SUFFIXES - {".shp"}):
                sidecar = unique_candidates.get((stem, suffix))
                if sidecar is not None:
                    add_file(sidecar)

    return sorted(
        found.values(), key=lambda path: path.relative_to(output).as_posix().casefold()
    ), truncated


def _result_summary(
    app: Any,
    run: dict[str, Any],
    *,
    publish_shapefiles: bool | None = None,
) -> dict[str, Any] | None:
    if publish_shapefiles is None:
        manifest, manifest_problem = _read_run_manifest(app, run)
        publish_shapefiles = run.get("status") == "completed" and (
            (
                _succeeded_manifest_contract_problem(
                    app, run, manifest, manifest_problem
                )
                is None
            )
            if _requires_run_execution_contract(run) or manifest is not None
            else True
        )
    try:
        output = _run_work_dir(app, run) / "output"
    except Exception:
        return None
    if not output.is_dir() or output.is_symlink():
        return None
    ordinary_paths, truncated = _bounded_result_files(
        output,
        max_files=app.state.config.max_result_files,
    )
    priority_paths, priority_truncated = _priority_result_files(
        output,
        max_shapefiles=app.state.config.max_result_shapefiles,
        max_bundle_files=app.state.config.max_result_shapefile_files,
        max_entries=app.state.config.max_result_priority_entries,
    )
    result_paths = list(
        {
            path.relative_to(output).as_posix().casefold(): path
            for path in (*priority_paths, *ordinary_paths)
        }.values()
    )
    truncated = truncated or priority_truncated
    result_paths.sort(key=lambda path: path.relative_to(output).as_posix().casefold())
    published_paths = _published_shapefile_paths(app, run)
    models_manifest_policy = _published_models_manifest_policy(app, run)
    result_paths = [
        path
        for path in result_paths
        if _is_published_shapefile_component(
            path.relative_to(output).as_posix(), published_paths
        )
        and _is_published_models_manifest(
            path.relative_to(output).as_posix(), models_manifest_policy
        )
    ]
    files: list[dict[str, Any]] = []
    for path in result_paths:
        try:
            relative = path.relative_to(output).as_posix()
            stat = path.stat()
        except (OSError, ValueError):
            continue
        files.append(
            {
                "path": relative,
                "name": path.name,
                "size": int(stat.st_size),
                "type": path.suffix.casefold().lstrip("."),
                "url": (
                    f"/api/runs/{run['id']}/artifacts?path={quote(relative, safe='')}"
                ),
            }
        )
    summary: dict[str, Any] = {
        "files": files,
        "file_count": len(files),
        "truncated": truncated,
        # Never expose an absolute server path. This stable logical location
        # tells the UI where the worker wrote the run and which API owns it.
        "output_location": {
            "kind": "server_managed",
            "relative_path": f"runs/{run['id']}/output",
            "results_url": f"/api/runs/{run['id']}/results",
        },
    }
    shapefiles: list[dict[str, Any]] = []
    published_paths = published_paths if publish_shapefiles else frozenset()
    completed_primaries = (
        (
            path
            for path in result_paths
            if path.suffix.casefold() == ".shp"
            and (
                published_paths is None
                or _output_path_identity(path.relative_to(output).as_posix())
                in published_paths
            )
        )
        if publish_shapefiles
        else ()
    )
    for primary in completed_primaries:
        try:
            relative = primary.relative_to(output).as_posix()
        except ValueError:
            continue
        sidecars = [
            path.relative_to(output).as_posix()
            for path in result_paths
            if path.parent == primary.parent
            and path.stem.casefold() == primary.stem.casefold()
        ]
        shapefiles.append(
            {
                "name": primary.stem,
                "path": relative,
                "files": sorted(sidecars),
                "download_url": (
                    f"/api/runs/{run['id']}/shapefile?path={quote(relative, safe='')}"
                ),
                "import_url": f"/api/runs/{run['id']}/shapefile/import",
            }
        )
    summary["shapefiles"] = shapefiles
    manifests = [
        path for path in result_paths if path.name.casefold() in RESULT_MANIFEST_NAMES
    ]
    manifests.sort(
        key=lambda path: (
            path.name.casefold() != "run_manifest.json",
            path.relative_to(output).as_posix().casefold(),
        )
    )
    for manifest in manifests:
        try:
            if (
                manifest.is_file()
                and not manifest.is_symlink()
                and manifest.stat().st_size <= 5_000_000
            ):
                value = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    copied = False
                    for key in ("feature_counts", "status"):
                        if key in value:
                            summary[key] = (
                                "failed"
                                if key == "status"
                                and value[key] == JobStatus.SUCCEEDED.value
                                and not publish_shapefiles
                                else value[key]
                            )
                            copied = True
                    if copied:
                        break
        except (OSError, json.JSONDecodeError):
            continue
    return summary


def public_run(
    app: Any,
    item: dict[str, Any],
    *,
    include_log: bool = False,
    include_results: bool = False,
) -> dict[str, Any]:
    manifest, manifest_problem = _read_run_manifest(app, item)
    has_valid_manifest = manifest is not None
    succeeded_contract_problem = (
        _succeeded_manifest_contract_problem(app, item, manifest, manifest_problem)
        if manifest is not None and manifest.get("status") == JobStatus.SUCCEEDED.value
        else None
    )
    if succeeded_contract_problem is not None:
        manifest = None
    log_text = _runtime_log_text(app, item) if manifest is None or include_log else ""
    public_status = PUBLIC_STATUS.get(item["status"], item["status"])
    completed_contract_problem: tuple[str, str] | None = None
    if public_status == "completed" and (
        _requires_run_execution_contract(item)
        or has_valid_manifest
        or succeeded_contract_problem is not None
    ):
        completed_contract_problem = (
            succeeded_contract_problem
            or _succeeded_manifest_contract_problem(
                app, item, manifest, manifest_problem
            )
        )
        if completed_contract_problem is not None:
            public_status = "failed"
            manifest = None
    manifest_progress = manifest.get("progress", {}) if manifest is not None else {}
    progress = (
        float(manifest_progress.get("percent", 0.0))
        if manifest is not None and isinstance(manifest_progress, dict)
        else _progress_from_log(public_status, log_text)
    )
    # Unregistered datasets remain as small tombstones so completed run
    # history can still show the delivery name without making that dataset
    # available for new browsing or processing.
    dataset = app.state.store.get_dataset(item["dataset_id"], include_unregistered=True)
    result = {
        "id": item["id"],
        "dataset_id": item["dataset_id"],
        "dataset_name": dataset["name"] if dataset is not None else None,
        "status": public_status,
        "progress": progress,
        "request": item["request"],
        "resolved": item["resolved"],
        "created_at": item["created_at"],
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "updated_at": item["updated_at"],
        "cancel_requested": bool(item.get("cancel_requested")),
        "return_code": item.get("return_code"),
        "stage": public_status,
    }
    if manifest is not None:
        errors = manifest.get("errors") or []
        error_info = errors[-1] if errors and isinstance(errors[-1], dict) else None
        result.update(
            {
                "job_id": manifest["job_id"],
                "manifest_schema_version": manifest["schema_version"],
                "canonical_status": manifest["status"],
                "attempt": manifest["attempt"],
                "current_stage": manifest_progress.get("current_stage"),
                "versions": _redact_manifest_value(
                    app, manifest.get("versions", {}), item
                ),
                "counts": _redact_manifest_value(app, manifest.get("counts", {}), item),
                "stage_results": _redact_manifest_value(
                    app, manifest.get("stages", []), item
                ),
                "error_info": (
                    _redact_manifest_value(app, error_info, item)
                    if error_info is not None
                    else None
                ),
            }
        )
    elif (
        succeeded_contract_problem is not None or completed_contract_problem is not None
    ):
        contract_code, contract_reason = (
            completed_contract_problem or succeeded_contract_problem
        )
        messages = {
            "RUN_MANIFEST_INVALID": "Completed run manifest is missing or invalid.",
            "RUN_MANIFEST_NOT_SUCCEEDED": (
                "Completed run does not have a succeeded manifest."
            ),
            "RUN_OUTPUT_INVALID": (
                "Succeeded manifest has incomplete or unsafe outputs."
            ),
        }
        error_info = PipelineErrorInfo(
            code=contract_code,
            message=messages[contract_code],
            stage="finalize_manifest",
            job_id=str(item["id"]),
            retryable=False,
            context={"reason": contract_reason},
            cause_type=(
                "OutputValidationError"
                if contract_code == "RUN_OUTPUT_INVALID"
                else "ManifestValidationError"
            ),
        ).to_dict()
        result.update(
            {
                "job_id": item["id"],
                "error_info": _redact_manifest_value(app, error_info, item),
            }
        )
    if public_status == "completed":
        result["result_url"] = f"/api/runs/{item['id']}/results"
    if item.get("error"):
        result["error"] = _redact(app, str(item["error"]), item)
    if include_results:
        summary = _result_summary(
            app,
            item,
            publish_shapefiles=public_status == "completed",
        )
        if summary is not None:
            result["results"] = summary
    if include_log:
        result["log_tail"] = _redact(app, log_text, item)
    return result


class RunManager:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active_run_id: str | None = None
        self._active_process: asyncio.subprocess.Process | None = None
        self._lifecycle_lock = asyncio.Lock()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._loop(), name="mms-single-gpu-runner"
            )
            self._wake.set()

    def recover_after_restart(self, now: str) -> int:
        """Recover the registry and reconcile its interrupted child manifest."""

        def apply_terminal_manifest(
            current: dict[str, Any],
            manifest: dict[str, Any] | None,
            manifest_problem: str | None,
        ) -> bool:
            manifest_status = manifest.get("status") if manifest is not None else None
            error: str | None = None
            if manifest_status == JobStatus.SUCCEEDED.value:
                succeeded_problem = _succeeded_manifest_contract_problem(
                    self.app, current, manifest, manifest_problem
                )
                if succeeded_problem is not None:
                    _code, reason = succeeded_problem
                    legacy_status = "failed"
                    error = (
                        f"Succeeded manifest has incomplete or unsafe outputs: {reason}"
                    )
                else:
                    legacy_status = "completed"
            elif manifest_status == JobStatus.FAILED.value:
                legacy_status = "failed"
                errors = manifest.get("errors") or [] if manifest is not None else []
                if errors and isinstance(errors[-1], dict):
                    error = _redact(
                        self.app,
                        str(errors[-1].get("message") or "Pipeline failed."),
                        current,
                    )
            elif manifest_status == JobStatus.CANCELLED.value:
                legacy_status = "cancelled"
            else:
                return False
            return self.app.state.store.transition_run(
                str(current["id"]),
                now,
                from_statuses=(str(current["status"]),),
                to_status=legacy_status,
                error=error,
                finished_at=(
                    str(manifest.get("finished_at") or now)
                    if manifest is not None
                    else now
                ),
            )

        recovery_candidates = (
            self.app.state.store.list_runs_requiring_restart_reconciliation(
                RUN_EXECUTION_CONTRACT_VERSION
            )
        )
        recovered = self.app.state.store.recover_after_restart(now)
        for previous in recovery_candidates:
            run_id = str(previous["id"])
            current = self.app.state.store.get_run(run_id)
            if current is None or current.get("status") not in {
                "interrupted",
                "cancelled",
                "failed",
            }:
                continue
            current_status = str(current["status"])
            manifest, manifest_problem = _read_run_manifest(self.app, current)
            if apply_terminal_manifest(current, manifest, manifest_problem):
                continue
            target = (
                JobStatus.CANCELLED
                if current_status == "cancelled"
                else JobStatus.FAILED
            )
            synchronized = _sync_manifest_terminal(
                self.app,
                current,
                target,
                error=(
                    PipelineErrorInfo(
                        code="WORKER_RESTARTED",
                        message="Server restarted while this run was active.",
                        stage="worker",
                        job_id=run_id,
                        retryable=True,
                        cause_type="WorkerRestart",
                    )
                    if target == JobStatus.FAILED
                    else None
                ),
            )
            refreshed = self.app.state.store.get_run(run_id)
            if refreshed is None:
                continue
            refreshed_manifest, refreshed_problem = _read_run_manifest(
                self.app, refreshed
            )
            if apply_terminal_manifest(
                refreshed, refreshed_manifest, refreshed_problem
            ):
                continue
            if synchronized and target == JobStatus.FAILED:
                self.app.state.store.transition_run(
                    run_id,
                    now,
                    from_statuses=(str(refreshed["status"]),),
                    to_status="failed",
                    error="Server restarted while this run was active.",
                    finished_at=now,
                )
        return recovered

    async def _stop_active_run(
        self,
        run_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        now = utc_now()
        active_run = self.app.state.store.get_run(run_id)
        manifest, manifest_problem = (
            _read_run_manifest(self.app, active_run)
            if active_run is not None
            else (None, "run registry entry is missing")
        )
        durable_success = (
            active_run is not None
            and _succeeded_manifest_contract_problem(
                self.app, active_run, manifest, manifest_problem
            )
            is None
        )
        if durable_success:
            self.app.state.store.transition_run(
                run_id,
                now,
                from_statuses=("starting", "running", "cancelling"),
                to_status="completed",
                error=None,
                finished_at=str(manifest.get("finished_at") or now),
            )
        else:
            transitioned = self.app.state.store.transition_run(
                run_id,
                now,
                from_statuses=("starting", "running", "cancelling"),
                to_status="interrupted",
                error="Server stopped while this run was active.",
                finished_at=now,
            )
            if transitioned:
                interrupted_run = self.app.state.store.get_run(run_id)
                if interrupted_run is not None:
                    _sync_manifest_terminal(
                        self.app,
                        interrupted_run,
                        JobStatus.FAILED,
                        error=PipelineErrorInfo(
                            code="WORKER_STOPPED",
                            message="Server stopped while this run was active.",
                            stage="worker",
                            job_id=run_id,
                            retryable=True,
                            cause_type="WorkerShutdown",
                        ),
                    )
        await self._terminate(process)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        async with self._lifecycle_lock:
            if (
                self._active_process is not None
                and self._active_process.returncode is None
                and self._active_run_id is not None
            ):
                await self._stop_active_run(
                    self._active_run_id,
                    self._active_process,
                )
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()

    def notify(self) -> None:
        self._wake.set()

    async def cancel(self, run_id: str) -> dict[str, Any]:
        for _attempt in range(8):
            run = self.app.state.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            run_status = str(run["status"])
            if run_status in TERMINAL_STATUSES:
                return run
            now = utc_now()
            if run_status in {"queued", "preparing", "starting"}:
                transitioned = self.app.state.store.transition_run(
                    run_id,
                    now,
                    from_statuses=(run_status,),
                    to_status="cancelled",
                    cancel_requested=1,
                    finished_at=now,
                )
                if transitioned:
                    cancelled = self.app.state.store.get_run(run_id)
                    if cancelled is not None:
                        synchronized = _sync_manifest_terminal(
                            self.app, cancelled, JobStatus.CANCELLED
                        )
                        if not synchronized:
                            manifest, manifest_problem = _read_run_manifest(
                                self.app, cancelled
                            )
                            if (
                                _succeeded_manifest_contract_problem(
                                    self.app,
                                    cancelled,
                                    manifest,
                                    manifest_problem,
                                )
                                is None
                            ):
                                self.app.state.store.transition_run(
                                    run_id,
                                    now,
                                    from_statuses=("cancelled",),
                                    to_status="completed",
                                    error=None,
                                    finished_at=str(manifest.get("finished_at") or now),
                                )
                    self._wake.set()
                    return self.app.state.store.get_run(run_id)  # type: ignore[return-value]
            elif run_status in {"running", "cancelling"}:
                transitioned = self.app.state.store.transition_run(
                    run_id,
                    now,
                    from_statuses=(run_status,),
                    to_status="cancelling",
                    cancel_requested=1,
                )
                if transitioned:
                    if (
                        self._active_run_id == run_id
                        and self._active_process is not None
                    ):
                        await self._terminate(self._active_process)
                    self._wake.set()
                    return self.app.state.store.get_run(run_id)  # type: ignore[return-value]
            else:
                return run
            await asyncio.sleep(0)
        self.app.state.logger.warning(
            "Cancellation for run %s lost repeated status races.", run_id
        )
        self._wake.set()
        return self.app.state.store.get_run(run_id)  # type: ignore[return-value]

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                pid = int(process.pid)
                if pid <= 0:
                    raise ProcessLookupError("Invalid child process ID.")
                tree_kill = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(pid),
                    "/T",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await tree_kill.wait()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=10)
        except (ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                try:
                    if os.name == "nt":
                        pid = int(process.pid)
                        if pid <= 0:
                            raise ProcessLookupError("Invalid child process ID.")
                        force_kill = await asyncio.create_subprocess_exec(
                            "taskkill",
                            "/PID",
                            str(pid),
                            "/T",
                            "/F",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await force_kill.wait()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            run = self.app.state.store.claim_next_queued_run(utc_now())
            if run is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._execute(run)

    async def _execute(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        latest_before_start = self.app.state.store.get_run(run_id)
        if (
            latest_before_start is None
            or latest_before_start.get("cancel_requested")
            or latest_before_start.get("status") == "cancelled"
        ):
            return
        try:
            work_dir = _run_work_dir(self.app, run)
            config_path = work_dir / "config.yaml"
            log_dir = work_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "process.log"
            command = [
                sys.executable,
                str(self.app.state.config.project_root / "scripts" / "run_pipeline.py"),
                "--config",
                str(config_path),
            ]
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["MMS_PIPELINE_JOB_ID"] = run_id
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                # Do not attach native numerical libraries in the pipeline to
                # the interactive web-server console.  Intel Fortran/MKL can
                # otherwise abort a healthy job with ``forrtl: error (200)``
                # when the launcher window receives a close event.  taskkill
                # /T still terminates the hidden child tree on explicit cancel.
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            async with self._lifecycle_lock:
                if self._stop.is_set():
                    return
                if not self.app.state.store.begin_run_start(run_id, utc_now()):
                    return
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.app.state.config.project_root),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **kwargs,
                )
                self._active_run_id = run_id
                self._active_process = process
                if self._stop.is_set():
                    await self._stop_active_run(run_id, process)
                    return
                started = utc_now()
                if not self.app.state.store.mark_run_running(
                    run_id,
                    pid=process.pid,
                    started_at=started,
                ):
                    await self._terminate(process)
                    return
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                if process.stdout is not None:
                    while True:
                        chunk = await process.stdout.read(64 * 1024)
                        if not chunk:
                            break
                        log.write(chunk.decode("utf-8", errors="replace"))
            return_code = await process.wait()
            latest = self.app.state.store.get_run(run_id) or run
            finished = utc_now()
            manifest_error: PipelineErrorInfo | None = None
            manifest, manifest_problem = _read_run_manifest(self.app, latest)
            succeeded_problem = _succeeded_manifest_contract_problem(
                self.app, latest, manifest, manifest_problem
            )
            if succeeded_problem is None:
                # A fully validated terminal manifest is the durable commit. It
                # wins over a cancellation/shutdown request that raced with the
                # child process after publication completed.
                final_status = "completed"
                error = None
            elif latest.get("status") == "interrupted":
                final_status = "interrupted"
                error = (
                    latest.get("error") or "Server stopped while this run was active."
                )
            elif latest.get("cancel_requested") or latest.get("status") == "cancelling":
                final_status = "cancelled"
                error = None
            elif return_code == 0:
                problem_code, problem_reason = succeeded_problem
                final_status = "failed"
                error = (
                    "Pipeline exited successfully but did not publish a valid "
                    "succeeded run manifest."
                )
                manifest_error = PipelineErrorInfo(
                    code=problem_code,
                    message=error,
                    stage="finalize_manifest",
                    job_id=run_id,
                    retryable=False,
                    context={"reason": problem_reason},
                    cause_type=(
                        "OutputValidationError"
                        if problem_code == "RUN_OUTPUT_INVALID"
                        else "ManifestValidationError"
                    ),
                )
            else:
                final_status = "failed"
                error = _process_failure_message(self.app, latest, return_code)
                progress = manifest.get("progress", {}) if manifest is not None else {}
                stage = (
                    progress.get("current_stage")
                    or progress.get("failed_stage")
                    or "pipeline"
                    if isinstance(progress, dict)
                    else "pipeline"
                )
                manifest_error = PipelineErrorInfo(
                    code="PIPELINE_PROCESS_FAILED",
                    message=error,
                    stage=str(stage),
                    job_id=run_id,
                    retryable=False,
                    context={"return_code": return_code},
                    cause_type="ChildProcessError",
                )
            allowed_sources = {
                "completed": ("running", "cancelling", "interrupted"),
                "failed": ("preparing", "starting", "running", "cancelling"),
                "cancelled": ("running", "cancelling"),
                "interrupted": ("running", "cancelling", "interrupted"),
            }
            transitioned = self.app.state.store.transition_run(
                run_id,
                finished,
                from_statuses=allowed_sources[final_status],
                to_status=final_status,
                return_code=return_code,
                error=error,
                finished_at=finished,
            )
            if transitioned and final_status == "cancelled":
                terminal_run = self.app.state.store.get_run(run_id) or latest
                _sync_manifest_terminal(self.app, terminal_run, JobStatus.CANCELLED)
            elif transitioned and final_status == "failed":
                terminal_run = self.app.state.store.get_run(run_id) or latest
                _sync_manifest_terminal(
                    self.app,
                    terminal_run,
                    JobStatus.FAILED,
                    error=manifest_error,
                )
        except BaseException as exc:
            self.app.state.logger.exception("Run %s failed before completion", run_id)
            finished = utc_now()
            latest = self.app.state.store.get_run(run_id)
            final_status = (
                "cancelled" if latest and latest.get("cancel_requested") else "failed"
            )
            error_text = _redact(self.app, str(exc) or type(exc).__name__, run)
            transitioned = self.app.state.store.transition_run(
                run_id,
                finished,
                from_statuses=("preparing", "starting", "running", "cancelling"),
                to_status=final_status,
                error=error_text if final_status == "failed" else None,
                finished_at=finished,
            )
            if transitioned and latest is not None:
                terminal_run = self.app.state.store.get_run(run_id) or latest
                if final_status == "cancelled":
                    _sync_manifest_terminal(self.app, terminal_run, JobStatus.CANCELLED)
                else:
                    _sync_manifest_terminal(
                        self.app,
                        terminal_run,
                        JobStatus.FAILED,
                        error=PipelineErrorInfo(
                            code="RUN_LAUNCH_FAILED",
                            message=error_text,
                            stage="launcher",
                            job_id=run_id,
                            retryable=False,
                            cause_type=type(exc).__name__,
                        ),
                    )
        finally:
            self._active_run_id = None
            self._active_process = None


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(payload: RunRequest, request: Request) -> dict[str, Any]:
    dataset = require_ready_dataset(request, payload.dataset_id)
    mode = payload.mode or payload.parameter_mode or "automatic"
    preset = (
        payload.auto.get("preset")
        if isinstance(payload.auto, dict) and payload.auto.get("preset")
        else payload.profile or "balanced"
    )
    try:
        ui_parameters, core_parameters, selected_profile = resolve_run_parameters(
            mode=mode,
            parameters=payload.parameters,
            preset=str(preset),
        )
        frames = request.app.state.store.all_frames(payload.dataset_id)
        run_id = f"run_{uuid.uuid4().hex}"
        config_path, selection = _build_job_config(
            request.app,
            run_id=run_id,
            dataset=dataset,
            frames=frames,
            payload=payload,
            core_parameters=core_parameters,
        )
    except (ValueError, UnsafePath, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    frame_range = selection["frame_range"]
    safe_request = {
        "dataset_id": payload.dataset_id,
        "track_ids": selection["track_ids"],
        "frame_range": frame_range,
        "mode": "manual" if selected_profile == "manual" else "automatic",
        "parameters": ui_parameters,
        **(
            {"auto": {**payload.auto, "preset": selected_profile}}
            if selected_profile != "manual"
            else {}
        ),
    }
    resolved = {
        **selection,
        "profile": selected_profile,
        "ui_parameters": ui_parameters,
        "core_parameters": core_parameters,
    }
    now = utc_now()
    run = {
        "id": run_id,
        "dataset_id": payload.dataset_id,
        "request": safe_request,
        "resolved": resolved,
        "work_relative": run_id,
        "created_at": now,
        "updated_at": now,
    }
    try:
        request.app.state.store.create_run(run)
    except BaseException:
        # The generated work directory contains only this new job.
        import shutil

        shutil.rmtree(config_path.parent, ignore_errors=True)
        raise
    request.app.state.run_manager.notify()
    stored = request.app.state.store.get_run(run_id)
    return public_run(request.app, stored, include_log=False)  # type: ignore[arg-type]


@router.get("/runs")
def list_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    items = [
        public_run(request.app, item, include_log=False)
        for item in request.app.state.store.list_runs(limit=limit)
    ]
    return {"items": items, "runs": items}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    run = request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return public_run(
        request.app,
        run,
        include_log=True,
        include_results=run["status"] in TERMINAL_STATUSES,
    )


@router.get("/runs/{run_id}/results")
def get_results(run_id: str, request: Request) -> dict[str, Any]:
    run = request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _result_summary(request.app, run) or {"files": [], "file_count": 0}


@router.get("/runs/{run_id}/artifacts")
async def get_artifact(run_id: str, path: str, request: Request) -> Response:
    run = request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    try:
        relative = normalize_relative_path(path, allow_empty=False)
        output = _run_work_dir(request.app, run) / "output"
        artifact = resolve_under_root(
            output,
            relative,
            must_exist=True,
            expect_directory=False,
            reject_symlinks=True,
        )
        if any(part.casefold() == "logs" for part in Path(relative).parts):
            raise UnsafePath(
                "Diagnostic logs are available only through redacted run status."
            )
        if artifact.suffix.casefold() not in SAFE_RESULT_SUFFIXES:
            raise UnsafePath("This artifact type is not downloadable.")
        published_paths = _published_shapefile_paths(request.app, run)
        if not _is_published_shapefile_component(relative, published_paths):
            raise UnsafePath("This Shapefile component is not a published output.")
        models_manifest_policy = _published_models_manifest_policy(request.app, run)
        if not _is_published_models_manifest(relative, models_manifest_policy):
            raise UnsafePath("This models manifest is not a published output.")
    except (UnsafePath, OSError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc
    if artifact.suffix.casefold() in {".json", ".txt"}:
        size_limit = (
            MAX_RUN_MANIFEST_BYTES
            if artifact.name.casefold() in RESULT_MANIFEST_NAMES
            else MAX_PUBLIC_STRUCTURED_ARTIFACT_BYTES
        )
        try:
            with artifact.open("rb") as handle:
                size = int(os.fstat(handle.fileno()).st_size)
                if size <= 0 or size > size_limit:
                    raise ValueError(
                        "Structured artifact size is outside the public limit."
                    )
                payload = handle.read(size_limit + 1)
            if len(payload) > size_limit:
                raise ValueError(
                    "Structured artifact size is outside the public limit."
                )
            decoded = payload.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Artifact not found.") from exc
        attachment_headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(artifact.name, safe="")
            ),
        }
        try:
            document = json.loads(decoded)
        except json.JSONDecodeError as exc:
            if artifact.suffix.casefold() == ".json":
                raise HTTPException(
                    status_code=404, detail="Artifact not found."
                ) from exc
            return Response(
                content=_redact(request.app, decoded, run),
                media_type="text/plain; charset=utf-8",
                headers=attachment_headers,
            )
        return JSONResponse(
            _redact_manifest_value(request.app, document, run),
            headers=attachment_headers,
        )
    return FileResponse(
        artifact,
        filename=artifact.name,
        headers={
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _event_stream(request: Request, run_id: str) -> AsyncIterator[str]:
    last_payload = ""
    while True:
        if await request.is_disconnected():
            return
        run = request.app.state.store.get_run(run_id)
        if run is None:
            yield 'event: error\ndata: {"detail":"Run not found."}\n\n'
            return
        public = public_run(request.app, run, include_log=False)
        payload = json.dumps(
            {"type": "snapshot", "run": public},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if payload != last_payload:
            # Default MessageEvent keeps EventSource.onmessage clients live.
            yield f"data: {payload}\n\n"
            last_payload = payload
        else:
            yield ": keep-alive\n\n"
        if run["status"] in TERMINAL_STATUSES:
            return
        await asyncio.sleep(1.0)


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    if request.app.state.store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return StreamingResponse(
        _event_stream(request, run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    try:
        run = await request.app.state.run_manager.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc
    return public_run(request.app, run, include_log=False)
