from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping


class JobStatus(str, Enum):
    """Canonical pipeline lifecycle states persisted in run manifests."""

    PENDING = "pending"
    VALIDATING = "validating"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


ALLOWED_JOB_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = MappingProxyType(
    {
        JobStatus.PENDING: frozenset(
            {JobStatus.VALIDATING, JobStatus.FAILED, JobStatus.CANCELLED}
        ),
        JobStatus.VALIDATING: frozenset(
            {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}
        ),
        JobStatus.RUNNING: frozenset(
            {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
        ),
        JobStatus.FAILED: frozenset({JobStatus.RETRYING}),
        JobStatus.RETRYING: frozenset(
            {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}
        ),
        JobStatus.SUCCEEDED: frozenset(),
        JobStatus.CANCELLED: frozenset(),
    }
)


def ensure_job_transition(current: JobStatus, target: JobStatus) -> None:
    if current == target:
        return
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise ValueError(
            f"Invalid pipeline job transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True)
class CalibrationMatch:
    job_name: str
    track_name: str
    calibration_id: str
    source_path: Path
    matched_by: str
    fingerprint: str
    candidate_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job_name,
            "track": self.track_name,
            "calibration_id": self.calibration_id,
            "source_path": str(self.source_path),
            "matched_by": self.matched_by,
            "fingerprint": self.fingerprint,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True)
class ArtifactRef:
    role: str
    path: str
    fingerprint: str | None = None
    media_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "fingerprint": self.fingerprint,
            "media_type": self.media_type,
        }


@dataclass(frozen=True)
class PipelineWarning:
    code: str
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class StageResult:
    stage_name: str
    stage_version: str
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    input_count: int = 0
    output_count: int = 0
    rejected_count: int = 0
    artifacts: tuple[ArtifactRef, ...] = ()
    metrics: Mapping[str, float | int | str] = field(default_factory=dict)
    warnings: tuple[PipelineWarning, ...] = ()

    @property
    def elapsed_ms(self) -> int:
        return max(
            0,
            round((self.finished_at - self.started_at).total_seconds() * 1000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "stage_version": self.stage_version,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "elapsed_ms": self.elapsed_ms,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "rejected_count": self.rejected_count,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metrics": dict(self.metrics),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class PipelineErrorInfo:
    code: str
    message: str
    stage: str
    job_id: str
    retryable: bool
    object_id: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    cause_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "job_id": self.job_id,
            "retryable": self.retryable,
            "object_id": self.object_id,
            "context": dict(self.context),
            "cause_type": self.cause_type,
        }


class PipelineError(RuntimeError):
    """Non-retryable pipeline failure carrying an operator-safe payload."""

    def __init__(self, info: PipelineErrorInfo) -> None:
        self.info = info
        super().__init__(f"{info.code}: {info.message}")


class RetryablePipelineError(PipelineError):
    """Pipeline failure that an executor may retry with the same request."""

