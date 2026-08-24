"""Pure, table-driven QA rules for P1 review workspace features.

The web adapter is responsible for reading SQLite and resolving dataset scope.
This module deliberately accepts plain feature/provenance mappings so the rules
remain deterministic, cheap to test, and independent from FastAPI or storage.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .manual_object_tools import ManualObjectTemplate

QaSeverity = Literal["info", "warning", "error"]

CLASS_FIELD_ALIASES = frozenset(
    {
        "CLASS",
        "CLASS_NM",
        "CLASSNAME",
        "CLASS_NAME",
        "OBJ_TYPE",
        "TYPE",
    }
)
SUPPORT_FIELD_ALIASES = frozenset(
    {"SUPPORT", "SUPPORT_ID", "SUPPORTID", "POLE_ID", "POLEID"}
)
PROPOSAL_STATUS_FIELD_ALIASES = frozenset(
    {"PROPOSAL_STATUS", "QUALITY_STATUS", "QA_STATUS"}
)
SOURCE_FRAME_FIELD_ALIASES = frozenset(
    {
        "FRAME",
        "FRAME_ID",
        "SOURCE_FRAME",
        "SOURCE_FRAME_ID",
        "IMAGE_NAME",
        "IMG_NAME",
    }
)
_UNREVIEWED_STATUSES = frozenset({"unreviewed", "todo", "in_progress"})


def normalized_field_name(value: object) -> str:
    """Return the stable spelling used for semantic field matching."""

    return str(value).strip().upper().replace(" ", "_").replace("-", "_")


def missing_value(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class QaFinding:
    rule_id: str
    severity: QaSeverity
    message: str
    related_feature_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class QaRuleContext:
    """Resolved QA inputs for one feature.

    Bounds are expressed in the dataset coordinate system.  ``duplicate_ids``
    must already be filtered by class/template and distance by the batch helper;
    this keeps ``evaluate`` linear and suitable for table-driven unit tests.
    """

    layer_id: str
    fields: tuple[Mapping[str, Any], ...] = ()
    known_frame_ids: frozenset[str] = frozenset()
    dataset_bounds_xy: tuple[float, float, float, float] | None = None
    z_bounds: tuple[float, float] | None = None
    duplicate_ids: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    domains: Mapping[str, frozenset[object]] = field(default_factory=dict)


def _property_by_alias(
    properties: Mapping[str, Any], aliases: frozenset[str]
) -> tuple[str, Any] | None:
    for name, value in properties.items():
        if normalized_field_name(name) in aliases:
            return str(name), value
    return None


def class_name_for_feature(
    feature: Mapping[str, Any],
    provenance: Mapping[str, Any] | None,
) -> str | None:
    """Resolve class without requiring provenance fields in the DBF schema."""

    properties = feature.get("properties")
    if isinstance(properties, Mapping):
        match = _property_by_alias(properties, CLASS_FIELD_ALIASES)
        if match is not None and not missing_value(match[1]):
            return normalized_field_name(match[1])
    if isinstance(provenance, Mapping):
        tool = str(provenance.get("creation_tool") or "").casefold()
        if tool == "panorama_bbox_point_v1":
            return "TRAFFIC_SIGN"
        if tool == "manual_pole_base_v1":
            return "SIGN_SUPPORT_POLE"
    return None


def _point_coordinates(feature: Mapping[str, Any]) -> tuple[float, ...] | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, Sequence) or isinstance(coordinates, (str, bytes)):
        return None
    if len(coordinates) not in {2, 3}:
        return None
    try:
        values = tuple(float(value) for value in coordinates)
    except (TypeError, ValueError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _bounded_text(value: object, maximum: int = 80) -> str:
    text = str(value)
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def evaluate(
    feature: Mapping[str, Any],
    template: ManualObjectTemplate | None,
    context: QaRuleContext,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[QaFinding]:
    """Evaluate the initial P1 QA registry for one committed feature."""

    findings: list[QaFinding] = []
    properties_value = feature.get("properties")
    properties: Mapping[str, Any] = (
        properties_value if isinstance(properties_value, Mapping) else {}
    )
    property_names = {str(name): value for name, value in properties.items()}

    required_fields = set(context.required_fields)
    for definition in context.fields:
        if bool(definition.get("required")) and definition.get("name"):
            required_fields.add(str(definition["name"]))
    for name in sorted(required_fields):
        if name not in property_names or missing_value(property_names[name]):
            findings.append(
                QaFinding(
                    "REQUIRED_FIELD",
                    "error",
                    f"Required field '{_bounded_text(name)}' is missing.",
                )
            )

    if template is not None and "class" in template.required_semantics:
        class_property = _property_by_alias(properties, CLASS_FIELD_ALIASES)
        # A layer without a class DBF field still has a fixed template class in
        # internal provenance.  If the field exists, however, it must be filled.
        if class_property is not None and missing_value(class_property[1]):
            findings.append(
                QaFinding(
                    "REQUIRED_FIELD",
                    "error",
                    f"Required class field '{_bounded_text(class_property[0])}' is missing.",
                )
            )

    domains: dict[str, frozenset[object]] = dict(context.domains)
    for definition in context.fields:
        name = str(definition.get("name") or "")
        if not name:
            continue
        raw_domain = definition.get("domain", definition.get("allowed_values"))
        if isinstance(raw_domain, Mapping):
            raw_domain = raw_domain.get("values")
        if isinstance(raw_domain, Sequence) and not isinstance(
            raw_domain, (str, bytes)
        ):
            domains[name] = frozenset(raw_domain)
    for name in sorted(domains):
        value = property_names.get(name)
        if missing_value(value) or value in domains[name]:
            continue
        allowed = ", ".join(
            _bounded_text(item, 30) for item in sorted(domains[name], key=str)
        )
        findings.append(
            QaFinding(
                "DOMAIN_VALUE",
                "error",
                f"Field '{_bounded_text(name)}' has a value outside its domain ({allowed}).",
            )
        )

    coordinates = _point_coordinates(feature)
    if coordinates is None:
        findings.append(
            QaFinding(
                "GEOMETRY_REQUIRED",
                "error",
                "A finite Point geometry is required.",
            )
        )
    else:
        if context.dataset_bounds_xy is not None:
            minimum_x, minimum_y, maximum_x, maximum_y = context.dataset_bounds_xy
            x, y = coordinates[:2]
            if not (minimum_x <= x <= maximum_x and minimum_y <= y <= maximum_y):
                findings.append(
                    QaFinding(
                        "OUTSIDE_DATASET_BOUNDS",
                        "error",
                        "Feature geometry is outside the dataset point-cloud bounds.",
                    )
                )
        if context.z_bounds is not None and len(coordinates) < 3:
            findings.append(
                QaFinding(
                    "Z_OUTLIER",
                    "error",
                    "Point geometry has no finite Z coordinate.",
                )
            )
        elif context.z_bounds is not None:
            minimum_z, maximum_z = context.z_bounds
            if not minimum_z <= coordinates[2] <= maximum_z:
                findings.append(
                    QaFinding(
                        "Z_OUTLIER",
                        "error",
                        "Feature Z is outside the dataset point-cloud elevation range.",
                    )
                )

    if context.duplicate_ids:
        findings.append(
            QaFinding(
                "DUPLICATE_NEARBY",
                "warning",
                "A nearby feature with the same object class may be a duplicate.",
                tuple(sorted(set(context.duplicate_ids))),
            )
        )

    if isinstance(provenance, Mapping):
        source_frames_value = provenance.get("source_frame_ids")
        source_frames = (
            [str(value) for value in source_frames_value]
            if isinstance(source_frames_value, Sequence)
            and not isinstance(source_frames_value, (str, bytes))
            else []
        )
        missing_frames = [
            frame_id
            for frame_id in source_frames
            if frame_id not in context.known_frame_ids
        ]
        if not source_frames or missing_frames:
            findings.append(
                QaFinding(
                    "MISSING_SOURCE_FRAME",
                    "warning",
                    "Feature provenance has no valid source frame in this dataset.",
                )
            )
        origin = str(provenance.get("origin") or "").upper()
        review_status = str(provenance.get("review_status") or "").casefold()
        if origin == "MANUAL" and review_status in _UNREVIEWED_STATUSES:
            findings.append(
                QaFinding(
                    "UNREVIEWED_MANUAL_FEATURE",
                    "warning",
                    "A manually created feature still requires operator review.",
                )
            )
        if str(provenance.get("proposal_status") or "").casefold() == "review":
            findings.append(
                QaFinding(
                    "REVIEW_PROPOSAL_UNRESOLVED",
                    "warning",
                    "The feature's REVIEW proposal has not been resolved.",
                )
            )
    else:
        source_property = _property_by_alias(properties, SOURCE_FRAME_FIELD_ALIASES)
        if (
            source_property is None
            or missing_value(source_property[1])
            or str(source_property[1]) not in context.known_frame_ids
        ):
            findings.append(
                QaFinding(
                    "MISSING_SOURCE_FRAME",
                    "warning",
                    "Feature has no valid source frame evidence in this dataset.",
                )
            )

    if template is not None and "support_id" in template.relation_semantics:
        support_property = _property_by_alias(properties, SUPPORT_FIELD_ALIASES)
        if support_property is None or missing_value(support_property[1]):
            findings.append(
                QaFinding(
                    "SUPPORT_RELATION_REQUIRED",
                    "warning",
                    "Traffic sign requires a support relation.",
                )
            )

    proposal_status = _property_by_alias(properties, PROPOSAL_STATUS_FIELD_ALIASES)
    if (
        proposal_status is not None
        and str(proposal_status[1]).casefold() == "review"
        and not any(
            finding.rule_id == "REVIEW_PROPOSAL_UNRESOLVED" for finding in findings
        )
    ):
        findings.append(
            QaFinding(
                "REVIEW_PROPOSAL_UNRESOLVED",
                "warning",
                "The feature's REVIEW proposal has not been resolved.",
            )
        )

    return findings


def nearby_duplicate_ids(
    features: Iterable[tuple[str, tuple[float, float, float], str, float]],
    *,
    maximum_z_difference: float = 2.0,
) -> dict[str, tuple[str, ...]]:
    """Return deterministic same-class duplicate neighbors using a spatial grid.

    Input tuples are ``(feature_id, (x, y, z), group_key, radius_m)``.  Every
    matching pair is reported for both features; the caller can decide whether
    to show one issue per pair or one issue per affected object.
    """

    records = sorted(features, key=lambda item: item[0])
    if not records:
        return {}
    cell_size = max(0.01, max(float(item[3]) for item in records))
    grid: dict[
        tuple[str, int, int], list[tuple[str, tuple[float, float, float], float]]
    ] = {}
    results: dict[str, set[str]] = {}
    for feature_id, position, group_key, radius in records:
        x, y, z = position
        cell_x = math.floor(x / cell_size)
        cell_y = math.floor(y / cell_size)
        reach = max(1, math.ceil(float(radius) / cell_size))
        for offset_x in range(-reach, reach + 1):
            for offset_y in range(-reach, reach + 1):
                for other_id, other_position, other_radius in grid.get(
                    (group_key, cell_x + offset_x, cell_y + offset_y), []
                ):
                    threshold = max(float(radius), other_radius)
                    if (
                        math.hypot(x - other_position[0], y - other_position[1])
                        > threshold
                    ):
                        continue
                    if abs(z - other_position[2]) > maximum_z_difference:
                        continue
                    results.setdefault(feature_id, set()).add(other_id)
                    results.setdefault(other_id, set()).add(feature_id)
        grid.setdefault((group_key, cell_x, cell_y), []).append(
            (feature_id, position, float(radius))
        )
    return {
        feature_id: tuple(sorted(related_ids))
        for feature_id, related_ids in sorted(results.items())
    }


__all__ = [
    "CLASS_FIELD_ALIASES",
    "PROPOSAL_STATUS_FIELD_ALIASES",
    "SOURCE_FRAME_FIELD_ALIASES",
    "QaFinding",
    "QaRuleContext",
    "class_name_for_feature",
    "evaluate",
    "missing_value",
    "nearby_duplicate_ids",
    "normalized_field_name",
]
