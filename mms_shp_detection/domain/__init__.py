"""Typed domain contracts shared by CLI, pipeline, and web adapters."""

from .models import (
    ArtifactRef,
    CalibrationMatch,
    JobStatus,
    PipelineError,
    PipelineErrorInfo,
    PipelineWarning,
    RetryablePipelineError,
    StageResult,
)

__all__ = [
    "ArtifactRef",
    "CalibrationMatch",
    "JobStatus",
    "PipelineError",
    "PipelineErrorInfo",
    "PipelineWarning",
    "RetryablePipelineError",
    "StageResult",
]
