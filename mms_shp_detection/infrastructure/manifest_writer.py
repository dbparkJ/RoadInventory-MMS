from __future__ import annotations

import json
import os
import threading
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
        elif not isinstance(document[key], expected):
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
        if not isinstance(percent, (int, float)) or not 0 <= float(percent) <= 100:
            errors.append("progress.percent must be between 0 and 100")
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
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid run manifest at {self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Run manifest root must be an object: {self.path}")
        errors = validate_manifest_document(value)
        if errors:
            raise ValueError(f"Invalid run manifest at {self.path}: {'; '.join(errors)}")
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
                existing = self.read()
                if existing["job_id"] != job_id:
                    raise ValueError(
                        f"Manifest job_id mismatch: {existing['job_id']} != {job_id}"
                    )
                return existing
            self._write_unlocked(document)
        return document

    def update(self, mutation: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            document = self.read()
            mutation(document)
            errors = validate_manifest_document(document)
            if errors:
                raise ValueError("Refusing to write invalid run manifest: " + "; ".join(errors))
            self._write_unlocked(document)
            return document

    def transition(self, target: JobStatus) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            current = JobStatus(document["status"])
            ensure_job_transition(current, target)
            if current == target:
                return
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

        return self.update(mutate)

    def begin_stage(self, name: str, *, version: str = "1") -> datetime:
        started = utc_now()

        def mutate(document: dict[str, Any]) -> None:
            document["progress"]["current_stage"] = name
            document["stages"].append(
                {
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
                    document["stages"][index] = payload
                    break
            else:
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
        """Close an interrupted running stage when an exception crosses orchestration."""

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
        fingerprints: Mapping[str, Any] | None = None,
        dataset_job: str | None = None,
        track: str | None = None,
    ) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> None:
            if files is not None:
                document["input"]["files"] = list(files)
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
            document["config"] = {
                "source": str(config.source_path) if config.source_path else None,
                "hash": config.config_hash,
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
            document["errors"].append(error.to_dict())
            document["progress"]["failed_stage"] = error.stage
            document["progress"]["current_stage"] = None

        return self.update(mutate)

    def write_summary(self) -> tuple[Path, Path]:
        document = self.read()
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
                f"- Completed stages: `{len(document['progress']['completed_stages'])}`",
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
        rendered = json.dumps(
            _json_value(document),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
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
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
