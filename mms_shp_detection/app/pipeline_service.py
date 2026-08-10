from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from ..config import ConfigError, PipelineConfig
from ..domain.models import (
    ArtifactRef,
    CalibrationMatch,
    PipelineError,
    PipelineErrorInfo,
    PipelineWarning,
    StageResult,
)
from ..infrastructure.manifest_writer import RunManifestStore


@dataclass(frozen=True)
class PipelineContext:
    """Stable application context introduced alongside legacy runtime dictionaries."""

    job_id: str
    config: PipelineConfig
    input_root: Path
    output_root: Path
    dataset_job: str
    track: str
    calibrations: tuple[CalibrationMatch, ...]
    manifest: RunManifestStore


class PipelineStage(Protocol):
    name: str
    version: str

    def validate_input(self, context: PipelineContext) -> None: ...

    def run(self, context: PipelineContext) -> StageResult: ...

    def validate_output(
        self, context: PipelineContext, result: StageResult
    ) -> None: ...


@dataclass
class StageOutcome:
    input_count: int = 0
    output_count: int = 0
    rejected_count: int = 0
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    warnings: list[PipelineWarning] = field(default_factory=list)


def _safe_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return normalized[:80] or fallback


def _single_scope_name(value: Any, *, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    else:
        try:
            values = [str(part).strip() for part in value if str(part).strip()]
        except TypeError:
            values = [str(value).strip()]
    return values[0] if len(values) == 1 else fallback


def pipeline_scope(args: Any) -> tuple[str, str]:
    return (
        _single_scope_name(
            getattr(args, "include_job_names", None), fallback="multiple"
        ),
        _single_scope_name(
            getattr(args, "include_track_names", None), fallback="multiple"
        ),
    )


def generate_job_id(args: Any, config: PipelineConfig) -> str:
    """Use a web-provided ID or derive a deterministic, sortable CLI run ID."""

    requested = os.environ.get("MMS_PIPELINE_JOB_ID", "").strip()
    if requested:
        if len(requested) > 128 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", requested
        ):
            raise ConfigError(
                "MMS_PIPELINE_JOB_ID must contain only letters, digits, '.', '_' or '-'"
            )
        return requested
    dataset_job, track = pipeline_scope(args)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    input_root = str(Path(getattr(args, "data_root", ".")).resolve(strict=False))
    digest = hashlib.sha256(
        f"{config.config_hash}\0{input_root}\0{timestamp}\0{uuid.uuid4().hex}".encode(
            "utf-8"
        )
    ).hexdigest()[:8]
    return "_".join(
        (
            _safe_identifier(dataset_job, fallback="dataset"),
            _safe_identifier(track, fallback="track"),
            timestamp,
            digest,
        )
    )


def resolve_git_commit(project_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else None


def pipeline_error_info(
    exc: BaseException,
    *,
    job_id: str,
    stage: str,
) -> PipelineErrorInfo:
    if isinstance(exc, PipelineError):
        return exc.info
    if isinstance(exc, ConfigError):
        code = "CONFIG_INVALID"
    elif isinstance(exc, FileNotFoundError):
        code = "INPUT_FILE_MISSING"
    elif isinstance(exc, PermissionError):
        code = "OUTPUT_WRITE_FAILED"
    else:
        code = "PIPELINE_FAILED"
    return PipelineErrorInfo(
        code=code,
        message=str(exc) or type(exc).__name__,
        stage=stage,
        job_id=job_id,
        retryable=False,
        context={},
        cause_type=type(exc).__name__,
    )


@contextmanager
def tracked_stage(
    manifest: RunManifestStore,
    name: str,
    *,
    version: str = "1",
) -> Iterator[StageOutcome]:
    started = manifest.begin_stage(name, version=version)
    outcome = StageOutcome()
    try:
        yield outcome
    except BaseException:
        manifest.record_stage(
            StageResult(
                stage_name=name,
                stage_version=version,
                status="failed",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                input_count=outcome.input_count,
                output_count=outcome.output_count,
                rejected_count=outcome.rejected_count,
                artifacts=tuple(outcome.artifacts),
                metrics=outcome.metrics,
                warnings=tuple(outcome.warnings),
            )
        )
        raise
    else:
        manifest.record_stage(
            StageResult(
                stage_name=name,
                stage_version=version,
                status="succeeded",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                input_count=outcome.input_count,
                output_count=outcome.output_count,
                rejected_count=outcome.rejected_count,
                artifacts=tuple(outcome.artifacts),
                metrics=outcome.metrics,
                warnings=tuple(outcome.warnings),
            )
        )


class ManifestProgressReporter:
    """Throttle per-image worker events into durable manifest heartbeats."""

    def __init__(
        self,
        manifest: RunManifestStore,
        *,
        total_images: int,
        minimum_interval_seconds: float = 1.0,
    ) -> None:
        self.manifest = manifest
        self.total_images = max(0, int(total_images))
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._last_write = 0.0

    def update(self, totals: Mapping[str, int], *, force: bool = False) -> None:
        now = time.monotonic()
        completed = int(totals.get("images", 0))
        if (
            not force
            and completed < self.total_images
            and now - self._last_write < self.minimum_interval_seconds
        ):
            return
        self._last_write = now
        self.manifest.update_processing_progress(
            completed_images=completed,
            total_images=self.total_images,
            detections=int(totals.get("detections", 0)),
            projected_points=int(totals.get("points", 0)),
            failures=int(totals.get("failures", 0)),
        )
