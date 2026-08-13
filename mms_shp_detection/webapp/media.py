from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
import time
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .datasets import require_ready_dataset, schedule_catalog
from .security import UnsafePath, resolve_under_root

router = APIRouter(prefix="/api", tags=["preview"])

MMSP_HEADER = struct.Struct("<4sHHI6fI")
MMSP_RECORD_BYTES = 15
MMSP_VERSION = 1
MMSO_HEADER = struct.Struct("<4sHHI6fI")
MMSO_RECORD_BYTES = 15
MMSO_VERSION = 1
MAX_PREVIEW_BLOCKS = 256
PANORAMA_POINT_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
POINT_PREVIEW_CACHE_LIMIT_BYTES = 1024 * 1024 * 1024
POINT_PREVIEW_DEFAULT_BUDGET = 250_000
POINT_PREVIEW_MIN_BUDGET = 250_000
POINT_PREVIEW_MAX_BUDGET = 1_000_000
POINT_PREVIEW_MAX_PAYLOAD_BYTES = (
    MMSP_HEADER.size + POINT_PREVIEW_MAX_BUDGET * MMSP_RECORD_BYTES
)
POINT_PREVIEW_DENSE_RADIUS_M = 15.0
POINT_PREVIEW_MAX_RADIUS_M = 25.0
POINT_PREVIEW_DENSE_BUDGET_FRACTION = 0.75
VWORLD_ADDRESS_ENDPOINT = "https://api.vworld.kr/req/address"
VWORLD_DEVELOPMENT_KEY = "EE4D1CA9-BEA1-3AFF-AE81-DE0B92A0E352"
VWORLD_ADDRESS_RESPONSE_LIMIT_BYTES = 256 * 1024
VWORLD_ADDRESS_FAILURE_TTL_SECONDS = 30.0
VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES = 2048
VWORLD_ADDRESS_MAX_INFLIGHT = 32
T = TypeVar("T")


def _metadata_frame_address(task: dict[str, Any]) -> str | None:
    """Return a delivery-supplied address without contacting an external API."""

    for key in (
        "road_address",
        "road_addr",
        "address",
        "parcel_address",
        "parcel_addr",
    ):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    metadata = task.get("metadata")
    if isinstance(metadata, dict):
        return _metadata_frame_address(metadata)
    return None


def _vworld_reverse_geocode(
    longitude: float,
    latitude: float,
    api_key: str,
    *,
    timeout_seconds: float = 4.0,
) -> dict[str, str | None] | None:
    """Resolve one WGS84 point with V-World Geocoder API 2.0.

    The request URL is fixed, the response body is bounded, and the API key is
    never included in an exception returned to a browser or written to logs.
    """

    for address_type in ("ROAD", "PARCEL"):
        query = urlencode(
            {
                "service": "address",
                "request": "getaddress",
                "version": "2.0",
                "crs": "EPSG:4326",
                "type": address_type,
                "point": f"{longitude:.8f},{latitude:.8f}",
                "format": "json",
                "errorformat": "json",
                "simple": "false",
                "key": api_key,
            }
        )
        outgoing = UrlRequest(
            f"{VWORLD_ADDRESS_ENDPOINT}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "MMS-Web-Workspace/0.1",
            },
            method="GET",
        )
        with urlopen(outgoing, timeout=timeout_seconds) as response:
            payload = response.read(VWORLD_ADDRESS_RESPONSE_LIMIT_BYTES + 1)
        if len(payload) > VWORLD_ADDRESS_RESPONSE_LIMIT_BYTES:
            raise ValueError("V-World address response exceeded the size limit.")
        document = json.loads(payload.decode("utf-8"))
        response_payload = document.get("response") if isinstance(document, dict) else None
        if not isinstance(response_payload, dict):
            continue
        if response_payload.get("status") == "NOT_FOUND":
            continue
        if response_payload.get("status") != "OK":
            return None
        result = response_payload.get("result")
        candidates = result if isinstance(result, list) else [result]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            address = str(item.get("text") or "").strip()
            if not address:
                continue
            return {
                "address": address[:500],
                "address_type": str(item.get("type") or address_type).strip()[:32] or None,
                "zipcode": str(item.get("zipcode") or "").strip()[:16] or None,
            }
    return None


def _prune_address_failure_cache(
    failures: dict[str, float], now: float
) -> None:
    expired = [key for key, deadline in failures.items() if deadline <= now]
    for key in expired:
        failures.pop(key, None)
    overflow = len(failures) - VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES
    if overflow > 0:
        for key, _deadline in sorted(failures.items(), key=lambda item: item[1])[:overflow]:
            failures.pop(key, None)


def _remember_address_failure(failures: dict[str, float], key: str, now: float) -> None:
    failures[key] = now + VWORLD_ADDRESS_FAILURE_TTL_SECONDS
    _prune_address_failure_cache(failures, now)


def _address_inflight_is_full(
    inflight: dict[str, asyncio.Task[dict[str, str | None] | None]],
) -> bool:
    for pending_key, pending_task in list(inflight.items()):
        if pending_task.done():
            inflight.pop(pending_key, None)
    return len(inflight) >= VWORLD_ADDRESS_MAX_INFLIGHT


async def _finish_preview_after_request_cancel(
    work: Coroutine[Any, Any, T],
    *,
    owner_tasks: set[asyncio.Task[Any]],
    logger: Any,
    context: str,
) -> T:
    """Drain non-cancellable worker work before releasing its lock/semaphore.

    Callers acquire their cache lock and concurrency semaphore before entering
    this helper.  That keeps queued requests normally cancellable, while a
    request cancelled after ``asyncio.to_thread`` starts still waits for its
    worker and atomic cache write to finish before the surrounding contexts are
    released.  Owners are also tracked so application shutdown can drain them
    before closing the shared point reader.
    """

    task = asyncio.create_task(work)
    owner_tasks.add(task)
    task.add_done_callback(owner_tasks.discard)
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
        except BaseException as exc:
            if cancellation_requested:
                logger.error(
                    "%s failed after its request was cancelled: %s",
                    context,
                    exc,
                )
                raise asyncio.CancelledError() from exc
            raise

    if task.cancelled():
        raise asyncio.CancelledError()
    error = task.exception()
    if cancellation_requested:
        if error is not None:
            logger.error(
                "%s failed after its request was cancelled: %s",
                context,
                error,
            )
        raise asyncio.CancelledError() from error
    if error is not None:
        raise error
    return task.result()


def _dataset_root(request: Request, dataset: dict[str, Any]) -> Path:
    root = request.app.state.storage_roots_by_id.get(dataset["root_id"])
    if root is None:
        raise HTTPException(status_code=409, detail="Dataset storage is unavailable.")
    try:
        return resolve_under_root(
            root.path,
            dataset["relative_path"],
            must_exist=True,
            expect_directory=True,
        )
    except (UnsafePath, OSError) as exc:
        raise HTTPException(status_code=409, detail="Dataset storage is unavailable.") from exc


def _safe_frame_image(dataset_root: Path, task: dict[str, Any]) -> Path:
    try:
        discovered = Path(str(task["image_path"])).resolve(strict=True)
        relative = discovered.relative_to(dataset_root.resolve(strict=True)).as_posix()
        return resolve_under_root(
            dataset_root,
            relative,
            must_exist=True,
            expect_directory=False,
            reject_symlinks=True,
        )
    except (KeyError, UnsafePath, OSError, ValueError) as exc:
        raise ValueError("The panorama source is no longer safely available.") from exc


def _panorama_fingerprint(source: Path, width: int, *, frame_id: str) -> str:
    stat = source.stat()
    payload = (
        f"panorama-v3\0{frame_id}\0{source}\0{stat.st_size}\0{stat.st_mtime_ns}\0{width}"
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _resize_panorama(source: Path, output_base: Path, width: int) -> tuple[Path, str]:
    from PIL import Image, ImageOps, features

    Image.MAX_IMAGE_PIXELS = 600_000_000
    output_base.parent.mkdir(parents=True, exist_ok=True)
    use_webp = bool(features.check("webp"))
    suffix = ".webp" if use_webp else ".jpg"
    media_type = "image/webp" if use_webp else "image/jpeg"
    output_path = output_base.with_suffix(suffix)
    if output_path.is_file():
        return output_path, media_type

    temporary = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp{suffix}"
    )
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            target_width = min(width, image.width)
            if target_width < image.width:
                target_height = max(1, round(image.height * target_width / image.width))
                image = image.resize(
                    (target_width, target_height),
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
            if use_webp:
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(
                    temporary,
                    format="WEBP",
                    quality=82,
                    method=4,
                    exact=False,
                )
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(
                    temporary,
                    format="JPEG",
                    quality=86,
                    optimize=True,
                    progressive=True,
                )
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return output_path, media_type


def _etag_response(
    request: Request,
    path: Path,
    *,
    etag_value: str,
    media_type: str,
    cache_seconds: int,
) -> Response:
    quoted_etag = f'"{etag_value}"'
    cache_control = f"public, max-age={cache_seconds}, immutable"
    if request.headers.get("if-none-match") == quoted_etag:
        return Response(
            status_code=304,
            headers={"ETag": quoted_etag, "Cache-Control": cache_control},
        )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"ETag": quoted_etag, "Cache-Control": cache_control},
    )


@router.get("/datasets/{dataset_id}/panoramas/{frame_id}")
async def panorama(
    dataset_id: str,
    frame_id: str,
    request: Request,
    width: int = Query(2048, ge=256, le=8192),
) -> Response:
    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    dataset_root = _dataset_root(request, dataset)
    try:
        source = _safe_frame_image(dataset_root, frame["task"])
        fingerprint = _panorama_fingerprint(source, width, frame_id=frame_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    output_base = (
        request.app.state.config.state_dir
        / "media"
        / "panoramas"
        / dataset_id
        / fingerprint
    )
    for suffix, media_type in ((".webp", "image/webp"), (".jpg", "image/jpeg")):
        cached_path = output_base.with_suffix(suffix)
        if cached_path.is_file():
            return _etag_response(
                request,
                cached_path,
                etag_value=fingerprint,
                media_type=media_type,
                cache_seconds=31_536_000,
            )
    lock_key = f"panorama:{fingerprint}"
    lock = request.app.state.media_locks.setdefault(lock_key, asyncio.Lock())

    async def prepare_panorama() -> tuple[Path, str]:
        async with lock:
            for suffix, media_type in ((".webp", "image/webp"), (".jpg", "image/jpeg")):
                cached_path = output_base.with_suffix(suffix)
                if cached_path.is_file():
                    return cached_path, media_type
            async with request.app.state.panorama_semaphore:
                return await _finish_preview_after_request_cancel(
                    asyncio.to_thread(_resize_panorama, source, output_base, width),
                    owner_tasks=request.app.state.media_owner_tasks,
                    logger=request.app.state.logger,
                    context=f"Panorama preview {frame_id}",
                )

    try:
        output_path, media_type = await prepare_panorama()
    except Exception as exc:
        request.app.state.logger.exception("Could not resize panorama %s", frame_id)
        raise HTTPException(
            status_code=500, detail="Could not prepare panorama preview."
        ) from exc
    return _etag_response(
        request,
        output_path,
        etag_value=fingerprint,
        media_type=media_type,
        cache_seconds=31_536_000,
    )


def _bbox_distance_3d(block: dict[str, Any], origin: np.ndarray) -> float:
    minimum = np.asarray(block.get("min", []), dtype=np.float64)
    maximum = np.asarray(block.get("max", []), dtype=np.float64)
    if minimum.shape != (3,) or maximum.shape != (3,):
        return math.inf
    delta = np.maximum(np.maximum(minimum - origin, origin - maximum), 0.0)
    return float(np.linalg.norm(delta))


def _catalog_fingerprint(catalog: dict[str, Any]) -> str:
    serialized = json.dumps(
        catalog.get("signature", {}),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _task_point_fingerprint(task: dict[str, Any]) -> str:
    """Hash pose and matching fields that affect a frame point preview.

    Frame IDs intentionally remain stable across a rescan of the same delivery.
    Including these fields prevents an old media derivative from surviving a
    corrected pose, track association, or panorama orientation.
    """

    serialized = json.dumps(
        {
            "origin": task.get("origin"),
            "direction": task.get("direction"),
            "up": task.get("up"),
            "right": task.get("right"),
            "job_name": task.get("job_name"),
            "track_name": task.get("track_name"),
            "record_name": task.get("record_name"),
            "pointcloud_scope": task.get("pointcloud_scope"),
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sample_nearby_points(
    task: dict[str, Any],
    catalog: dict[str, Any],
    reader: Any,
    *,
    budget: int,
    radius: float,
    dense_radius: float | None = None,
    dense_budget_fraction: float = POINT_PREVIEW_DENSE_BUDGET_FRACTION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from mms_shp_detection.pointcloud import (
        match_nearest_pointcloud_files,
        resolve_safe_pointcloud_source,
    )

    origin = np.asarray(task.get("origin"), dtype=np.float64)
    if origin.shape != (3,) or not np.all(np.isfinite(origin)):
        raise ValueError("Frame has no valid projected point-cloud origin.")

    matched = match_nearest_pointcloud_files(task, catalog, neighbor_count=8)
    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for point_file in matched:
        for block in point_file.get("blocks", []):
            distance = _bbox_distance_3d(block, origin)
            if distance <= radius:
                candidates.append((distance, point_file, block))
    candidates.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("path", "")).casefold(),
            str(item[2].get("name", "")),
        )
    )

    # A quota per nearby block keeps a dense scanner strip from consuming the
    # complete response before adjacent blocks contribute any context.
    bounded_candidates = candidates[: min(MAX_PREVIEW_BLOCKS, 32)]
    if dense_radius is not None:
        return _sample_distance_bands(
            bounded_candidates,
            catalog,
            reader,
            origin=origin,
            budget=budget,
            dense_radius=dense_radius,
            maximum_radius=radius,
            dense_budget_fraction=dense_budget_fraction,
        )

    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    per_block_quota = max(1, math.ceil(budget / max(1, len(bounded_candidates))))
    remaining = budget
    for _distance, point_file, block in bounded_candidates:
        if remaining <= 0:
            break
        safe_point_file = point_file
        if catalog.get("data_root"):
            safe_point_file = {
                **point_file,
                "path": str(
                    resolve_safe_pointcloud_source(
                        str(point_file.get("path", "")),
                        str(catalog["data_root"]),
                    )
                ),
            }
        xyz, rgb, _intensity = reader.read_block_points(safe_point_file, block)
        if xyz.size == 0:
            continue
        squared = np.sum((xyz - origin[None, :]) ** 2, axis=1)
        selected = np.flatnonzero(squared <= radius * radius)
        if selected.size == 0:
            continue
        # Deterministic even sampling prevents one dense block from producing an
        # unbounded response while retaining its full spatial extent.
        take = min(remaining, per_block_quota)
        if selected.size > take:
            indices = np.linspace(0, selected.size - 1, take, dtype=np.int64)
            selected = selected[indices]
        selected_xyz = np.asarray(xyz[selected], dtype=np.float64)
        selected_rgb = np.asarray(rgb[selected], dtype=np.uint8)
        if selected_rgb.shape != (selected_xyz.shape[0], 3):
            selected_rgb = np.full((selected_xyz.shape[0], 3), 210, dtype=np.uint8)
        xyz_parts.append(selected_xyz)
        rgb_parts.append(selected_rgb)
        remaining -= int(selected_xyz.shape[0])

    if xyz_parts:
        xyz = np.concatenate(xyz_parts, axis=0)
        rgb = np.concatenate(rgb_parts, axis=0)
    else:
        xyz = np.empty((0, 3), dtype=np.float64)
        rgb = np.empty((0, 3), dtype=np.uint8)
    return xyz, rgb, origin


def _distance_band_quotas(
    dense_count: int,
    sparse_count: int,
    budget: int,
    *,
    dense_budget_fraction: float = POINT_PREVIEW_DENSE_BUDGET_FRACTION,
) -> tuple[int, int]:
    """Allocate a denser inner band and redistribute unused capacity."""

    available = max(0, dense_count) + max(0, sparse_count)
    target = min(max(0, budget), available)
    if target <= 0:
        return 0, 0
    fraction = min(1.0, max(0.0, float(dense_budget_fraction)))
    preferred_dense = round(target * fraction)
    dense_quota = min(max(0, dense_count), preferred_dense)
    sparse_quota = min(max(0, sparse_count), target - preferred_dense)
    remaining = target - dense_quota - sparse_quota
    dense_extra = min(max(0, dense_count) - dense_quota, remaining)
    dense_quota += dense_extra
    remaining -= dense_extra
    sparse_quota += min(max(0, sparse_count) - sparse_quota, remaining)
    return dense_quota, sparse_quota


def _per_block_sample_quotas(counts: list[int], target: int) -> list[int]:
    """Distribute a band quota deterministically while retaining block coverage."""

    quotas = [0] * len(counts)
    active = [index for index, count in enumerate(counts) if count > 0]
    remaining = min(max(0, target), sum(max(0, count) for count in counts))
    if remaining <= 0:
        return quotas
    if remaining >= len(active):
        for index in active:
            quotas[index] = 1
        remaining -= len(active)
    capacities = [max(0, counts[index] - quotas[index]) for index in range(len(counts))]
    if remaining <= 0 or not sum(capacities):
        return quotas
    total_capacity = sum(capacities)
    exact = [remaining * capacity / total_capacity for capacity in capacities]
    additions = [min(capacity, math.floor(value)) for capacity, value in zip(capacities, exact)]
    for index, addition in enumerate(additions):
        quotas[index] += addition
    remainder = remaining - sum(additions)
    order = sorted(
        range(len(counts)),
        key=lambda index: (
            -(exact[index] - math.floor(exact[index])),
            -capacities[index],
            index,
        ),
    )
    for index in order:
        if remainder <= 0:
            break
        if quotas[index] >= counts[index]:
            continue
        quotas[index] += 1
        remainder -= 1
    return quotas


def _safe_preview_point_file(
    point_file: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    if not catalog.get("data_root"):
        return point_file
    from mms_shp_detection.pointcloud import resolve_safe_pointcloud_source

    return {
        **point_file,
        "path": str(
            resolve_safe_pointcloud_source(
                str(point_file.get("path", "")), str(catalog["data_root"])
            )
        ),
    }


def _sample_distance_bands(
    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]],
    catalog: dict[str, Any],
    reader: Any,
    *,
    origin: np.ndarray,
    budget: int,
    dense_radius: float,
    maximum_radius: float,
    dense_budget_fraction: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample 0..dense_radius more heavily than the outer preview band."""

    safe_candidates = [
        (_safe_preview_point_file(point_file, catalog), block)
        for _distance, point_file, block in candidates
    ]
    dense_squared = dense_radius * dense_radius
    maximum_squared = maximum_radius * maximum_radius
    band_counts: list[tuple[int, int]] = []
    for point_file, block in safe_candidates:
        xyz, _rgb, _intensity = reader.read_block_points(point_file, block)
        if xyz.size == 0:
            band_counts.append((0, 0))
            continue
        squared = np.sum((xyz - origin[None, :]) ** 2, axis=1)
        dense_count = int(np.count_nonzero(squared <= dense_squared))
        sparse_count = int(
            np.count_nonzero((squared > dense_squared) & (squared <= maximum_squared))
        )
        band_counts.append((dense_count, sparse_count))

    dense_quota, sparse_quota = _distance_band_quotas(
        sum(count[0] for count in band_counts),
        sum(count[1] for count in band_counts),
        budget,
        dense_budget_fraction=dense_budget_fraction,
    )
    dense_by_block = _per_block_sample_quotas(
        [count[0] for count in band_counts], dense_quota
    )
    sparse_by_block = _per_block_sample_quotas(
        [count[1] for count in band_counts], sparse_quota
    )

    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    for index, (point_file, block) in enumerate(safe_candidates):
        if dense_by_block[index] <= 0 and sparse_by_block[index] <= 0:
            continue
        xyz, rgb, _intensity = reader.read_block_points(point_file, block)
        if xyz.size == 0:
            continue
        squared = np.sum((xyz - origin[None, :]) ** 2, axis=1)
        selected_parts: list[np.ndarray] = []
        for mask, quota in (
            (squared <= dense_squared, dense_by_block[index]),
            (
                (squared > dense_squared) & (squared <= maximum_squared),
                sparse_by_block[index],
            ),
        ):
            selected = np.flatnonzero(mask)
            if selected.size > quota:
                sample_positions = np.linspace(
                    0, selected.size - 1, quota, dtype=np.int64
                )
                selected = selected[sample_positions]
            if selected.size:
                selected_parts.append(selected)
        if not selected_parts:
            continue
        selected = np.concatenate(selected_parts)
        selected_xyz = np.asarray(xyz[selected], dtype=np.float64)
        selected_rgb = np.asarray(rgb[selected], dtype=np.uint8)
        if selected_rgb.shape != (selected_xyz.shape[0], 3):
            selected_rgb = np.full((selected_xyz.shape[0], 3), 210, dtype=np.uint8)
        xyz_parts.append(selected_xyz)
        rgb_parts.append(selected_rgb)

    if not xyz_parts:
        return (
            np.empty((0, 3), dtype=np.float64),
            np.empty((0, 3), dtype=np.uint8),
            origin,
        )
    return np.concatenate(xyz_parts), np.concatenate(rgb_parts), origin


def _build_mmsp(
    task: dict[str, Any],
    catalog: dict[str, Any],
    reader: Any,
    *,
    budget: int,
) -> bytes:
    if budget < 1 or budget > POINT_PREVIEW_MAX_BUDGET:
        raise ValueError(
            f"Point preview budget must be between 1 and {POINT_PREVIEW_MAX_BUDGET}."
        )
    xyz, rgb, origin = _sample_nearby_points(
        task,
        catalog,
        reader,
        budget=budget,
        radius=POINT_PREVIEW_MAX_RADIUS_M,
        dense_radius=POINT_PREVIEW_DENSE_RADIUS_M,
    )
    if xyz.size:
        relative = np.asarray(xyz - origin[None, :], dtype="<f4")
        minimum = relative.min(axis=0).astype("<f4")
        maximum = relative.max(axis=0).astype("<f4")
    else:
        relative = np.empty((0, 3), dtype="<f4")
        minimum = np.zeros(3, dtype="<f4")
        maximum = np.zeros(3, dtype="<f4")

    count = int(relative.shape[0])
    header = MMSP_HEADER.pack(
        b"MMSP",
        MMSP_VERSION,
        1,
        count,
        *minimum.tolist(),
        *maximum.tolist(),
        0,
    )
    if count == 0:
        return header
    # NumPy structured arrays guarantee the compact 15-byte interleaved layout.
    records = np.empty(
        count,
        dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))], align=False),
    )
    records["xyz"] = relative
    records["rgb"] = rgb
    payload = records.tobytes(order="C")
    if len(payload) != count * MMSP_RECORD_BYTES:
        raise RuntimeError("Unexpected MMSP record alignment.")
    return header + payload


def _panorama_axes(
    task: dict[str, Any],
    *,
    yaw_offset_deg: float,
    pitch_offset_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from mms_shp_detection.geometry import (
        apply_panorama_angular_offsets,
        build_camera_axes,
    )

    direction = np.asarray(task.get("direction"), dtype=np.float64)
    up = np.asarray(task.get("up"), dtype=np.float64)
    if (
        direction.shape != (3,)
        or up.shape != (3,)
        or not np.all(np.isfinite(direction))
        or not np.all(np.isfinite(up))
    ):
        raise ValueError("Frame has no valid panorama orientation axes.")
    forward_vec, right_vec, up_vec = build_camera_axes(direction, up)
    return apply_panorama_angular_offsets(
        forward_vec,
        right_vec,
        up_vec,
        yaw_offset_deg=yaw_offset_deg,
        pitch_offset_deg=pitch_offset_deg,
    )


@router.get("/datasets/{dataset_id}/frames/{frame_id}/panorama-projection")
def panorama_projection_metadata(
    dataset_id: str,
    frame_id: str,
    request: Request,
    yaw_offset_deg: float | None = Query(None, ge=-180.0, le=180.0),
    pitch_offset_deg: float | None = Query(None, ge=-45.0, le=45.0),
) -> dict[str, Any]:
    """Return the calibrated frame basis used for interactive panorama projection."""

    require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    resolved_yaw = (
        float(request.app.state.panorama_yaw_offset_deg)
        if yaw_offset_deg is None
        else yaw_offset_deg
    )
    resolved_pitch = (
        float(request.app.state.panorama_pitch_offset_deg)
        if pitch_offset_deg is None
        else pitch_offset_deg
    )
    try:
        origin = np.asarray(frame["task"].get("origin"), dtype=np.float64)
        if origin.shape != (3,) or not np.all(np.isfinite(origin)):
            raise ValueError("Frame has no valid dataset-space origin.")
        forward, right, up = _panorama_axes(
            frame["task"],
            yaw_offset_deg=resolved_yaw,
            pitch_offset_deg=resolved_pitch,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "frame_id": frame_id,
        "coordinate_space": "dataset",
        "projection": "normalized_equirectangular",
        "origin": origin.tolist(),
        "forward": forward.tolist(),
        "right": right.tolist(),
        "up": up.tolist(),
        "yaw_offset_deg": resolved_yaw,
        "pitch_offset_deg": resolved_pitch,
    }


def _frame_address_payload(
    dataset_id: str,
    frame_id: str,
    longitude: float,
    latitude: float,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "frame_id": frame_id,
        "coordinate": {"lon": longitude, "lat": latitude},
        "address": record.get("address") if record else None,
        "address_type": record.get("address_type") if record else None,
        "zipcode": record.get("zipcode") if record else None,
        "source": record.get("source") if record else "coordinate_fallback",
    }


@router.get("/datasets/{dataset_id}/frames/{frame_id}/address")
async def frame_address(
    dataset_id: str,
    frame_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return a delivery/V-World address without persistently storing it."""

    require_ready_dataset(request, dataset_id)
    store = request.app.state.store
    frame = store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    try:
        longitude = float(frame["longitude"])
        latitude = float(frame["latitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Frame has no WGS84 coordinate.") from exc
    if not (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180.0 <= longitude <= 180.0
        and -90.0 <= latitude <= 90.0
    ):
        raise HTTPException(status_code=422, detail="Frame has no valid WGS84 coordinate.")

    metadata_address = _metadata_frame_address(frame.get("task") or {})
    if metadata_address:
        metadata_record = {
            "address": metadata_address,
            "address_type": "delivery",
            "zipcode": None,
            "source": "delivery_metadata",
        }
        return _frame_address_payload(
            dataset_id, frame_id, longitude, latitude, metadata_record
        )

    # Five decimal places are about one metre in Korea. Nearby duplicate
    # requests share a lock/failure TTL without degrading the displayed pose.
    coordinate_key = f"{longitude:.5f}:{latitude:.5f}"
    failures: dict[str, float] = request.app.state.address_failure_cache
    now = time.monotonic()
    _prune_address_failure_cache(failures, now)
    if failures.get(coordinate_key, 0.0) > now:
        return _frame_address_payload(
            dataset_id, frame_id, longitude, latitude, None
        )

    api_key = str(request.app.state.vworld_api_key or "").strip()
    if not api_key:
        _remember_address_failure(failures, coordinate_key, time.monotonic())
        return _frame_address_payload(
            dataset_id, frame_id, longitude, latitude, None
        )

    async def resolve() -> dict[str, str | None] | None:
        try:
            async with request.app.state.address_semaphore:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _vworld_reverse_geocode,
                        longitude,
                        latitude,
                        api_key,
                    ),
                    timeout=5.0,
                )
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            request.app.state.logger.warning(
                "V-World reverse geocoding unavailable for frame %s (%s).",
                frame_id,
                type(exc).__name__,
            )
            return None

    # V-World's terms prohibit saving real-time geocoder results to a separate
    # database. Share only requests that are concurrently in flight, then
    # discard the task immediately after it completes.
    inflight: dict[str, asyncio.Task[dict[str, str | None] | None]] = (
        request.app.state.address_inflight
    )
    task = inflight.get(coordinate_key)
    if task is None or task.done():
        # The semaphore bounds active V-World calls, while this cap also bounds
        # callers waiting to enter it.  Without it, a burst of unique frame
        # coordinates could retain an unbounded task backlog in memory.
        if _address_inflight_is_full(inflight):
            _remember_address_failure(failures, coordinate_key, time.monotonic())
            return _frame_address_payload(
                dataset_id, frame_id, longitude, latitude, None
            )
        task = asyncio.create_task(resolve())
        inflight[coordinate_key] = task

        def discard(completed: asyncio.Task[Any]) -> None:
            if inflight.get(coordinate_key) is completed:
                inflight.pop(coordinate_key, None)
            # Every original HTTP waiter may have been aborted. Consume an
            # unexpected error so an orphaned coalesced task cannot emit an
            # unhandled-task warning; active waiters still receive the error.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(discard)
    resolved = await asyncio.shield(task)
    if resolved is None:
        _remember_address_failure(failures, coordinate_key, time.monotonic())
        return _frame_address_payload(
            dataset_id, frame_id, longitude, latitude, None
        )
    failures.pop(coordinate_key, None)
    record = {**resolved, "source": "vworld"}
    return _frame_address_payload(
        dataset_id, frame_id, longitude, latitude, record
    )


def _nearest_per_panorama_cell(
    u: np.ndarray,
    v: np.ndarray,
    distance: np.ndarray,
    *,
    cell_size_px: int,
    reference_width: int = 4096,
) -> np.ndarray:
    """Select the nearest sample in each equirectangular screen-space cell."""

    reference_height = reference_width // 2
    columns = max(1, math.ceil(reference_width / cell_size_px))
    rows = max(1, math.ceil(reference_height / cell_size_px))
    cell_x = np.minimum(columns - 1, np.floor(u * columns).astype(np.int64))
    cell_y = np.minimum(rows - 1, np.floor(v * rows).astype(np.int64))
    cell_key = (cell_y * columns) + cell_x
    source_order = np.arange(cell_key.size, dtype=np.int64)
    # Primary sort by cell, then nearest depth, then stable source order.
    order = np.lexsort((source_order, distance, cell_key))
    ordered_keys = cell_key[order]
    first = np.empty(order.size, dtype=bool)
    if first.size:
        first[0] = True
        first[1:] = ordered_keys[1:] != ordered_keys[:-1]
    return order[first]


def _build_mmso(
    task: dict[str, Any],
    catalog: dict[str, Any],
    reader: Any,
    *,
    budget: int,
    radius: float,
    cell_size_px: int,
    yaw_offset_deg: float,
    pitch_offset_deg: float,
) -> bytes:
    """Build a compact panorama-overlay stream with normalized UV and depth.

    MMSO v1 shares MMSP's compact 40-byte header and 15-byte records. Header
    bounds are ``min/max (u, v, distance_m)`` and every record is normalized
    ``u, v``, radial distance in metres, and RGB. A virtual 4096x2048 grid keeps
    only the nearest sample in each cell, greatly reducing browser overdraw.
    """

    # A modest oversample gives the screen-space reducer enough candidates to
    # choose useful near-surface points while preserving a strict read ceiling.
    sample_budget = min(250_000, max(budget, budget * 3))
    xyz, rgb, origin = _sample_nearby_points(
        task,
        catalog,
        reader,
        budget=sample_budget,
        radius=radius,
    )
    forward_vec, right_vec, up_vec = _panorama_axes(
        task,
        yaw_offset_deg=yaw_offset_deg,
        pitch_offset_deg=pitch_offset_deg,
    )

    if xyz.size:
        from mms_shp_detection.geometry import project_points_equirectangular

        u, v, distance = project_points_equirectangular(
            xyz,
            origin,
            forward_vec,
            right_vec,
            up_vec,
            1,
            1,
        )
        valid = (
            np.isfinite(u)
            & np.isfinite(v)
            & np.isfinite(distance)
            & (distance > 0.05)
        )
        u = np.mod(u[valid], 1.0)
        v = np.clip(v[valid], 0.0, 1.0)
        distance = distance[valid]
        rgb = rgb[valid]
        selected = _nearest_per_panorama_cell(
            u,
            v,
            distance,
            cell_size_px=cell_size_px,
        )
        if selected.size > budget:
            # Deterministic even selection retains coverage across the sorted
            # screen cells instead of returning one contiguous panorama region.
            selected = selected[
                np.linspace(0, selected.size - 1, budget, dtype=np.int64)
            ]
        coordinates = np.column_stack(
            (u[selected], v[selected], distance[selected])
        ).astype("<f4")
        rgb = np.asarray(rgb[selected], dtype=np.uint8)
    else:
        coordinates = np.empty((0, 3), dtype="<f4")
        rgb = np.empty((0, 3), dtype=np.uint8)

    if coordinates.size:
        minimum = coordinates.min(axis=0).astype("<f4")
        maximum = coordinates.max(axis=0).astype("<f4")
    else:
        minimum = np.zeros(3, dtype="<f4")
        maximum = np.zeros(3, dtype="<f4")
    count = int(coordinates.shape[0])
    header = MMSO_HEADER.pack(
        b"MMSO",
        MMSO_VERSION,
        3,  # bit 0: RGB, bit 1: normalized equirectangular UV
        count,
        *minimum.tolist(),
        *maximum.tolist(),
        0,
    )
    if count == 0:
        return header
    records = np.empty(
        count,
        dtype=np.dtype([("uvd", "<f4", (3,)), ("rgb", "u1", (3,))], align=False),
    )
    records["uvd"] = coordinates
    records["rgb"] = rgb
    payload = records.tobytes(order="C")
    if len(payload) != count * MMSO_RECORD_BYTES:
        raise RuntimeError("Unexpected MMSO record alignment.")
    return header + payload


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_bytes(payload)
    temporary.replace(path)


def _enforce_file_cache_quota(
    directory: Path,
    *,
    suffix: str,
    maximum_bytes: int,
    keep: Path,
) -> None:
    """Bound a derived-file cache without ever touching delivery sources."""

    entries: list[tuple[int, int, Path]] = []
    total = 0
    try:
        candidates = list(directory.glob(f"*{suffix}"))
    except OSError:
        return
    for candidate in candidates:
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        size = int(stat.st_size)
        total += size
        entries.append((int(stat.st_mtime_ns), size, candidate))
    if total <= maximum_bytes:
        return
    keep_path = keep.resolve(strict=False)
    for _mtime, size, candidate in sorted(entries):
        if total <= maximum_bytes:
            break
        if candidate.resolve(strict=False) == keep_path:
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        total -= size


@router.get("/datasets/{dataset_id}/points/{frame_id}")
async def points(
    dataset_id: str,
    frame_id: str,
    request: Request,
    budget: int = Query(
        POINT_PREVIEW_DEFAULT_BUDGET,
        ge=POINT_PREVIEW_MIN_BUDGET,
        le=POINT_PREVIEW_MAX_BUDGET,
    ),
) -> Response:
    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    if request.app.state.point_reader is None:
        raise HTTPException(
            status_code=503,
            detail="Point-cloud preview dependencies are not installed on this server.",
        )

    catalog = request.app.state.catalogs.get(dataset_id)
    if catalog is None:
        # A persisted ready cache is safe to use in this process only after the
        # core builder revalidates its source signature.  The asynchronous build
        # does exactly that and usually completes quickly for an unchanged cache.
        if dataset.get("catalog_status") == "error":
            raise HTTPException(
                status_code=503,
                detail=(
                    dataset.get("catalog_error")
                    or "Point-cloud indexing failed. Rescan the dataset after correcting its source."
                ),
            )
        schedule_catalog(request.app, dataset_id)
        return JSONResponse(
            {
                "status": "indexing",
                "detail": "Point-cloud index is being prepared. Retry this request shortly.",
            },
            status_code=202,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )

    fingerprint_payload = (
        f"mmsp-v3\0{dataset_id}\0{frame_id}\0{budget}\0"
        f"{POINT_PREVIEW_DENSE_RADIUS_M:.4f}\0{POINT_PREVIEW_MAX_RADIUS_M:.4f}\0"
        f"{POINT_PREVIEW_DENSE_BUDGET_FRACTION:.4f}\0"
        f"{_catalog_fingerprint(catalog)}\0{_task_point_fingerprint(frame['task'])}"
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    output_path = (
        request.app.state.config.state_dir
        / "media"
        / "points"
        / dataset_id
        / f"{fingerprint}.mmsp"
    )
    if output_path.is_file():
        return _etag_response(
            request,
            output_path,
            etag_value=fingerprint,
            media_type="application/vnd.mmsp",
            cache_seconds=3_600,
        )
    lock_key = f"points:{fingerprint}"
    lock = request.app.state.media_locks.setdefault(lock_key, asyncio.Lock())

    async def prepare_points() -> None:
        async with lock:
            if not output_path.is_file():
                async with request.app.state.point_preview_semaphore:
                    async def build_and_write() -> None:
                        payload = await asyncio.to_thread(
                            _build_mmsp,
                            frame["task"],
                            catalog,
                            request.app.state.point_reader,
                            budget=budget,
                        )
                        await asyncio.to_thread(_write_once, output_path, payload)
                        await asyncio.to_thread(
                            _enforce_file_cache_quota,
                            output_path.parent,
                            suffix=".mmsp",
                            maximum_bytes=POINT_PREVIEW_CACHE_LIMIT_BYTES,
                            keep=output_path,
                        )

                    await _finish_preview_after_request_cancel(
                        build_and_write(),
                        owner_tasks=request.app.state.media_owner_tasks,
                        logger=request.app.state.logger,
                        context=f"Point preview {frame_id}",
                    )

    try:
        await prepare_points()
    except Exception as exc:
        request.app.state.logger.exception(
            "Could not build point preview for frame %s", frame_id
        )
        raise HTTPException(
            status_code=500, detail="Could not prepare point-cloud preview."
        ) from exc
    return _etag_response(
        request,
        output_path,
        etag_value=fingerprint,
        media_type="application/vnd.mmsp",
        cache_seconds=3_600,
    )


@router.get("/datasets/{dataset_id}/panorama-points/{frame_id}")
async def panorama_points(
    dataset_id: str,
    frame_id: str,
    request: Request,
    budget: int = Query(30_000, ge=1_000, le=100_000),
    radius: float = Query(30.0, ge=1.0, le=100.0),
    cell_size_px: int = Query(3, ge=1, le=32),
    yaw_offset_deg: float | None = Query(None, ge=-180.0, le=180.0),
    pitch_offset_deg: float | None = Query(None, ge=-45.0, le=45.0),
) -> Response:
    """Return frame points projected onto normalized equirectangular UV space."""

    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    if request.app.state.point_reader is None:
        raise HTTPException(
            status_code=503,
            detail="Point-cloud preview dependencies are not installed on this server.",
        )
    resolved_yaw_offset = (
        float(request.app.state.panorama_yaw_offset_deg)
        if yaw_offset_deg is None
        else yaw_offset_deg
    )
    resolved_pitch_offset = (
        float(request.app.state.panorama_pitch_offset_deg)
        if pitch_offset_deg is None
        else pitch_offset_deg
    )
    try:
        _panorama_axes(
            frame["task"],
            yaw_offset_deg=resolved_yaw_offset,
            pitch_offset_deg=resolved_pitch_offset,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    catalog = request.app.state.catalogs.get(dataset_id)
    if catalog is None:
        if dataset.get("catalog_status") == "error":
            raise HTTPException(
                status_code=503,
                detail=(
                    dataset.get("catalog_error")
                    or "Point-cloud indexing failed. Rescan the dataset after correcting its source."
                ),
            )
        schedule_catalog(request.app, dataset_id)
        return JSONResponse(
            {
                "status": "indexing",
                "detail": "Point-cloud index is being prepared. Retry this request shortly.",
            },
            status_code=202,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )

    fingerprint_payload = (
        f"mmso-v1\0{dataset_id}\0{frame_id}\0{budget}\0{radius:.4f}\0"
        f"{cell_size_px}\0{resolved_yaw_offset:.6f}\0{resolved_pitch_offset:.6f}\0"
        f"{_catalog_fingerprint(catalog)}\0{_task_point_fingerprint(frame['task'])}"
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    output_path = (
        request.app.state.config.state_dir
        / "media"
        / "panorama-points"
        / dataset_id
        / f"{fingerprint}.mmso"
    )
    if output_path.is_file():
        return _etag_response(
            request,
            output_path,
            etag_value=fingerprint,
            media_type="application/vnd.mmso",
            cache_seconds=3_600,
        )
    lock_key = f"panorama-points:{fingerprint}"
    lock = request.app.state.media_locks.setdefault(lock_key, asyncio.Lock())

    async def prepare_overlay() -> None:
        async with lock:
            if not output_path.is_file():
                async with request.app.state.point_preview_semaphore:
                    async def build_and_write() -> None:
                        payload = await asyncio.to_thread(
                            _build_mmso,
                            frame["task"],
                            catalog,
                            request.app.state.point_reader,
                            budget=budget,
                            radius=radius,
                            cell_size_px=cell_size_px,
                            yaw_offset_deg=resolved_yaw_offset,
                            pitch_offset_deg=resolved_pitch_offset,
                        )
                        await asyncio.to_thread(_write_once, output_path, payload)
                        await asyncio.to_thread(
                            _enforce_file_cache_quota,
                            output_path.parent,
                            suffix=".mmso",
                            maximum_bytes=PANORAMA_POINT_CACHE_LIMIT_BYTES,
                            keep=output_path,
                        )

                    await _finish_preview_after_request_cancel(
                        build_and_write(),
                        owner_tasks=request.app.state.media_owner_tasks,
                        logger=request.app.state.logger,
                        context=f"Panorama point overlay {frame_id}",
                    )

    try:
        await prepare_overlay()
    except Exception as exc:
        request.app.state.logger.exception(
            "Could not build panorama point overlay for frame %s", frame_id
        )
        raise HTTPException(
            status_code=500,
            detail="Could not prepare panorama point overlay.",
        ) from exc
    return _etag_response(
        request,
        output_path,
        etag_value=fingerprint,
        media_type="application/vnd.mmso",
        cache_seconds=3_600,
    )
