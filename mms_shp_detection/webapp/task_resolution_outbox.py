"""Durable feature-DB outbox for linked review-task transitions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from .datasets import utc_now
from .security import opaque_id

TransitionKind = Literal["resolve", "reopen"]

_TERMINAL_STATUSES = {
    "confirmed",
    "corrected",
    "manual_added",
    "false_positive",
    "skipped",
    "field_survey",
}
_MAX_SESSION_LAYERS = 1_000
_MAX_STARTUP_DATASETS = 1_000
_MAX_STARTUP_LAYERS = 2_000


def _existing_overlay_root(app: Any, dataset_id: str) -> Path | None:
    """Resolve existing overlay storage without creating it on read boundaries."""

    parent = app.state.config.state_dir / "overlays"
    if parent.is_symlink():
        raise ValueError("Overlay storage cannot be a symbolic link.")
    if not parent.exists():
        return None
    if not parent.is_dir():
        raise NotADirectoryError("Overlay storage is not a directory.")
    resolved_parent = parent.resolve(strict=True)
    directory = parent / opaque_id("ds", dataset_id, length=32)
    if directory.is_symlink():
        raise ValueError("Overlay storage cannot be a symbolic link.")
    if not directory.exists():
        return None
    resolved = directory.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != resolved_parent:
        raise ValueError("Overlay storage is outside its configured root.")
    return resolved


def review_session_lock(app: Any, session_id: str) -> asyncio.Lock:
    """Return the process-local fence shared by session and feature mutations."""

    return app.state.review_session_locks.setdefault(session_id, asyncio.Lock())


def review_dataset_lock(app: Any, dataset_id: str) -> asyncio.Lock:
    """Fence dataset removal against review scope and feature commits."""

    return app.state.review_dataset_locks.setdefault(dataset_id, asyncio.Lock())


def ensure_task_resolution_outbox(connection: sqlite3.Connection) -> None:
    """Create the portable outbox without committing the caller's transaction."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_resolution_outbox (
            id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            session_id TEXT,
            dataset_id TEXT,
            layer_id TEXT,
            task_id TEXT NOT NULL,
            feature_id TEXT NOT NULL,
            transition_kind TEXT NOT NULL CHECK(transition_kind IN ('resolve','reopen')),
            resolution TEXT,
            expected_status TEXT,
            allow_claim INTEGER NOT NULL DEFAULT 0 CHECK(allow_claim IN (0,1)),
            actor TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','reconciled','error')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reconciled_at TEXT,
            UNIQUE(source_key,task_id,transition_kind)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_resolution_outbox_status
        ON task_resolution_outbox(status,updated_at,id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_resolution_outbox_session
        ON task_resolution_outbox(session_id,status,updated_at,id)
        """
    )


def _intent_id(source_key: str, task_id: str, transition_kind: str) -> str:
    digest = hashlib.sha256(
        f"{source_key}\0{task_id}\0{transition_kind}".encode()
    ).hexdigest()
    return f"tri_{digest[:32]}"


def enqueue_task_resolution_intent(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    task_id: str | None,
    feature_id: str,
    transition_kind: TransitionKind,
    resolution: str | None,
    expected_status: str | None,
    allow_claim: bool,
    actor: str,
    now: str,
    session_id: str | None = None,
    dataset_id: str | None = None,
    layer_id: str | None = None,
) -> str | None:
    """Persist an idempotent intent inside the authoritative feature transaction."""

    if task_id is None:
        return None
    if transition_kind == "resolve":
        if resolution not in _TERMINAL_STATUSES or expected_status is not None:
            raise ValueError("A resolve intent requires one terminal resolution.")
    elif resolution is not None or expected_status not in _TERMINAL_STATUSES:
        raise ValueError("A reopen intent requires its expected terminal status.")
    intent_id = _intent_id(source_key, task_id, transition_kind)
    ensure_task_resolution_outbox(connection)
    connection.execute(
        """
        INSERT INTO task_resolution_outbox(
            id,source_key,session_id,dataset_id,layer_id,task_id,feature_id,
            transition_kind,resolution,expected_status,allow_claim,actor,status,
            attempt_count,last_error_code,created_at,updated_at,reconciled_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending',0,NULL,?,?,NULL)
        ON CONFLICT(source_key,task_id,transition_kind) DO NOTHING
        """,
        (
            intent_id,
            source_key,
            session_id,
            dataset_id,
            layer_id,
            task_id,
            feature_id,
            transition_kind,
            resolution,
            expected_status,
            int(allow_claim),
            actor,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM task_resolution_outbox WHERE id=?", (intent_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("Task transition intent was not persisted.")
    expected = {
        "task_id": task_id,
        "feature_id": feature_id,
        "transition_kind": transition_kind,
        "resolution": resolution,
        "expected_status": expected_status,
        "allow_claim": int(allow_claim),
        "actor": actor,
        "session_id": session_id,
        "dataset_id": dataset_id,
        "layer_id": layer_id,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise ValueError("Task transition source key belongs to another intent.")
    return intent_id


def _desired_state(intent: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    if str(intent["transition_kind"]) == "reopen":
        return str(task["status"]) == "todo"
    return (
        str(task["status"]) == str(intent["resolution"])
        and str(intent["feature_id"])
        in {str(value) for value in task.get("resolved_feature_ids", [])}
    )


def review_resolution_feature_error(
    app: Any,
    task: Mapping[str, Any],
    resolution: str,
    feature_ids: list[str],
) -> str | None:
    """Validate authoritative feature/edit linkage for feature resolutions."""

    if resolution not in {"manual_added", "corrected"}:
        # confirmed explicitly remains valid without a feature: it can confirm
        # a source artifact that was already represented before this session.
        return None
    if not feature_ids:
        return "RESOLUTION_FEATURE_REQUIRED"
    layer_id = task.get("target_layer_id")
    if layer_id is None:
        return "RESOLUTION_LAYER_REQUIRED"
    if len(feature_ids) != len(set(feature_ids)):
        return "RESOLUTION_FEATURE_LINK_INVALID"

    from .overlays import _feature_db, _layer_directory, _read_manifest

    try:
        layer_dir = _layer_directory(
            app, str(task["dataset_id"]), str(layer_id)
        )
        manifest = _read_manifest(layer_dir)
        if (
            str(manifest.get("dataset_id")) != str(task["dataset_id"])
            or str(manifest.get("id")) != str(layer_id)
        ):
            return "RESOLUTION_FEATURE_LINK_INVALID"
        with _feature_db(layer_dir) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name IN ('feature_provenance','edit_transactions')"
                ).fetchall()
            }
            if tables != {"feature_provenance", "edit_transactions"}:
                return "RESOLUTION_FEATURE_LINK_INVALID"
            allowed_actions = (
                ("manual_create", "review_create")
                if resolution == "manual_added"
                else ("review_update",)
            )
            placeholders = ",".join("?" for _ in allowed_actions)
            for feature_id in feature_ids:
                active = connection.execute(
                    "SELECT 1 FROM features WHERE id=? AND deleted=0",
                    (feature_id,),
                ).fetchone()
                provenance_row = connection.execute(
                    "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                    (feature_id,),
                ).fetchone()
                edit = connection.execute(
                    f"""
                    SELECT 1 FROM edit_transactions
                    WHERE task_id=? AND feature_id=? AND status='committed'
                      AND action IN ({placeholders})
                    LIMIT 1
                    """,
                    (str(task["id"]), feature_id, *allowed_actions),
                ).fetchone()
                if active is None or provenance_row is None or edit is None:
                    return "RESOLUTION_FEATURE_LINK_INVALID"
                provenance = json.loads(str(provenance_row["provenance_json"]))
                if (
                    not isinstance(provenance, dict)
                    or str(provenance.get("review_status")) != resolution
                    or str(provenance.get("feature_id")) != feature_id
                    or str(provenance.get("layer_id")) != str(layer_id)
                ):
                    return "RESOLUTION_FEATURE_LINK_INVALID"
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return "RESOLUTION_FEATURE_LINK_INVALID"
    return None


def _scope_error(
    intent: Mapping[str, Any],
    task: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    dataset_id: str,
    layer_id: str,
) -> str | None:
    if intent.get("dataset_id") not in {None, dataset_id}:
        return "DATASET_SCOPE_MISMATCH"
    if intent.get("layer_id") not in {None, layer_id}:
        return "LAYER_SCOPE_MISMATCH"
    if intent.get("session_id") not in {None, str(task["session_id"])}:
        return "SESSION_SCOPE_MISMATCH"
    if str(task["dataset_id"]) != dataset_id:
        return "DATASET_SCOPE_MISMATCH"
    if str(session["dataset_id"]) != dataset_id:
        return "DATASET_SCOPE_MISMATCH"
    if task.get("target_layer_id") not in {None, layer_id}:
        return "LAYER_SCOPE_MISMATCH"
    targets = {str(value) for value in session.get("target_layer_ids", [])}
    if targets and layer_id not in targets:
        return "LAYER_SCOPE_MISMATCH"
    return None


def _refresh_task(store: Any, task_id: str) -> dict[str, Any] | None:
    task = store.get_review_task(task_id)
    return task if isinstance(task, dict) else None


def _attempt_intent(
    app: Any,
    intent: Mapping[str, Any],
    *,
    dataset_id: str,
    layer_id: str,
    feature_active: bool,
) -> tuple[str, str | None, str | None]:
    """Return ``(outbox_status, error_code, discovered_session_id)``."""

    store = app.state.store
    task_id = str(intent["task_id"])
    task = _refresh_task(store, task_id)
    if task is None:
        return "error", "TASK_NOT_FOUND", None
    session_id = str(task["session_id"])
    session = store.get_review_session(session_id)
    if session is None:
        return "error", "SESSION_NOT_FOUND", session_id
    scope_error = _scope_error(
        intent, task, session, dataset_id=dataset_id, layer_id=layer_id
    )
    if scope_error is not None:
        return "error", scope_error, session_id
    if _desired_state(intent, task):
        return "reconciled", None, session_id
    if str(intent["transition_kind"]) == "resolve" and not feature_active:
        return "error", "FEATURE_NOT_ACTIVE", session_id
    if str(intent["transition_kind"]) == "resolve":
        linkage_error = review_resolution_feature_error(
            app,
            task,
            str(intent["resolution"]),
            [str(intent["feature_id"])],
        )
        if linkage_error is not None:
            return "error", linkage_error, session_id
    if task.get("claimed_by") not in {None, str(intent["actor"])}:
        return "error", "TASK_OWNER_MISMATCH", session_id

    session_status = str(session["status"])
    if session_status != "active":
        if session_status in {"draft", "paused"}:
            return "pending", "SESSION_INACTIVE", session_id
        return "error", "SESSION_IMMUTABLE", session_id

    transition_kind = str(intent["transition_kind"])
    task_status = str(task["status"])
    if transition_kind == "reopen":
        if (
            task_status != str(intent["expected_status"])
            or str(intent["feature_id"])
            not in {str(value) for value in task.get("resolved_feature_ids", [])}
        ):
            return "error", "TERMINAL_TASK_CONFLICT", session_id
        outcome, updated = store.update_review_task(
            task_id,
            expected_status=task_status,
            now=utc_now(),
            fields={
                "status": "todo",
                "resolution": None,
                "resolved_feature_ids": [],
                "claimed_by": None,
            },
            event_type="feature_undo",
            actor=str(intent["actor"]),
        )
    else:
        if task_status == "todo" and bool(intent["allow_claim"]):
            outcome, updated = store.update_review_task(
                task_id,
                expected_status="todo",
                now=utc_now(),
                fields={"status": "in_progress", "claimed_by": str(intent["actor"])},
                event_type="feature_redo_claim",
                actor=str(intent["actor"]),
            )
            if outcome == "updated" and updated is not None:
                task = updated
                task_status = str(task["status"])
            elif outcome in {"inactive", "immutable"}:
                code = "SESSION_INACTIVE" if outcome == "inactive" else "SESSION_IMMUTABLE"
                return (
                    "pending" if outcome == "inactive" else "error",
                    code,
                    session_id,
                )
            else:
                task = _refresh_task(store, task_id) or task
                task_status = str(task["status"])
        if task_status != "in_progress":
            if _desired_state(intent, task):
                return "reconciled", None, session_id
            return "error", "TERMINAL_TASK_CONFLICT", session_id
        outcome, updated = store.resolve_review_task(
            task_id,
            resolution=str(intent["resolution"]),
            resolved_feature_ids=[str(intent["feature_id"])],
            now=utc_now(),
            actor=str(intent["actor"]),
        )

    if outcome == "updated" and updated is not None and _desired_state(intent, updated):
        return "reconciled", None, session_id
    if outcome == "inactive":
        return "pending", "SESSION_INACTIVE", session_id
    if outcome == "immutable":
        return "error", "SESSION_IMMUTABLE", session_id
    latest = _refresh_task(store, task_id)
    if latest is not None and _desired_state(intent, latest):
        return "reconciled", None, session_id
    return "error", "TERMINAL_TASK_CONFLICT", session_id


def _update_intent(
    layer_dir: Path,
    intent_id: str,
    *,
    status: str,
    error_code: str | None,
    session_id: str | None,
    dataset_id: str,
    layer_id: str,
) -> None:
    from .overlays import _feature_db

    now = utc_now()
    with _feature_db(layer_dir, write=True) as connection:
        ensure_task_resolution_outbox(connection)
        connection.execute(
            """
            UPDATE task_resolution_outbox
            SET status=?,attempt_count=attempt_count+1,last_error_code=?,
                session_id=COALESCE(session_id,?),
                dataset_id=COALESCE(dataset_id,?),layer_id=COALESCE(layer_id,?),
                updated_at=?,reconciled_at=?
            WHERE id=?
            """,
            (
                status,
                error_code,
                session_id,
                dataset_id,
                layer_id,
                now,
                now if status == "reconciled" else None,
                intent_id,
            ),
        )


def _read_intents(
    layer_dir: Path,
    *,
    intent_id: str | None,
    session_id: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    from .overlays import _feature_db

    with _feature_db(layer_dir) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_resolution_outbox'"
        ).fetchone()
        if exists is None:
            return [], False
        where = ["status IN ('pending','error')"]
        parameters: list[Any] = []
        if intent_id is not None:
            where.append("id=?")
            parameters.append(intent_id)
        if session_id is not None:
            where.append("(session_id=? OR session_id IS NULL)")
            parameters.append(session_id)
        rows = connection.execute(
            f"""
            SELECT * FROM task_resolution_outbox
            WHERE {' AND '.join(where)}
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,created_at,id
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
    return [dict(row) for row in rows[:limit]], len(rows) > limit


def _status_counts(layer_dir: Path, session_id: str | None) -> dict[str, int]:
    from .overlays import _feature_db

    with _feature_db(layer_dir) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_resolution_outbox'"
        ).fetchone()
        if exists is None:
            return {"pending": 0, "error": 0}
        where = ""
        parameters: tuple[Any, ...] = ()
        if session_id is not None:
            # A registry outage may prevent the first replay from discovering
            # the parent session.  Keep those unassigned intents visible to a
            # scoped completion/report boundary until a later replay can
            # safely backfill their session_id.
            where = "WHERE session_id=? OR session_id IS NULL"
            parameters = (session_id,)
        rows = connection.execute(
            f"""
            SELECT status,COUNT(*) AS count FROM task_resolution_outbox
            {where} GROUP BY status
            """,
            parameters,
        ).fetchall()
    values = {str(row["status"]): int(row["count"]) for row in rows}
    return {"pending": values.get("pending", 0), "error": values.get("error", 0)}


def reconcile_layer_task_resolutions(
    app: Any,
    dataset_id: str,
    layer_id: str,
    *,
    intent_id: str | None = None,
    session_id: str | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Replay bounded intents after validating their dataset/layer ownership."""

    from .overlays import _feature_db, _layer_directory, _read_manifest

    layer_dir = _layer_directory(app, dataset_id, layer_id)
    manifest = _read_manifest(layer_dir)
    if (
        str(manifest.get("dataset_id")) != dataset_id
        or str(manifest.get("id")) != layer_id
    ):
        raise ValueError("Overlay ownership mismatch.")
    intents, truncated = _read_intents(
        layer_dir, intent_id=intent_id, session_id=session_id, limit=max(1, limit)
    )
    reconciled = 0
    for intent in intents:
        with _feature_db(layer_dir) as connection:
            feature_active = (
                connection.execute(
                    "SELECT 1 FROM features WHERE id=? AND deleted=0",
                    (intent["feature_id"],),
                ).fetchone()
                is not None
            )
        try:
            status, error_code, discovered_session_id = _attempt_intent(
                app,
                intent,
                dataset_id=dataset_id,
                layer_id=layer_id,
                feature_active=feature_active,
            )
        except (KeyError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            status, error_code, discovered_session_id = (
                "pending",
                "RETRYABLE_REGISTRY_ERROR",
                intent.get("session_id"),
            )
            app.state.logger.warning(
                "Review task transition intent %s remains pending.", intent["id"]
            )
        _update_intent(
            layer_dir,
            str(intent["id"]),
            status=status,
            error_code=error_code,
            session_id=(
                None
                if discovered_session_id is None
                else str(discovered_session_id)
            ),
            dataset_id=dataset_id,
            layer_id=layer_id,
        )
        reconciled += int(status == "reconciled")
    counts = _status_counts(layer_dir, session_id)
    return {
        **counts,
        "reconciled": reconciled,
        "attempted": len(intents),
        "truncated": int(truncated),
    }


def reconcile_task_resolution_intent(
    app: Any,
    dataset_id: str,
    layer_id: str,
    intent_id: str | None,
) -> bool:
    """Attempt one intent and report only whether that intent is reconciled."""

    if intent_id is None:
        return True
    from .overlays import _feature_db, _layer_directory

    try:
        reconcile_layer_task_resolutions(
            app, dataset_id, layer_id, intent_id=intent_id, limit=1
        )
        layer_dir = _layer_directory(app, dataset_id, layer_id)
        with _feature_db(layer_dir) as connection:
            row = connection.execute(
                "SELECT status FROM task_resolution_outbox WHERE id=?", (intent_id,)
            ).fetchone()
        return row is not None and str(row["status"]) == "reconciled"
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ):
        app.state.logger.warning(
            "Review task transition intent %s could not be reconciled immediately.",
            intent_id,
        )
        return False


def _merge_summary(target: dict[str, int], value: Mapping[str, int]) -> None:
    for key in ("pending", "error", "reconciled", "attempted", "truncated"):
        target[key] += int(value.get(key, 0))


def _session_layer_ids(app: Any, session: Mapping[str, Any]) -> tuple[list[str], bool]:
    from .overlays import OVERLAY_ID

    layer_ids = {str(value) for value in session.get("target_layer_ids", [])}
    with app.state.store.connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT target_layer_id FROM review_tasks
            WHERE session_id=? AND target_layer_id IS NOT NULL
            """,
            (session["id"],),
        ).fetchall()
    layer_ids.update(str(row[0]) for row in rows)
    truncated = False
    if not layer_ids:
        root = _existing_overlay_root(app, str(session["dataset_id"]))
        if root is None:
            return [], False
        candidates = [
            candidate.name
            for candidate in sorted(root.iterdir(), key=lambda value: value.name)
            if OVERLAY_ID.fullmatch(candidate.name)
            and candidate.is_dir()
            and not candidate.is_symlink()
        ]
        truncated = len(candidates) > _MAX_SESSION_LAYERS
        layer_ids.update(candidates[:_MAX_SESSION_LAYERS])
    ordered = sorted(layer_ids)
    return ordered[:_MAX_SESSION_LAYERS], truncated or len(ordered) > _MAX_SESSION_LAYERS


def reconcile_session_task_resolutions(
    app: Any,
    session: Mapping[str, Any],
    *,
    limit: int = 1_000,
) -> dict[str, int]:
    """Replay and summarize intents that can affect one review session."""

    summary = {key: 0 for key in ("pending", "error", "reconciled", "attempted", "truncated")}
    layer_ids, scan_truncated = _session_layer_ids(app, session)
    summary["truncated"] = int(scan_truncated)
    remaining = max(1, limit)
    for layer_id in layer_ids:
        if remaining <= 0:
            summary["truncated"] += 1
            break
        try:
            value = reconcile_layer_task_resolutions(
                app,
                str(session["dataset_id"]),
                layer_id,
                session_id=str(session["id"]),
                limit=remaining,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError, sqlite3.Error):
            summary["truncated"] += 1
            app.state.logger.warning(
                "Review task outbox session scan could not inspect layer %s.",
                layer_id,
            )
            continue
        _merge_summary(summary, value)
        remaining -= int(value["attempted"])
    return summary


def reconcile_all_task_resolutions(app: Any, *, limit: int = 2_000) -> dict[str, int]:
    """Bounded startup recovery across registered dataset overlay stores."""

    from .overlays import OVERLAY_ID

    summary = {key: 0 for key in ("pending", "error", "reconciled", "attempted", "truncated")}
    remaining = max(1, limit)
    inspected_layers = 0
    try:
        datasets = app.state.store.list_datasets(limit=_MAX_STARTUP_DATASETS + 1)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        summary["truncated"] += 1
        app.state.logger.warning("Review task outbox startup dataset scan failed.")
        return summary
    if len(datasets) > _MAX_STARTUP_DATASETS:
        summary["truncated"] += 1
    for dataset in datasets[:_MAX_STARTUP_DATASETS]:
        dataset_id = str(dataset["id"])
        try:
            root = _existing_overlay_root(app, dataset_id)
            if root is None:
                continue
            candidates = sorted(root.iterdir(), key=lambda value: value.name)
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
            summary["truncated"] += 1
            app.state.logger.warning(
                "Review task outbox startup layer scan failed for dataset %s.",
                dataset_id,
            )
            continue
        for candidate in candidates:
            if OVERLAY_ID.fullmatch(candidate.name) is None:
                continue
            inspected_layers += 1
            if inspected_layers > _MAX_STARTUP_LAYERS or remaining <= 0:
                summary["truncated"] += 1
                return summary
            try:
                value = reconcile_layer_task_resolutions(
                    app, dataset_id, candidate.name, limit=remaining
                )
            except (FileNotFoundError, OSError, TypeError, ValueError, sqlite3.Error):
                summary["truncated"] += 1
                app.state.logger.warning(
                    "Review task outbox startup could not inspect layer %s.",
                    candidate.name,
                )
                continue
            _merge_summary(summary, value)
            remaining -= int(value["attempted"])
    return summary


def reconcile_dataset_task_resolutions(
    app: Any, dataset_id: str, *, limit: int = 2_000
) -> dict[str, int]:
    """Boundedly replay every registered overlay outbox for one dataset."""

    from .overlays import OVERLAY_ID

    summary = {
        key: 0
        for key in ("pending", "error", "reconciled", "attempted", "truncated")
    }
    try:
        root = _existing_overlay_root(app, dataset_id)
        if root is None:
            return summary
        candidates = [
            candidate
            for candidate in sorted(root.iterdir(), key=lambda value: value.name)
            if OVERLAY_ID.fullmatch(candidate.name)
        ]
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        summary["truncated"] = 1
        return summary
    if len(candidates) > _MAX_STARTUP_LAYERS:
        summary["truncated"] = 1
    remaining = max(1, limit)
    for candidate in candidates[:_MAX_STARTUP_LAYERS]:
        if remaining <= 0:
            summary["truncated"] = 1
            break
        try:
            value = reconcile_layer_task_resolutions(
                app, dataset_id, candidate.name, limit=remaining
            )
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            summary["truncated"] = 1
            continue
        _merge_summary(summary, value)
        remaining -= int(value["attempted"])
    return summary


__all__ = [
    "enqueue_task_resolution_intent",
    "ensure_task_resolution_outbox",
    "reconcile_all_task_resolutions",
    "reconcile_dataset_task_resolutions",
    "reconcile_layer_task_resolutions",
    "reconcile_session_task_resolutions",
    "reconcile_task_resolution_intent",
    "review_dataset_lock",
    "review_resolution_feature_error",
    "review_session_lock",
]
