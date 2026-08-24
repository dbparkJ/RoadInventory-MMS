from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from mms_shp_detection.review_candidates import (
    CandidateSourceSettings,
    generate_review_candidates,
)
from mms_shp_detection.shp_writer import make_detection_id

from .datasets import require_ready_dataset, utc_now
from .detections import (
    MAX_DETECTION_RESULT_BYTES,
    MAX_DETECTIONS_PER_FRAME,
    _read_json_object,
)
from .overlays import _db_revision, _feature_db, _layer_directory, _read_manifest
from .review_contracts import (
    ReviewSession,
    ReviewSessionCreate,
    ReviewSessionPatch,
    ReviewSessionStatus,
    ReviewTask,
    ReviewTaskCreate,
    ReviewTaskGenerateRequest,
    ReviewTaskPatch,
    ReviewTaskResolve,
    ReviewTaskStatus,
    ReviewTaskType,
)
from .security import UnsafePath, resolve_under_root
from .task_resolution_outbox import (
    reconcile_session_task_resolutions,
    review_dataset_lock,
    review_resolution_feature_error,
    review_session_lock,
)

router = APIRouter(prefix="/api", tags=["review"])

_MODEL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_REVIEW_ARTIFACT_FILES = 100_000
_MAX_REVIEW_ARTIFACT_BYTES = 1024 * 1024**2
_MAX_GENERATED_TASKS = 100_000
_MAX_REVIEW_ARTIFACTS = _MAX_GENERATED_TASKS
_MAX_QUEUE_CURSOR_LENGTH = 512

_TERMINAL_TASK_STATUSES = {
    ReviewTaskStatus.CONFIRMED.value,
    ReviewTaskStatus.CORRECTED.value,
    ReviewTaskStatus.MANUAL_ADDED.value,
    ReviewTaskStatus.FALSE_POSITIVE.value,
    ReviewTaskStatus.SKIPPED.value,
    ReviewTaskStatus.FIELD_SURVEY.value,
}
_SESSION_TRANSITIONS = {
    ReviewSessionStatus.DRAFT.value: {ReviewSessionStatus.ACTIVE.value},
    ReviewSessionStatus.ACTIVE.value: {
        ReviewSessionStatus.PAUSED.value,
        ReviewSessionStatus.COMPLETED.value,
    },
    ReviewSessionStatus.PAUSED.value: {
        ReviewSessionStatus.ACTIVE.value,
        ReviewSessionStatus.COMPLETED.value,
    },
    ReviewSessionStatus.COMPLETED.value: {ReviewSessionStatus.ARCHIVED.value},
    ReviewSessionStatus.ARCHIVED.value: set(),
}


def _opaque_digest(*values: Any) -> str:
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_task_cursor(task: dict[str, Any]) -> str:
    queue_priority = float(task["queue_priority"])
    if not math.isfinite(queue_priority):
        raise ValueError("Review task queue priority is not finite.")
    payload = json.dumps(
        [queue_priority, str(task["created_at"]), str(task["id"])],
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_task_cursor(value: str) -> tuple[float, str, str]:
    if not value or len(value) > _MAX_QUEUE_CURSOR_LENGTH:
        raise HTTPException(status_code=422, detail="Invalid review task cursor.")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or isinstance(payload[0], bool)
            or not isinstance(payload[0], (int, float))
            or not math.isfinite(float(payload[0]))
            or not isinstance(payload[1], str)
            or not payload[1]
            or not isinstance(payload[2], str)
            or _safe_component(payload[2]) is None
        ):
            raise ValueError("invalid cursor payload")
        return float(payload[0]), payload[1], payload[2]
    except (
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=422, detail="Invalid review task cursor."
        ) from exc


def _safe_component(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        return None
    return text


def _safe_basename(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        return None
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    return name if len(name) <= 260 and _safe_component(name) is not None else None


def _scoped_frames(request: Request, session: dict[str, Any]) -> list[dict[str, Any]]:
    frames = request.app.state.store.all_frames(str(session["dataset_id"]))
    track_ids = {str(item) for item in session.get("track_ids", [])}
    frame_range = session.get("frame_range")
    return [
        frame
        for frame in frames
        if (not track_ids or str(frame["track_id"]) in track_ids)
        and (
            frame_range is None
            or int(frame_range[0]) <= int(frame["ordinal"]) <= int(frame_range[1])
        )
    ]


def _frame_lookup(
    frames: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any] | None],
]:
    by_record: dict[tuple[str, str], dict[str, Any]] = {}
    by_image: dict[str, dict[str, Any] | None] = {}
    for frame in frames:
        task = frame.get("task") or {}
        image_name = Path(str(task.get("image_name") or "")).name.casefold()
        image_stem = Path(image_name).stem.casefold()
        record_name = str(task.get("record_name") or "").strip().casefold()
        if image_stem and record_name:
            by_record[(record_name, image_stem)] = frame
        if image_name:
            by_image[image_name] = frame if image_name not in by_image else None
    return by_record, by_image


def _result_roots(
    output_dir: Path,
) -> list[tuple[str, Path, dict[str, Any]]]:
    """Resolve declared or legacy model TXT roots without trusting manifest paths."""

    result: list[tuple[str, Path, dict[str, Any]]] = []
    manifest = _read_json_object(output_dir / "models_manifest.json", 5 * 1024**2)
    models = manifest.get("models") if manifest is not None else None
    if isinstance(models, list):
        for raw_model in models[:256]:
            if not isinstance(raw_model, dict) or raw_model.get("status") not in {
                None,
                "completed",
            }:
                continue
            model_key = str(raw_model.get("model_key") or "").strip()
            if not _MODEL_KEY.fullmatch(model_key):
                continue
            try:
                txt_root = resolve_under_root(
                    output_dir,
                    f"{model_key}/txt",
                    must_exist=True,
                    expect_directory=True,
                    reject_symlinks=True,
                )
            except (OSError, UnsafePath, ValueError):
                continue
            result.append((model_key, txt_root, raw_model))
    if result:
        return result

    # Single-model jobs publish directly under output/txt.  Older multi-model
    # jobs can be recovered from safe immediate child directories.
    direct = output_dir / "txt"
    if direct.is_dir() and not direct.is_symlink():
        result.append(("default", direct, {}))
    try:
        children = sorted(output_dir.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        children = []
    for child in children[:256]:
        if (
            not child.is_dir()
            or child.is_symlink()
            or not _MODEL_KEY.fullmatch(child.name)
        ):
            continue
        txt_root = child / "txt"
        if txt_root.is_dir() and not txt_root.is_symlink():
            result.append((child.name, txt_root, {}))
    return result


def _artifact_payloads(
    request: Request,
    session: dict[str, Any],
    frames: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    """Read bounded result JSON objects and return path-free detection records."""

    by_record, by_image = _frame_lookup(frames)
    artifacts: list[dict[str, Any]] = []
    file_count = 0
    byte_count = 0
    skipped_files = 0
    truncated = False
    runs_root = request.app.state.config.state_dir / "runs"

    for run_id in session.get("source_run_ids", []):
        run = request.app.state.store.get_run(str(run_id))
        if (
            run is None
            or str(run.get("dataset_id")) != str(session["dataset_id"])
            or str(run.get("status")) != "completed"
        ):
            skipped_files += 1
            continue
        try:
            work_dir = resolve_under_root(
                runs_root,
                str(run["work_relative"]),
                must_exist=True,
                expect_directory=True,
                reject_symlinks=True,
            )
            output_dir = resolve_under_root(
                work_dir,
                "output",
                must_exist=True,
                expect_directory=True,
                reject_symlinks=True,
            )
        except (KeyError, OSError, TypeError, UnsafePath, ValueError):
            skipped_files += 1
            continue
        run_fingerprint = _opaque_digest(
            run.get("id"), run.get("request"), run.get("resolved")
        )
        for model_key, txt_root, model in _result_roots(output_dir):
            try:
                record_dirs = sorted(
                    txt_root.iterdir(), key=lambda path: path.name.casefold()
                )
            except OSError:
                skipped_files += 1
                continue
            for record_dir in record_dirs:
                if (
                    not record_dir.is_dir()
                    or record_dir.is_symlink()
                    or _safe_component(record_dir.name) is None
                ):
                    continue
                try:
                    result_files = sorted(
                        (
                            path
                            for path in record_dir.iterdir()
                            if path.suffix.casefold() == ".txt"
                        ),
                        key=lambda path: path.name.casefold(),
                    )
                except OSError:
                    skipped_files += 1
                    continue
                for result_file in result_files:
                    if file_count >= _MAX_REVIEW_ARTIFACT_FILES:
                        truncated = True
                        break
                    try:
                        if result_file.is_symlink() or not result_file.is_file():
                            continue
                        size = int(result_file.stat().st_size)
                    except OSError:
                        skipped_files += 1
                        continue
                    if (
                        size <= 0
                        or size > MAX_DETECTION_RESULT_BYTES
                        or byte_count + size > _MAX_REVIEW_ARTIFACT_BYTES
                    ):
                        skipped_files += 1
                        if byte_count + size > _MAX_REVIEW_ARTIFACT_BYTES:
                            truncated = True
                            break
                        continue
                    payload = _read_json_object(result_file, MAX_DETECTION_RESULT_BYTES)
                    file_count += 1
                    byte_count += size
                    if payload is None:
                        skipped_files += 1
                        continue
                    record_name = _safe_component(
                        payload.get("record_name") or record_dir.name
                    )
                    image_name = _safe_basename(
                        payload.get("image_name") or result_file.stem
                    )
                    if (
                        record_name is None
                        or len(record_name) > 160
                        or image_name is None
                    ):
                        skipped_files += 1
                        continue
                    frame = by_record.get(
                        (record_name.casefold(), Path(image_name).stem.casefold())
                    )
                    if frame is None:
                        frame = by_image.get(image_name.casefold())
                    if frame is None:
                        skipped_files += 1
                        continue
                    detections = payload.get("detections")
                    if not isinstance(detections, list):
                        continue
                    if len(detections) > MAX_DETECTIONS_PER_FRAME:
                        detections = detections[:MAX_DETECTIONS_PER_FRAME]
                        truncated = True
                    model_fingerprint = str(
                        payload.get("model_sha256")
                        or model.get("run_fingerprint")
                        or _opaque_digest(model_key, payload.get("model_name"))
                    )
                    for ordinal, detection in enumerate(detections, start=1):
                        if len(artifacts) >= _MAX_REVIEW_ARTIFACTS:
                            truncated = True
                            break
                        if not isinstance(detection, dict):
                            continue
                        raw_index = detection.get("detection_index", ordinal)
                        try:
                            detection_index = int(raw_index)
                        except (TypeError, ValueError, OverflowError):
                            detection_index = ordinal
                        detection_image = _safe_basename(
                            detection.get("image_name") or image_name
                        )
                        if detection_image is None:
                            continue
                        detection_id = make_detection_id(
                            record_name, detection_image, detection_index
                        )
                        artifacts.append(
                            {
                                "source_run_id": str(run_id),
                                "run_fingerprint": str(
                                    payload.get("run_fingerprint") or run_fingerprint
                                ),
                                "model_fingerprint": model_fingerprint,
                                "frame_id": str(frame["id"]),
                                "track_id": str(frame["track_id"]),
                                "frame_ordinal": int(frame["ordinal"]),
                                "record_name": record_name,
                                "image_name": detection_image,
                                "detection_id": detection_id,
                                "detection_index": detection_index,
                                "class_name": detection.get("class_name"),
                                "detection": detection,
                            }
                        )
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break
    return artifacts, {
        "artifact_files": file_count,
        "artifact_bytes": byte_count,
        "skipped_artifacts": skipped_files,
        "truncated": truncated,
    }


def _public_session(value: dict[str, Any]) -> dict[str, Any]:
    return ReviewSession.model_validate(value).model_dump(mode="json")


def _public_task(value: dict[str, Any]) -> dict[str, Any]:
    return ReviewTask.model_validate(
        {key: item for key, item in value.items() if key != "queue_priority"}
    ).model_dump(mode="json")


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


def _reject_duplicates(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must not contain duplicate IDs.",
        )


def _require_layer(request: Request, dataset_id: str, layer_id: str) -> None:
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        if manifest.get("dataset_id") != dataset_id or manifest.get("id") != layer_id:
            raise ValueError("Layer ownership mismatch.")
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
            detail="Target layer is not an active layer owned by this dataset.",
        ) from exc


def _current_target_layer_revisions(
    request: Request,
    session: dict[str, Any],
) -> dict[str, int | None]:
    """Read path-free current revisions for the session completion gate."""

    dataset_id = str(session["dataset_id"])
    revisions: dict[str, int | None] = {}
    layer_ids = {
        str(value) for value in session.get("target_layer_ids", [])
    }
    layer_ids.update(
        request.app.state.store.review_session_effective_target_layer_ids(
            str(session["id"])
        )
    )
    for raw_layer_id in sorted(layer_ids):
        layer_id = str(raw_layer_id)
        try:
            layer_dir = _layer_directory(request.app, dataset_id, layer_id)
            manifest = _read_manifest(layer_dir)
            if (
                str(manifest.get("dataset_id")) != dataset_id
                or str(manifest.get("id")) != layer_id
            ):
                raise ValueError("Layer ownership mismatch.")
            with _feature_db(layer_dir) as connection:
                revisions[layer_id] = _db_revision(connection)
        except (
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            UnsafePath,
            json.JSONDecodeError,
            sqlite3.Error,
        ):
            revisions[layer_id] = None
    return revisions


def _require_active_features(
    request: Request,
    dataset_id: str,
    layer_id: str,
    feature_ids: list[str],
) -> None:
    _reject_duplicates(feature_ids, "resolved_feature_ids")
    if not feature_ids:
        return
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        if (
            str(manifest.get("dataset_id")) != dataset_id
            or str(manifest.get("id")) != layer_id
        ):
            raise FileNotFoundError("Layer ownership mismatch.")
        placeholders = ",".join("?" for _ in feature_ids)
        with _feature_db(layer_dir) as connection:
            rows = connection.execute(
                f"SELECT id FROM features WHERE deleted=0 AND id IN ({placeholders})",
                feature_ids,
            ).fetchall()
        found = {str(row["id"]) for row in rows}
        if found != set(feature_ids):
            raise FileNotFoundError("A resolved feature is missing.")
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
            detail="Every resolved feature must be active in the task target layer.",
        ) from exc


def _dataset_track_ids(dataset: dict[str, Any]) -> set[str]:
    return {
        str(item["id"])
        for item in dataset.get("tracks", [])
        if isinstance(item, dict) and item.get("id") is not None
    }


def _validate_session_scope(
    request: Request,
    dataset: dict[str, Any],
    values: dict[str, Any],
    *,
    require_completed_source_runs: bool,
) -> None:
    dataset_id = str(dataset["id"])
    source_run_ids = list(values.get("source_run_ids", []))
    target_layer_ids = list(values.get("target_layer_ids", []))
    track_ids = list(values.get("track_ids", []))
    _reject_duplicates(source_run_ids, "source_run_ids")
    _reject_duplicates(target_layer_ids, "target_layer_ids")
    _reject_duplicates(track_ids, "track_ids")
    for run_id in source_run_ids:
        run = request.app.state.store.get_run(run_id)
        if run is None or str(run["dataset_id"]) != dataset_id:
            raise HTTPException(
                status_code=422,
                detail="A source run is not owned by this dataset.",
            )
        if require_completed_source_runs and str(run.get("status")) != "completed":
            raise HTTPException(
                status_code=422,
                detail="A review session source run must be completed.",
            )
    for layer_id in target_layer_ids:
        _require_layer(request, dataset_id, layer_id)
    known_tracks = _dataset_track_ids(dataset)
    if any(track_id not in known_tracks for track_id in track_ids):
        raise HTTPException(
            status_code=422,
            detail="A track is not owned by this dataset.",
        )
    frame_range = values.get("frame_range")
    if frame_range is not None:
        bounds = request.app.state.store.frame_ordinal_bounds(dataset_id)
        if (
            bounds is None
            or int(frame_range[0]) < bounds[0]
            or int(frame_range[1]) > bounds[1]
        ):
            raise HTTPException(
                status_code=422,
                detail="frame_range is outside this dataset.",
            )


def _reconcile_session(request: Request, session: dict[str, Any]) -> dict[str, int]:
    try:
        return reconcile_session_task_resolutions(request.app, session)
    except (OSError, TypeError, ValueError, sqlite3.Error):
        request.app.state.logger.warning(
            "Review task transition reconciliation failed for session %s.",
            session["id"],
        )
        return {
            "pending": 0,
            "error": 0,
            "reconciled": 0,
            "attempted": 0,
            "truncated": 1,
        }


def _resolution_blockers(summary: dict[str, int]) -> dict[str, int]:
    return {
        "pending_task_resolutions": int(summary["pending"]),
        "task_resolution_errors": int(summary["error"]),
        "task_resolution_scan_truncated": int(bool(summary["truncated"])),
    }


def _require_session(request: Request, session_id: str) -> dict[str, Any]:
    session = request.app.state.store.get_review_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Review session not found.")
    _reconcile_session(request, session)
    return session


def _require_task(request: Request, task_id: str) -> dict[str, Any]:
    task = request.app.state.store.get_review_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Review task not found.")
    return task


def _require_active_session(session: dict[str, Any]) -> None:
    session_status = str(session["status"])
    if session_status in {
        ReviewSessionStatus.COMPLETED.value,
        ReviewSessionStatus.ARCHIVED.value,
    }:
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )
    if session_status != ReviewSessionStatus.ACTIVE.value:
        raise HTTPException(
            status_code=409,
            detail="Review tasks can only be changed while the session is active.",
        )


def _require_active_task_session(
    request: Request,
    task: dict[str, Any],
) -> None:
    session = _require_session(request, str(task["session_id"]))
    _require_active_session(session)


def _validate_task_scope(
    request: Request,
    session: dict[str, Any],
    payload: ReviewTaskCreate,
) -> dict[str, Any]:
    values = payload.model_dump(mode="json")
    dataset_id = str(session["dataset_id"])
    dataset = request.app.state.store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    frame = None
    if values["frame_id"] is not None:
        frame = request.app.state.store.get_frame(dataset_id, values["frame_id"])
        if frame is None:
            raise HTTPException(
                status_code=422,
                detail="Frame is not owned by the review session dataset.",
            )
        if values["track_id"] is None:
            values["track_id"] = str(frame["track_id"])
        elif values["track_id"] != str(frame["track_id"]):
            raise HTTPException(
                status_code=422,
                detail="frame_id and track_id belong to different tracks.",
            )
        frame_range = session.get("frame_range")
        if frame_range is not None and not (
            int(frame_range[0]) <= int(frame["ordinal"]) <= int(frame_range[1])
        ):
            raise HTTPException(
                status_code=422,
                detail="Frame is outside the review session frame range.",
            )
    if values["track_id"] is not None:
        known_tracks = _dataset_track_ids(dataset)
        if values["track_id"] not in known_tracks:
            raise HTTPException(
                status_code=422,
                detail="Track is not owned by the review session dataset.",
            )
        if session["track_ids"] and values["track_id"] not in session["track_ids"]:
            raise HTTPException(
                status_code=422,
                detail="Track is outside the review session scope.",
            )
    frame_start = values.get("frame_start")
    frame_end = values.get("frame_end")
    if frame_start is not None and frame_end is not None:
        if values["task_type"] != ReviewTaskType.UNREVIEWED_INTERVAL.value:
            raise HTTPException(
                status_code=422,
                detail="A frame span is only valid for an UNREVIEWED_INTERVAL task.",
            )
        if values["track_id"] is None:
            raise HTTPException(
                status_code=422,
                detail="An interval task frame span requires a track_id.",
            )
        bounds = request.app.state.store.frame_ordinal_bounds(dataset_id)
        if bounds is None or int(frame_start) < bounds[0] or int(frame_end) > bounds[1]:
            raise HTTPException(
                status_code=422,
                detail="Task frame span is outside this dataset.",
            )
        session_range = session.get("frame_range")
        if session_range is not None and (
            int(frame_start) < int(session_range[0])
            or int(frame_end) > int(session_range[1])
        ):
            raise HTTPException(
                status_code=422,
                detail="Task frame span is outside the review session frame range.",
            )
        if frame is not None and not (
            int(frame_start) <= int(frame["ordinal"]) <= int(frame_end)
        ):
            raise HTTPException(
                status_code=422,
                detail="The task anchor frame is outside its frame span.",
            )
    if values["source_detection_id"] is not None and values["source_run_id"] is None:
        raise HTTPException(
            status_code=422,
            detail="source_detection_id requires source_run_id provenance.",
        )
    if values["source_run_id"] is not None:
        run = request.app.state.store.get_run(values["source_run_id"])
        if run is None or str(run["dataset_id"]) != dataset_id:
            raise HTTPException(
                status_code=422,
                detail="Source run is not owned by the review session dataset.",
            )
        if str(run.get("status")) != "completed":
            raise HTTPException(
                status_code=422,
                detail="Source run must be completed before it can back a review task.",
            )
        if (
            session["source_run_ids"]
            and values["source_run_id"] not in session["source_run_ids"]
        ):
            raise HTTPException(
                status_code=422,
                detail="Source run is outside the review session scope.",
            )
    if values["target_layer_id"] is not None:
        _require_layer(request, dataset_id, values["target_layer_id"])
        if (
            session["target_layer_ids"]
            and values["target_layer_id"] not in session["target_layer_ids"]
        ):
            raise HTTPException(
                status_code=422,
                detail="Target layer is outside the review session scope.",
            )
    return values


def _new_task(
    session: dict[str, Any],
    values: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    return {
        "id": f"rvt_{uuid.uuid4().hex}",
        "session_id": session["id"],
        "dataset_id": session["dataset_id"],
        **values,
        "status": ReviewTaskStatus.TODO.value,
        "resolved_feature_ids": [],
        "resolution": None,
        "created_at": now,
        "updated_at": now,
    }


def _create_review_session_locked(
    dataset_id: str,
    payload: ReviewSessionCreate,
    request: Request,
) -> dict[str, Any]:
    dataset = require_ready_dataset(request, dataset_id)
    values = payload.model_dump(mode="json")
    if values["status"] not in {
        ReviewSessionStatus.DRAFT.value,
        ReviewSessionStatus.ACTIVE.value,
    }:
        raise HTTPException(
            status_code=422,
            detail="A review session must be created as draft or active.",
        )
    _validate_session_scope(
        request,
        dataset,
        values,
        require_completed_source_runs=True,
    )
    now = utc_now()
    session = request.app.state.store.create_review_session(
        {
            "id": f"rvw_{uuid.uuid4().hex}",
            "dataset_id": dataset_id,
            **values,
            "last_task_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    return {"session": _public_session(session)}


@router.post(
    "/datasets/{dataset_id}/review-sessions",
    status_code=status.HTTP_201_CREATED,
)
async def create_review_session(
    dataset_id: str,
    payload: ReviewSessionCreate,
    request: Request,
) -> dict[str, Any]:
    async with review_dataset_lock(request.app, dataset_id):
        return _create_review_session_locked(dataset_id, payload, request)


@router.get("/datasets/{dataset_id}/review-sessions")
def list_review_sessions(
    dataset_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session_status: Annotated[ReviewSessionStatus | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    items, total = request.app.state.store.list_review_sessions(
        dataset_id,
        offset=offset,
        limit=limit,
        status=session_status.value if session_status is not None else None,
    )
    for item in items:
        _reconcile_session(request, item)
    return _page(
        [_public_session(item) for item in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/review-sessions/{session_id}")
def get_review_session(session_id: str, request: Request) -> dict[str, Any]:
    return {"session": _public_session(_require_session(request, session_id))}


def _patch_review_session_locked(
    session_id: str,
    payload: ReviewSessionPatch,
    request: Request,
) -> dict[str, Any]:
    session = _require_session(request, session_id)
    fields = payload.model_dump(mode="json", exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="Review session patch is empty.")
    for name in ("source_run_ids", "target_layer_ids", "track_ids", "class_filters"):
        if name in fields and fields[name] is None:
            raise HTTPException(status_code=422, detail=f"{name} cannot be null.")
    scope_fields = {
        "source_run_ids",
        "target_layer_ids",
        "track_ids",
        "frame_range",
        "class_filters",
    }.intersection(fields)
    if scope_fields and (
        str(session["status"]) != ReviewSessionStatus.DRAFT.value
        or sum(request.app.state.store.review_task_status_counts(session_id).values())
        > 0
    ):
        raise HTTPException(
            status_code=409,
            detail="Review scope is immutable after work starts.",
        )
    requested_status: str | None = None
    if "status" in fields:
        if fields["status"] is None:
            raise HTTPException(status_code=422, detail="status cannot be null.")
        current_status = str(session["status"])
        requested_status = str(fields["status"])
        if (
            requested_status != current_status
            and requested_status not in _SESSION_TRANSITIONS[current_status]
        ):
            raise HTTPException(
                status_code=409,
                detail="Invalid review session status transition.",
            )
    if "last_task_id" in fields and fields["last_task_id"] is not None:
        task = request.app.state.store.get_review_task(fields["last_task_id"])
        if task is None or str(task["session_id"]) != session_id:
            raise HTTPException(
                status_code=422,
                detail="last_task_id is not owned by this review session.",
            )
    merged = {
        **session,
        **{
            name: value
            for name, value in fields.items()
            if name
            in {
                "source_run_ids",
                "target_layer_ids",
                "track_ids",
                "frame_range",
                "class_filters",
            }
        },
    }
    dataset = require_ready_dataset(request, str(session["dataset_id"]))
    _validate_session_scope(
        request,
        dataset,
        merged,
        # Existing registries may contain pre-P1 sessions that reference a run
        # which was not terminal when the session was stored.  Preserve those
        # rows for status/last-position updates, while narrowing every explicit
        # source scope write to the completed-run product contract.
        require_completed_source_runs="source_run_ids" in fields,
    )
    current_layer_revisions: dict[str, int | None] | None = None
    reconciliation = _reconcile_session(request, session)
    resolution_blockers = _resolution_blockers(reconciliation)
    if requested_status == ReviewSessionStatus.COMPLETED.value:
        current_layer_revisions = _current_target_layer_revisions(request, session)
        blockers = request.app.state.store.review_session_completion_blockers(
            session_id,
            current_layer_revisions=current_layer_revisions,
        )
        blockers.update(resolution_blockers)
        if any(blockers.values()):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Review session completion requirements are not met.",
                    "blockers": blockers,
                },
            )
    outcome, updated = request.app.state.store.update_review_session(
        session_id,
        expected_status=str(session["status"]),
        now=utc_now(),
        fields=fields,
        current_layer_revisions=current_layer_revisions,
        pending_resolution_blockers=sum(resolution_blockers.values()),
    )
    if outcome == "missing" or updated is None:
        raise HTTPException(status_code=404, detail="Review session not found.")
    if outcome == "stale":
        raise HTTPException(
            status_code=409,
            detail="Review session changed concurrently; reload and retry.",
        )
    if outcome == "scope_locked":
        raise HTTPException(
            status_code=409,
            detail="Review scope is immutable after work starts.",
        )
    if outcome == "blocked":
        current_layer_revisions = _current_target_layer_revisions(request, session)
        blockers = request.app.state.store.review_session_completion_blockers(
            session_id,
            current_layer_revisions=current_layer_revisions,
        )
        blockers.update(
            _resolution_blockers(_reconcile_session(request, session))
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Review session completion requirements are not met.",
                "blockers": blockers,
            },
        )
    if str(updated["status"]) == ReviewSessionStatus.ACTIVE.value:
        _reconcile_session(request, updated)
    return {"session": _public_session(updated)}


@router.patch("/review-sessions/{session_id}")
async def patch_review_session(
    session_id: str,
    payload: ReviewSessionPatch,
    request: Request,
) -> dict[str, Any]:
    async with review_session_lock(request.app, session_id):
        # The locked implementation deliberately reloads the session before
        # computing blockers/CAS state, fencing concurrent feature history.
        return _patch_review_session_locked(session_id, payload, request)


@router.post(
    "/review-sessions/{session_id}/tasks",
    status_code=status.HTTP_201_CREATED,
)
def create_manual_review_task(
    session_id: str,
    payload: ReviewTaskCreate,
    request: Request,
) -> dict[str, Any]:
    session = _require_session(request, session_id)
    _require_active_session(session)
    if payload.task_type is not ReviewTaskType.MANUAL_SCAN:
        raise HTTPException(
            status_code=422,
            detail="The manual task endpoint only creates MANUAL_SCAN tasks.",
        )
    values = _validate_task_scope(request, session, payload)
    task = request.app.state.store.create_review_task(
        _new_task(session, values, utc_now())
    )
    return {"task": _public_task(task)}


@router.post("/review-sessions/{session_id}/tasks/generate")
def generate_review_tasks(
    session_id: str,
    payload: ReviewTaskGenerateRequest | ReviewTaskCreate,
    request: Request,
) -> dict[str, Any]:
    session = _require_session(request, session_id)
    _require_active_session(session)
    task_payloads = (
        payload.tasks if isinstance(payload, ReviewTaskGenerateRequest) else [payload]
    )
    now = utc_now()
    if task_payloads:
        records = [
            _new_task(session, _validate_task_scope(request, session, item), now)
            for item in task_payloads
        ]
        items = [
            _public_task(item)
            for item in request.app.state.store.create_review_tasks(records)
        ]
        return {
            "items": items,
            "created": len(items),
            "existing": 0,
            "source_counts": {},
        }

    if not isinstance(payload, ReviewTaskGenerateRequest):  # pragma: no cover
        raise HTTPException(status_code=422, detail="Task generation request is empty.")
    settings = CandidateSourceSettings(
        **payload.sources.model_dump(mode="python"),
        low_confidence_threshold=payload.low_confidence_threshold,
        unreviewed_interval_frames=payload.unreviewed_interval_frames,
    )
    frames = _scoped_frames(request, session)
    artifacts, discovery = _artifact_payloads(request, session, frames)
    target_layers = list(session.get("target_layer_ids", []))
    records = generate_review_candidates(
        session_id=session_id,
        dataset_id=str(session["dataset_id"]),
        artifacts=artifacts,
        frames=frames,
        reviewed_frame_ids=request.app.state.store.reviewed_frame_ids(session_id),
        settings=settings,
        target_layer_id=str(target_layers[0]) if len(target_layers) == 1 else None,
        class_filters=session.get("class_filters", []),
        max_candidates=_MAX_GENERATED_TASKS,
    )
    if len(records) >= _MAX_GENERATED_TASKS:
        discovery["truncated"] = True
    for record in records:
        record.update(
            {
                "status": ReviewTaskStatus.TODO.value,
                "resolved_feature_ids": [],
                "resolution": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    persisted, created_count = request.app.state.store.create_review_tasks_idempotent(
        records
    )
    source_counts: dict[str, int] = {}
    for item in persisted:
        source = str(item["task_type"])
        source_counts[source] = source_counts.get(source, 0) + 1
    # Queue listing is the paginated surface for large sessions.  Keep this
    # mutation response bounded while still returning exact aggregate counts.
    public_items = [_public_task(item) for item in persisted[:500]]
    return {
        "items": public_items,
        "created": created_count,
        "existing": len(persisted) - created_count,
        "total_candidates": len(persisted),
        "returned": len(public_items),
        "source_counts": source_counts,
        "sources": settings.public_sources(),
        "discovery": discovery,
    }


@router.get("/review-sessions/{session_id}/tasks")
def list_review_tasks(
    session_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    task_status: Annotated[ReviewTaskStatus | None, Query(alias="status")] = None,
    task_type: Annotated[ReviewTaskType | None, Query()] = None,
    cursor: str | None = Query(None, max_length=_MAX_QUEUE_CURSOR_LENGTH),
) -> dict[str, Any]:
    _require_session(request, session_id)
    if cursor is not None and offset:
        raise HTTPException(
            status_code=422, detail="cursor and non-zero offset cannot be combined."
        )
    after = None if cursor is None else _decode_task_cursor(cursor)
    items, total = request.app.state.store.list_review_tasks(
        session_id,
        offset=0 if after is not None else offset,
        limit=limit + 1,
        status=task_status.value if task_status is not None else None,
        task_type=task_type.value if task_type is not None else None,
        after=after,
        include_queue_priority=True,
    )
    has_more = len(items) > limit
    page_items = items[:limit]
    page = _page(
        [_public_task(item) for item in page_items],
        total=total,
        offset=0 if after is not None else offset,
        limit=limit,
    )
    if after is not None:
        page["next_offset"] = None
    page["next_cursor"] = (
        _encode_task_cursor(page_items[-1]) if has_more and page_items else None
    )
    page["status_counts"] = request.app.state.store.review_task_status_counts(
        session_id
    )
    return page


@router.get("/review-tasks/{task_id}")
def get_review_task(task_id: str, request: Request) -> dict[str, Any]:
    return {"task": _public_task(_require_task(request, task_id))}


@router.patch("/review-tasks/{task_id}")
def patch_review_task(
    task_id: str,
    payload: ReviewTaskPatch,
    request: Request,
) -> dict[str, Any]:
    task = _require_task(request, task_id)
    _require_active_task_session(request, task)
    current_status = str(task["status"])
    if current_status in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Terminal review tasks can only be changed through reopen.",
        )
    fields = payload.model_dump(mode="json", exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="Review task patch is empty.")
    requested_status = fields.get("status", current_status)
    if requested_status is None:
        raise HTTPException(status_code=422, detail="status cannot be null.")
    requested_status = str(requested_status)
    valid_transition = (
        requested_status == current_status
        or (
            current_status == ReviewTaskStatus.TODO.value
            and requested_status == ReviewTaskStatus.IN_PROGRESS.value
        )
        or (
            current_status == ReviewTaskStatus.IN_PROGRESS.value
            and requested_status in _TERMINAL_TASK_STATUSES
        )
    )
    if not valid_transition:
        raise HTTPException(
            status_code=409,
            detail="Invalid review task status transition.",
        )
    if requested_status in _TERMINAL_TASK_STATUSES:
        if requested_status in {
            ReviewTaskStatus.CORRECTED.value,
            ReviewTaskStatus.MANUAL_ADDED.value,
        }:
            raise HTTPException(
                status_code=422,
                detail=(
                    "corrected/manual_added require the resolve endpoint with "
                    "a committed linked feature."
                ),
            )
        fields["resolution"] = requested_status
    outcome, updated = request.app.state.store.update_review_task(
        task_id,
        expected_status=current_status,
        now=utc_now(),
        fields=fields,
        event_type="patched",
        actor=fields.get("claimed_by", task.get("claimed_by")),
        set_session_last_task=requested_status == ReviewTaskStatus.IN_PROGRESS.value,
    )
    if outcome == "missing" or updated is None:
        raise HTTPException(status_code=404, detail="Review task not found.")
    if outcome == "stale":
        raise HTTPException(
            status_code=409,
            detail="Review task changed concurrently; reload and retry.",
        )
    if outcome == "immutable":
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )
    if outcome == "inactive":
        raise HTTPException(
            status_code=409,
            detail="Review tasks can only be changed while the session is active.",
        )
    return {"task": _public_task(updated)}


@router.post("/review-tasks/{task_id}/resolve")
def resolve_review_task(
    task_id: str,
    payload: ReviewTaskResolve,
    request: Request,
) -> dict[str, Any]:
    task = _require_task(request, task_id)
    _require_active_task_session(request, task)
    if str(task["status"]) != ReviewTaskStatus.IN_PROGRESS.value:
        raise HTTPException(
            status_code=409,
            detail="Only an in-progress review task can be resolved.",
        )
    if payload.resolved_feature_ids:
        if task.get("target_layer_id") is None:
            raise HTTPException(
                status_code=422,
                detail="Resolved features require a target layer.",
            )
        _require_active_features(
            request,
            str(task["dataset_id"]),
            str(task["target_layer_id"]),
            list(payload.resolved_feature_ids),
        )
    resolution = payload.resolution.value
    resolved_feature_ids = list(payload.resolved_feature_ids)
    linkage_error = review_resolution_feature_error(
        request.app, task, resolution, resolved_feature_ids
    )
    if linkage_error is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "manual_added/corrected require active features committed "
                    "by this task with matching provenance."
                ),
                "code": linkage_error,
            },
        )
    outcome, updated = request.app.state.store.resolve_review_task(
        task_id,
        resolution=resolution,
        resolved_feature_ids=resolved_feature_ids,
        now=utc_now(),
        actor=task.get("claimed_by"),
    )
    if outcome == "missing" or updated is None:
        raise HTTPException(status_code=404, detail="Review task not found.")
    if outcome == "stale":
        raise HTTPException(
            status_code=409,
            detail="Review task changed concurrently; reload and retry.",
        )
    if outcome == "immutable":
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )
    if outcome == "inactive":
        raise HTTPException(
            status_code=409,
            detail="Review tasks can only be changed while the session is active.",
        )
    return {"task": _public_task(updated)}


@router.post("/review-tasks/{task_id}/reopen")
def reopen_review_task(task_id: str, request: Request) -> dict[str, Any]:
    task = _require_task(request, task_id)
    _require_active_task_session(request, task)
    current_status = str(task["status"])
    if current_status not in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Only a terminal review task can be reopened.",
        )
    outcome, updated = request.app.state.store.update_review_task(
        task_id,
        expected_status=current_status,
        now=utc_now(),
        fields={
            "status": ReviewTaskStatus.TODO.value,
            "resolution": None,
            "resolved_feature_ids": [],
            "claimed_by": None,
        },
        event_type="reopened",
        actor=task.get("claimed_by"),
    )
    if outcome == "missing" or updated is None:
        raise HTTPException(status_code=404, detail="Review task not found.")
    if outcome == "stale":
        raise HTTPException(
            status_code=409,
            detail="Review task changed concurrently; reload and retry.",
        )
    if outcome == "immutable":
        raise HTTPException(
            status_code=409,
            detail="Completed or archived review sessions are read-only.",
        )
    if outcome == "inactive":
        raise HTTPException(
            status_code=409,
            detail="Review tasks can only be changed while the session is active.",
        )
    return {"task": _public_task(updated)}


__all__ = ["router"]
