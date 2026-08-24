"""Deterministic review-task candidates derived from durable run artifacts.

The web adapter is responsible for bounded, path-safe artifact discovery.  This
module deliberately consumes already-normalized dictionaries so candidate
selection and fingerprinting can be tested without a filesystem or web app.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

_REASON_TOKEN = re.compile(r"[^A-Z0-9]+")
_TASK_WEIGHTS = {
    "LOW_CONFIDENCE": 20.0,
    "PROJECTION_FAILED": 35.0,
    "GEOMETRY_REVIEW": 30.0,
    "POLE_BASE_REVIEW": 25.0,
    "UNREVIEWED_INTERVAL": 10.0,
    "SPACING_ANOMALY": 8.0,
}


@dataclass(frozen=True)
class CandidateSourceSettings:
    """Source switches and bounded tuning values for one generation pass."""

    low_confidence: bool = True
    projection_failed: bool = True
    geometry_review: bool = True
    pole_base_review: bool = True
    unreviewed_interval: bool = True
    spacing_anomaly: bool = True
    low_confidence_threshold: float = 0.5
    unreviewed_interval_frames: int = 50

    def __post_init__(self) -> None:
        threshold = float(self.low_confidence_threshold)
        if not math.isfinite(threshold) or not 0.0 < threshold <= 1.0:
            raise ValueError("low_confidence_threshold must be in (0, 1].")
        if not 1 <= int(self.unreviewed_interval_frames) <= 500:
            raise ValueError("unreviewed_interval_frames must be between 1 and 500.")

    def public_sources(self) -> dict[str, bool]:
        return {
            "LOW_CONFIDENCE": self.low_confidence,
            "PROJECTION_FAILED": self.projection_failed,
            "GEOMETRY_REVIEW": self.geometry_review,
            "POLE_BASE_REVIEW": self.pole_base_review,
            "UNREVIEWED_INTERVAL": self.unreviewed_interval,
            "SPACING_ANOMALY": self.spacing_anomaly,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def source_fingerprint(source_type: str, evidence: Mapping[str, Any]) -> str:
    """Return an opaque fingerprint that changes with task-producing evidence."""

    digest = hashlib.sha256(
        _canonical_json({"source_type": source_type, "evidence": dict(evidence)})
    ).hexdigest()
    return f"rcf_{digest}"


def deterministic_task_id(session_id: str, fingerprint: str) -> str:
    digest = hashlib.sha256(f"{session_id}\0{fingerprint}".encode()).hexdigest()
    return f"rvt_{digest[:32]}"


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _finite_position(value: Any) -> list[float] | None:
    if isinstance(value, Mapping):
        items = [value.get(axis) for axis in ("x", "y", "z")]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value[:3])
    else:
        return None
    if len(items) < 3:
        return None
    position = [_finite_float(item) for item in items]
    return (
        None if any(item is None for item in position) else [float(x) for x in position]
    )


def _candidate_position(detection: Mapping[str, Any]) -> list[float] | None:
    for value in (
        detection,
        {
            "x": detection.get("candidate_x"),
            "y": detection.get("candidate_y"),
            "z": detection.get("candidate_z"),
        },
        detection.get("position"),
        detection.get("location_hint"),
    ):
        position = _finite_position(value)
        if position is not None:
            return position
    return None


def _reason_code(value: Any, fallback: str) -> str:
    text = _REASON_TOKEN.sub("_", str(value or "").upper()).strip("_")
    if not text or not text[0].isalpha():
        text = fallback
    return text[:80]


def _class_hint(value: Any) -> str | None:
    if value is None:
        return None
    token = _reason_code(value, "CLASS")
    return token if token != "CLASS" or str(value).strip().upper() == "CLASS" else None


def _review_status(value: Any) -> bool:
    return str(value or "").strip().upper() == "REVIEW"


def _geometry_is_review(detection: Mapping[str, Any]) -> bool:
    if any(
        _review_status(detection.get(key))
        for key in ("geometry_status", "proposal_status", "review_status", "status")
    ):
        return True
    geometry = detection.get("geometry")
    return isinstance(geometry, Mapping) and any(
        _review_status(geometry.get(key))
        for key in ("status", "proposal_status", "review_status")
    )


def _projection_failed(detection: Mapping[str, Any]) -> bool:
    explicit = detection.get("projection_status")
    if str(explicit or "").strip().upper() in {"FAILED", "ERROR", "REJECTED"}:
        return True
    if detection.get("accepted_for_shp") is False:
        return True
    # A durable detection that was not assigned an accepted XYZ has no usable
    # 3-D projection, even when an older schema omitted accepted_for_shp.
    return _finite_position(detection) is None


def _priority(
    task_type: str,
    *,
    adjustment: float = 0.0,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> tuple[float, dict[str, Any]]:
    base = _TASK_WEIGHTS[task_type]
    bounded_adjustment = max(-base, min(25.0, float(adjustment)))
    value = round(base + bounded_adjustment, 6)
    return value, {
        "source": task_type,
        "source_weight": base,
        "adjustment": round(bounded_adjustment, 6),
        "computed_priority": value,
        "reason": reason[:240],
        **dict(details or {}),
    }


def _artifact_identity(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Select stable, non-path identifiers from a normalized artifact."""

    return {
        "source_run_id": artifact.get("source_run_id"),
        "run_fingerprint": artifact.get("run_fingerprint"),
        "model_fingerprint": artifact.get("model_fingerprint"),
        "frame_id": artifact.get("frame_id"),
        "record_name": artifact.get("record_name"),
        "image_name": artifact.get("image_name"),
        "detection_id": artifact.get("detection_id"),
        "detection_index": artifact.get("detection_index"),
    }


def _candidate(
    *,
    session_id: str,
    dataset_id: str,
    artifact: Mapping[str, Any],
    task_type: str,
    reason_codes: list[str],
    priority: float,
    priority_evidence: Mapping[str, Any],
    position: list[float] | None,
    target_layer_id: str | None,
    fingerprint_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    fingerprint = source_fingerprint(task_type, fingerprint_evidence)
    return {
        "id": deterministic_task_id(session_id, fingerprint),
        "session_id": session_id,
        "dataset_id": dataset_id,
        "task_type": task_type,
        "priority": priority,
        "frame_id": artifact.get("frame_id"),
        "track_id": artifact.get("track_id"),
        "source_run_id": artifact.get("source_run_id"),
        "source_detection_id": artifact.get("detection_id"),
        "target_layer_id": target_layer_id,
        "class_hint": _class_hint(artifact.get("class_name")),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "location_hint": position,
        "claimed_by": None,
        "source_fingerprint": fingerprint,
        "priority_evidence": dict(priority_evidence),
    }


def build_detection_candidates(
    *,
    session_id: str,
    dataset_id: str,
    artifact: Mapping[str, Any],
    settings: CandidateSourceSettings,
    target_layer_id: str | None = None,
    class_filters: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Build all enabled source tasks for one normalized detection artifact."""

    detection = artifact.get("detection")
    if not isinstance(detection, Mapping):
        return []
    class_name = detection.get("class_name") or artifact.get("class_name")
    normalized_artifact = {**artifact, "class_name": class_name}
    class_hint = _class_hint(class_name)
    filters = {str(item).upper() for item in class_filters}
    if filters and class_hint not in filters:
        return []

    identity = _artifact_identity(normalized_artifact)
    confidence = _finite_float(detection.get("confidence"))
    position = _candidate_position(detection)
    result: list[dict[str, Any]] = []

    if (
        settings.low_confidence
        and confidence is not None
        and confidence < settings.low_confidence_threshold
    ):
        gap = settings.low_confidence_threshold - confidence
        adjustment = 10.0 * gap / settings.low_confidence_threshold
        priority, evidence = _priority(
            "LOW_CONFIDENCE",
            adjustment=adjustment,
            reason="Detection confidence is below the configured review threshold.",
            details={
                "confidence": round(confidence, 6),
                "threshold": settings.low_confidence_threshold,
            },
        )
        result.append(
            _candidate(
                session_id=session_id,
                dataset_id=dataset_id,
                artifact=normalized_artifact,
                task_type="LOW_CONFIDENCE",
                reason_codes=["LOW_CONFIDENCE"],
                priority=priority,
                priority_evidence=evidence,
                position=position,
                target_layer_id=target_layer_id,
                fingerprint_evidence={
                    **identity,
                    "confidence": confidence,
                    "class_name": class_name,
                },
            )
        )

    if settings.projection_failed and _projection_failed(detection):
        raw_reason = (
            detection.get("exclude_reason")
            or detection.get("projection_reason")
            or detection.get("point_range_fallback_quality_reason")
            or "PROJECTION_FAILED"
        )
        reason_code = _reason_code(raw_reason, "PROJECTION_FAILED")
        priority, evidence = _priority(
            "PROJECTION_FAILED",
            adjustment=5.0 if position is None else 0.0,
            reason="The source detection has no accepted 3-D projection.",
            details={"artifact_reason": reason_code},
        )
        result.append(
            _candidate(
                session_id=session_id,
                dataset_id=dataset_id,
                artifact=normalized_artifact,
                task_type="PROJECTION_FAILED",
                reason_codes=list(dict.fromkeys(["PROJECTION_FAILED", reason_code])),
                priority=priority,
                priority_evidence=evidence,
                position=position,
                target_layer_id=target_layer_id,
                fingerprint_evidence={
                    **identity,
                    "accepted_for_shp": detection.get("accepted_for_shp"),
                    "reason": raw_reason,
                    "candidate_position": position,
                },
            )
        )

    if settings.geometry_review and _geometry_is_review(detection):
        raw_reason = detection.get("geometry_reason") or detection.get("reason")
        reason_code = _reason_code(raw_reason, "GEOMETRY_REVIEW")
        quality = _finite_float(detection.get("quality"))
        adjustment = (
            0.0 if quality is None else 10.0 * (1.0 - max(0.0, min(1.0, quality)))
        )
        priority, evidence = _priority(
            "GEOMETRY_REVIEW",
            adjustment=adjustment,
            reason="The geometry artifact explicitly requires operator review.",
            details={"artifact_reason": reason_code, "quality": quality},
        )
        result.append(
            _candidate(
                session_id=session_id,
                dataset_id=dataset_id,
                artifact=normalized_artifact,
                task_type="GEOMETRY_REVIEW",
                reason_codes=list(dict.fromkeys(["GEOMETRY_REVIEW", reason_code])),
                priority=priority,
                priority_evidence=evidence,
                position=position,
                target_layer_id=target_layer_id,
                fingerprint_evidence={
                    **identity,
                    "status": "REVIEW",
                    "reason": raw_reason,
                    "quality": quality,
                    "position": position,
                },
            )
        )

    pole = detection.get("pole")
    if (
        settings.pole_base_review
        and isinstance(pole, Mapping)
        and _review_status(pole.get("status") or pole.get("proposal_status"))
    ):
        raw_reason = pole.get("reason") or pole.get("occlusion_status")
        reason_code = _reason_code(raw_reason, "POLE_BASE_REVIEW")
        quality = _finite_float(pole.get("quality"))
        adjustment = (
            0.0 if quality is None else 10.0 * (1.0 - max(0.0, min(1.0, quality)))
        )
        pole_position = _candidate_position(pole) or position
        priority, evidence = _priority(
            "POLE_BASE_REVIEW",
            adjustment=adjustment,
            reason="The fitted pole base is marked REVIEW by the source artifact.",
            details={"artifact_reason": reason_code, "quality": quality},
        )
        result.append(
            _candidate(
                session_id=session_id,
                dataset_id=dataset_id,
                artifact=normalized_artifact,
                task_type="POLE_BASE_REVIEW",
                reason_codes=list(dict.fromkeys(["POLE_BASE_REVIEW", reason_code])),
                priority=priority,
                priority_evidence=evidence,
                position=pole_position,
                target_layer_id=target_layer_id,
                fingerprint_evidence={
                    **identity,
                    "status": "REVIEW",
                    "reason": raw_reason,
                    "quality": quality,
                    "position": pole_position,
                },
            )
        )
    return result


def _frame_position(frame: Mapping[str, Any]) -> list[float] | None:
    task = frame.get("task")
    if isinstance(task, Mapping):
        return _finite_position(task.get("origin"))
    return None


def build_unreviewed_interval_candidates(
    *,
    session_id: str,
    dataset_id: str,
    frames: Iterable[Mapping[str, Any]],
    reviewed_frame_ids: Iterable[str],
    settings: CandidateSourceSettings,
    target_layer_id: str | None = None,
) -> list[dict[str, Any]]:
    """Partition scoped, unreviewed frames into deterministic track intervals."""

    if not settings.unreviewed_interval:
        return []
    reviewed = {str(item) for item in reviewed_frame_ids}
    ordered = sorted(
        (frame for frame in frames if str(frame.get("id")) not in reviewed),
        key=lambda frame: (
            str(frame.get("track_id") or ""),
            int(frame.get("ordinal") or 0),
            str(frame.get("id") or ""),
        ),
    )
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for frame in ordered:
        if current and (
            str(frame.get("track_id")) != str(current[-1].get("track_id"))
            or int(frame.get("ordinal") or 0)
            != int(current[-1].get("ordinal") or 0) + 1
        ):
            groups.append(current)
            current = []
        current.append(frame)
    if current:
        groups.append(current)

    result: list[dict[str, Any]] = []
    size = int(settings.unreviewed_interval_frames)
    for group in groups:
        for start in range(0, len(group), size):
            chunk = group[start : start + size]
            anchor = chunk[len(chunk) // 2]
            start_ordinal = int(chunk[0].get("ordinal") or 0)
            end_ordinal = int(chunk[-1].get("ordinal") or start_ordinal)
            evidence_identity = {
                "dataset_id": dataset_id,
                "track_id": anchor.get("track_id"),
                "frame_ids": [str(frame.get("id")) for frame in chunk],
                "start_ordinal": start_ordinal,
                "end_ordinal": end_ordinal,
            }
            fingerprint = source_fingerprint("UNREVIEWED_INTERVAL", evidence_identity)
            adjustment = min(10.0, math.log2(len(chunk) + 1.0))
            priority, priority_evidence = _priority(
                "UNREVIEWED_INTERVAL",
                adjustment=adjustment,
                reason="The scoped frame interval has no completed review task.",
                details={
                    "frame_count": len(chunk),
                    "start_ordinal": start_ordinal,
                    "end_ordinal": end_ordinal,
                },
            )
            result.append(
                {
                    "id": deterministic_task_id(session_id, fingerprint),
                    "session_id": session_id,
                    "dataset_id": dataset_id,
                    "task_type": "UNREVIEWED_INTERVAL",
                    "priority": priority,
                    "frame_id": str(anchor.get("id")),
                    "track_id": str(anchor.get("track_id")),
                    "frame_start": start_ordinal,
                    "frame_end": end_ordinal,
                    "source_run_id": None,
                    "source_detection_id": None,
                    "target_layer_id": target_layer_id,
                    "class_hint": None,
                    "reason_codes": ["UNREVIEWED_INTERVAL"],
                    "location_hint": _frame_position(anchor),
                    "claimed_by": None,
                    "source_fingerprint": fingerprint,
                    "priority_evidence": priority_evidence,
                }
            )
    return result


def build_spacing_anomaly_candidates(
    *,
    session_id: str,
    dataset_id: str,
    artifacts: Iterable[Mapping[str, Any]],
    settings: CandidateSourceSettings,
    target_layer_id: str | None = None,
    class_filters: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Flag deterministic large gaps against a stable local class/track median."""

    if not settings.spacing_anomaly:
        return []
    filters = {str(item).upper() for item in class_filters}
    # Multiple detections in one panorama do not form an ordered spacing
    # sequence. Keep one deterministic, highest-confidence position per frame.
    per_frame: dict[
        tuple[str, str, str], tuple[Mapping[str, Any], list[float], float]
    ] = {}
    for artifact in artifacts:
        detection = artifact.get("detection")
        if (
            not isinstance(detection, Mapping)
            or detection.get("accepted_for_shp") is False
        ):
            continue
        position = _finite_position(detection)
        class_hint = _class_hint(
            detection.get("class_name") or artifact.get("class_name")
        )
        if (
            position is None
            or class_hint is None
            or (filters and class_hint not in filters)
        ):
            continue
        track_id = str(artifact.get("track_id") or "")
        frame_id = str(artifact.get("frame_id") or "")
        if not track_id or not frame_id or artifact.get("frame_ordinal") is None:
            continue
        confidence = _finite_float(detection.get("confidence")) or 0.0
        key = (track_id, class_hint, frame_id)
        previous = per_frame.get(key)
        identity = str(artifact.get("detection_id") or "")
        if previous is None or (confidence, identity) > (
            previous[2],
            str(previous[0].get("detection_id") or ""),
        ):
            per_frame[key] = (artifact, position, confidence)

    groups: dict[tuple[str, str], list[tuple[Mapping[str, Any], list[float]]]] = {}
    for (track_id, class_hint, _frame_id), (
        artifact,
        position,
        _confidence,
    ) in per_frame.items():
        groups.setdefault((track_id, class_hint), []).append((artifact, position))

    result: list[dict[str, Any]] = []
    for (track_id, class_hint), sequence in sorted(groups.items()):
        sequence.sort(
            key=lambda item: (
                int(item[0].get("frame_ordinal") or 0),
                str(item[0].get("frame_id") or ""),
            )
        )
        gaps = [
            math.hypot(right[1][0] - left[1][0], right[1][1] - left[1][1])
            for left, right in pairwise(sequence)
        ]
        stable_gaps = sorted(gap for gap in gaps if math.isfinite(gap) and gap >= 0.5)
        # At least four independent gaps are required; with fewer, one outlier
        # can define the median and the comparison is not explainable.
        if len(stable_gaps) < 4:
            continue
        middle = len(stable_gaps) // 2
        median_gap = (
            stable_gaps[middle]
            if len(stable_gaps) % 2
            else (stable_gaps[middle - 1] + stable_gaps[middle]) * 0.5
        )
        if not math.isfinite(median_gap) or median_gap < 0.5:
            continue
        threshold = max(25.0, 3.0 * median_gap)
        for index, gap in enumerate(gaps):
            if not math.isfinite(gap) or gap <= threshold:
                continue
            left, right = sequence[index], sequence[index + 1]
            right_artifact = {**right[0], "class_name": class_hint}
            midpoint = [(left[1][axis] + right[1][axis]) * 0.5 for axis in range(3)]
            ratio = gap / threshold
            priority, priority_evidence = _priority(
                "SPACING_ANOMALY",
                adjustment=min(10.0, ratio - 1.0),
                reason="Consecutive accepted positions exceed the deterministic spacing threshold.",
                details={
                    "gap_m": round(gap, 3),
                    "median_gap_m": round(median_gap, 3),
                    "threshold_m": round(threshold, 3),
                    "multiplier": 3.0,
                    "absolute_floor_m": 25.0,
                },
            )
            left_identity = _artifact_identity(left[0])
            right_identity = _artifact_identity(right[0])
            result.append(
                _candidate(
                    session_id=session_id,
                    dataset_id=dataset_id,
                    artifact=right_artifact,
                    task_type="SPACING_ANOMALY",
                    reason_codes=["SPACING_ANOMALY"],
                    priority=priority,
                    priority_evidence=priority_evidence,
                    position=midpoint,
                    target_layer_id=target_layer_id,
                    fingerprint_evidence={
                        "track_id": track_id,
                        "class_hint": class_hint,
                        "left": left_identity,
                        "right": right_identity,
                        "gap_m": round(gap, 6),
                        "median_gap_m": round(median_gap, 6),
                        "threshold_m": round(threshold, 6),
                    },
                )
            )
    return result


def generate_review_candidates(
    *,
    session_id: str,
    dataset_id: str,
    artifacts: Iterable[Mapping[str, Any]],
    frames: Iterable[Mapping[str, Any]],
    reviewed_frame_ids: Iterable[str] = (),
    settings: CandidateSourceSettings | None = None,
    target_layer_id: str | None = None,
    class_filters: Iterable[str] = (),
    max_candidates: int | None = None,
) -> list[dict[str, Any]]:
    """Generate a stable, de-duplicated task list for one session snapshot."""

    effective = settings or CandidateSourceSettings()
    candidates: list[dict[str, Any]] = []
    artifact_items = list(artifacts)
    for artifact in artifact_items:
        generated = build_detection_candidates(
            session_id=session_id,
            dataset_id=dataset_id,
            artifact=artifact,
            settings=effective,
            target_layer_id=target_layer_id,
            class_filters=class_filters,
        )
        remaining = (
            len(generated)
            if max_candidates is None
            else max(0, max_candidates - len(candidates))
        )
        candidates.extend(generated[:remaining])
        if max_candidates is not None and len(candidates) >= max_candidates:
            break
    remaining = (
        None if max_candidates is None else max(0, max_candidates - len(candidates))
    )
    if remaining is None or remaining > 0:
        spacing = build_spacing_anomaly_candidates(
            session_id=session_id,
            dataset_id=dataset_id,
            artifacts=artifact_items,
            settings=effective,
            target_layer_id=target_layer_id,
            class_filters=class_filters,
        )
        candidates.extend(spacing if remaining is None else spacing[:remaining])
    remaining = (
        None if max_candidates is None else max(0, max_candidates - len(candidates))
    )
    if remaining is None or remaining > 0:
        intervals = build_unreviewed_interval_candidates(
            session_id=session_id,
            dataset_id=dataset_id,
            frames=frames,
            reviewed_frame_ids=reviewed_frame_ids,
            settings=effective,
            target_layer_id=target_layer_id,
        )
        candidates.extend(intervals if remaining is None else intervals[:remaining])
    by_fingerprint = {str(item["source_fingerprint"]): item for item in candidates}
    return sorted(
        by_fingerprint.values(),
        key=lambda item: (
            str(item.get("track_id") or ""),
            str(item.get("frame_id") or ""),
            -float(item["priority"]),
            str(item["source_fingerprint"]),
        ),
    )


__all__ = [
    "CandidateSourceSettings",
    "build_detection_candidates",
    "build_spacing_anomaly_candidates",
    "build_unreviewed_interval_candidates",
    "deterministic_task_id",
    "generate_review_candidates",
    "source_fingerprint",
]
