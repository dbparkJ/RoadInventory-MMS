from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image
from tqdm.auto import tqdm

from .geometry import (
    apply_panorama_angular_offsets,
    build_camera_axes,
    project_points_equirectangular,
)
from .pointcloud import PointCloudReaderCache, match_nearest_pointcloud_files
from .pole import blocks_intersecting_bounds


def estimate_rgb_pixel_shift(
    image_rgb: np.ndarray,
    projected_pixels_xy: np.ndarray,
    point_rgb: np.ndarray,
    *,
    search_radius_px: int,
    trim_fraction: float,
) -> dict[str, float | int]:
    """Find the integer image shift that best matches LAS RGB to panorama RGB.

    Leica colourised point clouds provide a useful independent QA signal: when
    the exterior orientation is correct, a point's stored RGB should resemble
    the panorama pixel at its projected position.  The score is a trimmed mean
    absolute RGB error so moving objects and points colourised from another
    frame do not dominate the result.
    """

    image = np.asarray(image_rgb, dtype=np.uint8)
    pixels = np.asarray(projected_pixels_xy, dtype=np.float64)
    colors = np.asarray(point_rgb)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("image_rgb must have shape (H, W, 3)")
    if pixels.ndim != 2 or pixels.shape[1] != 2:
        raise ValueError("projected_pixels_xy must have shape (N, 2)")
    if colors.shape != (pixels.shape[0], 3):
        raise ValueError("point_rgb must have shape (N, 3)")
    if search_radius_px < 0:
        raise ValueError("search_radius_px cannot be negative")
    if not 0.0 < trim_fraction <= 1.0:
        raise ValueError("trim_fraction must be in (0, 1]")

    finite = np.all(np.isfinite(pixels), axis=1) & np.all(np.isfinite(colors), axis=1)
    if not np.any(finite):
        raise ValueError("No finite projected RGB points are available")
    pixels = pixels[finite]
    colors = np.clip(colors[finite], 0, 255).astype(np.float64) / 255.0

    height, width = image.shape[:2]
    x = np.rint(pixels[:, 0]).astype(np.int64)
    y = np.rint(pixels[:, 1]).astype(np.int64)
    vertical_margin = int(search_radius_px)
    valid = (
        (y >= vertical_margin)
        & (y < height - vertical_margin)
        & (x >= -width)
        & (x < width * 2)
    )
    x = x[valid]
    y = y[valid]
    colors = colors[valid]
    if x.size == 0:
        raise ValueError("No projected RGB points remain inside the search margin")

    keep_count = max(1, int(math.ceil(x.size * float(trim_fraction))))
    image_float = image.astype(np.float64) / 255.0
    candidates: list[tuple[float, int, int, int]] = []
    baseline_score: float | None = None
    for dy in range(-search_radius_px, search_radius_px + 1):
        sample_y = y + dy
        for dx in range(-search_radius_px, search_radius_px + 1):
            sample_x = (x + dx) % width
            sampled = image_float[sample_y, sample_x]
            errors = np.mean(np.abs(sampled - colors), axis=1)
            trimmed = np.partition(errors, keep_count - 1)[:keep_count]
            score = float(trimmed.mean())
            if dx == 0 and dy == 0:
                baseline_score = score
            # Prefer the smallest correction only when scores are numerically tied.
            candidates.append((score, abs(dx) + abs(dy), dx, dy))

    candidates.sort()
    best_score, _distance, best_dx, best_dy = candidates[0]
    return {
        "dx_px": int(best_dx),
        "dy_px": int(best_dy),
        "score": float(best_score),
        "baseline_score": float(baseline_score if baseline_score is not None else best_score),
        "point_count": int(x.size),
    }


def select_alignment_tasks(
    image_tasks: Iterable[dict[str, Any]],
    sample_count: int,
) -> list[dict[str, Any]]:
    """Select deterministic, route-balanced frames for alignment QA."""

    tasks = list(image_tasks)
    if sample_count <= 0 or not tasks:
        return []
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        groups.setdefault(str(task.get("record_name") or task.get("route_id") or ""), []).append(task)

    selected: list[dict[str, Any]] = []
    ordered_groups = [groups[key] for key in sorted(groups)]
    while len(selected) < sample_count:
        made_progress = False
        for group in ordered_groups:
            quota_index = sum(
                1
                for item in selected
                if str(item.get("record_name") or item.get("route_id") or "")
                == str(group[0].get("record_name") or group[0].get("route_id") or "")
            )
            if quota_index >= len(group):
                continue
            target_total = max(1, int(math.ceil(sample_count / len(ordered_groups))))
            if target_total == 1:
                index = len(group) // 2
            else:
                index = int(round(quota_index * (len(group) - 1) / (target_total - 1)))
            item = group[min(index, len(group) - 1)]
            if item not in selected:
                selected.append(item)
                made_progress = True
                if len(selected) >= sample_count:
                    break
        if not made_progress:
            break
    return selected


def _collect_nearby_rgb_points(
    image_task: dict[str, Any],
    pointcloud_catalog: dict[str, Any],
    pointcloud_cache: PointCloudReaderCache,
    *,
    neighbor_count: int,
    minimum_range_m: float,
    maximum_range_m: float,
    candidate_cap: int,
) -> tuple[np.ndarray, np.ndarray]:
    origin = np.asarray(image_task["origin"], dtype=np.float64)
    extent = np.asarray([maximum_range_m, maximum_range_m, maximum_range_m], dtype=np.float64)
    pointcloud_files = match_nearest_pointcloud_files(
        image_task,
        pointcloud_catalog,
        neighbor_count,
    )
    blocks = blocks_intersecting_bounds(pointcloud_files, origin - extent, origin + extent)
    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    for pointcloud_file, block in blocks:
        records = pointcloud_cache.read_block_records(pointcloud_file, block)
        xyz = np.asarray(records.get("xyz"), dtype=np.float64)
        rgb = np.asarray(records.get("rgb"))
        if xyz.ndim != 2 or xyz.shape[1] != 3 or rgb.shape != xyz.shape:
            continue
        distances = np.linalg.norm(xyz - origin[None, :], axis=1)
        keep = (
            np.all(np.isfinite(xyz), axis=1)
            & (distances >= minimum_range_m)
            & (distances <= maximum_range_m)
            & np.all(np.isfinite(rgb), axis=1)
            & (np.sum(rgb.astype(np.float64), axis=1) > 10.0)
        )
        if not np.any(keep):
            continue
        xyz_parts.append(xyz[keep])
        rgb_parts.append(np.clip(rgb[keep], 0, 255).astype(np.uint8))

    if not xyz_parts:
        return np.empty((0, 3), dtype=np.float64), np.empty((0, 3), dtype=np.uint8)
    xyz = np.concatenate(xyz_parts, axis=0)
    rgb = np.concatenate(rgb_parts, axis=0)
    if candidate_cap > 0 and xyz.shape[0] > candidate_cap:
        indices = np.linspace(0, xyz.shape[0] - 1, candidate_cap, dtype=np.int64)
        xyz = xyz[indices]
        rgb = rgb[indices]
    return xyz, rgb


def estimate_panorama_alignment(
    image_tasks: list[dict[str, Any]],
    pointcloud_catalog: dict[str, Any],
    logger: Any,
    *,
    neighbor_count: int,
    sample_images: int,
    max_points_per_image: int,
    search_radius_px: int,
    trim_fraction: float,
    minimum_range_m: float,
    maximum_range_m: float,
    base_yaw_offset_deg: float = 0.0,
    base_pitch_offset_deg: float = 0.0,
    show_progress: bool = False,
) -> dict[str, Any]:
    """Estimate a dataset-level residual panorama yaw/pitch from LAS RGB."""

    selected = select_alignment_tasks(image_tasks, sample_images)
    samples: list[dict[str, Any]] = []
    progress = tqdm(
        selected,
        total=len(selected),
        desc="Alignment QA",
        unit="image",
        dynamic_ncols=True,
        disable=not show_progress,
    )
    with PointCloudReaderCache() as cache:
        for task in progress:
            try:
                xyz, rgb = _collect_nearby_rgb_points(
                    task,
                    pointcloud_catalog,
                    cache,
                    neighbor_count=neighbor_count,
                    minimum_range_m=minimum_range_m,
                    maximum_range_m=maximum_range_m,
                    candidate_cap=max(max_points_per_image * 4, max_points_per_image),
                )
                if xyz.shape[0] < 500:
                    raise ValueError(f"only {xyz.shape[0]} usable RGB points")
                with Image.open(Path(task["image_path"])) as opened:
                    image_rgb = np.asarray(opened.convert("RGB"))
                raw_axes = build_camera_axes(tuple(task["direction"]), tuple(task["up"]))
                axes = apply_panorama_angular_offsets(
                    *raw_axes,
                    yaw_offset_deg=base_yaw_offset_deg,
                    pitch_offset_deg=base_pitch_offset_deg,
                )
                u, v, _distance = project_points_equirectangular(
                    xyz,
                    np.asarray(task["origin"], dtype=np.float64),
                    *axes,
                    image_rgb.shape[1],
                    image_rgb.shape[0],
                )
                finite = np.isfinite(u) & np.isfinite(v)
                indices = np.flatnonzero(finite)
                if indices.size > max_points_per_image:
                    indices = indices[
                        np.linspace(0, indices.size - 1, max_points_per_image, dtype=np.int64)
                    ]
                result = estimate_rgb_pixel_shift(
                    image_rgb,
                    np.column_stack((u[indices], v[indices])),
                    rgb[indices],
                    search_radius_px=search_radius_px,
                    trim_fraction=trim_fraction,
                )
                width = int(image_rgb.shape[1])
                height = int(image_rgb.shape[0])
                result.update(
                    {
                        "image_name": str(task.get("image_name")),
                        "record_name": str(task.get("record_name")),
                        "yaw_residual_deg": float(result["dx_px"]) * 360.0 / width,
                        "pitch_residual_deg": float(result["dy_px"]) * 180.0 / height,
                        "image_width": width,
                        "image_height": height,
                    }
                )
                samples.append(result)
                logger.info(
                    "Alignment QA %s: dx=%+dpx dy=%+dpx points=%d score=%.5f",
                    task.get("image_name"),
                    result["dx_px"],
                    result["dy_px"],
                    result["point_count"],
                    result["score"],
                )
            except Exception as exc:
                logger.warning("Alignment QA skipped %s: %s", task.get("image_name"), exc)
    progress.close()

    if not samples:
        return {
            "status": "insufficient_samples",
            "requested_sample_count": int(sample_images),
            "valid_sample_count": 0,
            "base_yaw_offset_deg": float(base_yaw_offset_deg),
            "base_pitch_offset_deg": float(base_pitch_offset_deg),
            "samples": [],
        }

    yaw = np.asarray([item["yaw_residual_deg"] for item in samples], dtype=np.float64)
    pitch = np.asarray([item["pitch_residual_deg"] for item in samples], dtype=np.float64)
    dx_pixels = np.asarray([item["dx_px"] for item in samples], dtype=np.float64)
    dy_pixels = np.asarray([item["dy_px"] for item in samples], dtype=np.float64)
    yaw_median = float(np.median(yaw))
    pitch_median = float(np.median(pitch))
    yaw_mad = float(np.median(np.abs(yaw - yaw_median)))
    pitch_mad = float(np.median(np.abs(pitch - pitch_median)))
    return {
        "status": "ok",
        "requested_sample_count": int(sample_images),
        "valid_sample_count": len(samples),
        "base_yaw_offset_deg": float(base_yaw_offset_deg),
        "base_pitch_offset_deg": float(base_pitch_offset_deg),
        "estimated_yaw_residual_deg": yaw_median,
        "estimated_pitch_residual_deg": pitch_median,
        "yaw_mad_deg": yaw_mad,
        "pitch_mad_deg": pitch_mad,
        "median_dx_px": float(np.median(dx_pixels)),
        "median_dy_px": float(np.median(dy_pixels)),
        "dx_mad_px": float(np.median(np.abs(dx_pixels - np.median(dx_pixels)))),
        "dy_mad_px": float(np.median(np.abs(dy_pixels - np.median(dy_pixels)))),
        "samples": samples,
    }


__all__ = [
    "estimate_panorama_alignment",
    "estimate_rgb_pixel_shift",
    "select_alignment_tasks",
]
