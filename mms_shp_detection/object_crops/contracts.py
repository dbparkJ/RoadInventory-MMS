"""Validated contracts for record-preserving object crops.

The config contract is deliberately independent from ``argparse`` and the
legacy pipeline.  It accepts an ordinary mapping (or JSON text), rejects
unknown keys, and serializes to one canonical mapping.  This lets the YAML,
CLI, run fingerprint, and dataset metadata share the same interpretation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


OBJECT_CROP_SCHEMA_VERSION = "1.0.0"
OBJECT_CROP_SCHEMA_NAME = "mms_object_crop"
SOURCE_INVENTORY_SCHEMA_NAME = "mms_object_crop/source_inventory"

_IDENTITY_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_POINT_ORDER = "source_file_hash_then_record_index"
_CANONICAL_CONVENTION = "panel_front_plus_y_z_up"


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value deterministically as UTF-8 bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
    return value


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    field_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{field_name} contains unknown key(s): {', '.join(unknown)}"
        )


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _as_float(
    value: Any,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")
    return result


def _as_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return int(value)


def _as_margin_triplet(value: Any, field_name: str) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must contain exactly three numbers")
    if len(value) != 3:
        raise ValueError(f"{field_name} must contain exactly three numbers")
    return tuple(
        _as_float(item, f"{field_name}[{index}]", minimum=0.0)
        for index, item in enumerate(value)
    )  # type: ignore[return-value]


def _as_identity(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    result = unicodedata.normalize("NFC", value.strip())
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    if _IDENTITY_CONTROL_PATTERN.search(result):
        raise ValueError(f"{field_name} must not contain control characters")
    if result in {".", ".."} or "/" in result or "\\" in result:
        raise ValueError(f"{field_name} must be one path-safe identity component")
    return result


def _identity_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _as_directory_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    result = value.strip()
    if not result or _IDENTITY_CONTROL_PATTERN.search(result):
        raise ValueError(f"{field_name} must be a non-empty safe directory name")
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or len(posix.parts) != 1
        or len(windows.parts) != 1
        or result in {".", ".."}
        or "/" in result
        or "\\" in result
        or ":" in result
    ):
        raise ValueError(f"{field_name} must be one safe relative directory name")
    return result


def _as_fixed_string(value: Any, expected: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value != expected:
        raise ValueError(f"{field_name} must be {expected!r}")
    return value


@dataclass(frozen=True)
class JobTrackScope:
    """One exact job and its exact track allowlist."""

    job_id: str
    tracks: tuple[str, ...]

    def __post_init__(self) -> None:
        job_id = _as_identity(self.job_id, "source_scope.jobs[].job_id")
        if isinstance(self.tracks, (str, bytes)) or not isinstance(
            self.tracks, Sequence
        ):
            raise TypeError("source_scope.jobs[].tracks must be a sequence")
        if not self.tracks:
            raise ValueError("source_scope.jobs[].tracks must not be empty")
        tracks_by_key: dict[str, str] = {}
        for index, item in enumerate(self.tracks):
            track = _as_identity(item, f"source_scope.jobs[].tracks[{index}]")
            key = _identity_key(track)
            if key in tracks_by_key:
                raise ValueError(
                    f"duplicate track in source_scope for {job_id!r}: {track!r}"
                )
            tracks_by_key[key] = track
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(
            self,
            "tracks",
            tuple(tracks_by_key[key] for key in sorted(tracks_by_key)),
        )

    @classmethod
    def from_value(cls, value: Any) -> "JobTrackScope":
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "source_scope.jobs[]")
        _reject_unknown(raw, {"job_id", "tracks"}, "source_scope.jobs[]")
        if "job_id" not in raw or "tracks" not in raw:
            raise ValueError("source_scope.jobs[] requires job_id and tracks")
        job_id = _as_identity(raw["job_id"], "source_scope.jobs[].job_id")
        raw_tracks = raw["tracks"]
        if isinstance(raw_tracks, (str, bytes)) or not isinstance(raw_tracks, Sequence):
            raise TypeError("source_scope.jobs[].tracks must be a list")
        if not raw_tracks:
            raise ValueError("source_scope.jobs[].tracks must not be empty")
        tracks_by_key: dict[str, str] = {}
        for index, item in enumerate(raw_tracks):
            track = _as_identity(item, f"source_scope.jobs[].tracks[{index}]")
            key = _identity_key(track)
            if key in tracks_by_key:
                raise ValueError(
                    f"duplicate track in source_scope for {job_id!r}: {track!r}"
                )
            tracks_by_key[key] = track
        tracks = tuple(tracks_by_key[key] for key in sorted(tracks_by_key))
        return cls(job_id=job_id, tracks=tracks)

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "tracks": list(self.tracks)}


@dataclass(frozen=True)
class SourceScope:
    """Canonical exact source allowlist used before object-crop selection."""

    strict: bool = True
    jobs: tuple[JobTrackScope, ...] = ()

    def __post_init__(self) -> None:
        strict = _as_bool(self.strict, "source_scope.strict")
        if isinstance(self.jobs, (str, bytes)) or not isinstance(self.jobs, Sequence):
            raise TypeError("source_scope.jobs must be a sequence")
        jobs_by_key: dict[str, JobTrackScope] = {}
        for item in self.jobs:
            job = JobTrackScope.from_value(item)
            key = _identity_key(job.job_id)
            if key in jobs_by_key:
                raise ValueError(f"duplicate job in source_scope: {job.job_id!r}")
            jobs_by_key[key] = job
        object.__setattr__(self, "strict", strict)
        object.__setattr__(
            self,
            "jobs",
            tuple(jobs_by_key[key] for key in sorted(jobs_by_key)),
        )

    @classmethod
    def from_value(cls, value: Any = None) -> "SourceScope":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"source_scope is not valid JSON: {exc.msg}") from exc
        raw = _as_mapping(value, "source_scope")
        _reject_unknown(raw, {"strict", "jobs"}, "source_scope")
        strict = _as_bool(raw.get("strict", True), "source_scope.strict")
        raw_jobs = raw.get("jobs", [])
        if isinstance(raw_jobs, (str, bytes)) or not isinstance(raw_jobs, Sequence):
            raise TypeError("source_scope.jobs must be a list")
        jobs_by_key: dict[str, JobTrackScope] = {}
        for item in raw_jobs:
            job = JobTrackScope.from_value(item)
            key = _identity_key(job.job_id)
            if key in jobs_by_key:
                raise ValueError(f"duplicate job in source_scope: {job.job_id!r}")
            jobs_by_key[key] = job
        jobs = tuple(jobs_by_key[key] for key in sorted(jobs_by_key))
        return cls(strict=strict, jobs=jobs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strict": self.strict,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @property
    def scope_id(self) -> str:
        # Matching is exact after NFC/case-fold normalization, so the identity
        # digest must use that same semantic form rather than display casing.
        identity_contract = {
            "strict": self.strict,
            "jobs": [
                {
                    "job_id": _identity_key(job.job_id),
                    "tracks": [_identity_key(track) for track in job.tracks],
                }
                for job in self.jobs
            ],
        }
        digest = hashlib.sha256(canonical_json_bytes(identity_contract)).hexdigest()
        return f"SCOPE_{digest[:24]}"

    def canonical_identity(
        self,
        job_id: Any,
        track_id: Any,
    ) -> tuple[str, str] | None:
        """Return the canonical allowed pair; never performs substring matching."""

        try:
            job_text = _as_identity(job_id, "job_id")
            track_text = _as_identity(track_id, "track_id")
        except (TypeError, ValueError):
            return None
        job_key = _identity_key(job_text)
        track_key = _identity_key(track_text)
        if not self.jobs:
            return (job_text, track_text) if not self.strict else None
        for job in self.jobs:
            if _identity_key(job.job_id) != job_key:
                continue
            for track in job.tracks:
                if _identity_key(track) == track_key:
                    return job.job_id, track
            return None
        return None

    def allows(self, job_id: Any, track_id: Any) -> bool:
        return self.canonical_identity(job_id, track_id) is not None


@dataclass(frozen=True)
class ObjectCropOutputConfig:
    directory_name: str = "object_crops_v1"
    evidence_directory_name: str = "object_crop_evidence"
    write_audit_laz: bool = True
    write_preview: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "directory_name",
            _as_directory_name(
                self.directory_name,
                "object_crops.output.directory_name",
            ),
        )
        object.__setattr__(
            self,
            "evidence_directory_name",
            _as_directory_name(
                self.evidence_directory_name,
                "object_crops.output.evidence_directory_name",
            ),
        )
        object.__setattr__(
            self,
            "write_audit_laz",
            _as_bool(
                self.write_audit_laz,
                "object_crops.output.write_audit_laz",
            ),
        )
        object.__setattr__(
            self,
            "write_preview",
            _as_bool(
                self.write_preview,
                "object_crops.output.write_preview",
            ),
        )

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropOutputConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "object_crops.output")
        _reject_unknown(
            raw,
            {
                "directory_name",
                "evidence_directory_name",
                "write_audit_laz",
                "write_preview",
            },
            "object_crops.output",
        )
        return cls(
            directory_name=_as_directory_name(
                raw.get("directory_name", "object_crops_v1"),
                "object_crops.output.directory_name",
            ),
            evidence_directory_name=_as_directory_name(
                raw.get("evidence_directory_name", "object_crop_evidence"),
                "object_crops.output.evidence_directory_name",
            ),
            write_audit_laz=_as_bool(
                raw.get("write_audit_laz", True),
                "object_crops.output.write_audit_laz",
            ),
            write_preview=_as_bool(
                raw.get("write_preview", True),
                "object_crops.output.write_preview",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory_name": self.directory_name,
            "evidence_directory_name": self.evidence_directory_name,
            "write_audit_laz": self.write_audit_laz,
            "write_preview": self.write_preview,
        }


@dataclass(frozen=True)
class ObjectCropSelectionConfig:
    point_order: str = _POINT_ORDER
    panel_margin_m: tuple[float, float, float] = (0.5, 1.0, 0.5)
    pole_radial_margin_m: float = 0.5
    base_ground_radius_m: float = 1.5
    vertical_bottom_margin_m: float = 0.3
    vertical_top_margin_m: float = 0.5
    context_margin_m: float = 0.75
    max_points: int = 200_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "point_order",
            _as_fixed_string(
                self.point_order,
                _POINT_ORDER,
                "object_crops.selection.point_order",
            ),
        )
        object.__setattr__(
            self,
            "panel_margin_m",
            _as_margin_triplet(
                self.panel_margin_m,
                "object_crops.selection.panel_margin_m",
            ),
        )
        for name in (
            "pole_radial_margin_m",
            "base_ground_radius_m",
            "vertical_bottom_margin_m",
            "vertical_top_margin_m",
            "context_margin_m",
        ):
            object.__setattr__(
                self,
                name,
                _as_float(
                    getattr(self, name),
                    f"object_crops.selection.{name}",
                    minimum=0.0,
                ),
            )
        object.__setattr__(
            self,
            "max_points",
            _as_positive_int(
                self.max_points,
                "object_crops.selection.max_points",
            ),
        )

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropSelectionConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "object_crops.selection")
        allowed = {
            "point_order",
            "panel_margin_m",
            "pole_radial_margin_m",
            "base_ground_radius_m",
            "vertical_bottom_margin_m",
            "vertical_top_margin_m",
            "context_margin_m",
            "max_points",
        }
        _reject_unknown(raw, allowed, "object_crops.selection")
        return cls(
            point_order=_as_fixed_string(
                raw.get("point_order", _POINT_ORDER),
                _POINT_ORDER,
                "object_crops.selection.point_order",
            ),
            panel_margin_m=_as_margin_triplet(
                raw.get("panel_margin_m", (0.5, 1.0, 0.5)),
                "object_crops.selection.panel_margin_m",
            ),
            pole_radial_margin_m=_as_float(
                raw.get("pole_radial_margin_m", 0.5),
                "object_crops.selection.pole_radial_margin_m",
                minimum=0.0,
            ),
            base_ground_radius_m=_as_float(
                raw.get("base_ground_radius_m", 1.5),
                "object_crops.selection.base_ground_radius_m",
                minimum=0.0,
            ),
            vertical_bottom_margin_m=_as_float(
                raw.get("vertical_bottom_margin_m", 0.3),
                "object_crops.selection.vertical_bottom_margin_m",
                minimum=0.0,
            ),
            vertical_top_margin_m=_as_float(
                raw.get("vertical_top_margin_m", 0.5),
                "object_crops.selection.vertical_top_margin_m",
                minimum=0.0,
            ),
            context_margin_m=_as_float(
                raw.get("context_margin_m", 0.75),
                "object_crops.selection.context_margin_m",
                minimum=0.0,
            ),
            max_points=_as_positive_int(
                raw.get("max_points", 200_000),
                "object_crops.selection.max_points",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_order": self.point_order,
            "panel_margin_m": list(self.panel_margin_m),
            "pole_radial_margin_m": self.pole_radial_margin_m,
            "base_ground_radius_m": self.base_ground_radius_m,
            "vertical_bottom_margin_m": self.vertical_bottom_margin_m,
            "vertical_top_margin_m": self.vertical_top_margin_m,
            "context_margin_m": self.context_margin_m,
            "max_points": self.max_points,
        }


@dataclass(frozen=True)
class ObjectCropCanonicalizationConfig:
    convention: str = _CANONICAL_CONVENTION
    min_confidence_for_model_input: float = 0.5
    min_confidence_for_training: float = 0.8

    def __post_init__(self) -> None:
        convention = _as_fixed_string(
            self.convention,
            _CANONICAL_CONVENTION,
            "object_crops.canonicalization.convention",
        )
        model = _as_float(
            self.min_confidence_for_model_input,
            "object_crops.canonicalization.min_confidence_for_model_input",
            minimum=0.0,
            maximum=1.0,
        )
        training = _as_float(
            self.min_confidence_for_training,
            "object_crops.canonicalization.min_confidence_for_training",
            minimum=0.0,
            maximum=1.0,
        )
        if training < model:
            raise ValueError(
                "min_confidence_for_training must be >= "
                "min_confidence_for_model_input"
            )
        object.__setattr__(self, "convention", convention)
        object.__setattr__(self, "min_confidence_for_model_input", model)
        object.__setattr__(self, "min_confidence_for_training", training)

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropCanonicalizationConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "object_crops.canonicalization")
        _reject_unknown(
            raw,
            {
                "convention",
                "min_confidence_for_model_input",
                "min_confidence_for_training",
            },
            "object_crops.canonicalization",
        )
        model = _as_float(
            raw.get("min_confidence_for_model_input", 0.5),
            "object_crops.canonicalization.min_confidence_for_model_input",
            minimum=0.0,
            maximum=1.0,
        )
        training = _as_float(
            raw.get("min_confidence_for_training", 0.8),
            "object_crops.canonicalization.min_confidence_for_training",
            minimum=0.0,
            maximum=1.0,
        )
        if training < model:
            raise ValueError(
                "min_confidence_for_training must be >= "
                "min_confidence_for_model_input"
            )
        return cls(
            convention=_as_fixed_string(
                raw.get("convention", _CANONICAL_CONVENTION),
                _CANONICAL_CONVENTION,
                "object_crops.canonicalization.convention",
            ),
            min_confidence_for_model_input=model,
            min_confidence_for_training=training,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "convention": self.convention,
            "min_confidence_for_model_input": self.min_confidence_for_model_input,
            "min_confidence_for_training": self.min_confidence_for_training,
        }


@dataclass(frozen=True)
class ObjectCropRoutingConfig:
    direct_max_panel_support_offset_m: float = 0.75
    keep_unsupported_samples: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "direct_max_panel_support_offset_m",
            _as_float(
                self.direct_max_panel_support_offset_m,
                "object_crops.routing.direct_max_panel_support_offset_m",
                minimum=0.0,
            ),
        )
        object.__setattr__(
            self,
            "keep_unsupported_samples",
            _as_bool(
                self.keep_unsupported_samples,
                "object_crops.routing.keep_unsupported_samples",
            ),
        )

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropRoutingConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "object_crops.routing")
        _reject_unknown(
            raw,
            {"direct_max_panel_support_offset_m", "keep_unsupported_samples"},
            "object_crops.routing",
        )
        return cls(
            direct_max_panel_support_offset_m=_as_float(
                raw.get("direct_max_panel_support_offset_m", 0.75),
                "object_crops.routing.direct_max_panel_support_offset_m",
                minimum=0.0,
            ),
            keep_unsupported_samples=_as_bool(
                raw.get("keep_unsupported_samples", True),
                "object_crops.routing.keep_unsupported_samples",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "direct_max_panel_support_offset_m": self.direct_max_panel_support_offset_m,
            "keep_unsupported_samples": self.keep_unsupported_samples,
        }


@dataclass(frozen=True)
class ObjectCropRayConfig:
    mode: str = "off"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            _as_fixed_string(
                self.mode,
                "off",
                "object_crops.rays.mode",
            ),
        )

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropRayConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        raw = _as_mapping(value, "object_crops.rays")
        _reject_unknown(raw, {"mode"}, "object_crops.rays")
        return cls(
            mode=_as_fixed_string(
                raw.get("mode", "off"),
                "off",
                "object_crops.rays.mode",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}


@dataclass(frozen=True)
class ObjectCropConfig:
    """Complete validated P0 object-crop configuration."""

    enabled: bool = False
    schema_version: str = OBJECT_CROP_SCHEMA_VERSION
    fail_pipeline_on_error: bool = False
    source_scope: SourceScope = field(default_factory=SourceScope)
    output: ObjectCropOutputConfig = field(default_factory=ObjectCropOutputConfig)
    selection: ObjectCropSelectionConfig = field(
        default_factory=ObjectCropSelectionConfig
    )
    canonicalization: ObjectCropCanonicalizationConfig = field(
        default_factory=ObjectCropCanonicalizationConfig
    )
    routing: ObjectCropRoutingConfig = field(default_factory=ObjectCropRoutingConfig)
    rays: ObjectCropRayConfig = field(default_factory=ObjectCropRayConfig)

    def __post_init__(self) -> None:
        _as_bool(self.enabled, "object_crops.enabled")
        _as_fixed_string(
            self.schema_version,
            OBJECT_CROP_SCHEMA_VERSION,
            "object_crops.schema_version",
        )
        _as_bool(
            self.fail_pipeline_on_error,
            "object_crops.fail_pipeline_on_error",
        )
        if not isinstance(self.source_scope, SourceScope):
            raise TypeError("object_crops.source_scope must be a SourceScope")
        if not isinstance(self.output, ObjectCropOutputConfig):
            raise TypeError("object_crops.output must be an ObjectCropOutputConfig")
        if not isinstance(self.selection, ObjectCropSelectionConfig):
            raise TypeError("object_crops.selection must be an ObjectCropSelectionConfig")
        if not isinstance(
            self.canonicalization,
            ObjectCropCanonicalizationConfig,
        ):
            raise TypeError(
                "object_crops.canonicalization must be an "
                "ObjectCropCanonicalizationConfig"
            )
        if not isinstance(self.routing, ObjectCropRoutingConfig):
            raise TypeError("object_crops.routing must be an ObjectCropRoutingConfig")
        if not isinstance(self.rays, ObjectCropRayConfig):
            raise TypeError("object_crops.rays must be an ObjectCropRayConfig")
        if self.enabled and self.source_scope.strict and not self.source_scope.jobs:
            raise ValueError(
                "enabled object_crops with strict source_scope requires at least one job"
            )
        if (
            self.output.directory_name.casefold()
            == self.output.evidence_directory_name.casefold()
        ):
            raise ValueError(
                "object-crop output and evidence directory names must be different"
            )

    @classmethod
    def from_value(cls, value: Any = None) -> "ObjectCropConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"object_crops is not valid JSON: {exc.msg}") from exc
        raw = _as_mapping(value, "object_crops")
        _reject_unknown(
            raw,
            {
                "enabled",
                "schema_version",
                "fail_pipeline_on_error",
                "source_scope",
                "output",
                "selection",
                "canonicalization",
                "routing",
                "rays",
            },
            "object_crops",
        )
        return cls(
            enabled=_as_bool(raw.get("enabled", False), "object_crops.enabled"),
            schema_version=_as_fixed_string(
                raw.get("schema_version", OBJECT_CROP_SCHEMA_VERSION),
                OBJECT_CROP_SCHEMA_VERSION,
                "object_crops.schema_version",
            ),
            fail_pipeline_on_error=_as_bool(
                raw.get("fail_pipeline_on_error", False),
                "object_crops.fail_pipeline_on_error",
            ),
            source_scope=SourceScope.from_value(raw.get("source_scope")),
            output=ObjectCropOutputConfig.from_value(raw.get("output")),
            selection=ObjectCropSelectionConfig.from_value(raw.get("selection")),
            canonicalization=ObjectCropCanonicalizationConfig.from_value(
                raw.get("canonicalization")
            ),
            routing=ObjectCropRoutingConfig.from_value(raw.get("routing")),
            rays=ObjectCropRayConfig.from_value(raw.get("rays")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "fail_pipeline_on_error": self.fail_pipeline_on_error,
            "source_scope": self.source_scope.to_dict(),
            "output": self.output.to_dict(),
            "selection": self.selection.to_dict(),
            "canonicalization": self.canonicalization.to_dict(),
            "routing": self.routing.to_dict(),
            "rays": self.rays.to_dict(),
        }


def parse_object_crop_config(value: Any) -> dict[str, Any]:
    """Argparse/YAML adapter returning one canonical JSON-compatible mapping."""

    try:
        return ObjectCropConfig.from_value(value).to_dict()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


__all__ = [
    "JobTrackScope",
    "OBJECT_CROP_SCHEMA_NAME",
    "OBJECT_CROP_SCHEMA_VERSION",
    "ObjectCropCanonicalizationConfig",
    "ObjectCropConfig",
    "ObjectCropOutputConfig",
    "ObjectCropRayConfig",
    "ObjectCropRoutingConfig",
    "ObjectCropSelectionConfig",
    "SOURCE_INVENTORY_SCHEMA_NAME",
    "SourceScope",
    "canonical_json_bytes",
    "parse_object_crop_config",
]
