from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from pyproj.exceptions import CRSError

from .datasets import require_ready_dataset, schedule_catalog

router = APIRouter(prefix="/api", tags=["pole-tools"])

SEED_SNAP_RADIUS_M = 0.20
LOCAL_XY_RADIUS_M = 2.0
LOCAL_Z_BELOW_SEED_M = 12.0
LOCAL_Z_ABOVE_SEED_M = 4.0
MAX_FRAME_SEED_XY_DISTANCE_M = 30.0
MAX_FRAME_SEED_Z_DISTANCE_M = 30.0
MAX_CANDIDATE_BLOCKS = 128
MAX_LOCAL_POINTS = 1_000_000
MAX_DEBUG_POINTS = 256


class PoleBaseInferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinate_space: Literal["dataset"]
    seed_position: tuple[float, float, float]
    profile: Literal["balanced"] = "balanced"
    debug: bool = False

    @field_validator("seed_position")
    @classmethod
    def _finite_seed(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if not all(math.isfinite(coordinate) for coordinate in value):
            raise ValueError("INVALID_SEED: seed_position must contain finite numbers.")
        return value


class _PoleToolFailure(RuntimeError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


async def _finish_inference_after_request_cancel(
    work: Awaitable[Any],
    *,
    owner_tasks: set[asyncio.Task[Any]],
    logger: Any,
    context: str,
) -> Any:
    """Keep a non-cancellable worker owned until it releases its semaphore.

    ``asyncio.to_thread`` cannot stop a running worker when the HTTP request is
    cancelled.  Draining the worker here keeps the surrounding semaphore held,
    so rapid frame changes cannot exceed the two-inference process limit.  The
    application also drains these owners before closing the shared point reader.
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
                logger.error("%s failed after request cancellation: %s", context, exc)
                raise asyncio.CancelledError() from exc
            raise

    if task.cancelled():
        raise asyncio.CancelledError()
    error = task.exception()
    if cancellation_requested:
        if error is not None:
            logger.error("%s failed after request cancellation: %s", context, error)
        raise asyncio.CancelledError() from error
    if error is not None:
        raise error
    return task.result()


def _validation_detail(reason_code: str, message: str) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "reason_codes": [reason_code],
        "message": message,
    }


def _validate_metric_dataset_crs(dataset_crs: Any) -> None:
    """Require a projected horizontal CRS whose two axes use metres."""

    try:
        from pyproj import CRS

        candidate = CRS.from_user_input(dataset_crs)
        if candidate.is_compound:
            projected = [item for item in candidate.sub_crs_list if item.is_projected]
            candidate = projected[0] if projected else candidate
        axes = list(candidate.axis_info[:2])
        metric_axes = len(axes) == 2 and all(
            math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12)
            and str(axis.unit_name or "").strip().casefold()
            in {"metre", "meter", "metres", "meters"}
            for axis in axes
        )
        if not candidate.is_projected or not metric_axes:
            raise ValueError
    except (AttributeError, CRSError, TypeError, ValueError):
        raise ValueError(
            "METRIC_CRS_REQUIRED: dataset horizontal coordinates must use a projected metre CRS."
        ) from None


def _validate_seed_against_frame(
    seed_xyz: Sequence[float],
    frame_task: Mapping[str, Any],
    *,
    max_xy_distance_m: float = MAX_FRAME_SEED_XY_DISTANCE_M,
    max_z_distance_m: float = MAX_FRAME_SEED_Z_DISTANCE_M,
) -> np.ndarray:
    """Validate a dataset-space seed against the selected frame pose."""

    seed = np.asarray(seed_xyz, dtype=np.float64)
    origin = np.asarray(frame_task.get("origin"), dtype=np.float64)
    if seed.shape != (3,) or not np.all(np.isfinite(seed)):
        raise ValueError(
            "INVALID_SEED: seed_position must contain three finite numbers."
        )
    if origin.shape != (3,) or not np.all(np.isfinite(origin)):
        raise ValueError(
            "SEED_OUTSIDE_FRAME_WINDOW: frame has no valid dataset-space origin."
        )
    if float(np.linalg.norm(seed[:2] - origin[:2])) > max_xy_distance_m:
        raise ValueError(
            "SEED_OUTSIDE_FRAME_WINDOW: seed is too far from the selected frame."
        )
    if abs(float(seed[2] - origin[2])) > max_z_distance_m:
        raise ValueError(
            "SEED_OUTSIDE_FRAME_WINDOW: seed elevation is too far from the selected frame."
        )
    return origin


def _block_bounds(block: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray] | None:
    try:
        minimum = np.asarray(block.get("min"), dtype=np.float64)
        maximum = np.asarray(block.get("max"), dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if (
        minimum.shape != (3,)
        or maximum.shape != (3,)
        or not np.all(np.isfinite(minimum))
        or not np.all(np.isfinite(maximum))
        or np.any(minimum > maximum)
    ):
        return None
    return minimum, maximum


def _block_intersects_local_window(
    block: Mapping[str, Any],
    seed_xyz: Sequence[float],
    *,
    xy_radius_m: float = LOCAL_XY_RADIUS_M,
    z_below_seed_m: float = LOCAL_Z_BELOW_SEED_M,
    z_above_seed_m: float = LOCAL_Z_ABOVE_SEED_M,
) -> bool:
    """Return whether a block AABB intersects the exact local query cylinder."""

    bounds = _block_bounds(block)
    if bounds is None:
        return False
    seed = np.asarray(seed_xyz, dtype=np.float64)
    if seed.shape != (3,) or not np.all(np.isfinite(seed)):
        return False
    minimum, maximum = bounds
    if maximum[2] < seed[2] - z_below_seed_m or minimum[2] > seed[2] + z_above_seed_m:
        return False
    nearest_xy = np.minimum(np.maximum(seed[:2], minimum[:2]), maximum[:2])
    return bool(np.sum((nearest_xy - seed[:2]) ** 2) <= xy_radius_m**2)


def _block_distance_to_seed(
    block: Mapping[str, Any], seed_xyz: Sequence[float]
) -> float:
    bounds = _block_bounds(block)
    if bounds is None:
        return math.inf
    seed = np.asarray(seed_xyz, dtype=np.float64)
    minimum, maximum = bounds
    nearest = np.minimum(np.maximum(seed, minimum), maximum)
    return float(np.linalg.norm(nearest - seed))


def _safe_point_file(
    point_file: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(point_file)
    data_root = catalog.get("data_root")
    if data_root:
        from mms_shp_detection.pointcloud import resolve_safe_pointcloud_source

        result["path"] = str(
            resolve_safe_pointcloud_source(
                str(point_file.get("path", "")), str(data_root)
            )
        )
    return result


def _collect_local_point_records(
    frame_task: Mapping[str, Any],
    catalog: Mapping[str, Any],
    reader: Any,
    seed_xyz: Sequence[float],
    *,
    xy_radius_m: float = LOCAL_XY_RADIUS_M,
    z_below_seed_m: float = LOCAL_Z_BELOW_SEED_M,
    z_above_seed_m: float = LOCAL_Z_ABOVE_SEED_M,
    max_candidate_blocks: int = MAX_CANDIDATE_BLOCKS,
    max_local_points: int = MAX_LOCAL_POINTS,
) -> dict[str, np.ndarray]:
    """Read and crop full-resolution records from source blocks near a seed."""

    from mms_shp_detection.pointcloud import match_nearest_pointcloud_files

    seed = np.asarray(seed_xyz, dtype=np.float64)
    matched = match_nearest_pointcloud_files(
        dict(frame_task), dict(catalog), neighbor_count=8
    )
    candidates: list[tuple[float, str, str, dict[str, Any], dict[str, Any]]] = []
    for point_file in matched:
        for block in point_file.get("blocks", []):
            if not isinstance(block, Mapping) or not _block_intersects_local_window(
                block,
                seed,
                xy_radius_m=xy_radius_m,
                z_below_seed_m=z_below_seed_m,
                z_above_seed_m=z_above_seed_m,
            ):
                continue
            candidates.append(
                (
                    _block_distance_to_seed(block, seed),
                    str(point_file.get("path", "")).casefold(),
                    str(block.get("name", "")),
                    dict(point_file),
                    dict(block),
                )
            )
    candidates.sort(key=lambda item: item[:3])
    if len(candidates) > max_candidate_blocks:
        raise _PoleToolFailure(
            "TOO_MANY_CANDIDATE_BLOCKS",
            "The bounded local window intersects too many point-cloud blocks.",
        )

    xyz_parts: list[np.ndarray] = []
    classification_parts: list[np.ndarray] = []
    local_count = 0
    minimum_z = float(seed[2] - z_below_seed_m)
    maximum_z = float(seed[2] + z_above_seed_m)
    radius_squared = float(xy_radius_m**2)
    safe_files: dict[tuple[str, str], dict[str, Any]] = {}
    for _distance, path_key, _block_name, point_file, block in candidates:
        source_key = (path_key, str(catalog.get("data_root") or ""))
        safe_point_file = safe_files.get(source_key)
        if safe_point_file is None:
            safe_point_file = _safe_point_file(point_file, catalog)
            safe_files[source_key] = safe_point_file
        records = reader.read_block_records(safe_point_file, block)
        xyz = np.asarray(records.get("xyz"), dtype=np.float64)
        if xyz.ndim != 2 or xyz.shape[1] != 3:
            raise ValueError("Point reader returned an invalid XYZ block.")
        if xyz.shape[0] == 0:
            continue
        offsets = xyz[:, :2] - seed[None, :2]
        selected = (
            np.all(np.isfinite(xyz), axis=1)
            & (np.sum(offsets * offsets, axis=1) <= radius_squared)
            & (xyz[:, 2] >= minimum_z)
            & (xyz[:, 2] <= maximum_z)
        )
        if not np.any(selected):
            continue
        selected_xyz = np.asarray(xyz[selected], dtype=np.float64)
        local_count += int(selected_xyz.shape[0])
        if local_count > max_local_points:
            raise _PoleToolFailure(
                "LOCAL_POINT_LIMIT_EXCEEDED",
                "The bounded local window contains too many source points.",
            )
        raw_classification = records.get("classification")
        if raw_classification is None:
            classification = np.full(xyz.shape[0], -1, dtype=np.int16)
        else:
            classification = np.asarray(raw_classification, dtype=np.int16)
            if classification.shape != (xyz.shape[0],):
                classification = np.full(xyz.shape[0], -1, dtype=np.int16)
        xyz_parts.append(selected_xyz)
        classification_parts.append(
            np.asarray(classification[selected], dtype=np.int16)
        )

    if not xyz_parts:
        return {
            "xyz": np.empty((0, 3), dtype=np.float64),
            "classification": np.empty((0,), dtype=np.int16),
        }
    return {
        "xyz": np.concatenate(xyz_parts, axis=0),
        "classification": np.concatenate(classification_parts, axis=0),
    }


def _result_source(result: Any, *, debug: bool | None = None) -> Any:
    for name in ("public_dict", "to_dict", "public"):
        converter = getattr(result, name, None)
        if converter is not None:
            if not callable(converter):
                return converter
            if debug is not None:
                try:
                    return converter(debug=debug)
                except TypeError:
                    pass
            return converter()
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return result


def _json_compatible(value: Any) -> Any:
    value = _result_source(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if isinstance(value, np.ndarray):
        return _json_compatible(value.tolist())
    if isinstance(value, np.generic):
        return _json_compatible(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_compatible(item) for item in value]
    return str(value)


def _bounded_debug(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return None
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_debug(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, list):
        return [
            _bounded_debug(item, depth=depth + 1) for item in value[:MAX_DEBUG_POINTS]
        ]
    return value


def _failed_public_result(
    seed_xyz: Sequence[float], reason_code: str, *, detail: str | None = None
) -> dict[str, Any]:
    return {
        "status": "failed",
        "algorithm": "manual_seed_axis_ground_intersection",
        "algorithm_version": "1",
        "coordinate_space": "dataset",
        "seed_position": [float(value) for value in seed_xyz],
        "snapped_seed_position": None,
        "base_position": None,
        "axis": None,
        "ground": None,
        "quality": {
            "score": 0.0,
            "candidate_count": 0,
            "ambiguous": False,
            "bottom_gap_m": None,
            "components": {
                "seed": 0.0,
                "axis": 0.0,
                "span": 0.0,
                "continuity": 0.0,
                "ground": 0.0,
                "bottom_gap": 0.0,
            },
        },
        "reason_codes": [reason_code],
        "warnings": [detail] if detail else [],
        "debug": None,
    }


def _public_manual_pole_base_result(
    result: Any,
    *,
    seed_xyz: Sequence[float] | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """Convert dataclass/model/manual mappings to a bounded JSON response."""

    public = _json_compatible(_result_source(result, debug=debug))
    if not isinstance(public, dict):
        raise TypeError("Manual pole-base inference returned an invalid result.")
    if "snapped_seed_position" not in public and "snapped_seed" in public:
        public["snapped_seed_position"] = public.pop("snapped_seed")
    if "base_position" not in public and "base" in public:
        public["base_position"] = public.pop("base")
    public.setdefault("algorithm", "manual_seed_axis_ground_intersection")
    public.setdefault("algorithm_version", "1")
    public["coordinate_space"] = "dataset"
    if seed_xyz is not None:
        public["seed_position"] = [float(value) for value in seed_xyz]
    public.setdefault("base_position", None)
    public.setdefault("reason_codes", [])
    public.setdefault("warnings", [])
    if debug:
        public["debug"] = _bounded_debug(public.get("debug"))
    else:
        public["debug"] = None
    return public


def _infer_from_sources(
    frame_task: Mapping[str, Any],
    catalog: Mapping[str, Any],
    reader: Any,
    seed_xyz: Sequence[float],
    *,
    debug: bool,
) -> dict[str, Any]:
    try:
        records = _collect_local_point_records(frame_task, catalog, reader, seed_xyz)
    except _PoleToolFailure as exc:
        return _failed_public_result(seed_xyz, exc.reason_code, detail=str(exc))
    if records["xyz"].shape[0] == 0:
        return _failed_public_result(seed_xyz, "NO_LOCAL_POINTS")

    from mms_shp_detection.manual_pole_base import infer_pole_base_from_seed

    result = infer_pole_base_from_seed(
        records["xyz"],
        np.asarray(seed_xyz, dtype=np.float64),
        classifications=records["classification"],
    )
    return _public_manual_pole_base_result(
        result,
        seed_xyz=seed_xyz,
        debug=debug,
    )


@router.post("/datasets/{dataset_id}/frames/{frame_id}/pole-base/infer")
async def infer_pole_base(
    dataset_id: str,
    frame_id: str,
    payload: PoleBaseInferRequest,
    request: Request,
) -> Any:
    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    try:
        _validate_metric_dataset_crs(dataset.get("crs"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_validation_detail("METRIC_CRS_REQUIRED", str(exc)),
        ) from exc
    seed_xyz = np.asarray(payload.seed_position, dtype=np.float64)
    try:
        _validate_seed_against_frame(seed_xyz, frame.get("task") or {})
    except ValueError as exc:
        reason_code = (
            "INVALID_SEED"
            if str(exc).startswith("INVALID_SEED")
            else "SEED_OUTSIDE_FRAME_WINDOW"
        )
        raise HTTPException(
            status_code=422,
            detail=_validation_detail(reason_code, str(exc)),
        ) from exc
    if request.app.state.point_reader is None:
        raise HTTPException(
            status_code=503,
            detail="Point-cloud source reader is not available on this server.",
        )

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

    try:
        async with request.app.state.pole_tool_semaphore:
            return await _finish_inference_after_request_cancel(
                asyncio.to_thread(
                    _infer_from_sources,
                    frame.get("task") or {},
                    catalog,
                    request.app.state.point_reader,
                    seed_xyz,
                    debug=payload.debug,
                ),
                owner_tasks=request.app.state.pole_tool_owner_tasks,
                logger=request.app.state.logger,
                context=f"Pole-base inference for frame {frame_id}",
            )
    except (FileNotFoundError, OSError, ValueError) as exc:
        request.app.state.logger.warning(
            "Pole-base source query failed for frame %s (%s).",
            frame_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Point-cloud source records could not be read safely.",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        request.app.state.logger.exception(
            "Unexpected pole-base inference failure for frame %s.", frame_id
        )
        raise HTTPException(
            status_code=500,
            detail="Could not infer a pole base from the selected source point.",
        ) from exc


__all__ = [
    "MAX_CANDIDATE_BLOCKS",
    "MAX_DEBUG_POINTS",
    "MAX_LOCAL_POINTS",
    "PoleBaseInferRequest",
    "_block_intersects_local_window",
    "_collect_local_point_records",
    "_public_manual_pole_base_result",
    "_validate_metric_dataset_crs",
    "_validate_seed_against_frame",
    "router",
]
