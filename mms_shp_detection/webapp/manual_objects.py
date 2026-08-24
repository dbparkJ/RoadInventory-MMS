from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
import uuid
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mms_shp_detection.manual_object_tools import (
    MANUAL_OBJECT_TEMPLATES,
    PanoramaBboxPointResult,
    infer_panorama_bbox_point,
)

from .datasets import require_ready_dataset, schedule_catalog, utc_now
from .media import _panorama_axes, _sample_nearby_points
from .overlays import (
    OverlayTooLarge,
    _active_count,
    _blank_properties_with_next_id,
    _coerce_property,
    _feature_db,
    _json_bytes,
    _layer_directory,
    _layer_lock,
    _next_feature_identity,
    _point_columns,
    _read_manifest,
    _updated_revision,
)
from .pole_tools import _finish_inference_after_request_cancel
from .review_contracts import (
    EquirectangularBbox,
    FeatureProvenance,
    GeometryProposal,
    ManualObservation,
)
from .task_resolution_outbox import (
    enqueue_task_resolution_intent,
    ensure_task_resolution_outbox,
    reconcile_task_resolution_intent,
    review_dataset_lock,
)

router = APIRouter(prefix="/api", tags=["manual-objects"])

PROPOSAL_TTL_SECONDS = 15 * 60
MAX_PROPOSALS = 2_000
MAX_PROPOSAL_POINT_BUDGET = 200_000


class ManualObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_layer_id: str = Field(min_length=1, max_length=160)
    template_id: Literal["TRAFFIC_SIGN"] = "TRAFFIC_SIGN"
    geometry_2d: EquirectangularBbox
    created_by: str = Field(default="operator-local", min_length=1, max_length=160)


class ManualObjectProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_layer_id: str = Field(min_length=1, max_length=160)
    observation_id: str = Field(min_length=1, max_length=160)
    template_id: Literal["TRAFFIC_SIGN"] = "TRAFFIC_SIGN"
    property_patch: dict[str, Any] = Field(default_factory=dict, max_length=200)
    max_range_m: float = Field(default=100.0, ge=1.0, le=100.0)
    yaw_offset_deg: float | None = Field(None, ge=-180.0, le=180.0)
    pitch_offset_deg: float | None = Field(None, ge=-45.0, le=45.0)


class ProposalCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)
    task_id: str | None = Field(None, min_length=1, max_length=160)
    created_by: str = Field(default="operator-local", min_length=1, max_length=160)
    properties: dict[str, Any] = Field(default_factory=dict, max_length=200)
    allow_near_duplicate: bool = False
    override_reason: str | None = Field(None, min_length=3, max_length=500)


class DuplicatePreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_layer_id: str = Field(min_length=1, max_length=160)
    template_id: Literal["TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"]
    position: tuple[float, float, float]
    observation_id: str | None = Field(None, min_length=1, max_length=160)
    exclude_feature_id: str | None = Field(None, min_length=1, max_length=160)


def _ensure_manual_object_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_observations (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            layer_id TEXT NOT NULL,
            frame_id TEXT NOT NULL,
            view_type TEXT NOT NULL,
            class_name TEXT NOT NULL,
            geometry_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS manual_observations_frame
        ON manual_observations(frame_id, created_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_provenance (
            feature_id TEXT PRIMARY KEY,
            provenance_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS edit_transactions (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL,
            feature_id TEXT,
            task_id TEXT,
            revision INTEGER,
            before_json TEXT,
            after_json TEXT,
            status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    edit_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(edit_transactions)")
    }
    if "revision" not in edit_columns:
        connection.execute("ALTER TABLE edit_transactions ADD COLUMN revision INTEGER")
    if "proposal_id" not in edit_columns:
        connection.execute("ALTER TABLE edit_transactions ADD COLUMN proposal_id TEXT")
    if "override_reason" not in edit_columns:
        connection.execute(
            "ALTER TABLE edit_transactions ADD COLUMN override_reason TEXT"
        )
    ensure_task_resolution_outbox(connection)


def _insert_observation(
    layer_dir: Any,
    observation: ManualObservation,
    *,
    layer_id: str,
) -> None:
    with _feature_db(layer_dir, write=True) as connection:
        _ensure_manual_object_tables(connection)
        connection.execute(
            """
            INSERT INTO manual_observations(
                id,dataset_id,layer_id,frame_id,view_type,class_name,
                geometry_json,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                observation.observation_id,
                observation.dataset_id,
                layer_id,
                observation.frame_id,
                observation.view_type,
                observation.class_name,
                json.dumps(
                    observation.geometry_2d.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                observation.created_by,
                utc_now(),
            ),
        )


def _observation_from_row(row: sqlite3.Row) -> ManualObservation:
    return ManualObservation.model_validate(
        {
            "observation_id": row["id"],
            "dataset_id": row["dataset_id"],
            "frame_id": row["frame_id"],
            "view_type": row["view_type"],
            "class_name": row["class_name"],
            "geometry_2d": json.loads(row["geometry_json"]),
            "created_by": row["created_by"],
        }
    )


def _get_observation(layer_dir: Any, observation_id: str) -> ManualObservation | None:
    with _feature_db(layer_dir) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='manual_observations'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT * FROM manual_observations WHERE id=?", (observation_id,)
        ).fetchone()
    return None if row is None else _observation_from_row(row)


def _proposal_store(app: Any) -> dict[str, dict[str, Any]]:
    store = getattr(app.state, "manual_object_proposals", None)
    if store is None:
        store = {}
        app.state.manual_object_proposals = store
    now = time.monotonic()
    expired = [
        proposal_id
        for proposal_id, value in store.items()
        if now - float(value.get("created_monotonic", now)) > PROPOSAL_TTL_SECONDS
        and not bool(value.get("commit_in_progress"))
    ]
    for proposal_id in expired:
        store.pop(proposal_id, None)
    while len(store) >= MAX_PROPOSALS:
        evictable = [
            proposal_id
            for proposal_id, value in store.items()
            if not bool(value.get("commit_in_progress"))
        ]
        if not evictable:
            break
        oldest = min(
            evictable,
            key=lambda key: float(store[key].get("created_monotonic", now)),
        )
        store.pop(oldest, None)
    return store


def _finite_position(value: tuple[float, float, float]) -> np.ndarray:
    position = np.asarray(value, dtype=np.float64)
    if position.shape != (3,) or not np.all(np.isfinite(position)):
        raise ValueError("Position must contain three finite dataset coordinates.")
    return position


def _duplicate_candidates(
    connection: sqlite3.Connection,
    position: np.ndarray,
    *,
    radius_m: float,
    observation_id: str | None,
    class_name: str,
    exclude_feature_id: str | None = None,
) -> list[dict[str, Any]]:
    x, y, z = (float(value) for value in position)
    rows = connection.execute(
        """
        SELECT id,point_x,point_y,point_z,properties_json FROM features
        WHERE deleted=0 AND point_x BETWEEN ? AND ? AND point_y BETWEEN ? AND ?
        ORDER BY ((point_x-?)*(point_x-?))+((point_y-?)*(point_y-?)), id
        LIMIT 100
        """,
        (x - radius_m, x + radius_m, y - radius_m, y + radius_m, x, x, y, y),
    ).fetchall()
    has_provenance = (
        connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='feature_provenance'"
        ).fetchone()
        is not None
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        if exclude_feature_id is not None and str(row["id"]) == exclude_feature_id:
            continue
        try:
            properties = json.loads(str(row["properties_json"]))
        except (TypeError, json.JSONDecodeError):
            properties = {}
        if not isinstance(properties, dict):
            properties = {}
        existing_classes = {
            str(value).strip().upper()
            for key, value in properties.items()
            if _normalized_field_name(str(key)) in _CLASS_FIELD_ALIASES
            and value not in (None, "")
        }
        if existing_classes and class_name.upper() not in existing_classes:
            continue
        xy_distance = math.hypot(float(row["point_x"]) - x, float(row["point_y"]) - y)
        if xy_distance > radius_m:
            continue
        row_z = row["point_z"]
        z_difference = None if row_z is None else abs(float(row_z) - z)
        if z_difference is not None and z_difference > 2.0:
            continue
        same_observation = False
        provenance_row = (
            connection.execute(
                "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                (str(row["id"]),),
            ).fetchone()
            if has_provenance
            else None
        )
        if provenance_row is not None and observation_id:
            try:
                provenance = json.loads(str(provenance_row[0]))
                same_observation = observation_id in set(
                    provenance.get("manual_observation_ids") or []
                )
            except (AttributeError, TypeError, json.JSONDecodeError):
                same_observation = False
        exact = same_observation or (
            xy_distance <= 0.05 and (z_difference is None or z_difference <= 0.10)
        )
        results.append(
            {
                "feature_id": str(row["id"]),
                "xy_distance_m": xy_distance,
                "z_difference_m": z_difference,
                "match": "exact" if exact else "near",
                "reason_codes": (
                    ["SAME_MANUAL_OBSERVATION"]
                    if same_observation
                    else ["DUPLICATE_NEARBY"]
                ),
            }
        )
    return results


_CLASS_FIELD_ALIASES = {
    "CLASS",
    "CLASS_NM",
    "CLASSNAME",
    "CLASS_NAME",
    "OBJ_TYPE",
    "TYPE",
}


def _normalized_field_name(value: str) -> str:
    return value.strip().upper().replace(" ", "_")


def _commit_properties(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    template_id: str,
    proposal_patch: dict[str, Any],
    request_patch: dict[str, Any],
) -> dict[str, Any]:
    fields = list(manifest.get("fields") or [])
    field_map = {str(field["name"]): field for field in fields}
    properties = _blank_properties_with_next_id(connection, manifest)
    supplied = {**proposal_patch, **request_patch}
    unknown = {
        key
        for key in supplied
        if key not in field_map
        and _normalized_field_name(key) not in _CLASS_FIELD_ALIASES
    }
    if unknown:
        raise ValueError(f"Unknown SHP field(s): {', '.join(sorted(unknown))}")
    encoding = str(manifest.get("source_encoding", "utf-8"))
    for key, value in supplied.items():
        if key in field_map:
            properties[key] = _coerce_property(value, field_map[key], encoding=encoding)
    class_name = MANUAL_OBJECT_TEMPLATES[template_id].class_name
    for field_name, field in field_map.items():
        if _normalized_field_name(field_name) in _CLASS_FIELD_ALIASES:
            properties[field_name] = _coerce_property(
                class_name,
                field,
                encoding=encoding,
            )
    for field_name, field in field_map.items():
        value = properties.get(field_name)
        if bool(field.get("required")) and value in (None, ""):
            raise ValueError(f"{field_name} is required by the target layer schema.")
        domain = (
            field.get("domain") or field.get("allowed_values") or field.get("values")
        )
        if isinstance(domain, dict):
            domain = domain.get("values")
        if domain and value is not None and value not in domain:
            raise ValueError(f"{field_name} is outside the target layer domain.")
    return properties


def _public_proposal(
    result: PanoramaBboxPointResult,
    *,
    observation: ManualObservation,
    proposal_id: str,
    property_patch: dict[str, Any],
) -> GeometryProposal:
    return GeometryProposal.model_validate(
        {
            "proposal_id": proposal_id,
            "tool_id": "panorama_bbox_point_v1",
            "status": result.status,
            "coordinate_space": "dataset",
            "geometry": (
                None
                if result.position is None
                else {"type": "Point", "coordinates": result.position.tolist()}
            ),
            "property_patch": property_patch,
            "quality": {
                "score": result.score,
                "support_point_count": result.support_point_count,
                "depth_spread_m": result.depth_spread_m,
                "reprojection_error_px": result.reprojection_error_px,
            },
            "reason_codes": list(result.reason_codes),
            "evidence": {
                "frame_id": observation.frame_id,
                "observation_id": observation.observation_id,
                "seed_position": (
                    None
                    if result.seed_position is None
                    else result.seed_position.tolist()
                ),
            },
        }
    )


def _infer_proposal_from_frame(
    frame_task: dict[str, Any],
    catalog: dict[str, Any],
    reader: Any,
    observation: ManualObservation,
    *,
    max_range_m: float,
    yaw_offset_deg: float,
    pitch_offset_deg: float,
) -> PanoramaBboxPointResult:
    points, _rgb, origin = _sample_nearby_points(
        frame_task,
        catalog,
        reader,
        budget=MAX_PROPOSAL_POINT_BUDGET,
        radius=max_range_m,
    )
    forward, right, up = _panorama_axes(
        frame_task,
        yaw_offset_deg=yaw_offset_deg,
        pitch_offset_deg=pitch_offset_deg,
    )
    geometry = observation.geometry_2d
    return infer_panorama_bbox_point(
        points,
        origin,
        forward,
        right,
        up,
        u_intervals=tuple(geometry.u_intervals),
        v_min=geometry.v_min,
        v_max=geometry.v_max,
        image_width=geometry.image_width,
        image_height=geometry.image_height,
        max_range_m=max_range_m,
    )


def _commit_proposal_to_overlay(
    layer_dir: Any,
    manifest: dict[str, Any],
    stored: dict[str, Any],
    payload: ProposalCommitRequest,
    *,
    maximum_features: int,
    linked_task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proposal = GeometryProposal.model_validate(stored["proposal"])
    if proposal.status.value == "failed" or proposal.geometry is None:
        raise ValueError("A failed proposal cannot be committed.")
    template_id = str(stored["template_id"])
    layer_id = str(stored["target_layer_id"])
    template = MANUAL_OBJECT_TEMPLATES.get(template_id)
    if template is None or template.geometry_type != "Point":
        raise ValueError("Manual object template is not available.")
    if str(manifest.get("geometry_type", "")).casefold() != "point":
        raise ValueError("The target layer is incompatible with the Point template.")
    position = _finite_position(proposal.geometry.coordinates)
    now = utc_now()
    with _feature_db(layer_dir, write=True) as connection:
        _ensure_manual_object_tables(connection)
        previous = connection.execute(
            "SELECT * FROM edit_transactions WHERE idempotency_key=?",
            (payload.idempotency_key,),
        ).fetchone()
        if previous is not None:
            if (
                str(previous["action"]) != "manual_create"
                or str(previous["proposal_id"] or "") != proposal.proposal_id
                or (previous["task_id"] or None) != payload.task_id
            ):
                raise ValueError(
                    "The idempotency key belongs to another proposal or review task."
                )
            after = json.loads(previous["after_json"] or "null")
            task_intent_id = enqueue_task_resolution_intent(
                connection,
                source_key=f"manual-commit:{previous['id']}",
                task_id=payload.task_id,
                feature_id=str(previous["feature_id"]),
                transition_kind="resolve",
                resolution="manual_added",
                expected_status=None,
                allow_claim=False,
                actor=payload.created_by,
                now=now,
                session_id=(
                    str(linked_task["session_id"])
                    if linked_task is not None and linked_task.get("session_id")
                    else None
                ),
                dataset_id=str(stored["dataset_id"]),
                layer_id=layer_id,
            )
            return {
                "feature": after,
                "revision": int(previous["revision"]),
                "coordinate_space": "dataset",
                "idempotent_replay": True,
                "edit_transaction_id": str(previous["id"]),
                "duplicate_warnings": [],
                "_task_resolution_intent_id": task_intent_id,
            }
        if _active_count(connection) >= maximum_features:
            raise OverlayTooLarge(
                "SHP feature count exceeds the configured overlay limit."
            )
        duplicate_candidates = _duplicate_candidates(
            connection,
            position,
            radius_m=template.duplicate_radius_m,
            observation_id=str(stored.get("observation_id") or "") or None,
            class_name=template.class_name,
        )
        exact = [item for item in duplicate_candidates if item["match"] == "exact"]
        near = [item for item in duplicate_candidates if item["match"] == "near"]
        if exact:
            raise FileExistsError(
                f"Exact duplicate feature already exists: {exact[0]['feature_id']}"
            )
        if near and not payload.allow_near_duplicate:
            raise RuntimeError("near_duplicate")
        if near and not payload.override_reason:
            raise ValueError("A duplicate warning override requires a reason.")

        revision = _updated_revision(connection, payload.expected_revision)
        properties = _commit_properties(
            connection,
            manifest,
            template_id,
            proposal.property_patch,
            payload.properties,
        )
        feature_id, ordinal = _next_feature_identity(connection)
        geometry = proposal.geometry.model_dump(mode="json")
        x, y, z = _point_columns(geometry)
        after = {
            "type": "Feature",
            "id": feature_id,
            "geometry": geometry,
            "properties": properties,
        }
        connection.execute(
            """
            INSERT INTO features(
                id,ordinal,geometry_json,properties_json,
                point_x,point_y,point_z,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                feature_id,
                ordinal,
                _json_bytes(geometry).decode("utf-8"),
                _json_bytes(properties).decode("utf-8"),
                x,
                y,
                z,
                now,
            ),
        )
        transaction_id = f"edt_{uuid.uuid4().hex}"
        task_intent_id = enqueue_task_resolution_intent(
            connection,
            source_key=f"manual-commit:{transaction_id}",
            task_id=payload.task_id,
            feature_id=feature_id,
            transition_kind="resolve",
            resolution="manual_added",
            expected_status=None,
            allow_claim=False,
            actor=payload.created_by,
            now=now,
            session_id=(
                str(linked_task["session_id"])
                if linked_task is not None and linked_task.get("session_id")
                else None
            ),
            dataset_id=str(stored["dataset_id"]),
            layer_id=layer_id,
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)
        )
        connection.execute(
            """
            INSERT INTO audit(revision,action,feature_id,before_json,after_json,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                revision,
                "manual_create",
                feature_id,
                None,
                _json_bytes(after).decode("utf-8"),
                now,
            ),
        )
        provenance = FeatureProvenance.model_validate(
            {
                "layer_id": str(stored["target_layer_id"]),
                "feature_id": feature_id,
                "origin": "MANUAL",
                "source_run_id": (
                    linked_task.get("source_run_id") if linked_task is not None else None
                ),
                "source_frame_ids": [str(stored["frame_id"])],
                "source_detection_ids": (
                    [str(linked_task["source_detection_id"])]
                    if linked_task is not None
                    and linked_task.get("source_detection_id")
                    else []
                ),
                "manual_observation_ids": [str(stored["observation_id"])],
                "creation_tool": proposal.tool_id,
                "proposal_quality": proposal.quality.score,
                "review_status": "manual_added",
                "created_by": payload.created_by,
                "created_at": now,
                "updated_at": now,
            }
        )
        connection.execute(
            """
            INSERT INTO feature_provenance(feature_id,provenance_json,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(feature_id) DO UPDATE SET
                provenance_json=excluded.provenance_json,
                updated_at=excluded.updated_at
            """,
            (
                feature_id,
                _json_bytes(provenance.model_dump(mode="json")).decode("utf-8"),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO edit_transactions(
                id,idempotency_key,action,feature_id,task_id,revision,proposal_id,
                before_json,after_json,status,created_by,created_at,override_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                transaction_id,
                payload.idempotency_key,
                "manual_create",
                feature_id,
                payload.task_id,
                revision,
                proposal.proposal_id,
                None,
                _json_bytes(after).decode("utf-8"),
                "committed",
                payload.created_by,
                now,
                payload.override_reason,
            ),
        )
    return {
        "feature": after,
        "revision": revision,
        "coordinate_space": "dataset",
        "idempotent_replay": False,
        "edit_transaction_id": transaction_id,
        "duplicate_warnings": near,
        "provenance": provenance.model_dump(mode="json"),
        "_task_resolution_intent_id": task_intent_id,
    }


def _matching_committed_task_replay(
    layer_dir: Any,
    *,
    proposal_id: str,
    payload: ProposalCommitRequest,
    resolved_feature_ids: list[str],
) -> bool:
    """Allow a terminal task only for the exact committed idempotent retry."""

    with _feature_db(layer_dir) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='edit_transactions'"
        ).fetchone()
        if table is None:
            return False
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(edit_transactions)")
        }
        if "proposal_id" not in columns:
            return False
        row = connection.execute(
            """
            SELECT proposal_id,task_id,feature_id,action
            FROM edit_transactions WHERE idempotency_key=?
            """,
            (payload.idempotency_key,),
        ).fetchone()
    return bool(
        row is not None
        and str(row["action"]) == "manual_create"
        and str(row["proposal_id"] or "") == proposal_id
        and (row["task_id"] or None) == payload.task_id
        and str(row["feature_id"] or "") in set(resolved_feature_ids)
    )


@router.get("/manual-object-templates")
def manual_object_templates() -> dict[str, Any]:
    return {
        "items": [
            MANUAL_OBJECT_TEMPLATES[key].public_dict()
            for key in sorted(MANUAL_OBJECT_TEMPLATES)
        ]
    }


@router.post("/datasets/{dataset_id}/manual-objects/duplicate-preflight")
async def duplicate_preflight(
    dataset_id: str,
    payload: DuplicatePreflightRequest,
    request: Request,
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    try:
        position = _finite_position(payload.position)
        layer_dir = _layer_directory(request.app, dataset_id, payload.target_layer_id)
        template = MANUAL_OBJECT_TEMPLATES[payload.template_id]
        async with review_dataset_lock(request.app, dataset_id), _layer_lock(
            request.app, dataset_id, payload.target_layer_id
        ):
            with _feature_db(layer_dir) as connection:
                candidates = _duplicate_candidates(
                    connection,
                    position,
                    radius_m=template.duplicate_radius_m,
                    observation_id=payload.observation_id,
                    class_name=template.class_name,
                    exclude_feature_id=payload.exclude_feature_id,
                )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    exact = [item for item in candidates if item["match"] == "exact"]
    near = [item for item in candidates if item["match"] == "near"]
    return {
        "exact_duplicate": bool(exact),
        "blocked": bool(exact),
        "candidates": candidates,
        "warning_count": len(near),
        "radius_m": template.duplicate_radius_m,
    }


@router.post("/datasets/{dataset_id}/frames/{frame_id}/manual-observations")
async def create_manual_observation(
    dataset_id: str,
    frame_id: str,
    payload: ManualObservationCreate,
    request: Request,
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    if request.app.state.store.get_frame(dataset_id, frame_id) is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    template = MANUAL_OBJECT_TEMPLATES[payload.template_id]
    if template.tool_id != "panorama_bbox_point_v1":
        raise HTTPException(
            status_code=422,
            detail="This endpoint only accepts panorama bbox Point templates.",
        )
    try:
        layer_dir = _layer_directory(request.app, dataset_id, payload.target_layer_id)
        manifest = _read_manifest(layer_dir)
        if (
            manifest.get("dataset_id") != dataset_id
            or manifest.get("id") != payload.target_layer_id
        ):
            raise FileNotFoundError
        if str(manifest.get("geometry_type", "")).casefold() != "point":
            raise ValueError("Manual Point proposals require a Point overlay layer.")
        observation = ManualObservation(
            observation_id=f"mob_{uuid.uuid4().hex}",
            dataset_id=dataset_id,
            frame_id=frame_id,
            view_type="panorama",
            class_name=template.class_name,
            geometry_2d=payload.geometry_2d,
            created_by=payload.created_by,
        )
        async with review_dataset_lock(request.app, dataset_id), _layer_lock(
            request.app, dataset_id, payload.target_layer_id
        ):
            await asyncio.to_thread(
                _insert_observation,
                layer_dir,
                observation,
                layer_id=payload.target_layer_id,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "observation": observation.model_dump(mode="json"),
        "target_layer_id": payload.target_layer_id,
    }


@router.post("/datasets/{dataset_id}/frames/{frame_id}/manual-object-proposals")
async def create_manual_object_proposal(
    dataset_id: str,
    frame_id: str,
    payload: ManualObjectProposalCreate,
    request: Request,
) -> Any:
    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    if request.app.state.point_reader is None:
        raise HTTPException(
            status_code=503, detail="Point-cloud reader is unavailable."
        )
    template = MANUAL_OBJECT_TEMPLATES[payload.template_id]
    if template.tool_id != "panorama_bbox_point_v1":
        raise HTTPException(
            status_code=422,
            detail="This endpoint only accepts panorama bbox Point templates.",
        )
    try:
        layer_dir = _layer_directory(request.app, dataset_id, payload.target_layer_id)
        manifest = _read_manifest(layer_dir)
        if (
            manifest.get("dataset_id") != dataset_id
            or manifest.get("id") != payload.target_layer_id
        ):
            raise FileNotFoundError
        if str(manifest.get("geometry_type", "")).casefold() != "point":
            raise ValueError("Manual Point proposals require a Point overlay layer.")
        observation = await asyncio.to_thread(
            _get_observation, layer_dir, payload.observation_id
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if observation is None or observation.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Manual observation not found.")
    if observation.frame_id != frame_id:
        raise HTTPException(
            status_code=422,
            detail="Manual observation belongs to a different frame.",
        )
    if observation.class_name != template.class_name:
        raise HTTPException(
            status_code=422,
            detail="Manual observation and proposal templates do not match.",
        )

    catalog = request.app.state.catalogs.get(dataset_id)
    if catalog is None:
        if dataset.get("catalog_status") == "error":
            raise HTTPException(
                status_code=503,
                detail=dataset.get("catalog_error") or "Point-cloud indexing failed.",
            )
        schedule_catalog(request.app, dataset_id)
        return JSONResponse(
            {"status": "indexing", "detail": "Point-cloud index is being prepared."},
            status_code=202,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    yaw = (
        float(request.app.state.panorama_yaw_offset_deg)
        if payload.yaw_offset_deg is None
        else payload.yaw_offset_deg
    )
    pitch = (
        float(request.app.state.panorama_pitch_offset_deg)
        if payload.pitch_offset_deg is None
        else payload.pitch_offset_deg
    )
    try:
        async with request.app.state.pole_tool_semaphore:
            result = await _finish_inference_after_request_cancel(
                asyncio.to_thread(
                    _infer_proposal_from_frame,
                    frame.get("task") or {},
                    catalog,
                    request.app.state.point_reader,
                    observation,
                    max_range_m=payload.max_range_m,
                    yaw_offset_deg=yaw,
                    pitch_offset_deg=pitch,
                ),
                owner_tasks=request.app.state.pole_tool_owner_tasks,
                logger=request.app.state.logger,
                context=f"Manual bbox proposal for frame {frame_id}",
            )
    except (OSError, TypeError, ValueError) as exc:
        request.app.state.logger.warning(
            "Manual object proposal failed for frame %s: %s", frame_id, exc
        )
        raise HTTPException(
            status_code=503,
            detail="Point-cloud source records could not be read safely.",
        ) from exc

    properties = {**payload.property_patch, "CLASS_NM": template.class_name}
    proposal_id = f"prp_{uuid.uuid4().hex}"
    proposal = _public_proposal(
        result,
        observation=observation,
        proposal_id=proposal_id,
        property_patch=properties,
    )
    store = _proposal_store(request.app)
    store[proposal_id] = {
        "proposal": proposal.model_dump(mode="json"),
        "dataset_id": dataset_id,
        "frame_id": frame_id,
        "target_layer_id": payload.target_layer_id,
        "template_id": payload.template_id,
        "observation_id": observation.observation_id,
        "created_monotonic": time.monotonic(),
    }
    return {
        "proposal": proposal.model_dump(mode="json"),
        "target_layer_id": payload.target_layer_id,
        "expires_in_seconds": PROPOSAL_TTL_SECONDS,
    }


@router.post("/manual-object-proposals/{proposal_id}/commit")
async def commit_manual_object_proposal(
    proposal_id: str,
    payload: ProposalCommitRequest,
    request: Request,
) -> dict[str, Any]:
    stored = _proposal_store(request.app).get(proposal_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Manual object proposal not found.")
    dataset_id = str(stored["dataset_id"])
    layer_id = str(stored["target_layer_id"])
    require_ready_dataset(request, dataset_id)
    linked_task: dict[str, Any] | None = None
    if payload.task_id:
        linked_task = request.app.state.store.get_review_task(payload.task_id)
        if linked_task is None:
            raise HTTPException(status_code=404, detail="Review task not found.")
        session = request.app.state.store.get_review_session(
            str(linked_task["session_id"])
        )
        if (
            str(linked_task["dataset_id"]) != dataset_id
            or session is None
            or str(session["dataset_id"]) != dataset_id
            or (
                linked_task.get("target_layer_id") is not None
                and str(linked_task["target_layer_id"]) != layer_id
            )
            or (
                session.get("target_layer_ids")
                and layer_id not in set(session["target_layer_ids"])
            )
        ):
            raise HTTPException(
                status_code=422,
                detail="Review task is outside this proposal's dataset or layer scope.",
            )
        if str(session["status"]) != "active":
            raise HTTPException(
                status_code=409,
                detail="Manual object commits require an active review session.",
            )
        proposal_frame_id = str(stored["frame_id"])
        proposal_frame = request.app.state.store.get_frame(
            dataset_id, proposal_frame_id
        )
        if proposal_frame is None:
            raise HTTPException(
                status_code=422,
                detail="Manual object proposal frame is no longer available.",
            )
        task_frame_start = linked_task.get("frame_start")
        task_frame_end = linked_task.get("frame_end")
        if task_frame_start is not None or task_frame_end is not None:
            if (
                task_frame_start is None
                or task_frame_end is None
                or int(proposal_frame["ordinal"]) < int(task_frame_start)
                or int(proposal_frame["ordinal"]) > int(task_frame_end)
                or linked_task.get("track_id") is None
                or str(linked_task["track_id"]) != str(proposal_frame["track_id"])
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Review task frame range does not include this proposal frame.",
                )
        elif (
            linked_task.get("frame_id") is None
            or str(linked_task["frame_id"]) != proposal_frame_id
            or (
                linked_task.get("track_id") is not None
                and str(linked_task["track_id"]) != str(proposal_frame["track_id"])
            )
        ):
            raise HTTPException(
                status_code=422,
                detail="Review task frame does not match this proposal frame.",
            )
        if str(linked_task["status"]) not in {
            "in_progress",
            "manual_added",
        }:
            raise HTTPException(
                status_code=409,
                detail="Review task must be in progress before proposal commit.",
            )
        if (
            linked_task.get("claimed_by") is not None
            and str(linked_task["claimed_by"]) != payload.created_by
        ):
            raise HTTPException(
                status_code=409,
                detail="Review task is claimed by another operator.",
            )
    if bool(stored.get("commit_in_progress")):
        raise HTTPException(
            status_code=409,
            detail="Manual object proposal commit is already in progress.",
        )
    # This async endpoint has not awaited yet, so the claim is serialized on
    # the event loop with the async DELETE endpoint below.
    stored["commit_in_progress"] = True
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        if manifest.get("dataset_id") != dataset_id or manifest.get("id") != layer_id:
            raise FileNotFoundError("Overlay layer not found.")
        if (
            linked_task is not None
            and str(linked_task["status"]) == "manual_added"
            and not _matching_committed_task_replay(
                layer_dir,
                proposal_id=proposal_id,
                payload=payload,
                resolved_feature_ids=list(
                    linked_task.get("resolved_feature_ids") or []
                ),
            )
        ):
            raise RuntimeError("task_already_resolved")
        async with review_dataset_lock(request.app, dataset_id), _layer_lock(
            request.app, dataset_id, layer_id
        ):
            result = await _finish_inference_after_request_cancel(
                asyncio.to_thread(
                    _commit_proposal_to_overlay,
                    layer_dir,
                    manifest,
                    stored,
                    payload,
                    maximum_features=request.app.state.config.max_overlay_features,
                    linked_task=linked_task,
                ),
                owner_tasks=request.app.state.pole_tool_owner_tasks,
                logger=request.app.state.logger,
                context=f"Manual proposal commit {proposal_id}",
            )
    except RuntimeError as exc:
        detail = str(exc)
        if detail.startswith("revision:"):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Overlay was edited by another request.",
                    "current_revision": int(detail.split(":", 1)[1]),
                },
            ) from exc
        if detail == "near_duplicate":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A nearby feature requires operator confirmation.",
                    "reason_code": "DUPLICATE_NEARBY",
                },
            ) from exc
        if detail == "task_already_resolved":
            raise HTTPException(
                status_code=409,
                detail="The review task was resolved by another feature edit.",
            ) from exc
        raise
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "reason_code": "EXACT_DUPLICATE"},
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OverlayTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        current = _proposal_store(request.app).get(proposal_id)
        if current is stored:
            current["commit_in_progress"] = False

    # The outbox row was committed with the authoritative feature/provenance.
    intent_id = result.pop("_task_resolution_intent_id", None)
    result["task_resolution_pending"] = not reconcile_task_resolution_intent(
        request.app, dataset_id, layer_id, intent_id
    )
    return result


@router.get("/manual-object-proposals/{proposal_id}")
async def get_manual_object_proposal(
    proposal_id: str, request: Request
) -> dict[str, Any]:
    value = _proposal_store(request.app).get(proposal_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Manual object proposal not found.")
    return {
        "proposal": value["proposal"],
        "target_layer_id": value["target_layer_id"],
        "expires_in_seconds": max(
            0,
            math.ceil(
                PROPOSAL_TTL_SECONDS
                - (time.monotonic() - float(value["created_monotonic"]))
            ),
        ),
    }


@router.delete("/manual-object-proposals/{proposal_id}")
async def delete_manual_object_proposal(
    proposal_id: str, request: Request
) -> dict[str, Any]:
    store = _proposal_store(request.app)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Manual object proposal not found.")
    if bool(proposal.get("commit_in_progress")):
        raise HTTPException(
            status_code=409,
            detail="A proposal being committed cannot be cancelled.",
        )
    store.pop(proposal_id, None)
    return {"proposal_id": proposal_id, "deleted": True}


__all__ = [
    "ManualObjectProposalCreate",
    "ManualObservationCreate",
    "_ensure_manual_object_tables",
    "_infer_proposal_from_frame",
    "router",
]
