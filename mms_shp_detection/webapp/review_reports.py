"""Review-session reports and explicit, provenance-preserving exports."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import time
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask
from starlette.responses import Response

from .datasets import require_ready_dataset, utc_now
from .detections import MAX_DETECTION_RESULT_BYTES, _read_json_object
from .overlays import (
    _db_revision,
    _feature_db,
    _layer_directory,
    _read_manifest,
    _temporary_download_dir,
    _write_edited_bundle,
)
from .review_tasks import (
    _current_target_layer_revisions,
    _reconcile_session,
    _resolution_blockers,
    _result_roots,
)
from .security import UnsafePath, resolve_under_root

router = APIRouter(prefix="/api", tags=["review-reports"])

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_MODEL_PROVENANCE_FILES = 1_024
_MAX_MODEL_PROVENANCE_BYTES = 64 * 1024**2
_REVIEW_EXPORT_TEMP = re.compile(
    r"^(?:review|active-learning)-[0-9A-Za-z._-]{1,100}-[0-9A-Za-z_-]{6,16}$"
)
_TERMINAL_STATUSES = {
    "confirmed",
    "corrected",
    "manual_added",
    "false_positive",
    "skipped",
    "field_survey",
}
_CLASS_KEYS = (
    "class_name",
    "class_nm",
    "class",
    "type",
    "object_type",
    "CLASS_NAME",
    "CLASS_NM",
    "CLASS",
    "TYPE",
)


class ReviewExportTooLarge(ValueError):
    """A review export exceeded a configured, preflighted resource budget."""


class ReviewExportChanged(RuntimeError):
    """The registry or a target layer changed while an export was being built."""


class _ExportBudget:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = int(maximum_bytes)
        self.used_bytes = 0

    def consume(self, size: int) -> None:
        size = int(size)
        if size < 0 or self.used_bytes + size > self.maximum_bytes:
            raise ReviewExportTooLarge("Review export exceeds its byte budget.")
        self.used_bytes += size


def cleanup_stale_review_exports(app: Any) -> int:
    """Remove only bounded, old export temp directories from prior crashes."""

    def unsafe_link(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())

    root = app.state.config.state_dir / "downloads"
    try:
        state_root = app.state.config.state_dir.resolve(strict=True)
        if unsafe_link(root):
            return 0
        root_resolved = root.resolve(strict=True)
        if root_resolved.parent != state_root or not root_resolved.is_dir():
            return 0
        entries = root.iterdir()
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 0
    maximum_entries = int(app.state.config.max_review_export_cleanup_entries)
    stale_before = time.time() - float(
        app.state.config.review_export_stale_seconds
    )
    removed = 0
    try:
        for index, entry in enumerate(entries):
            if index >= maximum_entries:
                break
            try:
                if (
                    _REVIEW_EXPORT_TEMP.fullmatch(entry.name) is None
                    or unsafe_link(entry)
                    or not entry.is_dir()
                    or entry.stat().st_mtime > stale_before
                ):
                    continue
                resolved = entry.resolve(strict=True)
                if resolved.parent != root_resolved:
                    continue
                shutil.rmtree(resolved)
                removed += 1
            except (FileNotFoundError, NotADirectoryError, OSError):
                continue
    except OSError:
        return removed
    return removed


def _safe_name(value: Any, fallback: str) -> str:
    result = _SAFE_NAME.sub("_", str(value or "")).strip("._")[:100]
    return result or fallback


def _digest(prefix: str, *values: Any) -> str:
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _capture_registry_snapshot(
    app: Any,
    session: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = str(session["id"])
    maximum_tasks = int(app.state.config.max_review_export_tasks)
    with app.state.store.connection() as connection:
        connection.execute("BEGIN")
        try:
            session_row = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session_row is None:
                raise FileNotFoundError("Review session not found.")
            task_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM review_tasks WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            if task_count > maximum_tasks:
                raise ReviewExportTooLarge(
                    f"Review export is limited to {maximum_tasks} tasks."
                )
            task_rows = connection.execute(
                """
                SELECT * FROM review_tasks WHERE session_id=?
                ORDER BY queue_priority DESC,created_at,id
                """,
                (session_id,),
            ).fetchall()
            event_watermark = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(event.id),0)
                    FROM review_task_events event
                    JOIN review_tasks task ON task.id=event.task_id
                    WHERE task.session_id=?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            qa_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM qa_issues WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            if qa_count > maximum_tasks:
                raise ReviewExportTooLarge(
                    f"Review export is limited to {maximum_tasks} QA issues."
                )
            qa_rows = connection.execute(
                "SELECT * FROM qa_issues WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            if connection.in_transaction:
                connection.rollback()

    current_session = app.state.store.review_session_from_row(session_row)
    if current_session is None:  # pragma: no cover - guarded above
        raise FileNotFoundError("Review session not found.")
    if "_task_resolution_reconciliation" in session:
        current_session["_task_resolution_reconciliation"] = session[
            "_task_resolution_reconciliation"
        ]
    tasks = [
        task
        for row in task_rows
        if (task := app.state.store.review_task_from_row(row)) is not None
    ]
    session_state = {
        key: current_session.get(key)
        for key in (
            "id",
            "dataset_id",
            "source_run_ids",
            "target_layer_ids",
            "track_ids",
            "frame_range",
            "class_filters",
            "status",
            "created_by",
            "created_at",
            "updated_at",
            "last_task_id",
            "qa_layer_revisions",
            "qa_ran_at",
        )
    }
    task_digest = hashlib.sha256(_canonical_json_bytes(tasks)).hexdigest()
    qa_issues = [dict(row) for row in qa_rows]
    qa_digest = hashlib.sha256(_canonical_json_bytes(qa_issues)).hexdigest()
    fence = {
        "session_id": session_id,
        "session_updated_at": str(current_session["updated_at"]),
        "session_status": str(current_session["status"]),
        "session_digest": hashlib.sha256(
            _canonical_json_bytes(session_state)
        ).hexdigest(),
        "task_count": task_count,
        "task_event_watermark": event_watermark,
        "task_digest": task_digest,
        "qa_issue_count": qa_count,
        "qa_updated_at_watermark": max(
            (str(issue["updated_at"]) for issue in qa_issues), default=None
        ),
        "qa_digest": qa_digest,
    }
    return {
        "session": current_session,
        "tasks": tasks,
        "fence": fence,
        "fingerprint": hashlib.sha256(_canonical_json_bytes(fence)).hexdigest(),
    }


def _effective_scope(
    session: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    dataset_id = str(session["dataset_id"])
    run_ids = {str(item) for item in session.get("source_run_ids", [])}
    layer_ids = {str(item) for item in session.get("target_layer_ids", [])}
    for task in tasks:
        if str(task.get("dataset_id")) != dataset_id:
            raise ValueError("A review task escaped its session dataset scope.")
        if task.get("source_run_id") is not None:
            run_ids.add(str(task["source_run_id"]))
        if task.get("target_layer_id") is not None:
            layer_ids.add(str(task["target_layer_id"]))
    return sorted(run_ids), sorted(layer_ids)


def _capture_layer_snapshots(
    app: Any,
    dataset_id: str,
    layer_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    maximum_features = int(app.state.config.max_review_export_features)
    total_features = 0
    snapshots: dict[str, dict[str, Any]] = {}
    for layer_id in sorted({str(item) for item in layer_ids}):
        layer_dir = _layer_directory(app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        if (
            str(manifest.get("dataset_id")) != dataset_id
            or str(manifest.get("id")) != layer_id
        ):
            raise FileNotFoundError("Review target layer ownership mismatch.")
        with _feature_db(layer_dir) as connection:
            revision = _db_revision(connection)
            active_features = int(
                connection.execute(
                    "SELECT COUNT(*) FROM features WHERE deleted=0"
                ).fetchone()[0]
            )
        total_features += active_features
        if total_features > maximum_features:
            raise ReviewExportTooLarge(
                f"Review export is limited to {maximum_features} active features."
            )
        snapshots[layer_id] = {
            "directory": layer_dir,
            "manifest": manifest,
            "revision": revision,
            "active_features": active_features,
        }
    return snapshots


def _layer_revision_fence(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    return {
        layer_id: int(snapshot["revision"])
        for layer_id, snapshot in sorted(snapshots.items())
    }


class _ImageLocatorResolver:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.remaining_bytes = int(
            app.state.config.max_review_export_image_hash_bytes
        )
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}

    def resolve(self, dataset_id: str, frame_id: str) -> dict[str, Any]:
        key = (str(dataset_id), str(frame_id))
        cached = self.cache.get(key)
        if cached is not None:
            return dict(cached)
        locator: dict[str, Any] = {
            "dataset_id": key[0],
            "frame_id": key[1],
            "content_sha256": None,
            "content_sha256_status": "unavailable",
        }
        dataset = self.app.state.store.get_dataset(key[0])
        frame = self.app.state.store.get_frame(key[0], key[1])
        try:
            if dataset is None or frame is None:
                raise FileNotFoundError("Source frame is unavailable.")
            root = self.app.state.storage_roots_by_id.get(dataset["root_id"])
            if root is None:
                raise FileNotFoundError("Dataset root is unavailable.")
            dataset_root = resolve_under_root(
                root.path,
                str(dataset["relative_path"]),
                must_exist=True,
                expect_directory=True,
                reject_symlinks=True,
            )
            task = frame.get("task") or {}
            discovered = Path(str(task["image_path"])).resolve(strict=True)
            relative = discovered.relative_to(dataset_root).as_posix()
            source = resolve_under_root(
                dataset_root,
                relative,
                must_exist=True,
                expect_directory=False,
                reject_symlinks=True,
            )
            size = int(source.stat().st_size)
            if size < 0 or size > self.remaining_bytes:
                locator["content_sha256_status"] = "budget_exceeded"
            else:
                digest = hashlib.sha256()
                hashed_bytes = 0
                with source.open("rb") as handle:
                    while chunk := handle.read(
                        min(1024 * 1024, self.remaining_bytes - hashed_bytes + 1)
                    ):
                        hashed_bytes += len(chunk)
                        if hashed_bytes > self.remaining_bytes:
                            locator["content_sha256_status"] = "budget_exceeded"
                            break
                        digest.update(chunk)
                    else:
                        self.remaining_bytes -= hashed_bytes
                        locator["content_sha256"] = digest.hexdigest()
                        locator["content_sha256_status"] = "available"
        except (KeyError, OSError, TypeError, UnsafePath, ValueError):
            pass
        self.cache[key] = dict(locator)
        return locator


def _require_session(request: Request, session_id: str) -> dict[str, Any]:
    session = request.app.state.store.get_review_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Review session not found.")
    require_ready_dataset(request, str(session["dataset_id"]))
    session["_task_resolution_reconciliation"] = _reconcile_session(
        request, session
    )
    return session


def _finite_position(frame: Mapping[str, Any]) -> tuple[float, float, float] | None:
    task = frame.get("task")
    value = task.get("origin") if isinstance(task, Mapping) else None
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        result = tuple(float(value[index]) for index in range(3))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _coverage(request: Request, session: dict[str, Any]) -> dict[str, Any]:
    frame_count = 0
    reviewed_frames = 0
    total_distance = 0.0
    reviewed_distance = 0.0
    previous: dict[str, tuple[tuple[float, float, float], bool]] = {}
    for frame in request.app.state.store.iter_review_scope_frames(session):
        frame_count += 1
        reviewed = bool(frame.get("reviewed"))
        reviewed_frames += int(reviewed)
        position = _finite_position(frame)
        track_id = str(frame.get("track_id") or "")
        prior = previous.get(track_id)
        if position is not None and prior is not None:
            segment = _distance(prior[0], position)
            if math.isfinite(segment) and segment >= 0.0:
                total_distance += segment
                if reviewed and prior[1]:
                    reviewed_distance += segment
        if position is not None:
            previous[track_id] = (position, reviewed)
    return {
        "scope_frame_count": frame_count,
        "reviewed_frame_count": reviewed_frames,
        "frame_coverage_ratio": (
            round(reviewed_frames / frame_count, 6) if frame_count else 0.0
        ),
        "scope_distance_m": round(total_distance, 3),
        "reviewed_distance_m": round(reviewed_distance, 3),
        "distance_coverage_ratio": (
            round(reviewed_distance / total_distance, 6)
            if total_distance > 0.0
            else 0.0
        ),
    }


def _run_fingerprints(
    request: Request,
    session: dict[str, Any],
    run_ids: Iterable[str],
    model_versions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    dataset = request.app.state.store.get_dataset(str(session["dataset_id"])) or {}
    dataset_fingerprint = _digest(
        "dataset",
        dataset.get("id"),
        dataset.get("crs"),
        dataset.get("frame_count"),
        dataset.get("updated_at"),
        session.get("track_ids"),
        session.get("frame_range"),
    )
    runs: list[dict[str, Any]] = []
    models_by_run: dict[str, list[Mapping[str, Any]]] = {}
    for model in model_versions:
        models_by_run.setdefault(str(model["run_id"]), []).append(model)
    for run_id in sorted({str(item) for item in run_ids}):
        run = request.app.state.store.get_run(str(run_id))
        if run is None or str(run.get("dataset_id")) != str(session["dataset_id"]):
            raise ValueError("A source run is not owned by the review dataset.")
        artifact_fingerprints = sorted(
            {
                str(value)
                for model in models_by_run.get(run_id, [])
                for value in model.get("run_fingerprints", [])
                if isinstance(value, str) and _SHA256.fullmatch(value)
            }
        )
        runs.append(
            {
                "run_id": str(run_id),
                "fingerprint": (
                    artifact_fingerprints[0]
                    if len(artifact_fingerprints) == 1
                    else _digest("run-artifacts", artifact_fingerprints)
                    if artifact_fingerprints
                    else None
                ),
                "artifact_fingerprints": artifact_fingerprints,
                "registry_fingerprint": _digest(
                    "run",
                    run.get("id"),
                    run.get("request"),
                    run.get("resolved"),
                    run.get("created_at"),
                    run.get("finished_at"),
                ),
                "status": str(run.get("status") or "unknown"),
                "provenance_status": (
                    "available" if artifact_fingerprints else "unavailable"
                ),
            }
        )
    return {"dataset": dataset_fingerprint, "runs": runs}


def build_review_report(
    request: Request,
    session: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    model_versions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or _capture_registry_snapshot(request.app, session)
    session = snapshot["session"]
    run_ids, layer_ids = _effective_scope(session, snapshot["tasks"])
    reconciliation = session.get("_task_resolution_reconciliation")
    if not isinstance(reconciliation, dict):
        reconciliation = _reconcile_session(request, session)
    aggregates = request.app.state.store.review_report_aggregates(str(session["id"]))
    coverage = _coverage(request, session)
    if model_versions is None:
        model_versions = _model_versions(request, session, run_ids)
    fingerprints = _run_fingerprints(request, session, run_ids, model_versions)
    fingerprints["models"] = model_versions
    statuses = aggregates["task_status_counts"]
    blockers = request.app.state.store.review_session_completion_blockers(
        str(session["id"]),
        current_layer_revisions=_current_target_layer_revisions(request, session),
    )
    blockers.update(_resolution_blockers(reconciliation))
    total_tasks = int(aggregates["total_tasks"])
    completed_tasks = int(aggregates["completed_tasks"])
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "snapshot": {
            **snapshot["fence"],
            "registry_fingerprint": snapshot["fingerprint"],
        },
        "effective_scope": {
            "source_run_ids": run_ids,
            "target_layer_ids": layer_ids,
        },
        "session": {
            "id": str(session["id"]),
            "dataset_id": str(session["dataset_id"]),
            "status": str(session["status"]),
            "created_by": str(session["created_by"]),
            "created_at": str(session["created_at"]),
            "updated_at": str(session["updated_at"]),
            "source_run_ids": [str(item) for item in session.get("source_run_ids", [])],
            "target_layer_ids": [
                str(item) for item in session.get("target_layer_ids", [])
            ],
            "track_ids": [str(item) for item in session.get("track_ids", [])],
            "frame_range": session.get("frame_range"),
            "class_filters": [str(item) for item in session.get("class_filters", [])],
        },
        "coverage": coverage,
        "progress": {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "completion_ratio": (
                round(completed_tasks / total_tasks, 6) if total_tasks else 0.0
            ),
        },
        "completion_gate": {
            "eligible": not any(blockers.values()),
            "blockers": blockers,
        },
        "task_resolution_reconciliation": {
            "pending": int(reconciliation["pending"]),
            "errors": int(reconciliation["error"]),
            "scan_truncated": bool(reconciliation["truncated"]),
        },
        "tasks": {
            "by_source": aggregates["task_source_counts"],
            "by_status": statuses,
            "manual_added": int(statuses.get("manual_added", 0)),
            "corrected": int(statuses.get("corrected", 0)),
            "false_positive": int(statuses.get("false_positive", 0)),
            "field_survey": int(statuses.get("field_survey", 0)),
            "unresolved_review": int(aggregates["unresolved_review"]),
        },
        "qa": {
            "total_issues": int(aggregates["qa_issues"]),
            "open_issues": int(aggregates["open_qa_issues"]),
        },
        "operator": {
            "operators": list(aggregates["operators"]),
            "active_seconds": float(aggregates["operator_seconds"]),
            "first_event_at": aggregates["first_operator_event_at"],
            "last_event_at": aggregates["last_operator_event_at"],
        },
        "fingerprints": fingerprints,
    }


def _flatten_report(
    value: Any,
    prefix: str = "",
) -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key in sorted(value):
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_report(value[key], next_prefix)
    elif isinstance(value, list):
        yield (
            prefix,
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ),
        )
    elif value is None:
        yield prefix, ""
    elif isinstance(value, bool):
        yield prefix, "true" if value else "false"
    else:
        yield prefix, str(value)


def _report_csv(report: Mapping[str, Any]) -> str:
    def safe_cell(value: str) -> str:
        normalized = "".join(
            character
            for character in value
            if ord(character) >= 0x20 and ord(character) != 0x7F
        )
        if normalized.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + normalized
        return normalized

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("metric", "value"))
    writer.writerows(
        (safe_cell(metric), safe_cell(value))
        for metric, value in _flatten_report(report)
    )
    return output.getvalue()


def _markdown_table(title: str, values: Mapping[str, Any]) -> list[str]:
    rows = [f"## {title}", "", "| Metric | Value |", "|---|---:|"]
    for key, value in values.items():
        rendered = (
            json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
        )
        rows.append(f"| {key} | {str(rendered).replace('|', '\\|')} |")
    rows.append("")
    return rows


def _report_markdown(report: Mapping[str, Any]) -> str:
    session = report["session"]
    rows = [
        f"# Review report: {session['id']}",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"Dataset: `{session['dataset_id']}`",
        "",
    ]
    rows.extend(_markdown_table("Coverage", report["coverage"]))
    rows.extend(_markdown_table("Progress", report["progress"]))
    rows.extend(_markdown_table("Task outcomes", report["tasks"]))
    rows.extend(_markdown_table("QA", report["qa"]))
    rows.extend(_markdown_table("Operator time", report["operator"]))
    rows.extend(_markdown_table("Fingerprints", report["fingerprints"]))
    return "\n".join(rows).rstrip() + "\n"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _safe_provenance(value: Any) -> dict[str, Any]:
    parsed = value if isinstance(value, Mapping) else {}
    allowed = (
        "layer_id",
        "feature_id",
        "origin",
        "source_run_id",
        "source_frame_ids",
        "source_detection_ids",
        "manual_observation_ids",
        "creation_tool",
        "proposal_quality",
        "review_status",
        "created_by",
        "created_at",
        "updated_at",
    )
    return {str(key): parsed[key] for key in allowed if key in parsed}


def _write_json_array_member(
    archive: zipfile.ZipFile,
    arcname: str,
    prefix: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    budget: _ExportBudget,
) -> dict[str, Any]:
    count = 0
    digest = hashlib.sha256()
    written = 0

    def write(member: Any, value: bytes) -> None:
        nonlocal written
        budget.consume(len(value))
        member.write(value)
        digest.update(value)
        written += len(value)

    with archive.open(arcname, "w") as member:
        start = json.dumps(
            {**prefix, "items": []},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        marker = '"items":[]}'
        write(member, start.replace(marker, '"items":[').encode("utf-8"))
        first = True
        for item in items:
            if not first:
                write(member, b",")
            write(member, _canonical_json_bytes(item))
            first = False
            count += 1
        write(member, b"]}")
    return {"records": count, "bytes": written, "sha256": digest.hexdigest()}


def _write_bytes_member(
    archive: zipfile.ZipFile,
    arcname: str,
    value: bytes,
    budget: _ExportBudget,
) -> dict[str, Any]:
    budget.consume(len(value))
    archive.writestr(arcname, value)
    return {
        "bytes": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
    }


def _session_task_index(
    app: Any,
    tasks: Iterable[Mapping[str, Any]],
) -> tuple[set[str], dict[str, set[str]]]:
    task_ids: set[str] = set()
    features_by_layer: dict[str, set[str]] = {}
    for task in tasks:
        task_ids.add(str(task["id"]))
        layer_id = task.get("target_layer_id")
        if layer_id is not None:
            features_by_layer.setdefault(str(layer_id), set()).update(
                str(item) for item in task.get("resolved_feature_ids", [])
            )
    if sum(len(values) for values in features_by_layer.values()) > int(
        app.state.config.max_review_export_features
    ):
        raise ReviewExportTooLarge("Review export has too many linked features.")
    return task_ids, features_by_layer


def _session_layer_feature_ids(
    layer_dir: Path,
    task_ids: set[str],
    explicit_feature_ids: Iterable[str],
) -> set[str]:
    result = {str(item) for item in explicit_feature_ids}
    if not task_ids:
        return result
    with _feature_db(layer_dir) as connection:
        if not _table_exists(connection, "edit_transactions"):
            return result
        rows = connection.execute(
            """
            SELECT feature_id,task_id FROM edit_transactions
            WHERE task_id IS NOT NULL AND feature_id IS NOT NULL
            """
        )
        result.update(
            str(row["feature_id"]) for row in rows if str(row["task_id"]) in task_ids
        )
    return result


def _provenance_items(
    layer_dir: Path,
    feature_ids: set[str],
) -> Iterable[dict[str, Any]]:
    if not feature_ids:
        return
    remaining = set(feature_ids)
    with _feature_db(layer_dir) as connection:
        if _table_exists(connection, "feature_provenance"):
            rows = connection.execute(
                "SELECT feature_id,provenance_json FROM feature_provenance "
                "ORDER BY feature_id"
            )
            for row in rows:
                feature_id = str(row["feature_id"])
                if feature_id not in feature_ids:
                    continue
                try:
                    value = json.loads(str(row["provenance_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    value = {}
                public = _safe_provenance(value)
                public.setdefault("feature_id", feature_id)
                public["provenance_status"] = "available"
                remaining.discard(feature_id)
                yield public
    for feature_id in sorted(remaining):
        yield {"feature_id": feature_id, "provenance_status": "unavailable"}


def _copy_zip_members(
    source_zip: Path,
    destination: zipfile.ZipFile,
    prefix: str,
    budget: _ExportBudget,
) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(source_zip) as source:
        for info in source.infolist():
            name = Path(info.filename).name
            if not name or info.is_dir():
                continue
            arcname = f"{prefix}/{name}"
            digest = hashlib.sha256()
            written = 0
            with (
                source.open(info) as source_member,
                destination.open(arcname, "w") as target_member,
            ):
                while chunk := source_member.read(1024 * 1024):
                    budget.consume(len(chunk))
                    target_member.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            copied[arcname] = {"bytes": written, "sha256": digest.hexdigest()}
    return copied


def _build_review_export(
    app: Any,
    session: dict[str, Any],
    report: dict[str, Any],
    temp_dir: Path,
    tasks: list[dict[str, Any]],
    layer_snapshots: Mapping[str, Mapping[str, Any]],
) -> Path:
    safe_session = _safe_name(session["id"], "review-session")
    zip_path = temp_dir / f"{safe_session}-review-export.zip"
    budget = _ExportBudget(app.state.config.max_review_export_bytes)
    task_ids, features_by_layer = _session_task_index(app, tasks)
    files: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        files["report/report.json"] = _write_bytes_member(
            archive,
            "report/report.json",
            json.dumps(
                report, ensure_ascii=False, indent=2, allow_nan=False
            ).encode("utf-8"),
            budget,
        )
        files["report/report.csv"] = _write_bytes_member(
            archive, "report/report.csv", _report_csv(report).encode("utf-8"), budget
        )
        files["report/report.md"] = _write_bytes_member(
            archive,
            "report/report.md",
            _report_markdown(report).encode("utf-8"),
            budget,
        )
        for layer_id, layer_snapshot in sorted(layer_snapshots.items()):
            layer_dir = Path(layer_snapshot["directory"])
            manifest = dict(layer_snapshot["manifest"])
            safe_layer = _safe_name(layer_id, "layer")
            layer_temp = temp_dir / f"layer-{safe_layer}"
            layer_temp.mkdir()
            bundle_zip = _write_edited_bundle(layer_dir, manifest, layer_temp)
            files.update(
                _copy_zip_members(
                    bundle_zip,
                    archive,
                    f"overlays/{safe_layer}/edited",
                    budget,
                )
            )
            feature_ids = _session_layer_feature_ids(
                layer_dir,
                task_ids,
                features_by_layer.get(layer_id, set()),
            )
            provenance_name = f"overlays/{safe_layer}/provenance.json"
            files[provenance_name] = _write_json_array_member(
                archive,
                provenance_name,
                {
                    "schema_version": 1,
                    "generated_at": report["generated_at"],
                    "session_id": str(session["id"]),
                    "dataset_id": str(session["dataset_id"]),
                    "layer_id": layer_id,
                    "layer_revision": int(layer_snapshot["revision"]),
                    "fingerprints": report["fingerprints"],
                },
                _provenance_items(layer_dir, feature_ids),
                budget,
            )
        export_identity = {
            "schema_version": 1,
            "export_type": "review_delivery",
            "session_id": str(session["id"]),
            "dataset_id": str(session["dataset_id"]),
            "snapshot": report["snapshot"],
            "layer_revisions": _layer_revision_fence(layer_snapshots),
            "files": files,
        }
        export_manifest = {
            **export_identity,
            "generated_at": report["generated_at"],
            "export_fingerprint": hashlib.sha256(
                _canonical_json_bytes(export_identity)
            ).hexdigest(),
        }
        _write_bytes_member(
            archive,
            "manifest.json",
            _canonical_json_bytes(export_manifest),
            budget,
        )
    if zip_path.stat().st_size > int(app.state.config.max_review_export_bytes):
        raise ReviewExportTooLarge("Review export exceeds its compressed byte budget.")
    return zip_path


def _artifact_model_provenance(
    txt_root: Path,
    model_key: str,
    declared_sha256: str | None,
) -> dict[str, Any]:
    model_hashes: set[str] = set()
    run_fingerprints: set[str] = set()
    scanned_files = 0
    scanned_bytes = 0
    truncated = False
    try:
        record_dirs = sorted(txt_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        record_dirs = []
    for record_dir in record_dirs:
        if not record_dir.is_dir() or record_dir.is_symlink():
            continue
        try:
            result_files = sorted(
                (
                    path
                    for path in record_dir.iterdir()
                    if path.suffix.casefold() == ".txt"
                    and path.is_file()
                    and not path.is_symlink()
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue
        for result_file in result_files:
            if scanned_files >= _MAX_MODEL_PROVENANCE_FILES:
                truncated = True
                break
            try:
                size = int(result_file.stat().st_size)
            except OSError:
                continue
            if (
                size <= 0
                or size > MAX_DETECTION_RESULT_BYTES
                or scanned_bytes + size > _MAX_MODEL_PROVENANCE_BYTES
            ):
                if scanned_bytes + max(size, 0) > _MAX_MODEL_PROVENANCE_BYTES:
                    truncated = True
                    break
                continue
            payload = _read_json_object(result_file, MAX_DETECTION_RESULT_BYTES)
            scanned_files += 1
            scanned_bytes += size
            if payload is None:
                continue
            payload_key = str(payload.get("model_key") or "").strip()
            if payload_key and model_key != "default" and payload_key != model_key:
                continue
            raw_sha = payload.get("model_sha256")
            if isinstance(raw_sha, str) and _SHA256.fullmatch(raw_sha):
                model_hashes.add(raw_sha.lower())
            raw_run = payload.get("run_fingerprint")
            if isinstance(raw_run, str) and _SHA256.fullmatch(raw_run):
                run_fingerprints.add(raw_run.lower())
        if truncated:
            break

    normalized_declared = (
        declared_sha256.lower()
        if isinstance(declared_sha256, str) and _SHA256.fullmatch(declared_sha256)
        else None
    )
    if len(model_hashes) > 1 or (
        normalized_declared is not None
        and model_hashes
        and normalized_declared not in model_hashes
    ):
        return {
            "version": None,
            "provenance_status": "conflict",
            "reason_code": "MODEL_SHA256_CONFLICT",
            "run_fingerprints": sorted(run_fingerprints),
            "evidence_files_scanned": scanned_files,
            "scan_truncated": truncated,
        }
    version = next(iter(model_hashes), None)
    if version is None:
        return {
            "version": None,
            "provenance_status": "unavailable",
            "reason_code": "MODEL_SHA256_UNAVAILABLE",
            "run_fingerprints": sorted(run_fingerprints),
            "evidence_files_scanned": scanned_files,
            "scan_truncated": truncated,
        }
    return {
        "version": version,
        "provenance_status": "available",
        "reason_code": None,
        "run_fingerprints": sorted(run_fingerprints),
        "evidence_files_scanned": scanned_files,
        "scan_truncated": truncated,
    }


def _model_versions(
    request: Request,
    session: dict[str, Any],
    run_ids: Iterable[str],
) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    runs_root = request.app.state.config.state_dir / "runs"
    for run_id in sorted({str(item) for item in run_ids}):
        run = request.app.state.store.get_run(str(run_id))
        if run is None or str(run.get("dataset_id")) != str(session["dataset_id"]):
            raise ValueError("A source run is not owned by the review dataset.")
        try:
            work = resolve_under_root(
                runs_root,
                str(run["work_relative"]),
                must_exist=True,
                expect_directory=True,
                reject_symlinks=True,
            )
            output = resolve_under_root(
                work,
                "output",
                must_exist=True,
                expect_directory=True,
                reject_symlinks=True,
            )
        except (KeyError, OSError, TypeError, UnsafePath, ValueError):
            versions.append(
                {
                    "run_id": str(run_id),
                    "model_ref": _digest("model", run_id, "default"),
                    "version": None,
                    "provenance_status": "unavailable",
                    "reason_code": "RUN_OUTPUT_UNAVAILABLE",
                    "run_fingerprints": [],
                    "evidence_files_scanned": 0,
                    "scan_truncated": False,
                }
            )
            continue
        roots = _result_roots(output)
        if not roots:
            versions.append(
                {
                    "run_id": str(run_id),
                    "model_ref": _digest("model", run_id, "default"),
                    "version": None,
                    "provenance_status": "unavailable",
                    "reason_code": "MODEL_ARTIFACTS_UNAVAILABLE",
                    "run_fingerprints": [],
                    "evidence_files_scanned": 0,
                    "scan_truncated": False,
                }
            )
            continue
        for model_key, txt_root, model in roots:
            provenance = _artifact_model_provenance(
                txt_root,
                model_key,
                str(model.get("model_sha256"))
                if model.get("model_sha256") is not None
                else None,
            )
            versions.append(
                {
                    "run_id": str(run_id),
                    "model_ref": _digest("model", run_id, model_key),
                    **provenance,
                }
            )
    unique = {(str(item["run_id"]), str(item["model_ref"])): item for item in versions}
    return [unique[key] for key in sorted(unique)]


def _manual_observations(
    app: Any,
    session: dict[str, Any],
    task_ids: set[str],
    features_by_layer: Mapping[str, set[str]],
    layer_snapshots: Mapping[str, Mapping[str, Any]],
    effective_run_ids: list[str],
    model_refs_by_run: Mapping[str, list[str]],
    model_status_by_ref: Mapping[str, str],
    image_locator: _ImageLocatorResolver,
) -> Iterable[dict[str, Any]]:
    fallback_run_ids = effective_run_ids if len(effective_run_ids) == 1 else []
    allowed_run_ids = set(effective_run_ids)
    for layer_id, layer_snapshot in sorted(layer_snapshots.items()):
        layer_dir = Path(layer_snapshot["directory"])
        with _feature_db(layer_dir) as connection:
            if not _table_exists(
                connection, "manual_observations"
            ) or not _table_exists(connection, "feature_provenance"):
                continue
            linked_feature_ids = _session_layer_feature_ids(
                layer_dir,
                task_ids,
                features_by_layer.get(layer_id, set()),
            )
            observation_ids: set[str] = set()
            observation_run_ids: dict[str, set[str]] = {}
            provenance_rows = connection.execute(
                "SELECT feature_id,provenance_json FROM feature_provenance"
            )
            for provenance_row in provenance_rows:
                if str(provenance_row["feature_id"]) not in linked_feature_ids:
                    continue
                try:
                    provenance = json.loads(str(provenance_row["provenance_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(provenance, Mapping):
                    raw_ids = provenance.get("manual_observation_ids")
                    if isinstance(raw_ids, list):
                        normalized_ids = {str(item) for item in raw_ids}
                        observation_ids.update(normalized_ids)
                        source_run_id = provenance.get("source_run_id")
                        if (
                            source_run_id is not None
                            and str(source_run_id) in allowed_run_ids
                        ):
                            for observation_id in normalized_ids:
                                observation_run_ids.setdefault(
                                    observation_id, set()
                                ).add(str(source_run_id))
            if not observation_ids:
                continue
            rows = connection.execute(
                """
                SELECT id,frame_id,class_name,geometry_json
                FROM manual_observations WHERE dataset_id=? AND layer_id=?
                ORDER BY created_at,id
                """,
                (session["dataset_id"], layer_id),
            )
            for row in rows:
                if str(row["id"]) not in observation_ids:
                    continue
                try:
                    geometry = json.loads(str(row["geometry_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(geometry, dict):
                    continue
                allowed_geometry = {
                    key: geometry[key]
                    for key in (
                        "type",
                        "u_intervals",
                        "v_min",
                        "v_max",
                        "image_width",
                        "image_height",
                    )
                    if key in geometry
                }
                run_ids = sorted(
                    observation_run_ids.get(str(row["id"]), set())
                    or fallback_run_ids
                )
                model_refs = sorted(
                    {
                        model_ref
                        for run_id in run_ids
                        for model_ref in model_refs_by_run.get(run_id, [])
                    }
                )
                yield {
                    "observation_id": str(row["id"]),
                    "layer_id": layer_id,
                    "source_image_ref": _digest(
                        "image", session["dataset_id"], row["frame_id"]
                    ),
                    "source_image": image_locator.resolve(
                        str(session["dataset_id"]), str(row["frame_id"])
                    ),
                    "source_run_ids": run_ids,
                    "model_refs": model_refs,
                    "model_provenance_status": (
                        "available"
                        if model_refs
                        and all(
                            model_status_by_ref.get(model_ref) == "available"
                            for model_ref in model_refs
                        )
                        else "unavailable"
                    ),
                    "class_name": str(row["class_name"]),
                    "bbox": allowed_geometry,
                }


def _corrected_class(app: Any, task: Mapping[str, Any]) -> str | None:
    layer_id = task.get("target_layer_id")
    feature_ids = task.get("resolved_feature_ids") or []
    if layer_id is None or not feature_ids:
        return None
    try:
        layer_dir = _layer_directory(app, str(task["dataset_id"]), str(layer_id))
        with _feature_db(layer_dir) as connection:
            row = connection.execute(
                "SELECT properties_json FROM features WHERE id=? AND deleted=0",
                (str(feature_ids[0]),),
            ).fetchone()
        properties = json.loads(str(row[0])) if row is not None else {}
    except (FileNotFoundError, OSError, TypeError, ValueError, sqlite3.Error):
        return None
    if not isinstance(properties, Mapping):
        return None
    for key in _CLASS_KEYS:
        value = properties.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)[:160]
    return None


def _iter_training_labels(
    app: Any,
    tasks: Iterable[Mapping[str, Any]],
    models_by_run: Mapping[str, list[str]],
    model_status_by_ref: Mapping[str, str],
    image_locator: _ImageLocatorResolver,
) -> Iterable[dict[str, Any]]:
    for task in tasks:
        status = str(task.get("status"))
        if status not in {"false_positive", "corrected"}:
            continue
        run_id = (
            str(task["source_run_id"])
            if task.get("source_run_id") is not None
            else None
        )
        model_refs = list(models_by_run.get(run_id or "", []))
        model_statuses = {
            model_status_by_ref.get(model_ref, "unavailable")
            for model_ref in model_refs
        }
        if "conflict" in model_statuses:
            model_status = "conflict"
        elif model_refs and model_statuses == {"available"}:
            model_status = "available"
        else:
            model_status = "unavailable"
        frame_id = task.get("frame_id")
        dataset_id = str(task["dataset_id"])
        yield {
            "task_id": str(task["id"]),
            "label_action": status,
            "source_detection_id": task.get("source_detection_id"),
            "source_image_ref": (
                _digest("image", dataset_id, frame_id)
                if frame_id is not None
                else None
            ),
            "source_image": (
                image_locator.resolve(dataset_id, str(frame_id))
                if frame_id is not None
                else None
            ),
            "source_run_id": run_id,
            "model_refs": model_refs,
            "model_provenance_status": model_status,
            "original_class": task.get("class_hint"),
            "corrected_class": (
                _corrected_class(app, task) if status == "corrected" else None
            ),
        }


def _write_jsonl_member(
    archive: zipfile.ZipFile,
    arcname: str,
    items: Iterable[Mapping[str, Any]],
    budget: _ExportBudget,
) -> dict[str, Any]:
    count = 0
    written = 0
    digest = hashlib.sha256()
    with archive.open(arcname, "w") as member:
        for item in items:
            encoded = _canonical_json_bytes(item) + b"\n"
            budget.consume(len(encoded))
            member.write(encoded)
            digest.update(encoded)
            written += len(encoded)
            count += 1
    return {"records": count, "bytes": written, "sha256": digest.hexdigest()}


def _build_active_learning_export(
    app: Any,
    session: dict[str, Any],
    report: dict[str, Any],
    model_versions: list[dict[str, Any]],
    temp_dir: Path,
    tasks: list[dict[str, Any]],
    layer_snapshots: Mapping[str, Mapping[str, Any]],
    effective_run_ids: list[str],
) -> Path:
    safe_session = _safe_name(session["id"], "review-session")
    zip_path = temp_dir / f"{safe_session}-active-learning.zip"
    budget = _ExportBudget(app.state.config.max_review_export_bytes)
    models_by_run: dict[str, list[str]] = {}
    model_status_by_ref: dict[str, str] = {}
    for model in model_versions:
        model_ref = str(model["model_ref"])
        models_by_run.setdefault(str(model["run_id"]), []).append(model_ref)
        model_status_by_ref[model_ref] = str(
            model.get("provenance_status") or "unavailable"
        )
    for refs in models_by_run.values():
        refs.sort()
    task_ids, features_by_layer = _session_task_index(app, tasks)
    image_locator = _ImageLocatorResolver(app)
    files: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        files["manual_bboxes.jsonl"] = _write_jsonl_member(
            archive,
            "manual_bboxes.jsonl",
            _manual_observations(
                app,
                session,
                task_ids,
                features_by_layer,
                layer_snapshots,
                effective_run_ids,
                models_by_run,
                model_status_by_ref,
                image_locator,
            ),
            budget,
        )
        files["review_labels.jsonl"] = _write_jsonl_member(
            archive,
            "review_labels.jsonl",
            _iter_training_labels(
                app,
                tasks,
                models_by_run,
                model_status_by_ref,
                image_locator,
            ),
            budget,
        )
        export_identity = {
            "schema_version": 1,
            "export_type": "active_learning_evidence",
            "session_id": str(session["id"]),
            "dataset_id": str(session["dataset_id"]),
            "snapshot": report["snapshot"],
            "effective_scope": report["effective_scope"],
            "layer_revisions": _layer_revision_fence(layer_snapshots),
            "fingerprints": report["fingerprints"],
            "model_versions": model_versions,
            "files": files,
            "records": {
                "manual_bboxes": files["manual_bboxes.jsonl"]["records"],
                "review_labels": files["review_labels.jsonl"]["records"],
            },
            "automation": {
                "training_started": False,
                "deployment_started": False,
                "operator_action_required": True,
            },
        }
        manifest = {
            **export_identity,
            "generated_at": report["generated_at"],
            "export_fingerprint": hashlib.sha256(
                _canonical_json_bytes(export_identity)
            ).hexdigest(),
        }
        _write_bytes_member(
            archive,
            "manifest.json",
            _canonical_json_bytes(manifest),
            budget,
        )
    if zip_path.stat().st_size > int(app.state.config.max_review_export_bytes):
        raise ReviewExportTooLarge("Review export exceeds its compressed byte budget.")
    return zip_path


def _prepare_export_context(
    request: Request,
    initial_session: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[str],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    snapshot = _capture_registry_snapshot(request.app, initial_session)
    session = snapshot["session"]
    run_ids, layer_ids = _effective_scope(session, snapshot["tasks"])
    layer_snapshots = _capture_layer_snapshots(
        request.app, str(session["dataset_id"]), layer_ids
    )
    model_versions = _model_versions(request, session, run_ids)
    report = build_review_report(
        request,
        session,
        snapshot=snapshot,
        model_versions=model_versions,
    )
    return (
        snapshot,
        session,
        run_ids,
        model_versions,
        layer_snapshots,
        report,
    )


def _assert_export_fence(
    app: Any,
    before: Mapping[str, Any],
    before_layers: Mapping[str, Mapping[str, Any]],
) -> None:
    try:
        after = _capture_registry_snapshot(app, before["session"])
        if str(after["fingerprint"]) != str(before["fingerprint"]):
            raise ReviewExportChanged("Review tasks changed while exporting.")
        _, layer_ids = _effective_scope(after["session"], after["tasks"])
        after_layers = _capture_layer_snapshots(
            app, str(after["session"]["dataset_id"]), layer_ids
        )
    except ReviewExportChanged:
        raise
    except (FileNotFoundError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ReviewExportChanged("Review scope changed while exporting.") from exc
    if _layer_revision_fence(after_layers) != _layer_revision_fence(before_layers):
        raise ReviewExportChanged("A target layer changed while exporting.")


async def _to_thread_drained(callback: Any, *args: Any) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(callback, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


@router.get("/review-sessions/{session_id}/report")
def get_review_report(
    session_id: str,
    request: Request,
    report_format: Literal["json", "csv", "markdown"] = Query("json", alias="format"),
) -> Response:
    initial_session = _require_session(request, session_id)
    try:
        (
            snapshot,
            _session,
            _run_ids,
            _model_versions_for_report,
            layer_snapshots,
            report,
        ) = _prepare_export_context(request, initial_session)
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if report_format == "csv":
            response: Response = PlainTextResponse(
                _report_csv(report),
                media_type="text/csv; charset=utf-8",
                headers=headers,
            )
        elif report_format == "markdown":
            response = PlainTextResponse(
                _report_markdown(report),
                media_type="text/markdown; charset=utf-8",
                headers=headers,
            )
        else:
            response = JSONResponse(report, headers=headers)
        _assert_export_fence(request.app, snapshot, layer_snapshots)
        return response
    except ReviewExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ReviewExportChanged as exc:
        raise HTTPException(
            status_code=409,
            detail="Review data changed while rendering the report. Retry.",
        ) from exc
    except (FileNotFoundError, OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=404, detail="A review report target is unavailable."
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/review-sessions/{session_id}/export")
async def export_review_session(session_id: str, request: Request) -> FileResponse:
    initial_session = _require_session(request, session_id)
    temp_dir: Path | None = None
    response_owns_cleanup = False
    try:
        async with request.app.state.review_export_semaphore:
            (
                snapshot,
                session,
                _run_ids,
                _model_versions_for_report,
                layer_snapshots,
                report,
            ) = await _to_thread_drained(
                _prepare_export_context, request, initial_session
            )
            temp_dir = _temporary_download_dir(
                request.app, f"review-{_safe_name(session_id, 'session')}-"
            )
            zip_path = await _to_thread_drained(
                _build_review_export,
                request.app,
                session,
                report,
                temp_dir,
                snapshot["tasks"],
                layer_snapshots,
            )
            await _to_thread_drained(
                _assert_export_fence, request.app, snapshot, layer_snapshots
            )
        response = FileResponse(
            zip_path,
            media_type="application/zip",
            filename=zip_path.name,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
        response_owns_cleanup = True
        return response
    except ReviewExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ReviewExportChanged as exc:
        raise HTTPException(
            status_code=409,
            detail="Review data changed during export. Retry the export.",
        ) from exc
    except (
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
        zipfile.BadZipFile,
    ) as exc:
        raise HTTPException(
            status_code=404, detail="A review target layer could not be exported."
        ) from exc
    finally:
        if temp_dir is not None and not response_owns_cleanup:
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/review-sessions/{session_id}/active-learning-export")
async def export_active_learning(session_id: str, request: Request) -> FileResponse:
    if not request.app.state.config.enable_active_learning_export:
        raise HTTPException(
            status_code=403,
            detail="Active-learning export is disabled by server policy.",
        )
    initial_session = _require_session(request, session_id)
    temp_dir: Path | None = None
    response_owns_cleanup = False
    try:
        async with request.app.state.review_export_semaphore:
            (
                snapshot,
                session,
                run_ids,
                model_versions,
                layer_snapshots,
                report,
            ) = await _to_thread_drained(
                _prepare_export_context, request, initial_session
            )
            temp_dir = _temporary_download_dir(
                request.app, f"active-learning-{_safe_name(session_id, 'session')}-"
            )
            zip_path = await _to_thread_drained(
                _build_active_learning_export,
                request.app,
                session,
                report,
                model_versions,
                temp_dir,
                snapshot["tasks"],
                layer_snapshots,
                run_ids,
            )
            await _to_thread_drained(
                _assert_export_fence, request.app, snapshot, layer_snapshots
            )
        response = FileResponse(
            zip_path,
            media_type="application/zip",
            filename=zip_path.name,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
        response_owns_cleanup = True
        return response
    except ReviewExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ReviewExportChanged as exc:
        raise HTTPException(
            status_code=409,
            detail="Review data changed during export. Retry the export.",
        ) from exc
    except (
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
        zipfile.BadZipFile,
    ) as exc:
        raise HTTPException(
            status_code=404,
            detail="Active-learning evidence could not be exported.",
        ) from exc
    finally:
        if temp_dir is not None and not response_owns_cleanup:
            shutil.rmtree(temp_dir, ignore_errors=True)


__all__ = ["build_review_report", "router"]
