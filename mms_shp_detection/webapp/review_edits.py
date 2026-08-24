"""Revision-safe feature history, undo, and redo for review workflows."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .datasets import require_ready_dataset, utc_now
from .overlays import (
    _decode_feature,
    _feature_db,
    _json_bytes,
    _layer_directory,
    _layer_lock,
    _point_columns,
    _read_manifest,
    _updated_revision,
)
from .task_resolution_outbox import (
    enqueue_task_resolution_intent,
    reconcile_task_resolution_intent,
    review_dataset_lock,
    review_session_lock,
)

router = APIRouter(prefix="/api", tags=["review-history"])

_EDIT_ACTIONS = ("create", "manual_create", "update", "delete")


class HistoryMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)
    actor: str = Field(default="operator-local", min_length=1, max_length=160)


def _ensure_history_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS edit_history_state (
            audit_id INTEGER PRIMARY KEY,
            undone INTEGER NOT NULL DEFAULT 0 CHECK(undone IN (0,1)),
            last_operation_revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS edit_history_requests (
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK(operation IN ('undo','redo')),
            actor TEXT,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    request_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(edit_history_requests)")
    }
    if "actor" not in request_columns:
        connection.execute("ALTER TABLE edit_history_requests ADD COLUMN actor TEXT")


def _row_feature(
    connection: sqlite3.Connection, feature_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM features WHERE id=?", (feature_id,)
    ).fetchone()
    if row is None or int(row["deleted"]):
        return None
    return _decode_feature(row)


def _apply_feature_state(
    connection: sqlite3.Connection,
    feature_id: str,
    state: dict[str, Any] | None,
    *,
    now: str,
) -> None:
    row = connection.execute(
        "SELECT id FROM features WHERE id=?", (feature_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError("Feature history target no longer exists.")
    if state is None:
        connection.execute(
            "UPDATE features SET deleted=1,updated_at=? WHERE id=?",
            (now, feature_id),
        )
        return
    geometry = state.get("geometry")
    properties = state.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("Feature history properties are invalid.")
    x, y, z = _point_columns(geometry)
    connection.execute(
        """
        UPDATE features SET geometry_json=?,properties_json=?,point_x=?,point_y=?,
            point_z=?,deleted=0,updated_at=? WHERE id=?
        """,
        (
            None if geometry is None else _json_bytes(geometry).decode("utf-8"),
            _json_bytes(properties).decode("utf-8"),
            x,
            y,
            z,
            now,
            feature_id,
        ),
    )


def _json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError("Feature audit state is invalid.")
    return parsed


def _linked_task(connection: sqlite3.Connection, revision: int) -> str | None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edit_transactions'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """
        SELECT task_id FROM edit_transactions
        WHERE revision=? AND task_id IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (revision,),
    ).fetchone()
    return None if row is None else str(row[0])


def _history_preflight(
    layer_dir: Any,
    payload: HistoryMutationRequest,
    operation: Literal["undo", "redo"],
) -> dict[str, Any]:
    """Read the exact history target before taking its parent session fence."""

    with _feature_db(layer_dir, write=True) as connection:
        _ensure_history_tables(connection)
        replay = connection.execute(
            """
            SELECT operation,actor,response_json FROM edit_history_requests
            WHERE idempotency_key=?
            """,
            (payload.idempotency_key,),
        ).fetchone()
        if replay is not None:
            if str(replay["operation"]) != operation:
                raise ValueError("The idempotency key belongs to another operation.")
            if replay["actor"] is not None and str(replay["actor"]) != payload.actor:
                raise PermissionError(
                    "The idempotency key belongs to another operator."
                )
            response = json.loads(str(replay["response_json"]))
            return {
                "linked_task_id": response.get("linked_task_id"),
                "feature_id": str(response["feature_id"]),
                "target_action": str(response["target_action"]),
                "idempotent_replay": True,
            }

        placeholders = ",".join("?" for _ in _EDIT_ACTIONS)
        if operation == "undo":
            audit = connection.execute(
                f"""
                SELECT a.* FROM audit a
                LEFT JOIN edit_history_state state ON state.audit_id=a.id
                WHERE a.action IN ({placeholders}) AND COALESCE(state.undone,0)=0
                ORDER BY a.revision DESC,a.id DESC LIMIT 1
                """,
                _EDIT_ACTIONS,
            ).fetchone()
        else:
            state = connection.execute(
                """
                SELECT audit_id FROM edit_history_state
                WHERE undone=1 ORDER BY last_operation_revision DESC,audit_id DESC
                LIMIT 1
                """
            ).fetchone()
            audit = (
                None
                if state is None
                else connection.execute(
                    "SELECT * FROM audit WHERE id=?", (int(state["audit_id"]),)
                ).fetchone()
            )
        if audit is None:
            raise LookupError(f"Nothing is available to {operation}.")
        return {
            "linked_task_id": _linked_task(connection, int(audit["revision"])),
            "feature_id": str(audit["feature_id"]),
            "target_action": str(audit["action"]),
            "idempotent_replay": False,
        }


def _validate_history_task_fence(
    app: Any,
    snapshot: dict[str, Any],
    payload: HistoryMutationRequest,
    operation: Literal["undo", "redo"],
    *,
    dataset_id: str,
    layer_id: str,
) -> str | None:
    task_id = snapshot.get("linked_task_id")
    if task_id is None:
        return None
    task = app.state.store.get_review_task(str(task_id))
    if task is None:
        raise PermissionError("The linked review task no longer exists.")
    session = app.state.store.get_review_session(str(task["session_id"]))
    if session is None:
        raise PermissionError("The linked review session no longer exists.")
    if (
        str(task["dataset_id"]) != dataset_id
        or str(session["dataset_id"]) != dataset_id
        or task.get("target_layer_id") != layer_id
        or (
            session.get("target_layer_ids")
            and layer_id not in {str(value) for value in session["target_layer_ids"]}
        )
    ):
        raise PermissionError("The linked review task is outside this overlay scope.")
    if str(session["status"]) != "active":
        raise PermissionError(
            "Feature history requires an active linked review session."
        )
    if task.get("claimed_by") not in {None, payload.actor}:
        raise PermissionError("The linked review task belongs to another operator.")

    expected = (
        "corrected" if snapshot["target_action"] == "update" else "manual_added"
    )
    feature_id = str(snapshot["feature_id"])
    terminal_matches = (
        str(task["status"]) == expected
        and feature_id
        in {str(value) for value in task.get("resolved_feature_ids", [])}
    )
    replay = bool(snapshot.get("idempotent_replay"))
    if operation == "undo":
        valid_state = str(task["status"]) == "todo" if replay else terminal_matches
    else:
        valid_state = terminal_matches if replay else str(task["status"]) == "todo"
    if not valid_state:
        raise PermissionError("The linked review task state conflicts with history.")
    return str(session["id"])


def _enqueue_history_task_intent(
    connection: sqlite3.Connection,
    response: dict[str, Any],
    payload: HistoryMutationRequest,
    operation: Literal["undo", "redo"],
    *,
    session_id: str | None,
    dataset_id: str | None,
    layer_id: str | None,
    now: str,
) -> str | None:
    task_id = response.get("linked_task_id")
    feature_id = str(response["feature_id"])
    target_resolution = (
        "corrected" if response.get("target_action") == "update" else "manual_added"
    )
    if task_id is not None and session_id is None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_resolution_outbox'"
        ).fetchone()
        if table is not None:
            snapshot = connection.execute(
                """
                SELECT session_id FROM task_resolution_outbox
                WHERE task_id=? AND feature_id=? AND session_id IS NOT NULL
                ORDER BY created_at,id LIMIT 1
                """,
                (str(task_id), feature_id),
            ).fetchone()
            if snapshot is not None:
                session_id = str(snapshot["session_id"])
    return enqueue_task_resolution_intent(
        connection,
        source_key=f"feature-history:{payload.idempotency_key}",
        task_id=None if task_id is None else str(task_id),
        feature_id=feature_id,
        transition_kind="reopen" if operation == "undo" else "resolve",
        resolution=None if operation == "undo" else target_resolution,
        expected_status=target_resolution if operation == "undo" else None,
        allow_claim=operation == "redo",
        actor=payload.actor,
        now=now,
        session_id=session_id,
        dataset_id=dataset_id,
        layer_id=layer_id,
    )


def _mutate_history(
    layer_dir: Any,
    payload: HistoryMutationRequest,
    operation: Literal["undo", "redo"],
    *,
    session_id: str | None = None,
    dataset_id: str | None = None,
    layer_id: str | None = None,
    precommit_fence: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    with _feature_db(layer_dir, write=True) as connection:
        _ensure_history_tables(connection)
        replay = connection.execute(
            """
            SELECT operation,actor,response_json FROM edit_history_requests
            WHERE idempotency_key=?
            """,
            (payload.idempotency_key,),
        ).fetchone()
        if replay is not None:
            if str(replay["operation"]) != operation:
                raise ValueError("The idempotency key belongs to another operation.")
            if replay["actor"] is not None and str(replay["actor"]) != payload.actor:
                raise PermissionError(
                    "The idempotency key belongs to another operator."
                )
            response = json.loads(str(replay["response_json"]))
            response["idempotent_replay"] = True
            if precommit_fence is not None:
                precommit_fence(
                    {
                        "linked_task_id": response.get("linked_task_id"),
                        "feature_id": str(response["feature_id"]),
                        "target_action": str(response["target_action"]),
                        "idempotent_replay": True,
                    }
                )
            response["_task_transition_intent_id"] = _enqueue_history_task_intent(
                connection,
                response,
                payload,
                operation,
                session_id=session_id,
                dataset_id=dataset_id,
                layer_id=layer_id,
                now=now,
            )
            return response

        placeholders = ",".join("?" for _ in _EDIT_ACTIONS)
        if operation == "undo":
            audit = connection.execute(
                f"""
                SELECT a.* FROM audit a
                LEFT JOIN edit_history_state state ON state.audit_id=a.id
                WHERE a.action IN ({placeholders}) AND COALESCE(state.undone,0)=0
                ORDER BY a.revision DESC,a.id DESC LIMIT 1
                """,
                _EDIT_ACTIONS,
            ).fetchone()
        else:
            state = connection.execute(
                """
                SELECT audit_id,last_operation_revision FROM edit_history_state
                WHERE undone=1 ORDER BY last_operation_revision DESC,audit_id DESC LIMIT 1
                """
            ).fetchone()
            audit = (
                None
                if state is None
                else connection.execute(
                    "SELECT * FROM audit WHERE id=?", (int(state["audit_id"]),)
                ).fetchone()
            )
            if audit is not None:
                newest_original = connection.execute(
                    f"SELECT MAX(revision) FROM audit WHERE action IN ({placeholders})",
                    _EDIT_ACTIONS,
                ).fetchone()[0]
                if newest_original is not None and int(newest_original) > int(
                    state["last_operation_revision"]
                ):
                    raise RuntimeError("redo_invalidated")
        if audit is None:
            raise LookupError(f"Nothing is available to {operation}.")

        owner = (
            connection.execute(
                """
            SELECT created_by FROM edit_transactions
            WHERE revision=? ORDER BY created_at DESC LIMIT 1
            """,
                (int(audit["revision"]),),
            ).fetchone()
            if connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='edit_transactions'"
            ).fetchone()
            is not None
            else None
        )
        if owner is not None and str(owner["created_by"]) != payload.actor:
            raise PermissionError("The latest review edit belongs to another operator.")

        linked_task_id = _linked_task(connection, int(audit["revision"]))
        fence_snapshot = {
            "linked_task_id": linked_task_id,
            "feature_id": str(audit["feature_id"]),
            "target_action": str(audit["action"]),
            "idempotent_replay": False,
        }
        if precommit_fence is not None:
            precommit_fence(fence_snapshot)
        revision = _updated_revision(connection, payload.expected_revision)
        before = _row_feature(connection, str(audit["feature_id"]))
        target = _json_object(
            audit["before_json"] if operation == "undo" else audit["after_json"]
        )
        _apply_feature_state(
            connection,
            str(audit["feature_id"]),
            target,
            now=now,
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
                f"{operation}:{audit['action']}",
                str(audit["feature_id"]),
                None if before is None else _json_bytes(before).decode("utf-8"),
                None if target is None else _json_bytes(target).decode("utf-8"),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO edit_history_state(audit_id,undone,last_operation_revision,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(audit_id) DO UPDATE SET
                undone=excluded.undone,
                last_operation_revision=excluded.last_operation_revision,
                updated_at=excluded.updated_at
            """,
            (int(audit["id"]), 1 if operation == "undo" else 0, revision, now),
        )
        response = {
            "operation": operation,
            "revision": revision,
            "target_revision": int(audit["revision"]),
            "target_action": str(audit["action"]),
            "feature_id": str(audit["feature_id"]),
            "feature": target,
            "deleted": target is None,
            "linked_task_id": linked_task_id,
            "idempotent_replay": False,
        }
        response["_task_transition_intent_id"] = _enqueue_history_task_intent(
            connection,
            response,
            payload,
            operation,
            session_id=session_id,
            dataset_id=dataset_id,
            layer_id=layer_id,
            now=now,
        )
        connection.execute(
            """
            INSERT INTO edit_history_requests(
                idempotency_key,operation,actor,response_json,created_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                payload.idempotency_key,
                operation,
                payload.actor,
                _json_bytes(response).decode("utf-8"),
                now,
            ),
        )
        if precommit_fence is not None:
            # A direct registry change that bypasses the process lock is still
            # detected before SQLite commits the feature mutation.
            precommit_fence(fence_snapshot)
    return response


def _reconcile_linked_task(
    request: Request,
    result: dict[str, Any],
    *,
    dataset_id: str,
    layer_id: str,
) -> None:
    intent_id = result.pop("_task_transition_intent_id", None)
    result["task_transition_pending"] = not reconcile_task_resolution_intent(
        request.app, dataset_id, layer_id, intent_id
    )


def _history_mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeError) and str(exc).startswith("revision:"):
        return HTTPException(
            status_code=409,
            detail={
                "message": "Overlay was edited by another request.",
                "current_revision": int(str(exc).split(":", 1)[1]),
            },
        )
    if isinstance(exc, RuntimeError) and str(exc) == "redo_invalidated":
        return HTTPException(
            status_code=409,
            detail="Redo history was invalidated by a newer feature edit.",
        )
    if isinstance(exc, LookupError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/datasets/{dataset_id}/overlays/{layer_id}/edit-history")
def list_edit_history(
    dataset_id: str,
    layer_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        _read_manifest(layer_dir)
        with _feature_db(layer_dir, write=True) as connection:
            _ensure_history_tables(connection)
            rows = connection.execute(
                """
                SELECT a.id,a.revision,a.action,a.feature_id,a.created_at,
                       COALESCE(state.undone,0) AS undone
                FROM audit a
                LEFT JOIN edit_history_state state ON state.audit_id=a.id
                ORDER BY a.revision DESC,a.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise _history_mutation_error(exc) from exc
    return {
        "items": [
            {
                "audit_id": int(row["id"]),
                "revision": int(row["revision"]),
                "action": str(row["action"]),
                "feature_id": str(row["feature_id"]),
                "created_at": str(row["created_at"]),
                "undone": bool(row["undone"]),
            }
            for row in rows
        ]
    }


async def _history_endpoint(
    dataset_id: str,
    layer_id: str,
    payload: HistoryMutationRequest,
    request: Request,
    operation: Literal["undo", "redo"],
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        if manifest.get("dataset_id") != dataset_id:
            raise FileNotFoundError("Overlay layer not found.")
        async with review_dataset_lock(request.app, dataset_id), _layer_lock(
            request.app, dataset_id, layer_id
        ):
            snapshot = await asyncio.to_thread(
                _history_preflight, layer_dir, payload, operation
            )
            session_id = _validate_history_task_fence(
                request.app,
                snapshot,
                payload,
                operation,
                dataset_id=dataset_id,
                layer_id=layer_id,
            )

            async def mutate() -> dict[str, Any]:
                locked_snapshot = await asyncio.to_thread(
                    _history_preflight, layer_dir, payload, operation
                )
                if locked_snapshot != snapshot:
                    raise PermissionError(
                        "Feature history changed concurrently; reload and retry."
                    )

                def fence(actual: dict[str, Any]) -> None:
                    if actual != snapshot:
                        raise PermissionError(
                            "Feature history changed concurrently; reload and retry."
                        )
                    _validate_history_task_fence(
                        request.app,
                        actual,
                        payload,
                        operation,
                        dataset_id=dataset_id,
                        layer_id=layer_id,
                    )

                return await asyncio.to_thread(
                    _mutate_history,
                    layer_dir,
                    payload,
                    operation,
                    session_id=session_id,
                    dataset_id=dataset_id,
                    layer_id=layer_id,
                    precommit_fence=fence,
                )

            if session_id is None:
                result = await mutate()
            else:
                async with review_session_lock(request.app, session_id):
                    # Revalidate only after taking the same fence used by
                    # session completion/pause transitions.
                    _validate_history_task_fence(
                        request.app,
                        snapshot,
                        payload,
                        operation,
                        dataset_id=dataset_id,
                        layer_id=layer_id,
                    )
                    result = await mutate()
    except (
        FileNotFoundError,
        LookupError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise _history_mutation_error(exc) from exc
    _reconcile_linked_task(
        request, result, dataset_id=dataset_id, layer_id=layer_id
    )
    return result


@router.post("/datasets/{dataset_id}/overlays/{layer_id}/undo")
async def undo_overlay_edit(
    dataset_id: str,
    layer_id: str,
    payload: HistoryMutationRequest,
    request: Request,
) -> dict[str, Any]:
    return await _history_endpoint(dataset_id, layer_id, payload, request, "undo")


@router.post("/datasets/{dataset_id}/overlays/{layer_id}/redo")
async def redo_overlay_edit(
    dataset_id: str,
    layer_id: str,
    payload: HistoryMutationRequest,
    request: Request,
) -> dict[str, Any]:
    return await _history_endpoint(dataset_id, layer_id, payload, request, "redo")


__all__ = ["HistoryMutationRequest", "_mutate_history", "router"]
