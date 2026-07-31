from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
import uuid
from pathlib import Path
from typing import Any, Coroutine, TypeVar

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .datasets import (
    require_ready_dataset,
    schedule_catalog,
)
from .security import UnsafePath, resolve_under_root


router = APIRouter(prefix="/api", tags=["preview"])

MMSP_HEADER = struct.Struct("<4sHHI6fI")
MMSP_RECORD_BYTES = 15
MMSP_VERSION = 1
MAX_PREVIEW_BLOCKS = 256
T = TypeVar("T")


async def _finish_preview_after_request_cancel(
    work: Coroutine[Any, Any, T],
    *,
    logger: Any,
    context: str,
) -> T:
    """Keep the lock/semaphore owner alive until its worker thread finishes."""

    task = asyncio.create_task(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def consume_result(completed: asyncio.Task[T]) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("%s failed after its request was cancelled.", context)

        task.add_done_callback(consume_result)
        raise


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
                return await asyncio.to_thread(
                    _resize_panorama, source, output_base, width
                )

    try:
        output_path, media_type = await _finish_preview_after_request_cancel(
            prepare_panorama(),
            logger=request.app.state.logger,
            context=f"Panorama preview {frame_id}",
        )
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


def _build_mmsp(
    task: dict[str, Any],
    catalog: dict[str, Any],
    reader: Any,
    *,
    budget: int,
    radius: float,
) -> bytes:
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

    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    # A quota per nearby block keeps a dense scanner strip from consuming the
    # complete response before adjacent blocks contribute any context.
    bounded_candidates = candidates[: min(MAX_PREVIEW_BLOCKS, 32)]
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
        relative = np.asarray(xyz - origin[None, :], dtype="<f4")
        minimum = relative.min(axis=0).astype("<f4")
        maximum = relative.max(axis=0).astype("<f4")
    else:
        relative = np.empty((0, 3), dtype="<f4")
        rgb = np.empty((0, 3), dtype=np.uint8)
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


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_bytes(payload)
    temporary.replace(path)


@router.get("/datasets/{dataset_id}/points/{frame_id}")
async def points(
    dataset_id: str,
    frame_id: str,
    request: Request,
    budget: int = Query(100_000, ge=1_000, le=250_000),
    radius: float = Query(30.0, ge=1.0, le=100.0),
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
        f"mmsp-v1\0{dataset_id}\0{frame_id}\0{budget}\0{radius:.4f}\0"
        f"{_catalog_fingerprint(catalog)}"
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
                    payload = await asyncio.to_thread(
                        _build_mmsp,
                        frame["task"],
                        catalog,
                        request.app.state.point_reader,
                        budget=budget,
                        radius=radius,
                    )
                    await asyncio.to_thread(_write_once, output_path, payload)

    try:
        await _finish_preview_after_request_cancel(
            prepare_points(),
            logger=request.app.state.logger,
            context=f"Point preview {frame_id}",
        )
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
