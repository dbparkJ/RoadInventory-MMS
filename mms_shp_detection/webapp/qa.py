"""P1 review-session QA application service and issue API."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from mms_shp_detection.manual_object_tools import MANUAL_OBJECT_TEMPLATES
from mms_shp_detection.qa_rules import (
    QaRuleContext,
    class_name_for_feature,
    evaluate,
    nearby_duplicate_ids,
)

from .datasets import require_ready_dataset, utc_now
from .overlays import (
    _db_revision,
    _decode_feature,
    _feature_db,
    _layer_directory,
    _read_manifest,
)
from .security import UnsafePath
from .store import ReviewSessionReadOnlyError

router = APIRouter(prefix="/api", tags=["qa"])

QaIssueStatus = Literal["open", "resolved", "dismissed"]
QaIssueSeverity = Literal["info", "warning", "error"]


class QaIssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: QaIssueStatus
    override_reason: str | None = Field(None, min_length=3, max_length=500)


@dataclass(frozen=True)
class _StoredFeature:
    layer_id: str
    manifest: Mapping[str, Any]
    feature: Mapping[str, Any]
    provenance: Mapping[str, Any] | None
    class_name: str | None


def _require_session(request: Request, session_id: str) -> dict[str, Any]:
    session = request.app.state.store.get_review_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Review session not found.")
    require_ready_dataset(request, str(session["dataset_id"]))
    return session


def _require_mutable_session(session: Mapping[str, Any]) -> None:
    if str(session["status"]) in {"completed", "archived"}:
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )


def _public_issue(
    value: Mapping[str, Any],
    navigation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whitelist registry fields so implementation details never reach clients."""

    return {
        "id": str(value["id"]),
        "session_id": str(value["session_id"]),
        "layer_id": str(value["layer_id"]),
        "feature_id": (
            None if value.get("feature_id") is None else str(value["feature_id"])
        ),
        "rule_id": str(value["rule_id"]),
        "severity": str(value["severity"]),
        "message": str(value["message"]),
        "related_feature_ids": [
            str(item) for item in value.get("related_feature_ids", [])
        ],
        "status": str(value["status"]),
        "created_at": str(value["created_at"]),
        "updated_at": str(value["updated_at"]),
        "override_reason": value.get("override_reason"),
        "frame_id": None if navigation is None else navigation.get("frame_id"),
        "location_hint": (
            None if navigation is None else navigation.get("location_hint")
        ),
    }


def _issue_navigation(
    app: Any,
    dataset_id: str,
    issues: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Derive frame/location navigation without widening the registry schema."""

    by_issue = {
        str(issue["id"]): {"frame_id": None, "location_hint": None} for issue in issues
    }
    issue_ids_by_feature: dict[tuple[str, str], list[str]] = {}
    proposal_issue_ids: list[str] = []
    for issue in issues:
        issue_id = str(issue["id"])
        feature_id = issue.get("feature_id")
        if feature_id is None:
            proposal_issue_ids.append(issue_id)
            continue
        issue_ids_by_feature.setdefault(
            (str(issue["layer_id"]), str(feature_id)), []
        ).append(issue_id)

    layer_ids = sorted({key[0] for key in issue_ids_by_feature})
    for layer_id in layer_ids:
        feature_ids = sorted(
            feature_id
            for candidate_layer, feature_id in issue_ids_by_feature
            if candidate_layer == layer_id
        )
        try:
            layer_dir = _layer_directory(app, dataset_id, layer_id)
            manifest = _read_manifest(layer_dir)
            if str(manifest.get("dataset_id")) != dataset_id:
                continue
            with _feature_db(layer_dir) as connection:
                has_provenance = _table_exists(connection, "feature_provenance")
                for start in range(0, len(feature_ids), 400):
                    batch = feature_ids[start : start + 400]
                    placeholders = ",".join("?" for _ in batch)
                    for row in connection.execute(
                        f"""
                        SELECT id,point_x,point_y,point_z FROM features
                        WHERE id IN ({placeholders})
                        """,
                        batch,
                    ).fetchall():
                        values = (row["point_x"], row["point_y"], row["point_z"])
                        try:
                            location = [float(value) for value in values]
                        except (TypeError, ValueError):
                            location = []
                        if len(location) != 3 or not all(
                            math.isfinite(value) for value in location
                        ):
                            location = []
                        for issue_id in issue_ids_by_feature.get(
                            (layer_id, str(row["id"])), []
                        ):
                            by_issue[issue_id]["location_hint"] = location or None
                    if not has_provenance:
                        continue
                    for row in connection.execute(
                        f"""
                        SELECT feature_id,provenance_json FROM feature_provenance
                        WHERE feature_id IN ({placeholders})
                        """,
                        batch,
                    ).fetchall():
                        try:
                            provenance = json.loads(str(row["provenance_json"]))
                            frame_ids = provenance.get("source_frame_ids") or []
                            frame_id = str(frame_ids[0]) if frame_ids else None
                        except (AttributeError, TypeError, json.JSONDecodeError):
                            frame_id = None
                        for issue_id in issue_ids_by_feature.get(
                            (layer_id, str(row["feature_id"])), []
                        ):
                            by_issue[issue_id]["frame_id"] = frame_id
        except (
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
            UnsafePath,
            json.JSONDecodeError,
            sqlite3.Error,
        ):
            # A stale issue may outlive an archived/unavailable layer.  Keep the
            # issue visible and simply omit its navigation hint.
            continue

    if proposal_issue_ids:
        proposal_store = getattr(app.state, "manual_object_proposals", {})
        if isinstance(proposal_store, Mapping):
            proposal_store = dict(proposal_store)
            proposal_frames = {
                str(key): str(value.get("frame_id"))
                for key, value in tuple(proposal_store.items())
                if isinstance(value, Mapping)
                and str(value.get("dataset_id") or "") == dataset_id
                and value.get("frame_id") is not None
            }
            for issue in issues:
                issue_id = str(issue["id"])
                if issue_id not in proposal_issue_ids:
                    continue
                for related_id in issue.get("related_feature_ids", []):
                    frame_id = proposal_frames.get(str(related_id))
                    if frame_id is not None:
                        by_issue[issue_id]["frame_id"] = frame_id
                        break
    return by_issue


def _public_issues(
    app: Any,
    dataset_id: str,
    issues: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    navigation = _issue_navigation(app, dataset_id, issues)
    return [_public_issue(issue, navigation.get(str(issue["id"]))) for issue in issues]


def _page(
    items: list[dict[str, Any]],
    *,
    total: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    consumed = offset + len(items)
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": consumed if consumed < total else None,
    }


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def _read_target_layer(
    app: Any,
    dataset_id: str,
    layer_id: str,
) -> tuple[Mapping[str, Any], list[_StoredFeature], int]:
    layer_dir = _layer_directory(app, dataset_id, layer_id)
    manifest = _read_manifest(layer_dir)
    if (
        str(manifest.get("dataset_id")) != dataset_id
        or str(manifest.get("id")) != layer_id
    ):
        raise ValueError("Layer ownership mismatch.")
    with _feature_db(layer_dir) as connection:
        revision = _db_revision(connection)
        rows = connection.execute(
            "SELECT * FROM features WHERE deleted=0 ORDER BY ordinal, id"
        ).fetchall()
        provenance_by_id: dict[str, Mapping[str, Any]] = {}
        if _table_exists(connection, "feature_provenance"):
            for provenance_row in connection.execute(
                "SELECT feature_id,provenance_json FROM feature_provenance"
            ).fetchall():
                try:
                    value = json.loads(str(provenance_row["provenance_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, Mapping):
                    provenance_by_id[str(provenance_row["feature_id"])] = value
    features: list[_StoredFeature] = []
    for row in rows:
        try:
            feature = _decode_feature(row)
        except (TypeError, ValueError, json.JSONDecodeError):
            feature = {
                "type": "Feature",
                "id": str(row["id"]),
                "geometry": None,
                "properties": {},
            }
        feature_id = str(feature["id"])
        provenance = provenance_by_id.get(feature_id)
        features.append(
            _StoredFeature(
                layer_id=layer_id,
                manifest=manifest,
                feature=feature,
                provenance=provenance,
                class_name=class_name_for_feature(feature, provenance),
            )
        )
    return manifest, features, revision


def _finite_vector(value: object) -> tuple[float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) < 3:
        return None
    try:
        result = tuple(float(item) for item in value[:3])
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _catalog_bounds(
    app: Any, dataset_id: str
) -> tuple[tuple[float, float, float, float], tuple[float, float]] | None:
    catalog = getattr(app.state, "catalogs", {}).get(dataset_id)
    if not isinstance(catalog, Mapping):
        return None
    minima: list[tuple[float, float, float]] = []
    maxima: list[tuple[float, float, float]] = []
    files = catalog.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return None
    for item in files:
        if not isinstance(item, Mapping):
            continue
        minimum = _finite_vector(item.get("min", item.get("mins")))
        maximum = _finite_vector(item.get("max", item.get("maxs")))
        if minimum is None or maximum is None:
            continue
        if any(minimum[index] > maximum[index] for index in range(3)):
            continue
        minima.append(minimum)
        maxima.append(maximum)
    if not minima:
        return None
    minimum_x = min(item[0] for item in minima)
    minimum_y = min(item[1] for item in minima)
    minimum_z = min(item[2] for item in minima)
    maximum_x = max(item[0] for item in maxima)
    maximum_y = max(item[1] for item in maxima)
    maximum_z = max(item[2] for item in maxima)
    xy_pad = max(1.0, math.hypot(maximum_x - minimum_x, maximum_y - minimum_y) * 0.005)
    z_pad = max(1.0, (maximum_z - minimum_z) * 0.05)
    return (
        (
            minimum_x - xy_pad,
            minimum_y - xy_pad,
            maximum_x + xy_pad,
            maximum_y + xy_pad,
        ),
        (minimum_z - z_pad, maximum_z + z_pad),
    )


def _dataset_context(
    request: Request,
    session: Mapping[str, Any],
) -> tuple[
    frozenset[str], tuple[float, float, float, float] | None, tuple[float, float] | None
]:
    dataset_id = str(session["dataset_id"])
    frames = request.app.state.store.all_frames(dataset_id)
    known_frame_ids = frozenset(
        reference
        for frame in frames
        for reference in (
            str(frame["id"]),
            str((frame.get("task") or {}).get("image_name") or ""),
        )
        if reference
    )
    catalog_bounds = _catalog_bounds(request.app, dataset_id)
    if catalog_bounds is not None:
        return known_frame_ids, catalog_bounds[0], catalog_bounds[1]

    origins = [
        origin
        for frame in frames
        if (origin := _finite_vector((frame.get("task") or {}).get("origin")))
        is not None
    ]
    if not origins:
        return known_frame_ids, None, None
    # Frame origins are an always-available fallback until a point catalog is
    # loaded.  The generous margins avoid rejecting roadside objects while
    # still catching coordinate-system mistakes and extreme elevation values.
    return (
        known_frame_ids,
        (
            min(item[0] for item in origins) - 250.0,
            min(item[1] for item in origins) - 250.0,
            max(item[0] for item in origins) + 250.0,
            max(item[1] for item in origins) + 250.0,
        ),
        (
            min(item[2] for item in origins) - 50.0,
            max(item[2] for item in origins) + 50.0,
        ),
    )


def _point_position(feature: Mapping[str, Any]) -> tuple[float, float, float] | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
        return None
    return _finite_vector(geometry.get("coordinates"))


def _issue_id(
    session_id: str,
    layer_id: str,
    feature_id: str | None,
    rule_id: str,
    message: str,
    related_feature_ids: Sequence[str],
) -> str:
    payload = "\0".join(
        (
            session_id,
            layer_id,
            feature_id or "",
            rule_id,
            message,
            *sorted(related_feature_ids),
        )
    )
    return "qai_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _unresolved_proposal_issues(
    app: Any,
    *,
    session_id: str,
    dataset_id: str,
    target_layer_ids: set[str],
    committed_observation_ids: set[str],
    now: str,
) -> list[dict[str, Any]]:
    store = getattr(app.state, "manual_object_proposals", {})
    if not isinstance(store, Mapping):
        return []
    store = dict(store)
    issues: list[dict[str, Any]] = []
    for proposal_id in sorted(store):
        stored = store.get(proposal_id)
        if not isinstance(stored, Mapping):
            continue
        layer_id = str(stored.get("target_layer_id") or "")
        if (
            str(stored.get("dataset_id") or "") != dataset_id
            or layer_id not in target_layer_ids
        ):
            continue
        proposal = stored.get("proposal")
        if not isinstance(proposal, Mapping) or str(proposal.get("status")) != "review":
            continue
        observation_id = str(stored.get("observation_id") or "")
        if observation_id and observation_id in committed_observation_ids:
            continue
        opaque_proposal_id = str(proposal.get("proposal_id") or proposal_id)
        message = (
            f"REVIEW proposal {opaque_proposal_id} has not been confirmed or cancelled."
        )
        related = [opaque_proposal_id]
        issues.append(
            {
                "id": _issue_id(
                    session_id,
                    layer_id,
                    None,
                    "REVIEW_PROPOSAL_UNRESOLVED",
                    message,
                    related,
                ),
                "session_id": session_id,
                "layer_id": layer_id,
                "feature_id": None,
                "rule_id": "REVIEW_PROPOSAL_UNRESOLVED",
                "severity": "warning",
                "message": message,
                "related_feature_ids": related,
                "status": "open",
                "created_at": now,
                "updated_at": now,
                "override_reason": None,
            }
        )
    return issues


def _run_qa(
    request: Request,
    session: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    session_id = str(session["id"])
    dataset_id = str(session["dataset_id"])
    effective_target_layer_ids = {
        str(value) for value in session.get("target_layer_ids", [])
    }
    effective_target_layer_ids.update(
        request.app.state.store.review_session_effective_target_layer_ids(session_id)
    )
    target_layer_ids = sorted(effective_target_layer_ids)
    known_frames, dataset_bounds, z_bounds = _dataset_context(request, session)
    layer_manifests: dict[str, Mapping[str, Any]] = {}
    layer_revisions: dict[str, int] = {}
    stored_features: list[_StoredFeature] = []
    try:
        for layer_id in target_layer_ids:
            manifest, features, revision = _read_target_layer(
                request.app, dataset_id, layer_id
            )
            layer_manifests[layer_id] = manifest
            layer_revisions[layer_id] = revision
            stored_features.extend(features)
    except (
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        UnsafePath,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail="A target layer is unavailable or is not owned by this dataset.",
        ) from exc

    class_filters = {str(value).upper() for value in session.get("class_filters", [])}
    if class_filters:
        stored_features = [
            item
            for item in stored_features
            if item.class_name is None or item.class_name in class_filters
        ]

    duplicate_inputs: list[tuple[str, tuple[float, float, float], str, float]] = []
    globally_unique_id: dict[str, tuple[str, str]] = {}
    for item in stored_features:
        feature_id = str(item.feature["id"])
        qualified_id = f"{item.layer_id}:{feature_id}"
        globally_unique_id[qualified_id] = (item.layer_id, feature_id)
        position = _point_position(item.feature)
        if position is None:
            continue
        template = MANUAL_OBJECT_TEMPLATES.get(item.class_name or "")
        radius = template.duplicate_radius_m if template is not None else 0.50
        group_key = f"{item.layer_id}:{item.class_name or 'UNKNOWN'}"
        duplicate_inputs.append((qualified_id, position, group_key, radius))
    qualified_duplicates = nearby_duplicate_ids(duplicate_inputs)

    now = utc_now()
    issues: list[dict[str, Any]] = []
    committed_observation_ids: set[str] = set()
    for item in stored_features:
        feature_id = str(item.feature["id"])
        if item.provenance is not None:
            raw_observations = item.provenance.get("manual_observation_ids")
            if isinstance(raw_observations, Sequence) and not isinstance(
                raw_observations, (str, bytes)
            ):
                committed_observation_ids.update(
                    str(value) for value in raw_observations
                )
        template = MANUAL_OBJECT_TEMPLATES.get(item.class_name or "")
        qualified_id = f"{item.layer_id}:{feature_id}"
        related = tuple(
            globally_unique_id[value][1]
            for value in qualified_duplicates.get(qualified_id, ())
            if value in globally_unique_id
        )
        manifest = layer_manifests[item.layer_id]
        context = QaRuleContext(
            layer_id=item.layer_id,
            fields=tuple(manifest.get("fields") or ()),
            known_frame_ids=known_frames,
            dataset_bounds_xy=dataset_bounds,
            z_bounds=z_bounds,
            duplicate_ids=related,
        )
        for finding in evaluate(
            item.feature,
            template,
            context,
            provenance=item.provenance,
        ):
            related_ids = list(finding.related_feature_ids)
            issues.append(
                {
                    "id": _issue_id(
                        session_id,
                        item.layer_id,
                        feature_id,
                        finding.rule_id,
                        finding.message,
                        related_ids,
                    ),
                    "session_id": session_id,
                    "layer_id": item.layer_id,
                    "feature_id": feature_id,
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "message": finding.message,
                    "related_feature_ids": related_ids,
                    "status": "open",
                    "created_at": now,
                    "updated_at": now,
                    "override_reason": None,
                }
            )
    issues.extend(
        _unresolved_proposal_issues(
            request.app,
            session_id=session_id,
            dataset_id=dataset_id,
            target_layer_ids=set(target_layer_ids),
            committed_observation_ids=committed_observation_ids,
            now=now,
        )
    )
    return (
        sorted(
            issues,
            key=lambda value: (
                {"error": 0, "warning": 1, "info": 2}[str(value["severity"])],
                str(value["layer_id"]),
                str(value.get("feature_id") or ""),
                str(value["rule_id"]),
                str(value["id"]),
            ),
        ),
        layer_revisions,
        now,
    )


@router.post("/review-sessions/{session_id}/qa/run")
def run_review_qa(session_id: str, request: Request) -> dict[str, Any]:
    session = _require_session(request, session_id)
    _require_mutable_session(session)
    issues, layer_revisions, ran_at = _run_qa(request, session)
    try:
        persisted = request.app.state.store.replace_review_qa_issues(
            session_id,
            issues,
            layer_revisions=layer_revisions,
            ran_at=ran_at,
        )
    except ReviewSessionReadOnlyError as exc:
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        ) from exc
    counts = Counter(str(item["severity"]) for item in persisted)
    return {
        "items": _public_issues(request.app, str(session["dataset_id"]), persisted),
        "total": len(persisted),
        "counts": {
            "error": counts["error"],
            "warning": counts["warning"],
            "info": counts["info"],
        },
        "ran_at": ran_at,
        "layer_revisions": layer_revisions,
    }


@router.get("/review-sessions/{session_id}/qa/issues")
def list_review_qa_issues(
    session_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    issue_status: Annotated[QaIssueStatus | None, Query(alias="status")] = None,
    severity: Annotated[QaIssueSeverity | None, Query()] = None,
    rule_id: Annotated[
        str | None,
        Query(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$"),
    ] = None,
    layer_id: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
) -> dict[str, Any]:
    session = _require_session(request, session_id)
    dataset_id = str(session["dataset_id"])
    status_value = issue_status if issue_status is None else str(issue_status)
    items, total = request.app.state.store.list_review_qa_issues(
        session_id,
        offset=offset,
        limit=limit,
        status=status_value,
        severity=None if severity is None else str(severity),
        rule_id=rule_id,
        layer_id=layer_id,
    )
    return _page(
        _public_issues(request.app, dataset_id, items),
        total=total,
        offset=offset,
        limit=limit,
    )


def _issue_with_dataset(request: Request, issue_id: str) -> dict[str, Any] | None:
    with request.app.state.store.connection() as connection:
        row = connection.execute(
            """
            SELECT q.*,s.dataset_id,s.status AS session_status
            FROM qa_issues q
            JOIN review_sessions s ON s.id=q.session_id
            WHERE q.id=?
            """,
            (issue_id,),
        ).fetchone()
    if row is None:
        return None
    value = dict(row)
    try:
        value["related_feature_ids"] = json.loads(
            str(value.pop("related_feature_ids_json", "[]"))
        )
    except (TypeError, json.JSONDecodeError):
        value["related_feature_ids"] = []
    return value


@router.patch("/qa/issues/{issue_id}")
def patch_review_qa_issue(
    issue_id: str,
    payload: QaIssuePatch,
    request: Request,
) -> dict[str, Any]:
    current = _issue_with_dataset(request, issue_id)
    if current is None:
        raise HTTPException(status_code=404, detail="QA issue not found.")
    require_ready_dataset(request, str(current["dataset_id"]))
    _require_mutable_session({"status": current["session_status"]})
    requested_status = str(payload.status)
    reason = payload.override_reason
    if str(current["severity"]) == "error" and requested_status != "open":
        raise HTTPException(
            status_code=409,
            detail=(
                "Error QA issues can only be cleared by correcting the data and "
                "rerunning QA."
            ),
        )
    if requested_status == "dismissed":
        if reason is None:
            raise HTTPException(
                status_code=422,
                detail="Dismissing a QA warning requires an override reason.",
            )
    elif requested_status == "open":
        reason = None
    elif reason is not None:
        raise HTTPException(
            status_code=422,
            detail="override_reason is only accepted when dismissing an issue.",
        )
    outcome, updated = request.app.state.store.update_review_qa_issue(
        issue_id,
        requested_status,
        reason,
        utc_now(),
    )
    if outcome == "missing" or updated is None:
        raise HTTPException(status_code=404, detail="QA issue not found.")
    if outcome == "session_immutable":
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )
    if outcome == "error_immutable":
        raise HTTPException(
            status_code=409,
            detail=(
                "Error QA issues can only be cleared by correcting the data and "
                "rerunning QA."
            ),
        )
    return {
        "issue": _public_issues(request.app, str(current["dataset_id"]), [updated])[0]
    }


__all__ = ["QaIssuePatch", "router"]
