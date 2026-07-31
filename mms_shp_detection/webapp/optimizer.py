from __future__ import annotations

import math
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api", tags=["optimizer"])


PRESETS: dict[str, dict[str, float | int]] = {
    "fast": {
        "voxel_size": 0.10,
        "confidence": 0.80,
        "cluster_distance": 0.35,
        "min_points": 100,
        "search_radius": 15.0,
        "ground_tolerance": 0.35,
    },
    "balanced": {
        "voxel_size": 0.10,
        "confidence": 0.80,
        "cluster_distance": 0.35,
        "min_points": 100,
        "search_radius": 15.0,
        "ground_tolerance": 0.35,
    },
    "precise": {
        "voxel_size": 0.10,
        "confidence": 0.80,
        "cluster_distance": 0.35,
        "min_points": 100,
        "search_radius": 15.0,
        "ground_tolerance": 0.35,
    },
}

PRESET_ALIASES = {
    "speed": "fast",
    "accuracy": "precise",
    "accurate": "precise",
}

CORE_PARAMETER_MAP = {
    "voxel_size": "pole_xy_voxel_m",
    "confidence": "conf",
    "cluster_distance": "cluster_radius_m",
    "min_points": "min_point_count",
    "search_radius": "max_range_m",
    "ground_tolerance": "pole_max_ground_support_distance_m",
}

PARAMETER_RANGES: dict[str, tuple[float, float, type]] = {
    "voxel_size": (0.01, 1.0, float),
    "confidence": (0.01, 1.0, float),
    "cluster_distance": (0.01, 5.0, float),
    "min_points": (3, 100_000, int),
    "search_radius": (0.5, 100.0, float),
    "ground_tolerance": (0.01, 5.0, float),
}


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dataset_id: str | None = None
    mode: str | None = None
    parameter_mode: str | None = None
    profile: str | None = None
    preset: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    auto: dict[str, Any] = Field(default_factory=dict)


def normalize_preset(value: Any) -> str:
    selected = str(value or "balanced").strip().casefold()
    selected = PRESET_ALIASES.get(selected, selected)
    if selected not in PRESETS:
        raise ValueError("Preset must be fast, balanced, or precise.")
    return selected


def validate_ui_parameters(
    raw: dict[str, Any],
    *,
    require_all: bool = True,
) -> dict[str, float | int]:
    if not isinstance(raw, dict):
        raise ValueError("parameters must be an object.")
    unknown = sorted(set(raw) - set(PARAMETER_RANGES))
    if unknown:
        raise ValueError(f"Unsupported parameter(s): {', '.join(unknown)}")
    if require_all:
        missing = [key for key in PARAMETER_RANGES if key not in raw]
        if missing:
            raise ValueError(f"Missing parameter(s): {', '.join(missing)}")
    result: dict[str, float | int] = {}
    for key, value in raw.items():
        minimum, maximum, target_type = PARAMETER_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric.")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
            raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}.")
        if target_type is int:
            if not numeric.is_integer():
                raise ValueError(f"{key} must be an integer.")
            result[key] = int(numeric)
        else:
            result[key] = numeric
    return result


def detect_hardware() -> dict[str, Any]:
    cpu_count = max(1, int(os.cpu_count() or 1))
    hardware: dict[str, Any] = {
        "cpu_count": cpu_count,
        "accelerator": "cpu",
        "gpu_count": 0,
        "gpu_memory_gb": None,
    }
    try:
        import torch

        if torch.cuda.is_available():
            gpu_count = int(torch.cuda.device_count())
            properties = torch.cuda.get_device_properties(0)
            hardware.update(
                {
                    "accelerator": "cuda",
                    "gpu_count": gpu_count,
                    "gpu_name": str(properties.name),
                    "gpu_memory_gb": round(
                        float(properties.total_memory) / (1024**3), 1
                    ),
                }
            )
    except Exception:
        # Hardware detection is advisory; a CPU-safe plan remains valid if the
        # CUDA runtime cannot be imported in the web process.
        pass
    return hardware


def resolve_profile(
    preset: str,
    *,
    hardware: dict[str, Any] | None = None,
) -> tuple[dict[str, float | int], dict[str, Any]]:
    selected = normalize_preset(preset)
    detected = hardware or detect_hardware()
    parameters = dict(PRESETS[selected])
    gpu_memory = detected.get("gpu_memory_gb")

    # These are conservative resource adaptations, not accuracy tuning.  They
    # avoid obvious memory pressure while keeping the semantic preset intact.
    image_size = {"fast": 960, "balanced": 1280, "precise": 1600}[selected]
    tile_batch = {"fast": 6, "balanced": 4, "precise": 2}[selected]
    if detected.get("accelerator") == "cuda":
        if isinstance(gpu_memory, (int, float)) and gpu_memory < 8:
            image_size = min(image_size, 1024)
            tile_batch = 1
        elif isinstance(gpu_memory, (int, float)) and gpu_memory < 12:
            tile_batch = min(tile_batch, 2)
        num_workers = 1  # one GPU process; avoids unsafe CUDA multiprocessing
    else:
        num_workers = min(8, max(1, int(detected.get("cpu_count", 1)) // 2))
        tile_batch = min(tile_batch, 2)

    # Automatic mode intentionally leaves confidence, geometric gates, and all
    # per-model filters untouched.  Without labelled QA data, changing those
    # values would be guesswork rather than optimization.
    core = {
        "imgsz": image_size,
        "tile_batch_size": tile_batch,
        "num_workers": num_workers,
        "multi_model_inference_workers": 1,
    }
    return parameters, core


def resolve_run_parameters(
    *,
    mode: str,
    parameters: dict[str, Any] | None,
    preset: str | None,
) -> tuple[dict[str, float | int], dict[str, Any], str]:
    normalized_mode = str(mode).strip().casefold()
    if normalized_mode in {"automatic", "auto"}:
        selected = normalize_preset(preset)
        ui, core = resolve_profile(selected)
        return ui, core, selected
    if normalized_mode in {"manual", "numeric", "number"}:
        ui = validate_ui_parameters(parameters or {}, require_all=True)
        core = {CORE_PARAMETER_MAP[key]: value for key, value in ui.items()}
        return ui, core, "manual"
    raise ValueError("mode must be manual or automatic.")


@router.post("/optimize")
async def optimize(payload: OptimizeRequest, request: Request) -> dict[str, Any]:
    if payload.dataset_id is not None:
        dataset = request.app.state.store.get_dataset(payload.dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found.")
    mode = payload.mode or payload.parameter_mode or "automatic"
    auto_preset = (
        payload.auto.get("preset")
        if isinstance(payload.auto, dict)
        else None
    )
    preset = payload.preset or payload.profile or auto_preset or "balanced"
    sample_ratio = (
        payload.auto.get("sample_ratio", 0.1)
        if isinstance(payload.auto, dict)
        else 0.1
    )
    if isinstance(sample_ratio, bool) or not isinstance(sample_ratio, (int, float)):
        raise HTTPException(status_code=422, detail="sample_ratio must be numeric.")
    if not 0.01 <= float(sample_ratio) <= 1.0:
        raise HTTPException(status_code=422, detail="sample_ratio must be between 0.01 and 1.")
    try:
        ui_parameters, core_parameters, selected = resolve_run_parameters(
            mode=mode,
            parameters=payload.parameters,
            preset=str(preset),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    hardware = detect_hardware()
    # Re-resolve automatic mode with the hardware record returned to the caller.
    if str(mode).casefold() in {"automatic", "auto"}:
        ui_parameters, core_parameters = resolve_profile(selected, hardware=hardware)
    return {
        "mode": "automatic" if selected != "manual" else "manual",
        "preset": selected,
        "parameters": ui_parameters,
        "resolved_parameters": core_parameters,
        "hardware": hardware,
        "score": None,
        "sampled_frames": 0,
        "sample_ratio": float(sample_ratio),
        "method": "validated hardware-aware heuristic preset",
        "disclaimer": (
            "This preset changes hardware/resource settings only. Existing "
            "validated confidence and geometry filters are preserved; no "
            "ground-truth accuracy was measured for this dataset."
        ),
    }
