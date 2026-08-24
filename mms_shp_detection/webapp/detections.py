from __future__ import annotations

import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response

from mms_shp_detection.geometry import (
    apply_panorama_angular_offsets,
    perspective_pixel_to_world_ray,
    world_ray_to_equirectangular_pixel,
)
from mms_shp_detection.shp_writer import make_detection_id

from .datasets import require_ready_dataset
from .overlays import resolve_detection_overlay_features
from .security import UnsafePath, normalize_relative_path, opaque_id, resolve_under_root

router = APIRouter(prefix="/api", tags=["detections"])

MAX_COMPLETED_RUNS = 20
MAX_MODEL_COUNT = 64
MAX_MODELS_MANIFEST_BYTES = 5 * 1024**2
MAX_DETECTION_RESULT_BYTES = 16 * 1024**2
MAX_DETECTION_REQUEST_BYTES = 64 * 1024**2
MAX_DETECTIONS_PER_FRAME = 2_000
MODEL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DETECTION_SOURCE_ID = re.compile(r"^det-src_[0-9a-f]{32}$")
DETECTION_OBSERVATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
EQUIRECTANGULAR_BBOX_SPACES = {
    "equirectangular_pixels",
    "panorama_equirectangular_pixels",
}
FORWARD_BBOX_SPACES = {
    "forward_rectilinear_pixels",
    "rectilinear_forward_pixels",
}


def _image_name(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .split("?", 1)[0]
        .split("#", 1)[0]
        .replace("\\", "/")
        .rsplit("/", 1)[-1]
        .casefold()
    )


def _safe_result_component(value: Any) -> str | None:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or "\x00" in text
    ):
        return None
    return text


def _read_json_object(path: Path, maximum_bytes: int) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = int(path.stat().st_size)
        if size <= 0 or size > maximum_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_regular_file_size(path: Path, maximum_bytes: int) -> int | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = int(path.stat().st_size)
    except (OSError, ValueError):
        return None
    return size if 0 < size <= maximum_bytes else None


def _model_keys(
    output_dir: Path,
    *,
    byte_budget: int,
) -> tuple[list[str], int, bool]:
    """Read bounded, relative model keys without trusting manifest paths."""

    manifest_path = output_dir / "models_manifest.json"
    manifest_bytes = _bounded_regular_file_size(
        manifest_path, MAX_MODELS_MANIFEST_BYTES
    )
    if manifest_bytes is not None and manifest_bytes > byte_budget:
        return [], 0, True
    consumed_bytes = manifest_bytes or 0
    manifest = (
        _read_json_object(manifest_path, MAX_MODELS_MANIFEST_BYTES)
        if manifest_bytes is not None
        else None
    )
    keys: set[str] = set()
    models = manifest.get("models") if manifest is not None else None
    if isinstance(models, list) and len(models) > MAX_MODEL_COUNT:
        return [], consumed_bytes, True
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            key = str(model.get("model_key") or "").strip()
            if MODEL_KEY.fullmatch(key) and model.get("status") in {None, "completed"}:
                keys.add(key)
    if keys:
        return sorted(keys, key=str.casefold), consumed_bytes, False

    # Legacy result folders did not always publish models_manifest.json.  The
    # output directory is server-managed, but still keep discovery bounded and
    # reject links before deriving an exact TXT path.
    try:
        children: list[Path] = []
        for child in output_dir.iterdir():
            if (
                not child.is_symlink()
                and child.is_dir()
                and MODEL_KEY.fullmatch(child.name)
            ):
                if len(children) >= MAX_MODEL_COUNT:
                    return [], consumed_bytes, True
                children.append(child)
    except OSError:
        return [], consumed_bytes, False
    return (
        sorted({child.name for child in children}, key=str.casefold),
        consumed_bytes,
        False,
    )


def _finite_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def _finite_positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _unwrap_x(values: list[float], width: float) -> list[float]:
    angles = np.asarray(values, dtype=np.float64) * (2.0 * math.pi / width)
    mean_angle = math.atan2(float(np.sin(angles).mean()), float(np.cos(angles).mean()))
    reference = (mean_angle % (2.0 * math.pi)) * width / (2.0 * math.pi)
    return [float(value + round((reference - value) / width) * width) for value in values]


def _forward_bbox_to_panorama(
    bbox: list[float],
    detection: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[list[float], float, float] | None:
    """Inverse-map an explicitly labelled forward-view box when metadata is complete."""

    metadata = payload.get("panorama_detection")
    alignment = payload.get("panorama_alignment")
    panorama = payload.get("panorama")
    if not all(isinstance(value, dict) for value in (metadata, alignment, panorama)):
        return None
    if str(metadata.get("mode") or "").casefold() != "forward":
        return None
    view_width = _finite_positive(metadata.get("forward_view_width_px"))
    view_height = _finite_positive(metadata.get("forward_view_height_px"))
    hfov = _finite_positive(metadata.get("forward_view_hfov_deg"))
    vfov = _finite_positive(metadata.get("forward_view_vfov_deg"))
    panorama_width = _finite_positive(
        detection.get("panorama_width") or panorama.get("image_width")
    )
    panorama_height = _finite_positive(
        detection.get("panorama_height") or panorama.get("image_height")
    )
    try:
        yaw = float(alignment["yaw_offset_deg"])
        pitch = float(alignment["pitch_offset_deg"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        None in {view_width, view_height, hfov, vfov, panorama_width, panorama_height}
        or not math.isfinite(yaw)
        or not math.isfinite(pitch)
        or not 0 < float(hfov) < 180
        or not 0 < float(vfov) < 180
    ):
        return None

    view_forward = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    view_right = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
    view_up = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    pano_forward, pano_right, pano_up = apply_panorama_angular_offsets(
        view_forward,
        view_right,
        view_up,
        yaw_offset_deg=yaw,
        pitch_offset_deg=pitch,
    )
    x1, y1, x2, y2 = bbox
    # Sampling edge midpoints as well as corners prevents a curved perspective
    # edge from falling outside its equirectangular envelope.
    source_points = [
        (x1, y1),
        ((x1 + x2) * 0.5, y1),
        (x2, y1),
        (x2, (y1 + y2) * 0.5),
        (x2, y2),
        ((x1 + x2) * 0.5, y2),
        (x1, y2),
        (x1, (y1 + y2) * 0.5),
    ]
    mapped: list[tuple[float, float]] = []
    for pixel_x, pixel_y in source_points:
        ray = perspective_pixel_to_world_ray(
            pixel_x,
            pixel_y,
            int(view_width),
            int(view_height),
            view_forward,
            view_right,
            view_up,
            float(hfov),
            float(vfov),
        )
        pano_x, pano_y = world_ray_to_equirectangular_pixel(
            ray,
            pano_forward,
            pano_right,
            pano_up,
            int(panorama_width),
            int(panorama_height),
        )
        mapped.append((float(pano_x), float(pano_y)))
    unwrapped_x = _unwrap_x([point[0] for point in mapped], float(panorama_width))
    return (
        [
            min(unwrapped_x),
            min(point[1] for point in mapped),
            max(unwrapped_x),
            max(point[1] for point in mapped),
        ],
        float(panorama_width),
        float(panorama_height),
    )


def _panorama_bbox(
    detection: dict[str, Any], payload: dict[str, Any]
) -> tuple[list[float], float, float, str] | None:
    bbox = _finite_bbox(detection.get("bbox_xyxy"))
    if bbox is None:
        return None
    space = str(
        detection.get("bbox_coordinate_space")
        or payload.get("detection_bbox_coordinate_space")
        or ""
    ).strip().casefold()
    if not space:
        # Schema 17 is the first persisted format whose forward/tiled detector
        # boxes were already inverse-mapped into source panorama pixels.
        try:
            schema_version = int(payload.get("schema_version"))
        except (TypeError, ValueError):
            return None
        if schema_version < 17:
            return None
        space = "panorama_equirectangular_pixels"
    if space in FORWARD_BBOX_SPACES:
        converted = _forward_bbox_to_panorama(bbox, detection, payload)
        if converted is None:
            return None
        return (*converted, "forward_rectilinear_inverse_v1")
    if space not in EQUIRECTANGULAR_BBOX_SPACES:
        return None
    panorama = payload.get("panorama")
    if not isinstance(panorama, dict):
        panorama = {}
    width = _finite_positive(
        detection.get("panorama_width") or panorama.get("image_width")
    )
    height = _finite_positive(
        detection.get("panorama_height") or panorama.get("image_height")
    )
    if width is None or height is None:
        return None
    return bbox, width, height, "pipeline_equirectangular_v1"


def _display_model_name(payload: dict[str, Any], model_key: str) -> str:
    raw_name = str(payload.get("model_name") or model_key).strip().replace("\\", "/")
    return (raw_name.rsplit("/", 1)[-1] or model_key)[:120]


def _public_scalar(value: Any, *, maximum_length: int = 160) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:maximum_length]
    return None


def _accepted_dataset_position(detection: dict[str, Any]) -> list[float] | None:
    """Expose only the pipeline-accepted 3-D representative point.

    A 2-D panorama box alone has no trustworthy depth. Rejected candidate
    coordinates therefore remain private rather than being presented as an
    observed point in the 3-D viewer.
    """

    if detection.get("accepted_for_shp") is not True:
        return None
    try:
        position = [float(detection[axis]) for axis in ("x", "y", "z")]
    except (KeyError, TypeError, ValueError):
        return None
    return position if all(math.isfinite(value) for value in position) else None


def _items_from_payload(
    payload: dict[str, Any],
    *,
    frame_task: dict[str, Any],
    run_id: str,
    model_key: str,
    remaining: int,
) -> tuple[list[dict[str, Any]], bool]:
    detections = payload.get("detections")
    if not isinstance(detections, list):
        return [], False
    if remaining <= 0:
        return [], bool(detections)
    image_name = str(frame_task.get("image_name") or "").strip()
    record_name = str(frame_task.get("record_name") or "").strip()
    source_id = opaque_id("det-src", run_id, model_key, length=32)
    model_id = opaque_id("det-model", model_key, length=32)
    source_name = _display_model_name(payload, model_key)
    items: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for ordinal, detection in enumerate(detections, start=1):
        if len(items) >= remaining:
            return items, True
        if not isinstance(detection, dict):
            continue
        detection_image = str(
            detection.get("image_name") or payload.get("image_name") or image_name
        )
        if _image_name(detection_image) != _image_name(image_name):
            continue
        converted = _panorama_bbox(detection, payload)
        if converted is None:
            continue
        bbox, panorama_width, panorama_height, mapping = converted
        try:
            # Detection indices are zero-based in current pipeline artifacts.
            # ``0 or ordinal`` collapsed the first and second observations to
            # the same stable ID when the second observation had index 1.
            detection_index = int(detection.get("detection_index", ordinal))
        except (TypeError, ValueError, OverflowError):
            detection_index = ordinal
        detection_id = make_detection_id(record_name, detection_image, detection_index)
        identity = (
            source_id,
            detection_id.casefold(),
            *(round(value, 6) for value in bbox),
            round(panorama_width, 6),
            round(panorama_height, 6),
        )
        if identity in seen:
            continue
        seen.add(identity)
        dataset_position = _accepted_dataset_position(detection)
        properties = {
            "class_id": _public_scalar(detection.get("class_id")),
            "class_nm": _public_scalar(detection.get("class_name")),
            "conf": _public_scalar(detection.get("confidence")),
            "det_id": detection_id,
            "det_index": detection_index,
            "img_name": detection_image,
            "model_nm": source_name,
            "bbox_l": bbox[0],
            "bbox_t": bbox[1],
            "bbox_r": bbox[2],
            "bbox_b": bbox[3],
            "pano_w": panorama_width,
            "pano_h": panorama_height,
            "bbox_space": "panorama_equirectangular_pixels",
            "bbox_mapping": mapping,
            "accepted": _public_scalar(detection.get("accepted_for_shp")),
            "support_id": _public_scalar(detection.get("support_id")),
            **(
                {
                    "x": dataset_position[0],
                    "y": dataset_position[1],
                    "z": dataset_position[2],
                }
                if dataset_position is not None
                else {}
            ),
        }
        items.append(
            {
                "source_id": source_id,
                "model_id": model_id,
                "source_name": source_name,
                "observation_id": detection_id,
                "properties": properties,
                **({"dataset_position": dataset_position} if dataset_position else {}),
            }
        )
    return items, False


def _frame_payloads_for_run(
    app: Any,
    run: dict[str, Any],
    frame_task: dict[str, Any],
    *,
    byte_budget: int,
) -> tuple[list[tuple[str, dict[str, Any]]], int, bool]:
    record_name = _safe_result_component(frame_task.get("record_name"))
    image_name = str(frame_task.get("image_name") or "").strip()
    image_stem = _safe_result_component(
        frame_task.get("image_stem") or Path(image_name).stem
    )
    if record_name is None or image_stem is None or not image_name:
        return [], 0, False
    try:
        runs_root = app.state.config.state_dir / "runs"
        work_dir = resolve_under_root(
            runs_root,
            run["work_relative"],
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
        return [], 0, False
    payloads: list[tuple[str, dict[str, Any]]] = []
    model_keys, consumed_bytes, manifest_truncated = _model_keys(
        output_dir,
        byte_budget=byte_budget,
    )
    if manifest_truncated:
        return [], consumed_bytes, True
    for model_key in model_keys:
        try:
            relative = normalize_relative_path(
                PurePosixPath(
                    model_key, "txt", record_name, f"{image_stem}.txt"
                ).as_posix(),
                allow_empty=False,
            )
            path = resolve_under_root(
                output_dir,
                relative,
                must_exist=True,
                expect_directory=False,
                reject_symlinks=True,
            )
        except (FileNotFoundError, OSError, TypeError, UnsafePath, ValueError):
            continue
        result_bytes = _bounded_regular_file_size(path, MAX_DETECTION_RESULT_BYTES)
        if result_bytes is None:
            # An exact newest-run artifact exists but cannot be consumed safely
            # (empty, oversized, or changed after resolution). Do not silently
            # replace it with stale observations from an older completed run.
            return payloads, consumed_bytes, True
        if consumed_bytes + result_bytes > byte_budget:
            return payloads, consumed_bytes, True
        # Invalid and identity-mismatched files count against the request-wide
        # parsing budget too. This keeps an adversarial completed run from
        # causing unbounded fallback scans through older runs.
        consumed_bytes += result_bytes
        payload = _read_json_object(path, MAX_DETECTION_RESULT_BYTES)
        if payload is None:
            return payloads, consumed_bytes, True
        if (
            str(payload.get("record_name") or "") != record_name
            or _image_name(payload.get("image_name")) != _image_name(image_name)
        ):
            return payloads, consumed_bytes, True
        payloads.append((model_key, payload))
    return payloads, consumed_bytes, False


@router.get("/datasets/{dataset_id}/frames/{frame_id}/detections")
def frame_detections(
    dataset_id: str,
    frame_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return every model's 2-D YOLO boxes for the newest matching completed run."""

    require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    runs = request.app.state.store.list_completed_runs_for_dataset(
        dataset_id, limit=MAX_COMPLETED_RUNS
    )
    payloads: list[tuple[str, dict[str, Any]]] = []
    matched_run: dict[str, Any] | None = None
    remaining_json_bytes = MAX_DETECTION_REQUEST_BYTES
    payload_scan_truncated = False
    for run in runs:
        payloads, consumed_bytes, payload_scan_truncated = _frame_payloads_for_run(
            request.app,
            run,
            frame["task"],
            byte_budget=remaining_json_bytes,
        )
        remaining_json_bytes -= consumed_bytes
        if payloads:
            matched_run = run
            break
        if payload_scan_truncated:
            break

    items: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    items_truncated = False
    if matched_run is not None:
        for model_key, payload in payloads:
            model_items, model_truncated = _items_from_payload(
                payload,
                frame_task=frame["task"],
                run_id=str(matched_run["id"]),
                model_key=model_key,
                remaining=MAX_DETECTIONS_PER_FRAME - len(items),
            )
            items.extend(model_items)
            items_truncated = items_truncated or model_truncated
            models.append(
                {
                    "model_id": opaque_id("det-model", model_key, length=32),
                    "source_id": opaque_id(
                        "det-src", str(matched_run["id"]), model_key, length=32
                    ),
                    "source_name": _display_model_name(payload, model_key),
                    "count": len(model_items),
                }
            )
    resolution_by_observation = resolve_detection_overlay_features(
        request.app,
        dataset_id,
        [
            (str(item["source_id"]), str(item["observation_id"]))
            for item in items
        ],
    )
    visible_items: list[dict[str, Any]] = []
    suppressed_count = 0
    for item in items:
        key = (
            str(item["source_id"]).strip().casefold(),
            str(item["observation_id"]).strip().casefold(),
        )
        resolution = resolution_by_observation[key]
        if resolution["status"] == "deleted":
            suppressed_count += 1
            continue
        item["overlay_resolution"] = resolution["status"]
        item["overlay_candidate_count"] = resolution["candidate_count"]
        match = resolution["match"]
        if match is not None:
            item["layer_id"] = match["layer_id"]
            item["feature_id"] = match["feature_id"]
            item["overlay_revision"] = match["revision"]
        visible_items.append(item)
    visible_counts: dict[str, int] = {}
    for item in visible_items:
        source_id = str(item["source_id"])
        visible_counts[source_id] = visible_counts.get(source_id, 0) + 1
    for model in models:
        model["count"] = visible_counts.get(str(model["source_id"]), 0)
    return {
        "dataset_id": dataset_id,
        "frame_id": frame_id,
        "coordinate_space": "panorama_equirectangular_pixels",
        "projection": "equirectangular",
        "items": visible_items,
        "models": models,
        "count": len(visible_items),
        "suppressed_count": suppressed_count,
        "model_count": len(payloads),
        "truncated": payload_scan_truncated or items_truncated,
    }


@router.get("/datasets/{dataset_id}/detections/overlay-feature")
def detection_overlay_feature(
    dataset_id: str,
    request: Request,
    response: Response,
    source_id: str = Query(
        ...,
        min_length=40,
        max_length=40,
        pattern=DETECTION_SOURCE_ID.pattern,
    ),
    observation_id: str = Query(
        ...,
        min_length=1,
        max_length=160,
        pattern=DETECTION_OBSERVATION_ID.pattern,
    ),
) -> dict[str, Any]:
    """Find an exact editable Point feature for one raw AI observation."""

    require_ready_dataset(request, dataset_id)
    response.headers["Cache-Control"] = "private, no-store"
    key = (source_id.casefold(), observation_id.casefold())
    return resolve_detection_overlay_features(
        request.app,
        dataset_id,
        [(source_id, observation_id)],
    )[key]
