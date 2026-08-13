from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..config import PipelineConfig
from ..domain.models import (
    CalibrationMatch,
    JobStatus,
    PipelineErrorInfo,
    StageResult,
    ensure_job_transition,
)

DEFAULT_EXECUTION_PLAN = (
    "validate_config",
    "discover_inputs",
    "attach_calibration",
    "load_or_build_spatial_index",
    "validate_inputs",
    "detect_project_and_estimate",
    "write_outputs",
    "finalize_manifest",
)

MAX_MANIFEST_INPUT_FILES = 1_000
PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES = (
    ".dbf",
    ".shx",
    ".prj",
    ".cpg",
    ".qpj",
    ".wkt2",
    ".shp",
)

_STAGE_PROGRESS = {
    "validate_config": 3.0,
    "discover_inputs": 8.0,
    "attach_calibration": 14.0,
    "load_or_build_spatial_index": 22.0,
    "validate_inputs": 25.0,
    "detect_project_and_estimate": 90.0,
    "write_outputs": 98.0,
    "finalize_manifest": 100.0,
}

_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}

_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


def _is_transient_windows_replace_error(exc: PermissionError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in {5, 32}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return utc_now().isoformat()


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def validate_manifest_document(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the stable run-manifest contract without an optional dependency."""

    errors: list[str] = []
    required_types: dict[str, type | tuple[type, ...]] = {
        "schema_version": int,
        "job_id": str,
        "attempt": int,
        "status": str,
        "created_at": str,
        "input": dict,
        "versions": dict,
        "progress": dict,
        "counts": dict,
        "outputs": dict,
        "stages": list,
        "errors": list,
    }
    for key, expected in required_types.items():
        if key not in document:
            errors.append(f"missing required field: {key}")
        elif not isinstance(document[key], expected) or (
            expected is int and isinstance(document[key], bool)
        ):
            errors.append(f"field {key!r} has an invalid type")
    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    try:
        JobStatus(str(document.get("status")))
    except ValueError:
        errors.append(f"unsupported status: {document.get('status')!r}")
    if not str(document.get("job_id") or "").strip():
        errors.append("job_id cannot be empty")
    if isinstance(document.get("attempt"), int) and int(document["attempt"]) < 1:
        errors.append("attempt must be at least 1")
    progress = document.get("progress")
    if isinstance(progress, dict):
        percent = progress.get("percent", 0)
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not 0 <= float(percent) <= 100
        ):
            errors.append("progress.percent must be between 0 and 100")
    return tuple(errors)


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _has_linked_path_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_junction(current):
            return True
    return False


def _assert_succeeded_stage_invariant(document: Mapping[str, Any]) -> None:
    """Prevent a successful job from contradicting its current-attempt stages."""

    attempt = int(document.get("attempt") or 1)
    contradictory = [
        str(item.get("stage_name") or "pipeline")
        for item in document.get("stages", [])
        if isinstance(item, Mapping)
        and int(item.get("attempt") or 1) == attempt
        and item.get("status") in {"running", "failed"}
    ]
    failed_stage = document.get("progress", {}).get("failed_stage")
    if failed_stage and str(failed_stage) not in contradictory:
        contradictory.append(str(failed_stage))
    if contradictory:
        names = ", ".join(dict.fromkeys(contradictory))
        raise ValueError(
            "Cannot commit succeeded while the current attempt has "
            f"running or failed stage evidence: {names}."
        )


def validate_published_outputs(
    output_root: Path,
    outputs: Mapping[str, Any],
    *,
    require_shapefiles: bool = True,
) -> tuple[str, ...]:
    """Verify that manifest-declared final outputs exist inside the run root."""

    errors: list[str] = []
    declared_root = Path(output_root)
    if _is_link_or_junction(declared_root):
        return ("output root cannot be a symbolic link or junction",)
    root = declared_root.resolve(strict=False)
    declared = outputs.get("shapefiles")
    if not isinstance(declared, list):
        return ("outputs.shapefiles must be a list",)
    if require_shapefiles and not declared:
        errors.append("outputs.shapefiles cannot be empty for a succeeded run")
    seen: set[str] = set()
    for index, value in enumerate(declared):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"outputs.shapefiles[{index}] must be a relative path")
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"unsafe output path: {value!r}")
            continue
        raw_candidate = root / relative
        candidate = raw_candidate.resolve(strict=False)
        try:
            normalized = candidate.relative_to(root).as_posix()
        except ValueError:
            errors.append(f"output escapes the run root: {value!r}")
            continue
        if normalized in seen:
            errors.append(f"duplicate output path: {normalized!r}")
            continue
        seen.add(normalized)
        if (
            raw_candidate.suffix.casefold() != ".shp"
            or ".in_progress" in raw_candidate.name
        ):
            errors.append(f"output is not a final Shapefile: {value!r}")
            continue
        for suffix in PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES:
            component = raw_candidate.with_suffix(suffix)
            try:
                component.resolve(strict=False).relative_to(root)
                valid = (
                    component.is_file()
                    and not _has_linked_path_component(root, component)
                    and component.stat().st_size > 0
                )
            except (OSError, ValueError):
                valid = False
            if not valid:
                errors.append(
                    f"missing or empty Shapefile component: "
                    f"{component.relative_to(root).as_posix()}"
                )
    models_manifest = outputs.get("models_manifest")
    if models_manifest is not None:
        if not isinstance(models_manifest, str) or not models_manifest.strip():
            errors.append("outputs.models_manifest must be a relative path or null")
        else:
            relative = Path(models_manifest)
            raw_candidate = root / relative
            candidate = raw_candidate.resolve(strict=False)
            try:
                candidate.relative_to(root)
                valid = (
                    not relative.is_absolute()
                    and ".." not in relative.parts
                    and not _has_linked_path_component(root, raw_candidate)
                    and candidate.is_file()
                    and candidate.stat().st_size > 0
                )
            except (OSError, ValueError):
                valid = False
            if not valid:
                errors.append("outputs.models_manifest is missing or unsafe")
            else:
                try:
                    if candidate.stat().st_size > 5_000_000:
                        raise ValueError("file exceeds 5000000 bytes")
                    model_document = json.loads(candidate.read_text(encoding="utf-8"))
                    if not isinstance(model_document, dict):
                        raise ValueError("root must be an object")
                    if model_document.get("schema_version") != 2:
                        raise ValueError("schema_version must be 2")
                    models = model_document.get("models")
                    if not isinstance(models, list) or not models:
                        raise ValueError("models must be a non-empty list")
                    model_outputs: set[str] = set()
                    for index, item in enumerate(models):
                        if not isinstance(item, dict):
                            raise ValueError(f"models[{index}] must be an object")
                        if (
                            item.get("status") != "completed"
                            or item.get("published_current_run") is not True
                        ):
                            raise ValueError(
                                f"models[{index}] is not a completed publication"
                            )
                        final_shapefiles = item.get("final_shapefiles")
                        if not isinstance(final_shapefiles, dict):
                            raise ValueError(
                                f"models[{index}].final_shapefiles must be an object"
                            )
                        for published_path in final_shapefiles.values():
                            if published_path is None:
                                continue
                            if not isinstance(published_path, str):
                                raise ValueError(
                                    f"models[{index}] declares a non-string output"
                                )
                            model_path = Path(published_path)
                            resolved_model_path = (
                                model_path.resolve(strict=False)
                                if model_path.is_absolute()
                                else (root / model_path).resolve(strict=False)
                            )
                            normalized_model_path = resolved_model_path.relative_to(
                                root
                            ).as_posix()
                            if _has_linked_path_component(
                                root, root / normalized_model_path
                            ):
                                raise ValueError(
                                    f"models[{index}] declares a linked output"
                                )
                            model_outputs.add(normalized_model_path)
                    if model_outputs != seen:
                        raise ValueError(
                            "published model outputs do not match outputs.shapefiles"
                        )
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    ValueError,
                ) as exc:
                    errors.append(f"outputs.models_manifest is invalid: {exc}")
    return tuple(errors)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize manifest read-modify-write cycles across web and worker processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RunManifestStore:
    """Atomic, process-safe persistence for one pipeline execution."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._thread_lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> dict[str, Any]:
        # Readers participate in the same cross-process lock as writers. This
        # prevents a polling web client from holding run_manifest.json open at
        # the exact moment a Windows worker atomically replaces it.
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid run manifest at {self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Run manifest root must be an object: {self.path}")
        errors = validate_manifest_document(value)
        if errors:
            raise ValueError(
                f"Invalid run manifest at {self.path}: {'; '.join(errors)}"
            )
        return value

    def create(
        self,
        *,
        job_id: str,
        config: PipelineConfig,
        input_root: Path,
        dataset_job: str = "multiple",
        track: str = "multiple",
        attempt: int = 1,
        execution_plan: tuple[str, ...] = DEFAULT_EXECUTION_PLAN,
        created_at: datetime | None = None,
        request_file_hash: str | None = None,
        config_is_effective: bool = True,
    ) -> dict[str, Any]:
        created = (created_at or utc_now()).isoformat()
        document: dict[str, Any] = {
            "schema_version": 1,
            "job_id": str(job_id),
            "attempt": int(attempt),
            "dataset_job": dataset_job,
            "track": track,
            "status": JobStatus.PENDING.value,
            "created_at": created,
            "started_at": None,
            "finished_at": None,
            "input": {
                "root": str(Path(input_root).resolve(strict=False)),
                "files": [],
                "file_count": 0,
                "files_truncated": False,
                "fingerprints": {},
            },
            "versions": {
                "git_commit": None,
                "model": None,
                "model_hashes": {},
                "config_hash": config.config_hash,
                "config_schema": config.schema_version,
                "calibration_id": None,
                "calibration_hash": None,
            },
            "config": {
                "source": str(config.source_path) if config.source_path else None,
                "hash": config.config_hash,
                "request_file_hash": request_file_hash,
                "effective_hash": config.config_hash if config_is_effective else None,
            },
            "calibrations": [],
            "execution_plan": list(execution_plan),
            "progress": {
                "current_stage": None,
                "completed_stages": [],
                "failed_stage": None,
                "percent": 0.0,
            },
            "counts": {
                "images": 0,
                "detections_2d": 0,
                "projected_3d": 0,
                "valid_features": 0,
                "rejected_features": 0,
            },
            "outputs": {},
            "stages": [],
            "errors": [],
        }
        errors = validate_manifest_document(document)
        if errors:
            raise ValueError("Cannot create invalid run manifest: " + "; ".join(errors))
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            if self.path.is_file():
                existing = self._read_unlocked()
                mismatches: list[str] = []
                if existing["job_id"] != str(job_id):
                    mismatches.append(
                        f"job_id {existing['job_id']!r} != {str(job_id)!r}"
                    )
                existing_root = Path(str(existing["input"].get("root") or "")).resolve(
                    strict=False
                )
                requested_root = Path(input_root).resolve(strict=False)
                if existing_root != requested_root:
                    mismatches.append(
                        f"input root {existing_root!s} != {requested_root!s}"
                    )
                if int(existing.get("attempt") or 1) != int(attempt):
                    mismatches.append(
                        f"attempt {existing.get('attempt')!r} != {int(attempt)!r}"
                    )
                if list(existing.get("execution_plan") or []) != list(execution_plan):
                    mismatches.append("execution plan differs")
                existing_request_hash = existing.get("config", {}).get(
                    "request_file_hash"
                )
                if (
                    request_file_hash is not None
                    and existing_request_hash is not None
                    and existing_request_hash != request_file_hash
                ):
                    mismatches.append("request configuration file hash differs")
                existing_effective_hash = existing.get("config", {}).get(
                    "effective_hash"
                )
                if (
                    existing_effective_hash is not None
                    and existing_effective_hash != config.config_hash
                ):
                    mismatches.append("effective configuration hash differs")
                if mismatches:
                    raise ValueError(
                        "Existing manifest identity mismatch: " + "; ".join(mismatches)
                    )
                return existing
            self._write_unlocked(document)
        return document

    def archive_terminal(self, *, next_job_id: str) -> Path | None:
        """Archive a previous terminal manifest and its derived summaries."""

        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            if not self.path.is_file():
                return None
            document = self._read_unlocked()
            if document["job_id"] == next_job_id:
                return None
            status = JobStatus(document["status"])
            if status not in _TERMINAL_JOB_STATUSES:
                raise RuntimeError(
                    "Output directory already contains a non-terminal run manifest "
                    f"for job {document['job_id']!r}."
                )
            history_dir = self.path.parent / "run_history"
            history_dir.mkdir(parents=True, exist_ok=True)
            safe_job_id = (
                re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    str(document["job_id"]),
                ).strip("._-")
                or "job"
            )
            token = safe_job_id
            while any(
                (history_dir / f"{token}.{suffix}").exists()
                for suffix in ("manifest.json", "summary.json", "summary.md")
            ):
                token = f"{safe_job_id}.{uuid.uuid4().hex[:8]}"
            destination = history_dir / f"{token}.manifest.json"
            moves: list[tuple[Path, Path]] = []
            for source_name, suffix in (
                ("run_summary.json", "summary.json"),
                ("run_summary.md", "summary.md"),
            ):
                source = self.path.with_name(source_name)
                if source.is_file():
                    moves.append((source, history_dir / f"{token}.{suffix}"))
            # Move the canonical manifest last. If any ordinary filesystem
            # error occurs, roll already-moved derivative summaries back so a
            # locked summary cannot leave the output root half-archived.
            moves.append((self.path, destination))
            completed_moves: list[tuple[Path, Path]] = []
            try:
                for source, target in moves:
                    completed_moves.append((source, target))
                    os.replace(source, target)
            except BaseException as exc:
                for source, target in reversed(completed_moves):
                    if not target.exists() and not target.is_symlink():
                        continue
                    try:
                        os.replace(target, source)
                    except OSError as rollback_error:
                        exc.add_note(
                            f"Could not roll back archive move {target} -> {source}: "
                            f"{rollback_error}"
                        )
                raise
            return destination

    def claim_pending_for_validation(self) -> bool:
        """Atomically grant one executor ownership of a pending job."""

        claimed = False

        def mutate(document: dict[str, Any]) -> None:
            nonlocal claimed
            if document["status"] != JobStatus.PENDING.value:
                return
            document["status"] = JobStatus.VALIDATING.value
            claimed = True

        self.update(mutate)
        return claimed

    def update(
        self,
        mutation: Callable[[dict[str, Any]], None],
        *,
        _allow_failed_retry: bool = False,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            document = self._read_unlocked()
            before = copy.deepcopy(document)
            current = JobStatus(document["status"])
            mutation(document)
            failed_retry = (
                _allow_failed_retry
                and current == JobStatus.FAILED
                and document.get("status") == JobStatus.RETRYING.value
            )
            if (
                current in _TERMINAL_JOB_STATUSES
                and document != before
                and not failed_retry
            ):
                raise ValueError(
                    "Terminal run manifests are immutable: "
                    f"job {document['job_id']!r} is {current.value}."
                )
            errors = validate_manifest_document(document)
            if errors:
                raise ValueError(
                    "Refusing to write invalid run manifest: " + "; ".join(errors)
                )
            self._write_unlocked(document)
            return document

    def transition(self, target: JobStatus) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            current = JobStatus(document["status"])
            ensure_job_transition(current, target)
            if current == target:
                return
            if target == JobStatus.SUCCEEDED:
                _assert_succeeded_stage_invariant(document)
            now = _iso_now()
            document["status"] = target.value
            if target == JobStatus.RUNNING and document.get("started_at") is None:
                document["started_at"] = now
            if target in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                document["finished_at"] = now
                document["progress"]["current_stage"] = None
            if target == JobStatus.SUCCEEDED:
                document["progress"]["percent"] = 100.0
            elif target == JobStatus.RETRYING:
                document["attempt"] = int(document.get("attempt") or 1) + 1
                document["started_at"] = None
                document["finished_at"] = None
                document["progress"] = {
                    "current_stage": None,
                    "completed_stages": [],
                    "failed_stage": None,
                    "percent": 0.0,
                }
                document["counts"] = {
                    "images": 0,
                    "detections_2d": 0,
                    "projected_3d": 0,
                    "valid_features": 0,
                    "rejected_features": 0,
                }
                document["outputs"] = {}
                document.pop("heartbeat_at", None)

        return self.update(
            mutate,
            _allow_failed_retry=target == JobStatus.RETRYING,
        )

    def transition_terminal(
        self,
        target: JobStatus,
        *,
        error: PipelineErrorInfo | None = None,
    ) -> dict[str, Any]:
        """Atomically append an optional error and commit a terminal status."""

        if target not in _TERMINAL_JOB_STATUSES:
            raise ValueError(f"Target status is not terminal: {target.value}")

        def mutate(document: dict[str, Any]) -> None:
            current = JobStatus(document["status"])
            ensure_job_transition(current, target)
            if current == target:
                return
            attempt = int(document.get("attempt") or 1)
            has_attempt_error = any(
                int(item.get("attempt") or 1) == attempt
                for item in document.get("errors", [])
                if isinstance(item, dict)
            )
            if error is not None and not has_attempt_error:
                payload = error.to_dict()
                payload["attempt"] = attempt
                document["errors"].append(payload)
                document["progress"]["failed_stage"] = error.stage
            now = _iso_now()
            active_stage: str | None = None
            active_items = [
                item
                for item in document.get("stages", [])
                if isinstance(item, dict)
                and int(item.get("attempt") or 1) == attempt
                and item.get("status") == "running"
            ]
            if target == JobStatus.SUCCEEDED:
                _assert_succeeded_stage_invariant(document)
            for item in active_items:
                active_stage = str(item.get("stage_name") or "pipeline")
                # StageResult's stable terminal vocabulary is succeeded/failed/
                # skipped. The enclosing JobStatus retains the cancellation.
                item["status"] = "failed"
                item["finished_at"] = now
                try:
                    started_at = datetime.fromisoformat(str(item.get("started_at")))
                    finished_at = datetime.fromisoformat(now)
                    item["elapsed_ms"] = max(
                        0,
                        int((finished_at - started_at).total_seconds() * 1000),
                    )
                except (TypeError, ValueError):
                    item["elapsed_ms"] = None
            if (
                target == JobStatus.FAILED
                and document["progress"].get("failed_stage") is None
                and active_stage is not None
            ):
                document["progress"]["failed_stage"] = active_stage
            document["status"] = target.value
            document["finished_at"] = now
            document["progress"]["current_stage"] = None
            if target == JobStatus.SUCCEEDED:
                document["progress"]["percent"] = 100.0

        return self.update(mutate)

    def begin_stage(self, name: str, *, version: str = "1") -> datetime:
        started = utc_now()

        def mutate(document: dict[str, Any]) -> None:
            document["progress"]["current_stage"] = name
            document["stages"].append(
                {
                    "attempt": int(document.get("attempt") or 1),
                    "stage_name": name,
                    "stage_version": version,
                    "status": "running",
                    "started_at": started.isoformat(),
                    "finished_at": None,
                    "elapsed_ms": None,
                    "input_count": 0,
                    "output_count": 0,
                    "rejected_count": 0,
                    "artifacts": [],
                    "metrics": {},
                    "warnings": [],
                }
            )

        self.update(mutate)
        return started

    def record_stage(self, result: StageResult) -> dict[str, Any]:
        payload = result.to_dict()

        def mutate(document: dict[str, Any]) -> None:
            for index in range(len(document["stages"]) - 1, -1, -1):
                item = document["stages"][index]
                if (
                    item.get("stage_name") == result.stage_name
                    and item.get("status") == "running"
                ):
                    payload["attempt"] = int(
                        item.get("attempt") or document.get("attempt") or 1
                    )
                    document["stages"][index] = payload
                    break
            else:
                payload["attempt"] = int(document.get("attempt") or 1)
                document["stages"].append(payload)
            progress = document["progress"]
            if result.status == "succeeded":
                if result.stage_name not in progress["completed_stages"]:
                    progress["completed_stages"].append(result.stage_name)
                progress["percent"] = max(
                    float(progress.get("percent") or 0),
                    _STAGE_PROGRESS.get(result.stage_name, 0.0),
                )
                progress["current_stage"] = None
            elif result.status == "failed":
                progress["failed_stage"] = result.stage_name
                progress["current_stage"] = None

        return self.update(mutate)

    def fail_active_stage(self) -> dict[str, Any] | None:
        """Close the running stage when an exception crosses orchestration."""

        document = self.read()
        stage_name = document.get("progress", {}).get("current_stage")
        if not stage_name:
            return None
        started_at: datetime | None = None
        version = "1"
        input_count = output_count = rejected_count = 0
        for item in reversed(document.get("stages", [])):
            if item.get("stage_name") == stage_name and item.get("status") == "running":
                version = str(item.get("stage_version") or "1")
                input_count = int(item.get("input_count") or 0)
                output_count = int(item.get("output_count") or 0)
                rejected_count = int(item.get("rejected_count") or 0)
                try:
                    started_at = datetime.fromisoformat(str(item.get("started_at")))
                except (TypeError, ValueError):
                    started_at = None
                break
        return self.record_stage(
            StageResult(
                stage_name=str(stage_name),
                stage_version=version,
                status="failed",
                started_at=started_at or utc_now(),
                finished_at=utc_now(),
                input_count=input_count,
                output_count=output_count,
                rejected_count=rejected_count,
            )
        )

    def set_progress(
        self,
        *,
        percent: float | None = None,
        current_stage: str | None = None,
    ) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            progress = document["progress"]
            if percent is not None:
                progress["percent"] = min(
                    100.0,
                    max(float(progress.get("percent") or 0), float(percent)),
                )
            if current_stage is not None:
                progress["current_stage"] = current_stage

        return self.update(mutate)

    def set_input(
        self,
        *,
        files: list[str] | None = None,
        file_count: int | None = None,
        files_truncated: bool | None = None,
        fingerprints: Mapping[str, Any] | None = None,
        dataset_job: str | None = None,
        track: str | None = None,
    ) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            if files is not None:
                document["input"]["files"] = list(files)
            if file_count is not None:
                document["input"]["file_count"] = max(0, int(file_count))
            if files_truncated is not None:
                document["input"]["files_truncated"] = bool(files_truncated)
            if fingerprints is not None:
                document["input"]["fingerprints"].update(_json_value(fingerprints))
            if dataset_job is not None:
                document["dataset_job"] = dataset_job
            if track is not None:
                document["track"] = track

        return self.update(mutate)

    def set_versions(self, **versions: Any) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            document["versions"].update(_json_value(versions))

        return self.update(mutate)

    def set_config_provenance(self, config: PipelineConfig) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            request_file_hash = document.get("config", {}).get("request_file_hash")
            document["config"] = {
                "source": str(config.source_path) if config.source_path else None,
                "hash": config.config_hash,
                "request_file_hash": request_file_hash,
                "effective_hash": config.config_hash,
            }
            document["versions"]["config_hash"] = config.config_hash
            document["versions"]["config_schema"] = config.schema_version

        return self.update(mutate)

    def add_model_version(self, model_name: str, fingerprint: str) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            hashes = document["versions"].setdefault("model_hashes", {})
            hashes[str(model_name)] = str(fingerprint)

        return self.update(mutate)

    def set_calibrations(self, matches: tuple[CalibrationMatch, ...]) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            document["calibrations"] = [match.to_dict() for match in matches]
            identifiers = sorted({match.calibration_id for match in matches})
            fingerprints = sorted({match.fingerprint for match in matches})
            document["versions"]["calibration_id"] = (
                identifiers[0] if len(identifiers) == 1 else identifiers
            )
            document["versions"]["calibration_hash"] = (
                fingerprints[0] if len(fingerprints) == 1 else fingerprints
            )

        return self.update(mutate)

    def update_counts(self, **counts: int) -> dict[str, Any]:
        allowed = {
            "images",
            "detections_2d",
            "projected_3d",
            "valid_features",
            "rejected_features",
        }

        def mutate(document: dict[str, Any]) -> None:
            for key, value in counts.items():
                if key in allowed:
                    document["counts"][key] = max(0, int(value))

        return self.update(mutate)

    def update_processing_progress(
        self,
        *,
        completed_images: int,
        total_images: int,
        detections: int,
        projected_points: int,
        failures: int,
    ) -> dict[str, Any]:
        """Persist a throttled processing heartbeat with one atomic update."""

        def mutate(document: dict[str, Any]) -> None:
            total = max(0, int(total_images))
            completed = min(total, max(0, int(completed_images))) if total else 0
            ratio = completed / total if total else 1.0
            document["progress"]["current_stage"] = "detect_project_and_estimate"
            document["progress"]["percent"] = max(
                float(document["progress"].get("percent") or 0),
                25.0 + (65.0 * ratio),
            )
            document["counts"].update(
                {
                    "images": completed,
                    "detections_2d": max(0, int(detections)),
                    "projected_3d": max(0, int(projected_points)),
                    "rejected_features": max(0, int(failures)),
                }
            )
            document["heartbeat_at"] = _iso_now()

        return self.update(mutate)

    def set_outputs(self, outputs: Mapping[str, Any]) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            document["outputs"].update(_json_value(outputs))

        return self.update(mutate)

    def record_error(self, error: PipelineErrorInfo) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            payload = error.to_dict()
            payload["attempt"] = int(document.get("attempt") or 1)
            document["errors"].append(payload)
            document["progress"]["failed_stage"] = error.stage
            document["progress"]["current_stage"] = None

        return self.update(mutate)

    def write_summary(self) -> tuple[Path, Path]:
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            document = self._read_unlocked()
            summary_json = self.path.with_name("run_summary.json")
            summary_md = self.path.with_name("run_summary.md")
            summary = {
                "schema_version": 1,
                "job_id": document["job_id"],
                "attempt": document["attempt"],
                "status": document["status"],
                "created_at": document["created_at"],
                "started_at": document.get("started_at"),
                "finished_at": document.get("finished_at"),
                "versions": document["versions"],
                "counts": document["counts"],
                "outputs": document["outputs"],
                "errors": document["errors"],
                "stage_count": len(document["stages"]),
            }
            self._atomic_write_path(summary_json, summary)
            errors = document["errors"]
            markdown = "\n".join(
                [
                    f"# Run {document['job_id']}",
                    "",
                    f"- Status: `{document['status']}`",
                    f"- Attempt: `{document['attempt']}`",
                    f"- Config: `{document['versions'].get('config_hash')}`",
                    "- Completed stages: "
                    f"`{len(document['progress']['completed_stages'])}`",
                    f"- Errors: `{len(errors)}`",
                    "",
                ]
            )
            self._atomic_write_text(summary_md, markdown)
            return summary_json, summary_md

    def _write_unlocked(self, document: Mapping[str, Any]) -> None:
        self._atomic_write_path(self.path, document)

    @staticmethod
    def _atomic_write_path(path: Path, document: Mapping[str, Any]) -> None:
        rendered = (
            json.dumps(
                _json_value(document),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        RunManifestStore._atomic_write_text(path, rendered)

    @staticmethod
    def _atomic_write_text(path: Path, rendered: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            for retry_index in range(len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS) + 1):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as exc:
                    # On Windows a concurrent dashboard read (or virus scanner)
                    # can briefly open the destination without FILE_SHARE_DELETE.
                    # The uniquely-named temp file is already durable, so retrying
                    # only this final rename is safe and keeps a transient reader
                    # from terminating a model post-processing consumer.
                    if not _is_transient_windows_replace_error(exc):
                        raise
                    if retry_index >= len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS):
                        # Some SMB shares permit creating and updating files but
                        # permanently deny the delete/rename right required by
                        # os.replace. Keep the detection consumer alive by using
                        # a durable in-place update as the final compatibility
                        # fallback. The store lock still serializes all writers.
                        try:
                            with path.open("w", encoding="utf-8", newline="\n") as handle:
                                handle.write(rendered)
                                handle.flush()
                                os.fsync(handle.fileno())
                        except OSError:
                            raise exc
                        break
                    time.sleep(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[retry_index])
        finally:
            temporary.unlink(missing_ok=True)
