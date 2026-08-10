from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from .models import CalibrationMatch, PipelineError, PipelineErrorInfo

CALIBRATION_NOT_FOUND = "CALIBRATION_NOT_FOUND"
CALIBRATION_AMBIGUOUS = "CALIBRATION_AMBIGUOUS"
CALIBRATION_INVALID = "CALIBRATION_INVALID"
CALIBRATION_VERSION_UNSUPPORTED = "CALIBRATION_VERSION_UNSUPPORTED"
SUPPORTED_CALIBRATION_SCHEMA_VERSION = 2
_AVAILABLE_KEY_SAMPLE_LIMIT = 20


def normalize_calibration_component(value: Any, suffix: str) -> str:
    """Return one case-insensitive Job/Track key without its container suffix."""

    normalized = str(value or "").strip().casefold()
    normalized_suffix = suffix.casefold()
    normalized = normalized.removesuffix(normalized_suffix)
    return normalized.strip()


def normalized_task_key(task: Mapping[str, Any]) -> str:
    job = normalize_calibration_component(task.get("job_name"), ".job")
    track = normalize_calibration_component(task.get("track_name"), ".scan")
    return f"{job}/{track}"


def _bundle_track_key(item: Mapping[str, Any]) -> str:
    job = normalize_calibration_component(item.get("job"), ".job")
    track = normalize_calibration_component(item.get("track"), ".scan")
    return f"{job}/{track}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_json_value(value: Any, *, key: str = "") -> Any:
    """Normalize task metadata without reading calibration file contents."""

    if isinstance(value, Path) or (key.endswith("_path") and value):
        return os.path.normcase(
            str(Path(str(value)).expanduser().resolve(strict=False))
        )
    if isinstance(value, Mapping):
        return {
            str(item_key): _identity_json_value(item, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_identity_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_identity_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def calibration_task_identity(task: Mapping[str, Any]) -> str:
    """Bind a resolution to its Job/Track and delivery calibration metadata."""

    delivery = task.get("delivery_calibration")
    task_locator = {
        key: _identity_json_value(task[key], key=key)
        for key in (
            "record_name",
            "image_name",
            "image_path",
            "pose_csv_path",
            "frame_id",
        )
        if task.get(key) is not None
    }
    payload = {
        "normalized_key": normalized_task_key(task),
        "task_locator": task_locator,
        "delivery_calibration": (
            _identity_json_value(delivery) if isinstance(delivery, Mapping) else None
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def delivery_calibration_fingerprint(metadata: Mapping[str, Any]) -> str:
    """Match the legacy delivery-calibration provenance hash byte-for-byte."""

    digest = hashlib.sha256()
    for role, path_value in (
        ("mms_ini", metadata.get("ini_path")),
        (
            "sphere_internal_orientation",
            metadata.get("internal_orientation_path"),
        ),
    ):
        if not path_value:
            continue
        path = Path(str(path_value)).resolve()
        digest.update(role.encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _bundle_error(
    *,
    code: str,
    message: str,
    path: Path,
    job_id: str,
    stage: str,
    schema_version: Any = None,
    cause: BaseException | None = None,
) -> PipelineError:
    context: dict[str, Any] = {
        "calibration_path": str(path),
        "supported_schema_versions": [SUPPORTED_CALIBRATION_SCHEMA_VERSION],
    }
    if schema_version is not None:
        context["schema_version"] = schema_version
    return PipelineError(
        PipelineErrorInfo(
            code=code,
            message=message,
            stage=stage,
            job_id=job_id,
            retryable=False,
            context=context,
            cause_type=type(cause).__name__ if cause is not None else None,
        )
    )


def _load_bundle(
    path: Path | None,
    *,
    job_id: str,
    stage: str,
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    resolved = path.resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=f"Invalid calibration JSON at {resolved}: {exc}",
            path=resolved,
            job_id=job_id,
            stage=stage,
            cause=exc,
        ) from exc
    if not isinstance(payload, dict):
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=f"Calibration JSON root must be an object: {resolved}",
            path=resolved,
            job_id=job_id,
            stage=stage,
        )
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=f"Calibration JSON has no integer schema_version: {resolved}",
            path=resolved,
            job_id=job_id,
            stage=stage,
            schema_version=schema_version,
        )
    if schema_version != SUPPORTED_CALIBRATION_SCHEMA_VERSION:
        raise _bundle_error(
            code=CALIBRATION_VERSION_UNSUPPORTED,
            message=(
                "Unsupported calibration schema_version "
                f"{schema_version} at {resolved}; "
                f"expected {SUPPORTED_CALIBRATION_SCHEMA_VERSION}"
            ),
            path=resolved,
            job_id=job_id,
            stage=stage,
            schema_version=schema_version,
        )
    if not isinstance(payload.get("tracks"), list):
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=f"Calibration JSON has no tracks list: {resolved}",
            path=resolved,
            job_id=job_id,
            stage=stage,
            schema_version=schema_version,
        )
    invalid_track_indexes = [
        index
        for index, item in enumerate(payload["tracks"])
        if not isinstance(item, Mapping)
    ]
    if invalid_track_indexes:
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=(
                f"Calibration JSON contains non-object track entries at indexes "
                f"{invalid_track_indexes}: {resolved}"
            ),
            path=resolved,
            job_id=job_id,
            stage=stage,
            schema_version=schema_version,
        )
    payload["calibration_path"] = str(resolved)
    try:
        payload["sha256"] = _sha256_file(resolved)
    except OSError as exc:
        raise _bundle_error(
            code=CALIBRATION_INVALID,
            message=f"Could not fingerprint calibration JSON at {resolved}: {exc}",
            path=resolved,
            job_id=job_id,
            stage=stage,
            schema_version=schema_version,
            cause=exc,
        ) from exc
    return payload


@dataclass(frozen=True)
class CalibrationIssue:
    code: str
    job_name: str
    track_name: str
    normalized_key: str
    candidate_count: int
    matched_by: str
    candidate_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "job": self.job_name,
            "track": self.track_name,
            "normalized_key": self.normalized_key,
            "candidate_count": self.candidate_count,
            "matched_by": self.matched_by,
            "candidate_keys": list(self.candidate_keys),
        }


@dataclass(frozen=True)
class CalibrationResolution:
    matches: tuple[CalibrationMatch, ...]
    issues: tuple[CalibrationIssue, ...]
    bundle: dict[str, Any] | None
    searched_roots: tuple[Path, ...]
    normalized_keys: tuple[str, ...]
    available_keys_sample: tuple[str, ...]
    _task_matches: tuple[CalibrationMatch | None, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _task_keys: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _task_identities: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return not self.issues

    def match_for_index(self, index: int) -> CalibrationMatch | None:
        return self._task_matches[index]

    @property
    def task_keys(self) -> tuple[str, ...]:
        return self._task_keys

    @property
    def task_identities(self) -> tuple[str, ...]:
        return self._task_identities

    def require(
        self,
        *,
        job_id: str = "",
        stage: str = "attach_calibration",
    ) -> Self:
        if self.issues:
            _raise_resolution_error(self, job_id=job_id, stage=stage)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [match.to_dict() for match in self.matches],
            "issues": [issue.to_dict() for issue in self.issues],
            "searched_roots": [str(path) for path in self.searched_roots],
            "normalized_keys": list(self.normalized_keys),
            "available_keys_sample": list(self.available_keys_sample),
        }


class CalibrationResolver:
    """Resolve every task before callers mutate task metadata or load a model."""

    def __init__(
        self,
        calibration_path: Path | None,
        *,
        job_id: str = "",
        stage: str = "attach_calibration",
    ) -> None:
        self.calibration_path = (
            Path(calibration_path).expanduser()
            if calibration_path is not None
            else None
        )
        self.job_id = job_id
        self.stage = stage

    def resolve(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        required: bool = False,
    ) -> CalibrationResolution:
        bundle = _load_bundle(
            self.calibration_path,
            job_id=self.job_id,
            stage=self.stage,
        )
        bundle_tracks = tuple(
            item
            for item in (bundle.get("tracks", []) if bundle is not None else [])
            if isinstance(item, Mapping)
        )
        available_bundle_keys = tuple(
            sorted({_bundle_track_key(item) for item in bundle_tracks})
        )

        searched_roots: set[Path] = set()
        if self.calibration_path is not None:
            searched_roots.add(self.calibration_path.resolve(strict=False).parent)

        task_matches: list[CalibrationMatch | None] = []
        issues: list[CalibrationIssue] = []
        issue_identities: set[tuple[str, str, str, tuple[str, ...]]] = set()
        successful_by_identity: dict[
            tuple[str, str, str, str, str], CalibrationMatch
        ] = {}
        available_keys = set(available_bundle_keys)
        delivery_fingerprints: dict[tuple[str, str], str] = {}

        for task in tasks:
            job_name = str(task.get("job_name") or "")
            track_name = str(task.get("track_name") or "")
            normalized_job = normalize_calibration_component(job_name, ".job")
            normalized_track = normalize_calibration_component(track_name, ".scan")
            key = f"{normalized_job}/{normalized_track}"
            delivery = task.get("delivery_calibration")

            match: CalibrationMatch | None = None
            issue: CalibrationIssue | None = None
            if isinstance(delivery, Mapping):
                ini_path = Path(str(delivery.get("ini_path") or ""))
                internal_path = Path(
                    str(delivery.get("internal_orientation_path") or "")
                )
                if str(delivery.get("ini_path") or ""):
                    searched_roots.add(ini_path.resolve(strict=False).parent)
                if str(delivery.get("internal_orientation_path") or ""):
                    searched_roots.add(internal_path.resolve(strict=False).parent)
                if ini_path.is_file() and internal_path.is_file():
                    delivery_key = (
                        os.path.normcase(str(ini_path.resolve())),
                        os.path.normcase(str(internal_path.resolve())),
                    )
                    fingerprint = delivery_fingerprints.get(delivery_key)
                    if fingerprint is None:
                        fingerprint = delivery_calibration_fingerprint(delivery)
                        delivery_fingerprints[delivery_key] = fingerprint
                    calibration_id = str(
                        delivery.get("calibration_id")
                        or delivery.get("serial_number")
                        or ini_path.stem
                    )
                    match = CalibrationMatch(
                        job_name=job_name,
                        track_name=track_name,
                        calibration_id=calibration_id,
                        source_path=ini_path.resolve(),
                        matched_by="delivery_job_track",
                        fingerprint=fingerprint,
                    )
                    available_keys.add(key)
                else:
                    issue = CalibrationIssue(
                        code=CALIBRATION_NOT_FOUND,
                        job_name=job_name,
                        track_name=track_name,
                        normalized_key=key,
                        candidate_count=0,
                        matched_by="delivery_job_track",
                    )
            else:
                candidates = [
                    item
                    for item in bundle_tracks
                    if normalize_calibration_component(item.get("job"), ".job")
                    == normalized_job
                    and (
                        not normalized_track
                        or normalize_calibration_component(item.get("track"), ".scan")
                        == normalized_track
                    )
                ]
                candidate_keys = tuple(_bundle_track_key(item) for item in candidates)
                matched_by = (
                    "exact_job_track" if normalized_track else "exact_job_unique_track"
                )
                if len(candidates) == 1 and bundle is not None:
                    candidate = candidates[0]
                    candidate_job = candidate.get("job") or job_name
                    candidate_track = candidate.get("track") or track_name
                    calibration_id = str(
                        candidate.get("calibration_id")
                        or f"{candidate_job}/{candidate_track}"
                    )
                    match = CalibrationMatch(
                        job_name=job_name,
                        track_name=track_name,
                        calibration_id=calibration_id,
                        source_path=Path(bundle["calibration_path"]),
                        matched_by=matched_by,
                        fingerprint=str(bundle["sha256"]),
                        candidate_count=1,
                    )
                elif len(candidates) > 1:
                    issue = CalibrationIssue(
                        code=CALIBRATION_AMBIGUOUS,
                        job_name=job_name,
                        track_name=track_name,
                        normalized_key=key,
                        candidate_count=len(candidates),
                        matched_by=matched_by,
                        candidate_keys=candidate_keys,
                    )
                else:
                    issue = CalibrationIssue(
                        code=CALIBRATION_NOT_FOUND,
                        job_name=job_name,
                        track_name=track_name,
                        normalized_key=key,
                        candidate_count=0,
                        matched_by=matched_by,
                    )

            task_matches.append(match)
            if match is not None:
                identity = (
                    key,
                    match.matched_by,
                    str(match.source_path),
                    match.fingerprint,
                    match.calibration_id,
                )
                successful_by_identity.setdefault(identity, match)
            if issue is not None:
                identity = (
                    issue.code,
                    issue.normalized_key,
                    issue.matched_by,
                    issue.candidate_keys,
                )
                if identity not in issue_identities:
                    issue_identities.add(identity)
                    issues.append(issue)

        resolution = CalibrationResolution(
            matches=tuple(successful_by_identity.values()),
            issues=tuple(issues),
            bundle=bundle,
            searched_roots=tuple(
                sorted(searched_roots, key=lambda path: str(path).casefold())
            ),
            normalized_keys=tuple(
                sorted({normalized_task_key(task) for task in tasks})
            ),
            available_keys_sample=tuple(sorted(available_keys))[
                :_AVAILABLE_KEY_SAMPLE_LIMIT
            ],
            _task_matches=tuple(task_matches),
            _task_keys=tuple(normalized_task_key(task) for task in tasks),
            _task_identities=tuple(calibration_task_identity(task) for task in tasks),
        )
        if required and resolution.issues:
            _raise_resolution_error(
                resolution,
                job_id=self.job_id,
                stage=self.stage,
            )
        return resolution


def _raise_resolution_error(
    resolution: CalibrationResolution,
    *,
    job_id: str,
    stage: str,
) -> None:
    code = (
        CALIBRATION_AMBIGUOUS
        if any(issue.code == CALIBRATION_AMBIGUOUS for issue in resolution.issues)
        else CALIBRATION_NOT_FOUND
    )
    affected = ", ".join(issue.normalized_key for issue in resolution.issues)
    raise PipelineError(
        PipelineErrorInfo(
            code=code,
            message=(
                f"Calibration preflight failed for {len(resolution.issues)} "
                f"Job/Track key(s): {affected}"
            ),
            stage=stage,
            job_id=job_id,
            retryable=False,
            context={
                "searched_roots": [str(path) for path in resolution.searched_roots],
                "normalized_keys": list(resolution.normalized_keys),
                "available_keys_sample": list(resolution.available_keys_sample),
                "issues": [issue.to_dict() for issue in resolution.issues],
            },
        )
    )


__all__ = [
    "CALIBRATION_AMBIGUOUS",
    "CALIBRATION_INVALID",
    "CALIBRATION_NOT_FOUND",
    "CALIBRATION_VERSION_UNSUPPORTED",
    "SUPPORTED_CALIBRATION_SCHEMA_VERSION",
    "CalibrationIssue",
    "CalibrationResolution",
    "CalibrationResolver",
    "calibration_task_identity",
    "delivery_calibration_fingerprint",
    "normalize_calibration_component",
    "normalized_task_key",
]
