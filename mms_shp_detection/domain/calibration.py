from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from .models import CalibrationMatch, PipelineError, PipelineErrorInfo

CALIBRATION_NOT_FOUND = "CALIBRATION_NOT_FOUND"
CALIBRATION_AMBIGUOUS = "CALIBRATION_AMBIGUOUS"
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


def _load_bundle(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    resolved = path.resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid calibration JSON at {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        raise ValueError(  # noqa: TRY004 - preserve the legacy loader contract
            f"Calibration JSON has no tracks list: {resolved}"
        )
    payload["calibration_path"] = str(resolved)
    payload["sha256"] = _sha256_file(resolved)
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

    @property
    def ok(self) -> bool:
        return not self.issues

    def match_for_index(self, index: int) -> CalibrationMatch | None:
        return self._task_matches[index]

    @property
    def task_keys(self) -> tuple[str, ...]:
        return self._task_keys

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
            Path(calibration_path).expanduser() if calibration_path is not None else None
        )
        self.job_id = job_id
        self.stage = stage

    def resolve(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        required: bool = False,
    ) -> CalibrationResolution:
        bundle = _load_bundle(self.calibration_path)
        bundle_tracks = tuple(
            item
            for item in (bundle.get("tracks", []) if bundle is not None else [])
            if isinstance(item, Mapping)
        )
        available_bundle_keys = tuple(sorted({_bundle_track_key(item) for item in bundle_tracks}))

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
                    fingerprint = delivery_calibration_fingerprint(delivery)
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
                    "exact_job_track"
                    if normalized_track
                    else "exact_job_unique_track"
                )
                if len(candidates) == 1 and bundle is not None:
                    candidate = candidates[0]
                    calibration_id = str(
                        candidate.get("calibration_id")
                        or f"{candidate.get('job') or job_name}/{candidate.get('track') or track_name}"
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
            searched_roots=tuple(sorted(searched_roots, key=lambda path: str(path).casefold())),
            normalized_keys=tuple(sorted({normalized_task_key(task) for task in tasks})),
            available_keys_sample=tuple(sorted(available_keys))[
                :_AVAILABLE_KEY_SAMPLE_LIMIT
            ],
            _task_matches=tuple(task_matches),
            _task_keys=tuple(normalized_task_key(task) for task in tasks),
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
    "CALIBRATION_NOT_FOUND",
    "CalibrationIssue",
    "CalibrationResolution",
    "CalibrationResolver",
    "delivery_calibration_fingerprint",
    "normalize_calibration_component",
    "normalized_task_key",
]
