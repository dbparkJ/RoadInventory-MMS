from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import laspy
import numpy as np
from laspy.vlrs.known import WktCoordinateSystemVlr
from PIL import Image, ImageDraw, ImageFile
from pyproj import CRS
from pyproj.exceptions import CRSError
from scipy.spatial import cKDTree
from tqdm.auto import tqdm
from ultralytics import YOLO

from .alignment import estimate_panorama_alignment
from .calibration import attach_calibration_metadata
from .config import ConfigError, parse_args_with_config, serializable_config, validate_config_value
from .dataset import scan_image_tasks
from .geometry import (
    apply_panorama_angular_offsets,
    angular_radius_from_bbox,
    build_perspective_panorama_remap,
    build_camera_axes,
    build_view_axes,
    fit_perspective_overview,
    pixel_to_world_ray,
    perspective_pixel_to_world_ray,
    project_points_perspective,
    render_perspective_view_from_panorama,
    world_ray_to_equirectangular_pixel,
    world_ray_to_perspective_pixel,
)
from .pointcloud import (
    PointCloudReaderCache,
    build_pointcloud_catalog,
    match_nearest_pointcloud_files,
    select_candidate_blocks,
)
from .pole import (
    PoleSearchParameters,
    PoleSearchWorkspace,
    blocks_intersecting_bounds,
    cluster_pole_observations,
    find_pole_bases,
    pole_connection_coverage,
    remote_pole_junction_cost,
    select_pole_candidate,
)
from .shp_writer import (
    collect_detection_records,
    collect_pole_records,
    deduplicate_sign_and_pole_observations,
    publish_shapefile_bundles,
    write_pole_shapefile,
    write_shapefile,
)


RESULT_SCHEMA_VERSION = 17
DATASET_SIGNATURE_VERSION = 1
PANORAMA_ALIGNMENT_QA_ESTIMATOR_VERSION = 1
PANORAMA_ALIGNMENT_QA_CACHE_VERSION = 1
PANORAMA_ALIGNMENT_QA_FINAL_STATUSES = frozenset(
    {"recommendation", "insufficient_data", "ambiguous"}
)
POINT_CROP_SEMANTICS = {
    "kind": "derived_selected_points_visualization",
    "coordinate_dimensions_preserved": ["X", "Y", "Z"],
    "rgb_source": "rectified_sphere_image",
    "source_point_attributes_preserved": False,
    "note": (
        "This is a derived QA point set, not a record-preserving crop of the source LAS. "
        "Intensity, GPS time, classification, return metadata, and point IDs are not preserved."
    ),
}
POLE_CROP_SEMANTICS = {
    "kind": "derived_pole_axis_inliers",
    "coordinate_dimensions_preserved": ["X", "Y", "Z"],
    "source_point_attributes_preserved": [
        "RGB (normalized to 8-bit for the derived crop)",
        "intensity",
        "classification",
        "GPS time",
        "GPS time encoding (LAS global encoding bit 0)",
        "return metadata",
    ],
    "note": (
        "LAS attributes are copied when the source provides them. PCDB fields that do not "
        "exist in that format are written with explicit unknown/default values."
    ),
}


class _AdaptiveStageGate:
    """Thread-safe stage limiter whose capacity can be reduced after CUDA OOM."""

    def __init__(self, capacity: int, *, name: str) -> None:
        self.name = name
        self._capacity = max(1, int(capacity))
        self._active = 0
        self._condition = threading.Condition()
        self.wait_seconds = 0.0
        self.max_active = 0

    @contextmanager
    def slot(self):
        started = time.perf_counter()
        with self._condition:
            while self._active >= self._capacity:
                self._condition.wait()
            self.wait_seconds += time.perf_counter() - started
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def reduce_to_one(self) -> bool:
        with self._condition:
            changed = self._capacity != 1
            self._capacity = 1
            self._condition.notify_all()
            return changed

    @property
    def capacity(self) -> int:
        with self._condition:
            return self._capacity


class _QueuedStageGate:
    """Arrival-ordered gate used to bound memory-heavy pole processing."""

    def __init__(self, capacity: int, *, name: str) -> None:
        self.name = name
        self.capacity = max(1, int(capacity))
        self._condition = threading.Condition()
        self._waiters: list[object] = []
        self.wait_seconds = 0.0
        self.active = 0
        self.max_active = 0

    @contextmanager
    def slot(self):
        started = time.perf_counter()
        waiter = object()
        with self._condition:
            self._waiters.append(waiter)
            try:
                while self._waiters[0] is not waiter or self.active >= self.capacity:
                    self._condition.wait()
            except BaseException:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                self._condition.notify_all()
                raise
            self._waiters.pop(0)
            self.wait_seconds += time.perf_counter() - started
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self.active -= 1
                self._condition.notify_all()


class PersistentCudaOutOfMemoryError(RuntimeError):
    """Raised when serialized retry still cannot fit one resident model."""


class MultiModelCoordinator:
    """Own shared GPU/pole limits and aggregate scheduling diagnostics."""

    def __init__(
        self,
        *,
        inference_workers: int,
        pole_workers: int,
        queue_depth: int,
    ) -> None:
        self.inference_gate = _AdaptiveStageGate(
            inference_workers,
            name="model_inference",
        )
        self.pole_gate = _QueuedStageGate(
            pole_workers,
            name="pole_postprocess",
        )
        self.queue_depth = max(1, int(queue_depth))
        self.cuda_oom_fallbacks = 0
        self._stats_lock = threading.Lock()
        self.stage_seconds: dict[str, float] = {}
        self.stage_counts: dict[str, int] = {}

    @contextmanager
    def timed(self, stage: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            with self._stats_lock:
                self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + elapsed
                self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1

    def downgrade_inference_after_oom(self) -> bool:
        changed = self.inference_gate.reduce_to_one()
        with self._stats_lock:
            self.cuda_oom_fallbacks += 1
        return changed

    def snapshot(self) -> dict[str, Any]:
        with self._stats_lock:
            stages = {
                key: {
                    "seconds": float(value),
                    "count": int(self.stage_counts.get(key, 0)),
                }
                for key, value in sorted(self.stage_seconds.items())
            }
            oom_fallbacks = int(self.cuda_oom_fallbacks)
        return {
            "queue_depth": self.queue_depth,
            "inference_workers_effective": self.inference_gate.capacity,
            "inference_max_active": int(self.inference_gate.max_active),
            "inference_wait_seconds": float(self.inference_gate.wait_seconds),
            "cuda_oom_sequential_fallbacks": oom_fallbacks,
            "pole_workers": int(self.pole_gate.capacity),
            "pole_max_active": int(self.pole_gate.max_active),
            "pole_wait_seconds": float(self.pole_gate.wait_seconds),
            "stages": stages,
        }


def parse_class_id_list(value: Any) -> tuple[int, ...]:
    """Parse YAML lists or comma-separated CLI text into unique class IDs."""

    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        values = [item.strip() for item in stripped.split(",")]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise argparse.ArgumentTypeError("class IDs must be a YAML list or comma-separated text")
    try:
        parsed = tuple(dict.fromkeys(int(item) for item in values))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("every class ID must be an integer") from exc
    if any(item < 0 or item > 255 for item in parsed):
        raise argparse.ArgumentTypeError("LAS class IDs must be between 0 and 255")
    return parsed


def parse_name_list(value: Any) -> tuple[str, ...]:
    """Parse YAML lists or comma-separated CLI text into unique names."""

    if value is None:
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise argparse.ArgumentTypeError(
            "names must be a YAML list or comma-separated text"
        )

    parsed: list[str] = []
    seen: set[str] = set()
    for value_item in values:
        if not isinstance(value_item, str):
            raise argparse.ArgumentTypeError("every included name must be a string")
        name = value_item.strip()
        if not name:
            raise argparse.ArgumentTypeError("included names cannot be empty")
        folded = name.casefold()
        if folded not in seen:
            seen.add(folded)
            parsed.append(name)
    return tuple(parsed)


def parse_model_filters(value: Any) -> dict[str, dict[str, Any]]:
    """Parse the per-model filter map from YAML or a JSON CLI value."""

    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(
                "model filters must be a YAML mapping or JSON object"
            ) from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("model filters must be a mapping")

    parsed: dict[str, dict[str, Any]] = {}
    casefolded_names: set[str] = set()
    for raw_name, raw_profile in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise argparse.ArgumentTypeError(
                "every model filter name must be a non-empty string"
            )
        name = raw_name.strip()
        folded = name.casefold()
        if folded in casefolded_names:
            raise argparse.ArgumentTypeError(
                f"model filter names must be unique ignoring case: {name!r}"
            )
        casefolded_names.add(folded)
        if not isinstance(raw_profile, dict):
            raise argparse.ArgumentTypeError(
                f"model filter {name!r} must contain a mapping"
            )
        parsed[name] = copy.deepcopy(raw_profile)
    return parsed


def _classification_counts(summary: dict[str, Any] | None) -> dict[int, int]:
    counts: dict[int, int] = {}
    for raw_class_id, raw_count in ((summary or {}).get("class_counts") or {}).items():
        try:
            class_id = int(raw_class_id)
            count = int(raw_count)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= class_id <= 255 and count > 0:
            counts[class_id] = counts.get(class_id, 0) + count
    return counts


def resolve_pole_classification_policy(
    pointcloud_catalog: dict[str, Any],
    *,
    requested_mode: str,
    ground_class_ids: tuple[int, ...],
    pole_class_ids: tuple[int, ...],
    excluded_pole_class_ids: tuple[int, ...],
) -> dict[str, Any]:
    """Resolve whether semantic LAS classes may influence pole extraction.

    LAS always has a classification dimension in common point formats, so a
    field-presence check alone would incorrectly accept files whose values are
    all unclassified (0/1).  Auto mode instead requires at least one class ID
    whose meaning is explicitly configured for this algorithm.  Unknown custom
    IDs are reported but never assigned a meaning here.
    """

    mode = str(requested_mode).casefold()
    if mode not in {"auto", "off", "require"}:
        raise ValueError("pole_classification_mode must be one of: auto, off, require")

    groups = {
        "ground_class_ids": sorted({int(value) for value in ground_class_ids}),
        "pole_class_ids": sorted({int(value) for value in pole_class_ids}),
        "excluded_pole_class_ids": sorted(
            {int(value) for value in excluded_pole_class_ids}
        ),
    }
    configured_ids = sorted({value for values in groups.values() for value in values})
    catalog_counts = _classification_counts(pointcloud_catalog.get("classification_summary"))
    files = list(pointcloud_catalog.get("files") or [])
    if not catalog_counts:
        for item in files:
            for class_id, count in _classification_counts(
                item.get("classification_summary")
            ).items():
                catalog_counts[class_id] = catalog_counts.get(class_id, 0) + count

    configured_set = set(configured_ids)
    matched_ids = sorted(configured_set.intersection(catalog_counts))
    matched_point_count = sum(catalog_counts[class_id] for class_id in matched_ids)
    files_with_dimension = 0
    files_with_semantic_classes = 0
    files_without_semantic_classes: list[str] = []
    for item in files:
        summary = item.get("classification_summary") or {}
        if summary.get("dimension_present"):
            files_with_dimension += 1
        file_counts = _classification_counts(summary)
        if configured_set.intersection(file_counts):
            files_with_semantic_classes += 1
        else:
            files_without_semantic_classes.append(str(item.get("path") or "<unknown>"))

    source_type = str(pointcloud_catalog.get("selected_source_type") or "unknown")
    if mode == "off":
        effective_mode = "GEOMETRY"
        reason = "forced_off"
    elif matched_point_count > 0:
        effective_mode = "HYBRID"
        reason = "configured_semantic_classes_present"
    elif source_type != "las":
        effective_mode = "GEOMETRY"
        reason = "point_source_has_no_las_classification"
    elif not catalog_counts or set(catalog_counts).issubset({0, 1}):
        effective_mode = "GEOMETRY"
        reason = "unclassified_values_only"
    elif not configured_ids:
        effective_mode = "GEOMETRY"
        reason = "no_semantic_class_ids_configured"
    else:
        effective_mode = "GEOMETRY"
        reason = "observed_classes_are_not_mapped"

    if mode == "require":
        problems: list[str] = []
        if source_type != "las":
            problems.append(f"point source is {source_type!r}, not LAS")
        if matched_point_count <= 0:
            problems.append("none of the configured semantic class IDs occur")
        if files and files_without_semantic_classes:
            problems.append(
                f"{len(files_without_semantic_classes)}/{len(files)} selected files have no "
                "configured semantic class"
            )
        if problems:
            raise ValueError(
                "pole_classification_mode=require cannot be satisfied: "
                + "; ".join(problems)
                + ". Verify the supplier class map or use auto/off."
            )

    return {
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "uses_classification": effective_mode == "HYBRID",
        "reason": reason,
        "source_type": source_type,
        "configured": groups,
        "observed_class_ids": sorted(catalog_counts),
        "matched_class_ids": matched_ids,
        "matched_point_count": int(matched_point_count),
        "source_file_count": len(files),
        "files_with_classification_dimension": files_with_dimension,
        "files_with_semantic_classes": files_with_semantic_classes,
        "files_without_semantic_classes": len(files_without_semantic_classes),
        "_files_without_semantic_class_paths": files_without_semantic_classes,
    }


def public_pole_classification_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return policy provenance without internal path lists used only for logging."""

    return {key: value for key, value in policy.items() if not key.startswith("_")}


def pole_classifications_for_policy(
    classifications: np.ndarray,
    policy: dict[str, Any],
) -> np.ndarray | None:
    """Return the source class array only when the resolved policy permits its use."""

    values = np.asarray(classifications, dtype=np.int16)
    return values if bool(policy.get("uses_classification")) else None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect signs in MMS panoramas, match calibrated point clouds, and export a SHP.",
        allow_abbrev=False,
        epilog=(
            "YAML-first usage: scripts/run_pipeline.py [config.yaml]. With no arguments, "
            "./config.yaml is loaded when present. --config FILE is equivalent; "
            "--no-config keeps legacy CLI-only behavior. Remaining CLI options override YAML."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Parent folder recursively containing legacy or Leica MMS deliveries.",
    )
    parser.add_argument(
        "--include-record-names",
        type=parse_name_list,
        default=None,
        metavar="NAME[,NAME...]",
        help=(
            "Process only exact record names (case-insensitive). "
            "YAML accepts a list; CLI accepts comma-separated names."
        ),
    )
    parser.add_argument(
        "--include-job-names",
        type=parse_name_list,
        default=None,
        metavar="NAME[,NAME...]",
        help=(
            "Process only exact job names (case-insensitive). "
            "YAML accepts a list; CLI accepts comma-separated names."
        ),
    )
    parser.add_argument(
        "--include-track-names",
        type=parse_name_list,
        default=None,
        metavar="NAME[,NAME...]",
        help=(
            "Process only exact track names (case-insensitive). "
            "YAML accepts a list; CLI accepts comma-separated names."
        ),
    )
    parser.add_argument(
        "--frame-id-from",
        type=str,
        default=None,
        metavar="IMAGE_STEM",
        help=(
            "Inclusive lower image-stem bound using case-insensitive natural order."
        ),
    )
    parser.add_argument(
        "--frame-id-to",
        type=str,
        default=None,
        metavar="IMAGE_STEM",
        help=(
            "Inclusive upper image-stem bound using case-insensitive natural order."
        ),
    )
    parser.add_argument(
        "--pose-format",
        choices=("auto", "legacy", "leica-sphere", "leica-delivery"),
        default="auto",
        help=(
            "Image pose input format. Auto recursively combines legacy CAM, "
            "Pegasus Sphere, and Leica standard-delivery exports."
        ),
    )
    parser.add_argument(
        "--point-source",
        choices=("auto", "pcdb", "las"),
        default="auto",
        help="Point-cloud source. Auto prefers legacy PCDB and otherwise uses LAS.",
    )
    parser.add_argument(
        "--calibration-path",
        type=Path,
        default=Path("calibration.json"),
        help="Calibration snapshot made by scripts/extract_calibration.py.",
    )
    parser.add_argument(
        "--require-calibration",
        action="store_true",
        help="Fail when a pose track has no matching calibration snapshot.",
    )
    parser.add_argument(
        "--gps-week",
        type=int,
        default=None,
        help="GPS week for Leica seconds-of-week. When omitted it is inferred and cross-checked with calibration.",
    )
    parser.add_argument(
        "--gps-utc-offset-seconds",
        type=int,
        default=18,
        help="GPS minus UTC offset used for timestamp rendering.",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("best.pt"),
        help=(
            "Legacy single-model checkpoint. Ignored when --model-dir is set; "
            "use null in YAML for an all-model run."
        ),
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="Run every .pt checkpoint directly inside this directory, in name order.",
    )
    parser.add_argument(
        "--model-filters",
        type=parse_model_filters,
        default={},
        help=(
            "Per-model filter mapping. YAML mappings are preferred; the CLI accepts JSON."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--pointcloud-cache-path",
        "--pcdb-cache-path",
        dest="pointcloud_cache_path",
        type=Path,
        default=Path(".cache") / "pointcloud_catalog.json",
        help="Persistent PCDB/LAS spatial catalog cache path. --pcdb-cache-path remains an alias.",
    )
    parser.add_argument(
        "--las-index-chunk-points",
        type=int,
        default=250_000,
        help="Points per cached LAS spatial-index chunk.",
    )
    parser.add_argument(
        "--crs-wkt-path",
        type=Path,
        default=None,
        help="Optional authoritative CRS WKT file. Overrides point-cloud metadata.",
    )
    parser.add_argument(
        "--max-pose-pointcloud-separation-m",
        type=float,
        default=1_000.0,
        help=(
            "Fail when a camera origin is farther than this XY distance from every matched "
            "point-cloud bbox. This catches gross pose/point-cloud CRS mismatches; use 0 to disable."
        ),
    )
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument(
        "--detection-view-mode",
        choices=("forward", "panorama"),
        default="panorama",
        help=(
            "Run YOLO on one rectified vehicle-forward view or on the legacy "
            "full/tiled 360 panorama passes."
        ),
    )
    parser.add_argument(
        "--forward-view-size",
        type=int,
        default=1280,
        help="Square pixel size of the rectified vehicle-forward YOLO input.",
    )
    parser.add_argument(
        "--forward-view-hfov-deg",
        type=float,
        default=70.0,
        help="Horizontal field of view of the rectified forward YOLO input.",
    )
    parser.add_argument(
        "--forward-view-vfov-deg",
        type=float,
        default=70.0,
        help="Vertical field of view of the rectified forward YOLO input.",
    )
    parser.add_argument(
        "--panorama-yaw-offset-deg",
        type=float,
        default=0.0,
        help=(
            "Residual Sphere EO correction in image-space degrees. Positive values move "
            "projected point-cloud pixels to the right."
        ),
    )
    parser.add_argument(
        "--panorama-pitch-offset-deg",
        type=float,
        default=0.0,
        help=(
            "Residual Sphere EO correction in image-space degrees. Positive values move "
            "projected point-cloud pixels downward."
        ),
    )
    parser.add_argument(
        "--alignment-qa-enabled",
        action="store_true",
        help=(
            "Estimate report-only residual alignment from panorama RGB and point-cloud RGB. "
            "The recommendation is never applied automatically."
        ),
    )
    parser.add_argument("--alignment-qa-sample-images", type=int, default=8)
    parser.add_argument("--alignment-qa-max-points-per-image", type=int, default=20_000)
    parser.add_argument("--alignment-qa-search-radius-px", type=int, default=6)
    parser.add_argument("--alignment-qa-trim-fraction", type=float, default=0.80)
    parser.add_argument("--alignment-qa-min-range-m", type=float, default=2.0)
    parser.add_argument("--alignment-qa-max-range-m", type=float, default=15.0)
    parser.add_argument("--alignment-qa-min-valid-samples", type=int, default=3)
    parser.add_argument("--alignment-qa-max-mad-px", type=float, default=2.0)
    parser.add_argument(
        "--disable-full-panorama-detection",
        action="store_true",
        help="Disable full-panorama detection and keep only the panorama tile-based detection pass.",
    )
    parser.add_argument(
        "--disable-tiled-detection",
        action="store_true",
        help="Disable panorama tile-based detection and keep only the full-panorama detection pass.",
    )
    parser.add_argument(
        "--tile-width-px",
        type=int,
        default=0,
        help="Tile width used for panorama split detection. Use 0 to auto-set panorama_width / 4.",
    )
    parser.add_argument(
        "--tile-height-px",
        type=int,
        default=0,
        help="Tile height used for panorama split detection. Use 0 to auto-set panorama_height.",
    )
    parser.add_argument(
        "--tile-overlap-px",
        type=int,
        default=384,
        help="Overlap between neighboring panorama tiles.",
    )
    parser.add_argument(
        "--tile-batch-size",
        type=int,
        default=4,
        help="How many panorama tiles to send to YOLO in one predict batch.",
    )
    parser.add_argument(
        "--tile-merge-iou",
        type=float,
        default=0.5,
        help="IoU threshold used to merge duplicate detections from overlapping tiles and the full-panorama pass.",
    )
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--max-det", type=int, default=100)
    parser.add_argument(
        "--device",
        type=str,
        default="auto",
        help="Torch device string. Use auto, cpu, cuda:0, ...",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=1,
        help="Number of worker processes. On a single GPU, start with 1.",
    )
    parser.add_argument(
        "--multi-model-parallel",
        action="store_true",
        help=(
            "Keep all discovered models resident and pipeline shared forward inference "
            "with bounded per-model post-processing queues."
        ),
    )
    parser.add_argument(
        "--multi-model-inference-workers",
        type=int,
        default=2,
        help="Maximum concurrent model predictions in a multi-model run.",
    )
    parser.add_argument(
        "--multi-model-pole-workers",
        type=int,
        default=1,
        help="Maximum concurrent memory-heavy pole searches across all models.",
    )
    parser.add_argument(
        "--multi-model-queue-depth",
        type=int,
        default=4,
        help="Maximum queued frames per model between GPU inference and point/pole work.",
    )
    parser.add_argument(
        "--pointcloud-neighbor-count",
        "--pcdb-neighbor-count",
        dest="pointcloud_neighbor_count",
        type=int,
        default=6,
        help="Maximum number of nearby point-cloud files to search per image.",
    )
    parser.add_argument(
        "--point-padding-px",
        type=int,
        default=24,
        help="Padding added around the segmentation mask or bbox when collecting points.",
    )
    parser.add_argument(
        "--debug-crop-padding-px",
        type=int,
        default=32,
        help="Padding added when saving debug image crops.",
    )
    parser.add_argument(
        "--debug-mask-alpha",
        type=int,
        default=8,
        help="Overlay alpha for the segmentation area in debug crops. Use 0 to disable fill.",
    )
    parser.add_argument(
        "--max-range-m",
        type=float,
        default=80.0,
        help="Maximum camera-to-point distance for point extraction.",
    )
    parser.add_argument(
        "--point-range-fallback-enabled",
        action="store_true",
        help=(
            "Retry an otherwise empty detection at a limited longer range and "
            "accept only a geometrically coherent sparse cluster."
        ),
    )
    parser.add_argument(
        "--point-range-fallback-max-range-m",
        type=float,
        default=15.0,
        help="Maximum camera-to-point distance for the no-points fallback retry.",
    )
    parser.add_argument(
        "--point-range-fallback-min-point-count",
        type=int,
        default=60,
        help="Minimum final cluster size accepted only by the range fallback.",
    )
    parser.add_argument(
        "--point-range-fallback-min-cluster-fraction",
        type=float,
        default=0.80,
        help=(
            "Minimum share of front-surface points retained by the single fallback "
            "cluster."
        ),
    )
    parser.add_argument(
        "--point-range-fallback-min-core-mask-fraction",
        type=float,
        default=0.45,
        help=(
            "Minimum share of fallback cluster pixels lying inside the original "
            "un-padded detection mask."
        ),
    )
    parser.add_argument(
        "--point-range-fallback-max-depth-span-m",
        type=float,
        default=0.50,
        help=(
            "Maximum robust (95th-5th percentile) camera-depth span of a fallback "
            "cluster."
        ),
    )
    parser.add_argument(
        "--depth-window-m",
        type=float,
        default=1.0,
        help="Keep points within min_distance + depth_window_m to reduce background bleed.",
    )
    parser.add_argument(
        "--front-surface-quantile",
        type=float,
        default=0.02,
        help="Low distance quantile used as a robust front-surface anchor.",
    )
    parser.add_argument(
        "--front-surface-min-support",
        type=int,
        default=6,
        help="Minimum ordered point support before choosing the front-surface anchor.",
    )
    parser.add_argument(
        "--block-angle-margin-deg",
        type=float,
        default=1.5,
        help="Extra angular tolerance used when selecting candidate point-cloud blocks.",
    )
    parser.add_argument(
        "--max-center-ray-angle-deg",
        type=float,
        default=180.0,
        help="Reject detections farther than this angle from the uncorrected vehicle/EO forward axis.",
    )
    parser.add_argument(
        "--min-point-count",
        type=int,
        default=100,
        help="Detections with fewer than this many final points are excluded from the SHP.",
    )
    parser.add_argument(
        "--perspective-view-size",
        type=int,
        default=1024,
        help="Square output size for the rectified perspective view used for point matching.",
    )
    parser.add_argument(
        "--perspective-margin-deg",
        type=float,
        default=6.0,
        help="Extra FOV margin added around each detection when rectifying the panorama.",
    )
    parser.add_argument(
        "--perspective-min-fov-deg",
        type=float,
        default=18.0,
        help="Minimum rectified perspective FOV in degrees.",
    )
    parser.add_argument(
        "--perspective-max-fov-deg",
        type=float,
        default=110.0,
        help="Maximum rectified perspective FOV in degrees.",
    )
    parser.add_argument(
        "--cluster-radius-m",
        type=float,
        default=0.35,
        help="Neighbor radius used for density clustering on extracted 3D points.",
    )
    parser.add_argument(
        "--cluster-min-neighbors",
        type=int,
        default=6,
        help="Minimum neighbor count for a point to belong to a dense cluster.",
    )
    parser.add_argument(
        "--cluster-trim-radius-multiplier",
        type=float,
        default=2.5,
        help="Trim cluster points farther than this multiple of cluster-radius-m from the cluster median.",
    )
    parser.add_argument(
        "--point-preview-size",
        type=int,
        default=320,
        help="Panel size for saved point-cloud QA preview images.",
    )
    parser.add_argument(
        "--las-crop-half-extent-m",
        type=float,
        default=1.0,
        help="Save LAS points only within +/- this many meters on each XYZ axis around the representative cluster center. Use 0 or less to save the full selected cluster.",
    )
    parser.add_argument(
        "--pole-detection",
        action="store_true",
        help="Detect a sign support and estimate its axis-ground intersection.",
    )
    parser.add_argument(
        "--pole-classification-mode",
        choices=("auto", "off", "require"),
        default="auto",
        help=(
            "Use configured LAS classes automatically, ignore them completely, or require "
            "usable semantic classes before processing."
        ),
    )
    parser.add_argument("--pole-min-fov-deg", type=float, default=90.0)
    parser.add_argument(
        "--pole-debug-min-fov-deg",
        type=float,
        default=18.0,
        help=(
            "Minimum FOV for result-focused pole QA crops. Pole search keeps using "
            "--pole-min-fov-deg independently."
        ),
    )
    parser.add_argument("--pole-corridor-side-expand-ratio", type=float, default=4.0)
    parser.add_argument("--pole-corridor-top-margin-ratio", type=float, default=0.25)
    parser.add_argument("--pole-search-radius-m", type=float, default=8.0)
    parser.add_argument("--pole-max-drop-m", type=float, default=8.0)
    parser.add_argument("--pole-top-margin-m", type=float, default=3.0)
    parser.add_argument(
        "--pole-range-fallback-enabled",
        action="store_true",
        help=(
            "Retry a rejected pole with a wider horizontal and vertical physical "
            "search envelope."
        ),
    )
    parser.add_argument("--pole-fallback-search-radius-m", type=float, default=10.0)
    parser.add_argument("--pole-fallback-max-drop-m", type=float, default=12.0)
    parser.add_argument("--pole-fallback-top-margin-m", type=float, default=3.0)
    parser.add_argument(
        "--pole-fallback-max-axis-sign-distance-m",
        type=float,
        default=10.0,
    )
    parser.add_argument("--pole-fallback-min-vertical-span-m", type=float, default=0.75)
    parser.add_argument(
        "--pole-fallback-horizontal-connection-radius-m",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--pole-fallback-horizontal-connection-z-tolerance-m",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--pole-fallback-horizontal-connection-above-tolerance-m",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--pole-fallback-horizontal-connection-bin-m",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--pole-fallback-min-horizontal-connection-coverage",
        type=float,
        default=0.50,
    )
    parser.add_argument("--pole-xy-voxel-m", type=float, default=0.10)
    parser.add_argument("--pole-z-bin-m", type=float, default=0.15)
    parser.add_argument("--pole-axis-cluster-radius-m", type=float, default=0.24)
    parser.add_argument("--pole-axis-inlier-radius-m", type=float, default=0.18)
    parser.add_argument("--pole-min-vertical-span-m", type=float, default=0.75)
    parser.add_argument("--pole-min-vertical-bins", type=int, default=5)
    parser.add_argument("--pole-min-consecutive-vertical-bins", type=int, default=4)
    parser.add_argument("--pole-max-observed-z-gap-m", type=float, default=1.0)
    parser.add_argument("--pole-min-vertical-occupancy-ratio", type=float, default=0.35)
    parser.add_argument("--pole-middle-support-start-fraction", type=float, default=0.20)
    parser.add_argument("--pole-min-middle-support-coverage-ratio", type=float, default=0.30)
    parser.add_argument(
        "--pole-preferred-min-completeness-ratio",
        type=float,
        default=0.75,
        help=(
            "Prefer pole axes whose observed span and middle-bin support cover at least "
            "this fraction of the sign-to-ground height."
        ),
    )
    parser.add_argument(
        "--pole-geometry-ground-clearance-m",
        type=float,
        default=0.20,
        help=(
            "In geometry-only mode, remove shaft candidates this far above each "
            "local low-cell terrain estimate. Set 0 to disable."
        ),
    )
    parser.add_argument(
        "--pole-geometry-remote-min-completeness-ratio",
        type=float,
        default=0.75,
        help="Hard minimum shaft completeness for class-free remote supports.",
    )
    parser.add_argument(
        "--pole-geometry-remote-max-axis-rmse-m",
        type=float,
        default=0.095,
        help="Maximum radial axis RMSE for class-free remote supports.",
    )
    parser.add_argument(
        "--pole-geometry-remote-max-ground-rmse-m",
        type=float,
        default=0.15,
        help="Maximum local-ground RMSE for class-free remote supports.",
    )
    parser.add_argument("--pole-min-points", type=int, default=18)
    parser.add_argument("--pole-max-axis-tilt-deg", type=float, default=15.0)
    parser.add_argument(
        "--pole-axis-plumb-max-tilt-deg",
        type=float,
        default=4.0,
        help=(
            "Represent shafts this close to vertical with a plumb line through "
            "robust upper/lower Z-bin centres."
        ),
    )
    parser.add_argument(
        "--pole-axis-plumb-full-tilt-deg",
        type=float,
        default=2.0,
        help=(
            "Fully plumb axes up to this endpoint tilt, then smoothly retain "
            "the measured tilt up to --pole-axis-plumb-max-tilt-deg."
        ),
    )
    parser.add_argument(
        "--pole-axis-plumb-endpoint-fraction",
        type=float,
        default=0.20,
        help="Fraction of robust shaft Z-bins used in each endpoint centre.",
    )
    parser.add_argument("--pole-direct-max-axis-sign-distance-m", type=float, default=0.75)
    parser.add_argument("--pole-max-axis-sign-distance-m", type=float, default=8.0)
    parser.add_argument("--pole-horizontal-connection-radius-m", type=float, default=0.25)
    parser.add_argument(
        "--pole-horizontal-connection-z-tolerance-m",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--pole-horizontal-connection-above-tolerance-m",
        type=float,
        default=0.30,
        help=(
            "Maximum height above the detected object accepted as connected mast-arm evidence."
        ),
    )
    parser.add_argument("--pole-horizontal-connection-bin-m", type=float, default=0.25)
    parser.add_argument(
        "--pole-horizontal-connection-min-points-per-bin",
        type=int,
        default=2,
    )
    parser.add_argument(
        "--pole-horizontal-connection-coherence-radius-m",
        type=float,
        default=0.10,
    )
    parser.add_argument(
        "--pole-min-horizontal-connection-coverage",
        type=float,
        default=0.50,
    )
    parser.add_argument(
        "--pole-min-horizontal-connection-coherent-ratio",
        type=float,
        default=0.65,
    )
    parser.add_argument(
        "--pole-min-horizontal-connection-coherent-point-fraction",
        type=float,
        default=0.30,
    )
    parser.add_argument(
        "--pole-remote-max-endpoint-tilt-deg",
        type=float,
        default=5.0,
    )
    parser.add_argument("--pole-long-remote-distance-m", type=float, default=8.0)
    parser.add_argument("--pole-long-remote-transition-m", type=float, default=2.0)
    parser.add_argument(
        "--pole-long-remote-min-vertical-span-m",
        type=float,
        default=3.5,
    )
    parser.add_argument(
        "--pole-long-remote-min-completeness-ratio",
        type=float,
        default=0.85,
    )
    parser.add_argument(
        "--pole-long-remote-min-connection-coverage-ratio",
        type=float,
        default=0.85,
    )
    parser.add_argument("--pole-max-ground-class-fraction", type=float, default=0.35)
    parser.add_argument("--pole-min-ground-drop-m", type=float, default=1.8)
    parser.add_argument(
        "--pole-require-ground",
        action="store_true",
        help="Reject a pole bottom when no defensible local ground plane is available.",
    )
    parser.add_argument("--pole-ground-search-radius-m", type=float, default=1.5)
    parser.add_argument("--pole-ground-core-radius-m", type=float, default=0.75)
    parser.add_argument("--pole-ground-exclusion-radius-m", type=float, default=0.24)
    parser.add_argument("--pole-ground-cell-size-m", type=float, default=0.25)
    parser.add_argument("--pole-ground-cell-quantile", type=float, default=0.10)
    parser.add_argument("--pole-ground-min-cells", type=int, default=6)
    parser.add_argument("--pole-ground-max-rmse-m", type=float, default=0.20)
    parser.add_argument(
        "--pole-ground-geometry-preference-margin-m",
        type=float,
        default=0.10,
    )
    parser.add_argument("--pole-occlusion-gap-m", type=float, default=0.35)
    parser.add_argument("--pole-max-ground-penetration-m", type=float, default=0.10)
    parser.add_argument("--pole-max-ground-support-distance-m", type=float, default=0.35)
    parser.add_argument(
        "--pole-ground-class-ids",
        type=parse_class_id_list,
        default=(2, 11),
    )
    parser.add_argument(
        "--pole-class-ids",
        type=parse_class_id_list,
        default=(),
    )
    parser.add_argument(
        "--pole-excluded-pole-class-ids",
        type=parse_class_id_list,
        default=(3, 4, 5),
    )
    parser.add_argument("--pole-observation-merge-radius-m", type=float, default=0.75)
    parser.add_argument("--pole-min-observations", type=int, default=1)
    parser.add_argument("--sign-observation-merge-xy-radius-m", type=float, default=0.25)
    parser.add_argument("--sign-observation-merge-z-radius-m", type=float, default=0.25)
    parser.add_argument("--sign-observation-fallback-xy-radius-m", type=float, default=0.15)
    parser.add_argument("--sign-observation-fallback-z-radius-m", type=float, default=0.20)
    parser.add_argument(
        "--disable-pole-debug",
        action="store_true",
        help="Do not save the wide sign-and-pole QA image when pole detection is enabled.",
    )
    parser.add_argument(
        "--disable-pole-point-crop",
        action="store_true",
        help="Do not save the separate pole-axis LAS point crop.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip images whose txt result already exists.",
    )
    parser.add_argument(
        "--allow-unsafe-cuda-multiprocessing",
        action="store_true",
        help="Allow multiple workers to share the configured CUDA device. Disabled by default.",
    )
    parser.add_argument(
        "--worker-progress-every",
        type=int,
        default=10,
        help="How often each worker logs image progress.",
    )
    parser.add_argument(
        "--progress-log-interval-sec",
        type=int,
        default=60,
        help="Heartbeat interval for main-process waiting logs.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Minimum detail level written to output-dir/logs. Console output remains a progress bar.",
    )
    parser.add_argument(
        "--disable-console-progress",
        action="store_true",
        help="Disable the console tqdm progress bar (file logging is unaffected).",
    )
    parser.add_argument(
        "--disable-intermediate-shp",
        action="store_true",
        help="Disable intermediate SHP refresh while workers are still running.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start index inside the sorted image task list.",
    )
    parser.add_argument(
        "--limit-images",
        type=int,
        default=0,
        help="Limit how many images to process. Use 0 for all images.",
    )
    return parser


def validate_point_range_fallback_arguments(args: argparse.Namespace) -> None:
    """Validate cross-field constraints for the conditional range retry."""

    if not bool(getattr(args, "point_range_fallback_enabled", False)):
        return
    strict_range_m = float(getattr(args, "max_range_m", 0.0))
    fallback_range_m = float(
        getattr(args, "point_range_fallback_max_range_m", strict_range_m)
    )
    if fallback_range_m <= strict_range_m:
        raise ValueError(
            "point_range_fallback_max_range_m must be greater than max_range_m "
            "when point_range_fallback_enabled is true"
        )
    standard_minimum = int(getattr(args, "min_point_count", 1))
    fallback_minimum = int(
        getattr(args, "point_range_fallback_min_point_count", standard_minimum)
    )
    if fallback_minimum > standard_minimum:
        raise ValueError(
            "point_range_fallback_min_point_count cannot exceed min_point_count"
        )


def setup_logging(
    log_path: Path,
    *,
    file_mode: str = "a",
    logger_name: str | None = None,
    level: str | int = logging.INFO,
    capture_root: bool = True,
) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name or f"mms_shp_detection_{os.getpid()}")
    resolved_level = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    if not isinstance(resolved_level, int):
        raise ValueError(f"Unsupported log level: {level!r}")
    logger.setLevel(resolved_level)
    for existing_handler in list(logger.handlers):
        logger.removeHandler(existing_handler)
        existing_handler.close()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(processName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8", mode=file_mode)
    file_handler.setLevel(resolved_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if capture_root:
        # Capture warnings and third-party Python log records in a file as well.
        # Parallel model loggers deliberately skip this process-global handler.
        root_logger = logging.getLogger()
        root_logger.setLevel(resolved_level)
        for handler in list(root_logger.handlers):
            if getattr(handler, "_mms_file_handler", False):
                root_logger.removeHandler(handler)
                handler.close()
        root_file_handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
        root_file_handler.setLevel(resolved_level)
        root_file_handler.setFormatter(formatter)
        root_file_handler._mms_file_handler = True  # type: ignore[attr-defined]
        root_logger.addHandler(root_file_handler)
        logging.captureWarnings(True)
    return logger


def sanitize_name(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    safe = safe.strip("._")
    return safe or "item"


_MODEL_FILTER_SCALAR_KEYS = {
    "imgsz",
    "conf",
    "iou",
    "max_det",
    "point_padding_px",
    "max_range_m",
    "depth_window_m",
    "front_surface_quantile",
    "front_surface_min_support",
    "block_angle_margin_deg",
    "max_center_ray_angle_deg",
    "min_point_count",
    "perspective_view_size",
    "perspective_margin_deg",
    "perspective_min_fov_deg",
    "perspective_max_fov_deg",
}


def _model_filter_key_allowed(key: str) -> bool:
    return (
        key in _MODEL_FILTER_SCALAR_KEYS
        or key.startswith("point_range_fallback_")
        or key.startswith("cluster_")
        or key.startswith("pole_")
        or key.startswith("sign_observation_")
    )


def discover_model_paths(
    model_dir: Path | None,
    model_path: Path | None,
) -> list[Path]:
    """Resolve the stable, non-recursive model execution list."""

    if model_dir is None:
        if model_path is None:
            raise ValueError("Either model_dir or model_path must be configured")
        resolved = model_path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Model checkpoint does not exist: {resolved}")
        return [resolved]

    resolved_dir = model_dir.expanduser().resolve()
    if not resolved_dir.is_dir():
        raise FileNotFoundError(f"Model directory does not exist: {resolved_dir}")
    paths = sorted(
        (
            item.resolve()
            for item in resolved_dir.iterdir()
            if item.is_file() and item.suffix.casefold() == ".pt"
        ),
        key=lambda item: (item.name.casefold(), item.name),
    )
    if not paths:
        raise FileNotFoundError(f"No .pt model checkpoints found in: {resolved_dir}")

    output_keys: dict[str, Path] = {}
    for path in paths:
        output_key = sanitize_name(path.stem).casefold()
        previous = output_keys.get(output_key)
        if previous is not None:
            raise ValueError(
                "Model names collide after output-path sanitization: "
                f"{previous.name!r} and {path.name!r}"
            )
        output_keys[output_key] = path
    return paths


def _flatten_model_filter_profile(
    profile_name: str,
    profile: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    object_type = "generic"
    leaves: dict[str, Any] = {}
    source_paths: dict[str, str] = {}

    def visit(value: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        nonlocal object_type
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ConfigError(
                    f"model_filters.{profile_name} contains an invalid key"
                )
            key = raw_key.strip().replace("-", "_")
            path = (*prefix, key)
            dotted = ".".join(("model_filters", profile_name, *path))
            if isinstance(item, dict):
                visit(item, path)
                continue
            if key == "object_type":
                if prefix:
                    raise ConfigError(f"'{dotted}' must be at profile top level")
                if item not in {"traffic_sign", "traffic_signal", "generic"}:
                    raise ConfigError(
                        f"'{dotted}' must be traffic_sign, traffic_signal, or generic"
                    )
                object_type = str(item)
                continue
            if not _model_filter_key_allowed(key):
                raise ConfigError(
                    f"'{dotted}' is not a permitted model-specific filter"
                )
            if key in leaves:
                raise ConfigError(
                    f"model filter '{key}' is defined more than once in "
                    f"'{source_paths[key]}' and '{dotted}'"
                )
            leaves[key] = item
            source_paths[key] = dotted

    visit(profile)
    return object_type, leaves


def _convert_model_filter_value(
    key: str,
    raw_value: Any,
    current_value: Any,
    *,
    dotted_key: str,
) -> Any:
    if isinstance(current_value, bool):
        if not isinstance(raw_value, bool):
            raise ConfigError(f"'{dotted_key}' must be a YAML boolean")
        converted = raw_value
    elif isinstance(current_value, int) and not isinstance(current_value, bool):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ConfigError(f"'{dotted_key}' must be an integer")
        if isinstance(raw_value, float) and not raw_value.is_integer():
            raise ConfigError(f"'{dotted_key}' must be an integer")
        converted = int(raw_value)
    elif isinstance(current_value, float):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ConfigError(f"'{dotted_key}' must be a number")
        converted = float(raw_value)
    elif isinstance(current_value, tuple):
        try:
            converted = parse_class_id_list(raw_value)
        except argparse.ArgumentTypeError as exc:
            raise ConfigError(f"Invalid '{dotted_key}': {exc}") from exc
    elif isinstance(current_value, str):
        if not isinstance(raw_value, str):
            raise ConfigError(f"'{dotted_key}' must be a string")
        converted = raw_value
    else:
        raise ConfigError(
            f"'{dotted_key}' cannot override unsupported value type "
            f"{type(current_value).__name__}"
        )
    validate_config_value(key, converted, dotted_key)
    return converted


def apply_model_filter(
    args: argparse.Namespace,
    model_path: Path,
    *,
    require_profile: bool,
) -> tuple[argparse.Namespace, str, str]:
    """Clone args and apply the filter profile matching one model file."""

    configured = getattr(args, "model_filters", {}) or {}
    matches = [
        name
        for name in configured
        if name.casefold() in {model_path.name.casefold(), model_path.stem.casefold()}
    ]
    if len(matches) > 1:
        raise ConfigError(
            f"Model {model_path.name!r} matches more than one filter profile: {matches}"
        )
    if require_profile and not matches:
        raise ConfigError(
            f"No model_filters profile matches discovered model {model_path.name!r}"
        )

    selected_name = matches[0] if matches else "<base>"
    object_type = "generic"
    leaves: dict[str, Any] = {}
    if matches:
        object_type, leaves = _flatten_model_filter_profile(
            selected_name,
            configured[selected_name],
        )

    effective = copy.deepcopy(args)
    effective.model_path = model_path.resolve()
    effective.model_dir = None
    effective.model_filters = {}
    effective.model_profile = selected_name
    effective.model_object_type = object_type
    cli_override_dests = set(getattr(args, "_cli_override_dests", ()))
    for key, raw_value in leaves.items():
        if key in cli_override_dests:
            continue
        if not hasattr(effective, key):
            raise ConfigError(
                f"model filter {selected_name!r} refers to unknown option {key!r}"
            )
        dotted = f"model_filters.{selected_name}.{key}"
        setattr(
            effective,
            key,
            _convert_model_filter_value(
                key,
                raw_value,
                getattr(effective, key),
                dotted_key=dotted,
            ),
        )
    return effective, selected_name, object_type


def ensure_output_dirs(
    output_dir: Path,
    *,
    shared_forward_views_dir: Path | None = None,
) -> dict[str, Path]:
    dirs = {
        "root": output_dir,
        "txt": output_dir / "txt",
        "forward_views": (
            shared_forward_views_dir
            if shared_forward_views_dir is not None
            else output_dir / "forward_views"
        ),
        "image_crops": output_dir / "image_crops",
        "point_crops": output_dir / "point_crops",
        "point_previews": output_dir / "point_previews",
        "pole_crops": output_dir / "pole_crops",
        "pole_debug": output_dir / "pole_debug",
        "logs": output_dir / "logs",
        "cache": output_dir / "cache",
        "shp": output_dir / "shp",
    }
    for path in dict.fromkeys(dirs.values()):
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def resolve_device(device: str) -> str:
    if device != "auto":
        return device

    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


def resolve_num_workers(args: argparse.Namespace, device: str, logger) -> int:
    requested = max(1, args.num_workers)
    if not device.startswith("cuda"):
        return requested

    # Runtime currently carries one explicit device string (for example cuda:0)
    # into every spawned worker. Until workers receive distinct device IDs,
    # more GPUs do not make multiple workers safe.
    safe_limit = 1
    if requested > safe_limit and not args.allow_unsafe_cuda_multiprocessing:
        logger.warning(
            "Requested %d workers on %s, but workers currently share one configured CUDA device. "
            "Falling back to %d worker. Use --allow-unsafe-cuda-multiprocessing to override.",
            requested,
            device,
            safe_limit,
        )
        return safe_limit
    return requested


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_dataset_signature(image_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a stable signature for all image, pose, sidecar, and calibration inputs.

    Image bytes are deliberately not hashed because a Leica delivery can contain
    hundreds of multi-megapixel panoramas.  File size and nanosecond mtime still
    invalidate ordinary image replacements, while parsed pose and panorama values
    make EO and sidecar changes independent of CSV/text formatting.
    """

    stat_cache: dict[str, dict[str, Any]] = {}

    def file_stat(path_value: str | Path) -> dict[str, Any]:
        path = Path(path_value).resolve()
        path_text = str(path)
        cached = stat_cache.get(path_text)
        if cached is not None:
            return cached
        stat = path.stat()
        signature = {
            "path": path_text,
            "file_size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
        stat_cache[path_text] = signature
        return signature

    pose_keys = (
        "pose_format",
        "pose_row_number",
        "timestamp_iso",
        "timestamp_source",
        "gps_sow_seconds",
        "gps_week",
        "gps_week_source",
        "gps_week_inferred",
        "gps_utc_offset_seconds",
        "origin",
        "direction",
        "up",
        "right",
        "rotation_local_to_world",
        "roll_deg",
        "pitch_deg",
        "yaw_deg",
        "omega_deg",
        "phi_deg",
        "kappa_deg",
        "omega_gon",
        "phi_gon",
        "kappa_gon",
    )
    calibration_keys = (
        "calibration_sha256",
        "job",
        "track",
        "imaging_sensor_id",
        "imaging_sensor_name",
        "raw_camera_serials",
        "gps_week",
        "manufacturer",
        "model_name",
        "system_serial_number",
        "internal_orientation_path",
        "application",
    )

    records: list[dict[str, Any]] = []
    ordered_tasks = sorted(
        image_tasks,
        key=lambda item: (
            str(item.get("record_name", "")),
            str(item.get("image_path", "")),
            int(item.get("pose_row_number") or 0),
            str(item.get("timestamp_iso", "")),
        ),
    )
    for task in ordered_tasks:
        panorama = dict(task.get("panorama") or {})
        sidecar_path = panorama.pop("sidecar_path", None)
        calibration = task.get("calibration") or {}
        pose_csv_path = task.get("pose_csv_path")
        records.append(
            {
                "identity": {
                    "image_name": task.get("image_name"),
                    "image_stem": task.get("image_stem"),
                    "record_name": task.get("record_name"),
                    "route_id": task.get("route_id"),
                    "job_name": task.get("job_name"),
                    "track_name": task.get("track_name"),
                },
                "image_file": file_stat(task["image_path"]),
                "pose_file": file_stat(pose_csv_path) if pose_csv_path else None,
                "pose": {key: task.get(key) for key in pose_keys},
                "panorama": panorama,
                "sidecar_file": file_stat(sidecar_path) if sidecar_path else None,
                "calibration": {
                    key: calibration.get(key) for key in calibration_keys
                }
                if calibration
                else None,
            }
        )

    canonical = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "signature_version": DATASET_SIGNATURE_VERSION,
        "task_count": len(records),
        "image_file_count": len(
            {record["image_file"]["path"] for record in records}
        ),
        "pose_file_count": len(
            {
                record["pose_file"]["path"]
                for record in records
                if record["pose_file"] is not None
            }
        ),
        "sidecar_file_count": len(
            {
                record["sidecar_file"]["path"]
                for record in records
                if record["sidecar_file"] is not None
            }
        ),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def build_panorama_alignment_qa_fingerprint(
    image_tasks: list[dict[str, Any]],
    pointcloud_catalog: dict[str, Any],
    args: argparse.Namespace,
    *,
    dataset_signature: dict[str, Any] | None = None,
) -> str:
    """Fingerprint every input that can affect panorama alignment QA.

    The point-cloud catalog signature already contains source paths, sizes, and
    nanosecond mtimes.  Hashing the estimator and its direct processing
    dependencies also invalidates reports when the QA implementation changes.
    """

    package_root = Path(__file__).resolve().parent
    code_sha256 = {
        name: _sha256_file(package_root / name)
        for name in (
            "alignment.py",
            "geometry.py",
            "pcdb.py",
            "pointcloud.py",
        )
    }
    parameters = {
        "pointcloud_neighbor_count": max(
            1, int(getattr(args, "pointcloud_neighbor_count"))
        ),
        "sample_images": int(getattr(args, "alignment_qa_sample_images")),
        "max_points_per_image": int(
            getattr(args, "alignment_qa_max_points_per_image")
        ),
        "search_radius_px": int(getattr(args, "alignment_qa_search_radius_px")),
        "trim_fraction": float(getattr(args, "alignment_qa_trim_fraction")),
        "minimum_range_m": float(getattr(args, "alignment_qa_min_range_m")),
        "maximum_range_m": float(getattr(args, "alignment_qa_max_range_m")),
        "minimum_valid_samples": int(
            getattr(args, "alignment_qa_min_valid_samples")
        ),
        "maximum_mad_px": float(getattr(args, "alignment_qa_max_mad_px")),
        "base_yaw_offset_deg": float(
            getattr(args, "panorama_yaw_offset_deg", 0.0)
        ),
        "base_pitch_offset_deg": float(
            getattr(args, "panorama_pitch_offset_deg", 0.0)
        ),
    }
    payload = {
        "cache_version": PANORAMA_ALIGNMENT_QA_CACHE_VERSION,
        "estimator_version": PANORAMA_ALIGNMENT_QA_ESTIMATOR_VERSION,
        "code_sha256": code_sha256,
        "parameters": parameters,
        "dataset_signature": dataset_signature
        if dataset_signature is not None
        else build_dataset_signature(image_tasks),
        "pointcloud_catalog": {
            "selected_source_type": pointcloud_catalog.get("selected_source_type"),
            "signature": pointcloud_catalog.get("signature"),
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_run_fingerprint(
    args: argparse.Namespace,
    pointcloud_catalog: dict[str, Any],
    calibration_bundle: dict[str, Any] | None,
    dataset_signature: dict[str, Any],
    *,
    model_sha256: str | None = None,
) -> str:
    excluded = {
        "output_dir",
        "model_dir",
        "model_filters",
        "pointcloud_cache_path",
        "skip_existing",
        "num_workers",
        "allow_unsafe_cuda_multiprocessing",
        "multi_model_parallel",
        "multi_model_inference_workers",
        "multi_model_pole_workers",
        "multi_model_queue_depth",
        "worker_progress_every",
        "progress_log_interval_sec",
        "log_level",
        "disable_console_progress",
        "disable_intermediate_shp",
        "start_index",
        "limit_images",
        "include_record_names",
        "include_job_names",
        "include_track_names",
        "frame_id_from",
        "frame_id_to",
        "alignment_qa_enabled",
        "alignment_qa_sample_images",
        "alignment_qa_max_points_per_image",
        "alignment_qa_search_radius_px",
        "alignment_qa_trim_fraction",
        "alignment_qa_min_range_m",
        "alignment_qa_max_range_m",
        "alignment_qa_min_valid_samples",
        "alignment_qa_max_mad_px",
    }
    parameters = {
        key: str(value.resolve()) if isinstance(value, Path) else value
        for key, value in vars(args).items()
        if key not in excluded and not key.startswith("_")
    }
    model_path = args.model_path.resolve()
    package_root = Path(__file__).resolve().parent
    code_sha256 = {
        name: _sha256_file(package_root / name)
        for name in (
            "alignment.py",
            "calibration.py",
            "dataset.py",
            "geometry.py",
            "pcdb.py",
            "pipeline.py",
            "pole.py",
            "pointcloud.py",
            "shp_writer.py",
        )
    }
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "code_sha256": code_sha256,
        "parameters": parameters,
        "model_sha256": model_sha256 or _sha256_file(model_path),
        "calibration_sha256": calibration_bundle.get("sha256") if calibration_bundle else None,
        "dataset_signature": dataset_signature,
        "pointcloud_source": pointcloud_catalog.get("selected_source_type"),
        "pointcloud_signature": pointcloud_catalog.get("signature"),
        "crs_wkt": pointcloud_catalog.get("resolved_crs_wkt", pointcloud_catalog.get("crs_wkt")),
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def resolve_matched_crs_wkt(
    image_tasks: list[dict[str, Any]],
    pointcloud_catalog: dict[str, Any],
    neighbor_count: int,
) -> str | None:
    """Resolve a valid, semantically common CRS for the files these tasks can use."""
    matched_files: dict[str, dict[str, Any]] = {}
    for task in image_tasks:
        for item in match_nearest_pointcloud_files(task, pointcloud_catalog, neighbor_count):
            matched_files[str(item.get("path"))] = item
    missing = [item.get("path") for item in matched_files.values() if not item.get("crs_wkt")]
    if missing:
        return None

    parsed: list[tuple[str, CRS]] = []
    for path, item in sorted(matched_files.items()):
        wkt = str(item["crs_wkt"]).replace("\x00", "").strip()
        try:
            parsed.append((path, CRS.from_wkt(wkt)))
        except CRSError as exc:
            raise ValueError(f"Invalid CRS WKT in matched point cloud {path}: {exc}") from exc

    if not parsed:
        fallback = pointcloud_catalog.get("crs_wkt")
        return validate_crs_wkt(str(fallback), label="point-cloud catalog") if fallback else None

    reference_path, reference_crs = parsed[0]
    inconsistent = [
        path for path, crs in parsed[1:] if not reference_crs.equals(crs)
    ]
    if inconsistent:
        raise ValueError(
            "Matched point-cloud files declare semantically inconsistent CRS: "
            + ", ".join([reference_path, *inconsistent])
        )
    return reference_crs.to_wkt()


def validate_crs_wkt(value: str, *, label: str) -> str:
    """Parse and normalize a CRS WKT, failing before any mislabeled output is written."""
    cleaned = value.replace("\x00", "").strip()
    if not cleaned:
        raise ValueError(f"Empty CRS WKT supplied for {label}")
    try:
        return CRS.from_wkt(cleaned).to_wkt()
    except CRSError as exc:
        raise ValueError(f"Invalid CRS WKT supplied for {label}: {exc}") from exc


def validate_pose_pointcloud_proximity(
    image_tasks: list[dict[str, Any]],
    pointcloud_catalog: dict[str, Any],
    neighbor_count: int,
    max_separation_m: float,
) -> float | None:
    """Catch gross pose/point-cloud CRS mismatches using matched XY bounding boxes.

    This cannot prove datum correctness.  It is intentionally only a sanity
    check for errors such as geographic-degree poses paired with projected LAS.
    """
    limit = float(max_separation_m)
    if limit <= 0.0:
        return None

    worst_distance = 0.0
    offenders: list[tuple[str, float]] = []
    for task in image_tasks:
        origin = task.get("origin")
        if origin is None or len(origin) < 2:
            raise ValueError(f"Pose has no XY origin: {task.get('image_name')}")
        origin_x, origin_y = float(origin[0]), float(origin[1])
        distances: list[float] = []
        for item in match_nearest_pointcloud_files(task, pointcloud_catalog, neighbor_count):
            minimum = item.get("file_min")
            maximum = item.get("file_max")
            if (
                not minimum
                or not maximum
                or len(minimum) < 2
                or len(maximum) < 2
                or minimum[0] is None
                or minimum[1] is None
                or maximum[0] is None
                or maximum[1] is None
            ):
                continue
            min_x, min_y = float(minimum[0]), float(minimum[1])
            max_x, max_y = float(maximum[0]), float(maximum[1])
            dx = min_x - origin_x if origin_x < min_x else origin_x - max_x if origin_x > max_x else 0.0
            dy = min_y - origin_y if origin_y < min_y else origin_y - max_y if origin_y > max_y else 0.0
            distances.append(math.hypot(dx, dy))

        if not distances:
            raise ValueError(
                f"No finite matched point-cloud XY bounds for pose {task.get('image_name')}"
            )
        nearest = min(distances)
        worst_distance = max(worst_distance, nearest)
        if nearest > limit:
            offenders.append((str(task.get("image_name")), nearest))

    if offenders:
        examples = ", ".join(f"{name}={distance:.1f}m" for name, distance in offenders[:5])
        raise ValueError(
            "Pose origins are too far from their matched point clouds; a CRS/job mismatch is likely "
            f"(limit={limit:.1f}m, examples: {examples})."
        )
    return worst_distance


def missing_result_artifacts(payload: dict[str, Any]) -> list[str]:
    """Return missing non-null artifacts referenced by a current result JSON."""
    detections = payload.get("detections")
    if not isinstance(detections, list):
        return ["<invalid detections payload>"]
    missing: list[str] = []
    panorama_detection = payload.get("panorama_detection") or {}
    if isinstance(panorama_detection, dict):
        forward_view_path = panorama_detection.get("forward_view_path")
        if forward_view_path and not Path(str(forward_view_path)).is_file():
            missing.append(str(forward_view_path))
    for detection in detections:
        if not isinstance(detection, dict):
            missing.append("<invalid detection payload>")
            continue
        for key in ("image_crop_path", "point_crop_path", "point_preview_path"):
            value = detection.get(key)
            if value and not Path(str(value)).is_file():
                missing.append(str(value))
        pole = detection.get("pole") or {}
        if isinstance(pole, dict):
            if pole.get("reason") == "processing_error":
                missing.append("<pole processing error>")
            for key in ("point_crop_path", "debug_image_path"):
                value = pole.get(key)
                if value and not Path(str(value)).is_file():
                    missing.append(str(value))
    return missing


def validate_panorama_image(image_task: dict[str, Any], image_rgb: np.ndarray) -> None:
    """Validate that the exported Sphere matches the projection implemented here."""
    metadata = image_task.get("panorama") or {}
    if metadata.get("projection", "equirectangular") != "equirectangular":
        raise ValueError(f"Unsupported panorama projection for {image_task['image_name']}: {metadata}")

    actual_height, actual_width = image_rgb.shape[:2]
    expected_width = metadata.get("image_width")
    expected_height = metadata.get("image_height")
    if expected_width is not None and int(expected_width) != actual_width:
        raise ValueError(
            f"Panorama width mismatch for {image_task['image_name']}: "
            f"image={actual_width}, sidecar={expected_width}"
        )
    if expected_height is not None and int(expected_height) != actual_height:
        raise ValueError(
            f"Panorama height mismatch for {image_task['image_name']}: "
            f"image={actual_height}, sidecar={expected_height}"
        )

    longitude_limits = metadata.get("longitude_limits_deg") or [-180.0, 180.0]
    latitude_limits = metadata.get("latitude_limits_deg") or [-90.0, 90.0]
    if not np.allclose(longitude_limits, [-180.0, 180.0], atol=1e-9):
        raise ValueError(
            f"Unsupported Sphere longitude limits for {image_task['image_name']}: {longitude_limits}"
        )
    if not np.allclose(latitude_limits, [-90.0, 90.0], atol=1e-9):
        raise ValueError(
            f"Unsupported Sphere latitude limits for {image_task['image_name']}: {latitude_limits}"
        )
    hotspot = metadata.get("panorama_hotspot")
    if hotspot is not None and not np.allclose(hotspot, [0.0, 0.0], atol=1e-9):
        raise ValueError(
            f"Unsupported non-zero Leica PanoramaHotSpot for {image_task['image_name']}: {hotspot}"
        )


def load_panorama_rgb(image_path: Path, logger) -> np.ndarray:
    """Decode strictly, then retry once if Pillow can recover a truncated JPEG."""

    try:
        with Image.open(image_path) as opened_image:
            return np.array(opened_image.convert("RGB"))
    except OSError as strict_error:
        previous_setting = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(image_path) as opened_image:
                recovered = np.array(opened_image.convert("RGB"))
        except Exception as recovery_error:
            raise strict_error from recovery_error
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous_setting

        logger.warning(
            "Recovered a truncated image after strict JPEG decoding failed: %s (%s)",
            image_path,
            strict_error,
        )
        return recovered


def count_txt_files(txt_dir: Path) -> int:
    if not txt_dir.exists():
        return 0
    return sum(1 for _ in txt_dir.rglob("*.txt"))


def attach_support_ids_to_detection_records(
    detection_records: list[dict[str, Any]],
    pole_relations: list[dict[str, Any]],
) -> None:
    """Add the pole relation join key to the matching sign SHP records."""

    support_by_detection = {
        str(item.get("detection_id") or ""): str(item.get("support_id") or "")
        for item in pole_relations
        if item.get("detection_id") and item.get("support_id")
    }
    for record in detection_records:
        record["support_id"] = support_by_detection.get(
            str(record.get("detection_id") or ""),
            "",
        )


def reconcile_remote_supports_from_direct_anchors(
    detection_records: list[dict[str, Any]],
    pole_observations: list[dict[str, Any]],
    *,
    direct_distance_m: float = 0.75,
    anchor_cluster_radius_m: float = 0.15,
    anchor_max_spread_m: float = 0.15,
    anchor_max_z_spread_m: float = 0.20,
    max_frame_gap: int = 2,
    max_link_distance_m: float = 12.0,
    uniqueness_margin_m: float = 1.0,
    hypothesis_anchor_radius_m: float = 0.30,
) -> list[dict[str, Any]]:
    """Add REVIEW relations for signals supported by repeated direct anchors.

    This is deliberately a final, multi-frame fallback.  It never changes an
    AUTO relation or a direct support.  A missing or REVIEW relation is changed
    only when the target frame contains a shaft-valid/arm-rejected hypothesis
    at the same position and the physical pole is observed directly in two
    nearby frames.
    """

    def finite_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def optional_integer(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        number = finite_number(value)
        if number is None or not number.is_integer():
            return None
        return int(number)

    def has_direct_association(item: dict[str, Any]) -> bool:
        association_distance = finite_number(
            item.get("association_distance_m")
        )
        return (
            association_distance is not None
            and 0.0 <= association_distance <= direct_distance_m
        )

    usable_observations: list[dict[str, Any]] = []
    for item in pole_observations:
        if not isinstance(item, dict):
            continue
        pole_xyz = tuple(
            finite_number(item.get(key))
            for key in ("pole_x", "pole_y", "pole_z")
        )
        if any(value is None for value in pole_xyz):
            continue
        association_value = item.get("association_distance_m")
        association_distance = (
            None
            if association_value is None
            else finite_number(association_value)
        )
        if (
            association_value is not None
            and (
                association_distance is None
                or association_distance < 0.0
            )
        ):
            continue
        normalized = dict(item)
        normalized.update(
            {
                "pole_x": pole_xyz[0],
                "pole_y": pole_xyz[1],
                "pole_z": pole_xyz[2],
            }
        )
        if "association_distance_m" in item:
            normalized["association_distance_m"] = association_distance
        for key in ("pole_quality", "confidence"):
            if key in item:
                normalized[key] = finite_number(item.get(key))
        for key in ("class_id", "detection_index"):
            if key in item:
                normalized[key] = optional_integer(item.get(key))
        usable_observations.append(normalized)

    existing_by_detection: dict[str, list[dict[str, Any]]] = {}
    for item in usable_observations:
        detection_id = str(item.get("detection_id") or "")
        if detection_id:
            existing_by_detection.setdefault(detection_id, []).append(item)
    direct: list[dict[str, Any]] = []
    for item in usable_observations:
        association_distance = item.get("association_distance_m")
        pose_row = optional_integer(item.get("pose_row_number"))
        if (
            str(item.get("pole_status") or "") != "AUTO"
            or association_distance is None
            or association_distance > direct_distance_m
            or pose_row is None
        ):
            continue
        normalized = dict(item)
        normalized["pose_row_number"] = pose_row
        direct.append(normalized)
    if len(direct) < 2:
        return list(usable_observations)
    clustered = cluster_pole_observations(
        [dict(item) for item in direct],
        radius_m=anchor_cluster_radius_m,
    )
    by_support: dict[str, list[dict[str, Any]]] = {}
    for item in clustered:
        by_support.setdefault(str(item.get("support_id") or ""), []).append(item)

    anchors: list[dict[str, Any]] = []
    for support_id, members in by_support.items():
        if not support_id or not members:
            continue
        representative = members[0]
        consensus_outlier_count = optional_integer(
            representative.get("consensus_outlier_count")
        )
        if (
            str(representative.get("pole_status") or "") != "AUTO"
            or consensus_outlier_count != 0
        ):
            continue
        frame_rows = sorted(
            {
                int(item["pose_row_number"])
                for item in members
                if item.get("pose_row_number") is not None
            }
        )
        source_detection_ids = sorted(
            {
                str(item.get("detection_id") or "")
                for item in members
                if str(item.get("detection_id") or "")
            }
        )
        if (
            int(representative.get("obs_count") or 0) < 2
            or len(frame_rows) < 2
            or representative.get("xy_spread_m") is None
            or float(representative["xy_spread_m"]) > anchor_max_spread_m
            or representative.get("z_spread_m") is None
            or float(representative["z_spread_m"]) > anchor_max_z_spread_m
        ):
            continue
        anchors.append(
            {
                "record_name": str(representative.get("record_name") or ""),
                "pole_x": float(representative["pole_x"]),
                "pole_y": float(representative["pole_y"]),
                "pole_z": float(representative["pole_z"]),
                "pose_rows": frame_rows,
                "source_detection_ids": source_detection_ids,
                "xy_spread_m": float(representative["xy_spread_m"]),
                "z_spread_m": float(representative["z_spread_m"]),
                "template": representative,
            }
        )

    reconciled = list(usable_observations)
    for detection in detection_records:
        if not isinstance(detection, dict):
            continue
        detection_id = str(detection.get("detection_id") or "")
        if (
            not detection_id
            or str(detection.get("model_object_type") or "")
            != "traffic_signal"
        ):
            continue
        existing_relations = existing_by_detection.get(detection_id, [])
        if any(has_direct_association(item) for item in existing_relations):
            continue
        if existing_relations and any(
            str(item.get("pole_status") or "") != "REVIEW"
            for item in existing_relations
        ):
            continue
        pole_payload = detection.get("pole")
        if not isinstance(pole_payload, dict):
            continue
        raw_hypotheses = pole_payload.get("support_hypotheses") or []
        hypotheses: list[tuple[np.ndarray, dict[str, Any]]] = []
        for hypothesis in raw_hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            try:
                raw_coverage = float(
                    hypothesis[
                        "horizontal_connection_coverage_ratio"
                    ]
                )
                coherent_coverage = float(
                    hypothesis[
                        "horizontal_connection_coherent_coverage_ratio"
                    ]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if (
                str(hypothesis.get("rejection_reason") or "")
                not in {"raw_coverage", "coherent_arm"}
                or not math.isfinite(raw_coverage)
                or raw_coverage < 0.20
                or not math.isfinite(coherent_coverage)
                or coherent_coverage < 0.075
                or hypothesis.get(
                    "horizontal_connection_endpoint_anchored"
                )
                is not True
            ):
                continue
            try:
                hypothesis_xy = np.asarray(
                    [
                        float(hypothesis["axis_x"]),
                        float(hypothesis["axis_y"]),
                    ],
                    dtype=np.float64,
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if np.all(np.isfinite(hypothesis_xy)):
                hypotheses.append(
                    (
                        hypothesis_xy,
                        {
                            "axis_x": float(hypothesis_xy[0]),
                            "axis_y": float(hypothesis_xy[1]),
                            "rejection_reason": str(
                                hypothesis.get("rejection_reason") or ""
                            ),
                            "horizontal_connection_coverage_ratio": (
                                raw_coverage
                            ),
                            "horizontal_connection_coherent_coverage_ratio": (
                                coherent_coverage
                            ),
                            "horizontal_connection_coherent_ratio": (
                                finite_number(
                                    hypothesis.get(
                                        "horizontal_connection_coherent_ratio"
                                    )
                                )
                            ),
                            "horizontal_connection_coherent_point_fraction": (
                                finite_number(
                                    hypothesis.get(
                                        "horizontal_connection_coherent_point_fraction"
                                    )
                                )
                            ),
                            "horizontal_connection_endpoint_anchored": True,
                        },
                    )
                )
        if not hypotheses:
            continue
        values = [
            detection.get("x"),
            detection.get("y"),
            detection.get("z"),
            detection.get("pose_row_number"),
        ]
        try:
            sign_x, sign_y, sign_z = (float(value) for value in values[:3])
            pose_row = int(values[3])
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (sign_x, sign_y, sign_z)):
            continue

        matches: list[
            tuple[float, float, dict[str, Any], dict[str, Any]]
        ] = []
        for anchor in anchors:
            if anchor["record_name"] != str(detection.get("record_name") or ""):
                continue
            nearby_anchor_rows = {
                row
                for row in anchor["pose_rows"]
                if abs(pose_row - row) <= max_frame_gap
            }
            if len(nearby_anchor_rows) < 2:
                continue
            distance = math.hypot(
                anchor["pole_x"] - sign_x,
                anchor["pole_y"] - sign_y,
            )
            if not direct_distance_m < distance <= max_link_distance_m:
                continue
            if sign_z - anchor["pole_z"] < 1.8:
                continue
            anchor_xy = np.asarray(
                [anchor["pole_x"], anchor["pole_y"]],
                dtype=np.float64,
            )
            hypothesis_distance, matched_hypothesis = min(
                (
                    (
                        float(np.linalg.norm(hypothesis_xy - anchor_xy)),
                        evidence,
                    )
                    for hypothesis_xy, evidence in hypotheses
                ),
                key=lambda item: item[0],
            )
            if hypothesis_distance > hypothesis_anchor_radius_m:
                continue
            matches.append(
                (
                    hypothesis_distance,
                    distance,
                    anchor,
                    matched_hypothesis,
                )
            )
        matches.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2]["pole_x"],
                item[2]["pole_y"],
                item[2]["pole_z"],
            )
        )
        if not matches:
            continue
        if (
            len(matches) > 1
            and matches[1][0] - matches[0][0]
            < min(uniqueness_margin_m, hypothesis_anchor_radius_m / 3.0)
        ):
            continue

        hypothesis_distance, distance, anchor, matched_hypothesis = matches[0]
        template = dict(anchor["template"])
        template.update(
            {
                "record_name": detection.get("record_name"),
                "detection_index": detection.get("detection_index"),
                "detection_id": detection_id,
                "class_id": detection.get("class_id"),
                "class_name": detection.get("class_name"),
                "confidence": detection.get("confidence"),
                "image_name": detection.get("image_name"),
                "timestamp_iso": detection.get("timestamp_iso"),
                "pose_row_number": pose_row,
                "sign_x": sign_x,
                "sign_y": sign_y,
                "sign_z": sign_z,
                "pole_x": anchor["pole_x"],
                "pole_y": anchor["pole_y"],
                "pole_z": anchor["pole_z"],
                "pole_type": "SINGLE",
                "pole_method": "MULTI_FRAME_DIRECT_ANCHOR",
                "pole_status": "REVIEW",
                "pole_occluded": None,
                "pole_occlusion_status": "UNKNOWN",
                "pole_quality": 0.0,
                "association_distance_m": distance,
                "horizontal_connection_coverage_ratio": (
                    matched_hypothesis[
                        "horizontal_connection_coverage_ratio"
                    ]
                ),
                "horizontal_connection_coherent_coverage_ratio": (
                    matched_hypothesis[
                        "horizontal_connection_coherent_coverage_ratio"
                    ]
                ),
                "horizontal_connection_coherent_ratio": matched_hypothesis[
                    "horizontal_connection_coherent_ratio"
                ],
                "horizontal_connection_coherent_point_fraction": (
                    matched_hypothesis[
                        "horizontal_connection_coherent_point_fraction"
                    ]
                ),
                "horizontal_connection_endpoint_anchored": (
                    matched_hypothesis[
                        "horizontal_connection_endpoint_anchored"
                    ]
                ),
                "pole_search_mode": "multi_frame_direct_anchor",
                "pole_fallback_attempted": False,
                "pole_fallback_used": False,
                "pole_point_crop_path": None,
                "pole_debug_image_path": None,
                "support_reconciled": True,
                "support_hypothesis_distance_m": hypothesis_distance,
                "support_hypothesis_axis_x": matched_hypothesis["axis_x"],
                "support_hypothesis_axis_y": matched_hypothesis["axis_y"],
                "support_hypothesis_rejection_reason": matched_hypothesis[
                    "rejection_reason"
                ],
                "support_anchor_pose_rows": list(anchor["pose_rows"]),
                "support_anchor_source_detection_ids": list(
                    anchor["source_detection_ids"]
                ),
                "support_anchor_xy_spread_m": anchor["xy_spread_m"],
                "support_anchor_z_spread_m": anchor["z_spread_m"],
                "support_reconciled_replaced_remote": bool(
                    existing_relations
                ),
            }
        )
        if existing_relations:
            reconciled = [
                item
                for item in reconciled
                if str(item.get("detection_id") or "") != detection_id
            ]
        reconciled.append(template)
        existing_by_detection[detection_id] = [template]
    return reconciled


def refresh_shapefile_from_txt(
    txt_dir: Path,
    shp_path: Path,
    logger,
    *,
    reason: str,
    run_fingerprint: str,
    crs_wkt: str | None,
    pole_shp_path: Path | None = None,
    pole_merge_radius_m: float = 0.75,
    pole_min_observations: int = 1,
    sign_merge_xy_radius_m: float = 0.25,
    sign_merge_z_radius_m: float = 0.25,
    sign_fallback_xy_radius_m: float = 0.15,
    sign_fallback_z_radius_m: float = 0.20,
) -> int:
    records = collect_detection_records(txt_dir, logger=logger, run_fingerprint=run_fingerprint)
    pole_relations: list[dict[str, Any]] = []
    if pole_shp_path is not None:
        pole_observations = collect_pole_records(
            txt_dir,
            logger=logger,
            run_fingerprint=run_fingerprint,
        )
        pole_observations = reconcile_remote_supports_from_direct_anchors(
            records,
            pole_observations,
        )
        pole_relations = (
            cluster_pole_observations(pole_observations, radius_m=pole_merge_radius_m)
            if pole_observations
            else []
        )
        pole_relations = [
            item
            for item in pole_relations
            if int(item.get("obs_count") or 1) >= pole_min_observations
        ]
    attach_support_ids_to_detection_records(records, pole_relations)
    records, pole_relations = deduplicate_sign_and_pole_observations(
        records,
        pole_relations,
        supported_xy_radius_m=sign_merge_xy_radius_m,
        supported_z_radius_m=sign_merge_z_radius_m,
        unsupported_xy_radius_m=sign_fallback_xy_radius_m,
        unsupported_z_radius_m=sign_fallback_z_radius_m,
    )
    write_shapefile(records, shp_path, crs_wkt=crs_wkt)
    logger.info("Refreshed SHP (%s) with %d features at %s", reason, len(records), shp_path)
    if pole_shp_path is not None:
        write_pole_shapefile(pole_relations, pole_shp_path, crs_wkt=crs_wkt)
        logger.info(
            "Refreshed pole SHP (%s) with %d features at %s",
            reason,
            len(pole_relations),
            pole_shp_path,
        )
    return len(records)


def safely_refresh_shapefile_from_txt(
    txt_dir: Path,
    shp_path: Path,
    logger,
    **kwargs: Any,
) -> int | None:
    """Best-effort intermediate export; final publication remains strict."""

    try:
        return refresh_shapefile_from_txt(txt_dir, shp_path, logger, **kwargs)
    except Exception:
        logger.exception(
            "Intermediate SHP refresh failed and will be retried later: %s",
            shp_path,
        )
        return None


def remove_generated_shapefile_bundle(shp_path: Path, logger) -> None:
    """Remove only the exact temporary bundle created by this pipeline."""

    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2"):
        component = shp_path.with_suffix(suffix)
        try:
            component.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove completed intermediate artifact: %s", component)


def split_chunks(items: list[dict[str, Any]], num_chunks: int) -> list[list[dict[str, Any]]]:
    if not items:
        return []
    num_chunks = max(1, min(num_chunks, len(items)))
    chunks = [[] for _ in range(num_chunks)]
    for index, item in enumerate(items):
        chunks[index % num_chunks].append(item)
    return chunks


def build_panorama_tiles(
    image_width: int,
    image_height: int,
    tile_width_px: int,
    tile_height_px: int,
    tile_overlap_px: int,
) -> list[dict[str, Any]]:
    sector_count = 4
    output_width = max(256, tile_width_px if tile_width_px > 0 else image_width // sector_count)
    output_height = max(256, tile_height_px if tile_height_px > 0 else image_height)
    base_hfov_deg = 360.0 / sector_count
    overlap_hfov_deg = (float(tile_overlap_px) / float(max(1, image_width))) * 360.0
    hfov_deg = min(170.0, base_hfov_deg + overlap_hfov_deg)
    vfov_deg = math.degrees(
        2.0
        * math.atan(
            (output_height / max(1.0, output_width))
            * math.tan(math.radians(hfov_deg) * 0.5)
        )
    )
    vfov_deg = min(170.0, max(hfov_deg * 0.5, vfov_deg))

    tiles: list[dict[str, Any]] = []
    for tile_index in range(sector_count):
        center_x = image_width * ((tile_index + 0.5) / sector_count)
        tiles.append(
            {
                "tile_index": tile_index,
                "center_x": float(center_x),
                "output_width": int(output_width),
                "output_height": int(output_height),
                "hfov_deg": float(hfov_deg),
                "vfov_deg": float(vfov_deg),
            }
        )
    return tiles


def build_forward_detection_mapping(
    image_width: int,
    image_height: int,
    *,
    view_size: int,
    hfov_deg: float,
    vfov_deg: float,
    yaw_offset_deg: float = 0.0,
    pitch_offset_deg: float = 0.0,
) -> dict[str, Any]:
    """Describe one rectilinear YOLO view centered on the vehicle forward axis."""

    if image_width <= 0 or image_height <= 0 or view_size < 256:
        raise ValueError("Forward detection image dimensions are invalid")
    if not 1.0 <= hfov_deg < 180.0 or not 1.0 <= vfov_deg < 180.0:
        raise ValueError("Forward detection FOV values must be in [1, 180) degrees")
    vehicle_forward_vec = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    vehicle_right_vec = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
    vehicle_up_vec = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    pano_forward_vec, pano_right_vec, pano_up_vec = apply_panorama_angular_offsets(
        vehicle_forward_vec,
        vehicle_right_vec,
        vehicle_up_vec,
        yaw_offset_deg=yaw_offset_deg,
        pitch_offset_deg=pitch_offset_deg,
    )
    return {
        "tile_index": 0,
        "center_x": image_width * 0.5,
        "output_width": int(view_size),
        "output_height": int(view_size),
        "hfov_deg": float(hfov_deg),
        "vfov_deg": float(vfov_deg),
        # The output stays centred on the raw EO/vehicle forward axis while the
        # corrected pano basis shifts the source sampling by the residual EO.
        "view_forward_vec": vehicle_forward_vec,
        "view_right_vec": vehicle_right_vec,
        "view_up_vec": vehicle_up_vec,
        "pano_forward_vec": pano_forward_vec,
        "pano_right_vec": pano_right_vec,
        "pano_up_vec": pano_up_vec,
        "pano_width": int(image_width),
        "pano_height": int(image_height),
    }


def create_forward_detection_qa_image(
    forward_rgb: np.ndarray,
    *,
    hfov_deg: float,
    vfov_deg: float,
    max_center_ray_angle_deg: float,
) -> Image.Image:
    """Annotate a copy of the exact rectilinear YOLO source for visual QA."""

    source = np.asarray(forward_rgb)
    if source.ndim != 3 or source.shape[2] != 3:
        raise ValueError("Forward QA source must be an RGB image")
    height, width = source.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("Forward QA source dimensions are invalid")

    # Image.fromarray may share storage for some array layouts.  The explicit copy
    # guarantees the pixels handed to YOLO cannot be changed by QA annotation.
    canvas = Image.fromarray(source.copy()).convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    center_x = (width - 1) * 0.5
    center_y = (height - 1) * 0.5

    axis_color = (30, 225, 255, 225)
    limit_color = (255, 165, 30, 235)
    draw.line((0, center_y, width - 1, center_y), fill=axis_color, width=1)
    draw.line((center_x, 0, center_x, height - 1), fill=axis_color, width=1)
    cross_radius = max(8, int(round(min(width, height) * 0.0125)))
    draw.ellipse(
        (
            center_x - cross_radius,
            center_y - cross_radius,
            center_x + cross_radius,
            center_y + cross_radius,
        ),
        outline=axis_color,
        width=2,
    )

    limit_deg = float(max_center_ray_angle_deg)
    if 0.0 < limit_deg < 89.9:
        focal_x = width / (2.0 * math.tan(math.radians(float(hfov_deg)) * 0.5))
        focal_y = height / (2.0 * math.tan(math.radians(float(vfov_deg)) * 0.5))
        radius_x = focal_x * math.tan(math.radians(limit_deg))
        radius_y = focal_y * math.tan(math.radians(limit_deg))
        draw.ellipse(
            (
                center_x - radius_x,
                center_y - radius_y,
                center_x + radius_x,
                center_y + radius_y,
            ),
            outline=limit_color,
            width=2,
        )

    lines = (
        "FORWARD YOLO VIEW",
        f"HFOV/VFOV: {float(hfov_deg):.1f}/{float(vfov_deg):.1f} deg",
        f"allowed center-ray: +/-{limit_deg:.1f} deg",
        "cyan: forward axis | orange: angular limit",
    )
    top = 10
    for line in lines:
        bounds = draw.textbbox((12, top), line)
        draw.rectangle(
            (bounds[0] - 5, bounds[1] - 3, bounds[2] + 5, bounds[3] + 3),
            fill=(0, 0, 0, 165),
        )
        draw.text((12, top), line, fill=(255, 255, 255, 255))
        top = bounds[3] + 7
    return canvas.convert("RGB")


def save_forward_detection_qa_image(
    forward_rgb: np.ndarray,
    output_path: Path,
    *,
    hfov_deg: float,
    vfov_deg: float,
    max_center_ray_angle_deg: float,
) -> None:
    qa_image = create_forward_detection_qa_image(
        forward_rgb,
        hfov_deg=hfov_deg,
        vfov_deg=vfov_deg,
        max_center_ray_angle_deg=max_center_ray_angle_deg,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    qa_image.save(output_path, quality=95)


def bbox_iou_xyxy(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union_area = area_a + area_b - inter_area
    if union_area <= 0.0:
        return 0.0
    return float(inter_area / union_area)


def unwrap_panorama_x_coordinates(
    x_values: list[float],
    panorama_width: float,
    *,
    reference_x: float | None = None,
) -> list[float]:
    """Unwrap circular panorama X coordinates into the tightest local interval."""
    if not x_values or panorama_width <= 0:
        return x_values
    if reference_x is None:
        angles = np.asarray(x_values, dtype=np.float64) * (2.0 * math.pi / panorama_width)
        mean_angle = math.atan2(float(np.sin(angles).mean()), float(np.cos(angles).mean()))
        reference_x = (mean_angle % (2.0 * math.pi)) * panorama_width / (2.0 * math.pi)
    return [
        float(value + round((reference_x - value) / panorama_width) * panorama_width)
        for value in x_values
    ]


def circular_bbox_iou_xyxy(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
    panorama_width: float | None,
) -> float:
    if not panorama_width or panorama_width <= 0:
        return bbox_iou_xyxy(box_a, box_b)
    return max(
        bbox_iou_xyxy(
            box_a,
            (box_b[0] + shift, box_b[1], box_b[2] + shift, box_b[3]),
        )
        for shift in (-panorama_width, 0.0, panorama_width)
    )


def extract_detection_candidates_from_prediction(
    prediction,
    model: YOLO,
    *,
    detection_source: str,
    tile_offset_xy: tuple[int, int] | None = None,
    panorama_mapping: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    boxes = prediction.boxes
    masks = prediction.masks
    if boxes is None or len(boxes) == 0:
        return candidates

    xyxy = boxes.xyxy.cpu().numpy()
    class_ids = boxes.cls.cpu().numpy().astype(int)
    confidences = boxes.conf.cpu().numpy()
    polygons = masks.xy if masks is not None else [None] * len(xyxy)

    if tile_offset_xy is None:
        tile_offset_xy = (0, 0)
    offset_x, offset_y = tile_offset_xy

    for index, bbox in enumerate(xyxy, start=1):
        class_id = int(class_ids[index - 1])
        class_name = str(model.names[class_id])
        polygon_xy = None
        if panorama_mapping is None:
            bbox_xyxy = (
                float(bbox[0] + offset_x),
                float(bbox[1] + offset_y),
                float(bbox[2] + offset_x),
                float(bbox[3] + offset_y),
            )
            if polygons[index - 1] is not None:
                polygon = polygons[index - 1].astype(np.float64)
                polygon[:, 0] += offset_x
                polygon[:, 1] += offset_y
                polygon_xy = polygon.tolist()
            panorama_width = float(getattr(prediction, "orig_shape", (0, 0))[1] or 0)
        else:
            bbox_points = [
                (float(bbox[0]), float(bbox[1])),
                (float(bbox[0]), float(bbox[3])),
                (float(bbox[2]), float(bbox[1])),
                (float(bbox[2]), float(bbox[3])),
            ]
            mapped_points = []
            for pixel_x, pixel_y in bbox_points:
                ray = perspective_pixel_to_world_ray(
                    pixel_x,
                    pixel_y,
                    panorama_mapping["output_width"],
                    panorama_mapping["output_height"],
                    panorama_mapping["view_forward_vec"],
                    panorama_mapping["view_right_vec"],
                    panorama_mapping["view_up_vec"],
                    panorama_mapping["hfov_deg"],
                    panorama_mapping["vfov_deg"],
                )
                pano_u, pano_v = world_ray_to_equirectangular_pixel(
                    ray,
                    panorama_mapping["pano_forward_vec"],
                    panorama_mapping["pano_right_vec"],
                    panorama_mapping["pano_up_vec"],
                    panorama_mapping["pano_width"],
                    panorama_mapping["pano_height"],
                )
                mapped_points.append((float(pano_u), float(pano_v)))

            unwrapped_bbox_x = unwrap_panorama_x_coordinates(
                [point[0] for point in mapped_points],
                float(panorama_mapping["pano_width"]),
            )
            bbox_xyxy = (
                min(unwrapped_bbox_x),
                min(point[1] for point in mapped_points),
                max(unwrapped_bbox_x),
                max(point[1] for point in mapped_points),
            )
            if polygons[index - 1] is not None:
                polygon_xy = []
                for point in polygons[index - 1].astype(np.float64):
                    ray = perspective_pixel_to_world_ray(
                        float(point[0]),
                        float(point[1]),
                        panorama_mapping["output_width"],
                        panorama_mapping["output_height"],
                        panorama_mapping["view_forward_vec"],
                        panorama_mapping["view_right_vec"],
                        panorama_mapping["view_up_vec"],
                        panorama_mapping["hfov_deg"],
                        panorama_mapping["vfov_deg"],
                    )
                    pano_u, pano_v = world_ray_to_equirectangular_pixel(
                        ray,
                        panorama_mapping["pano_forward_vec"],
                        panorama_mapping["pano_right_vec"],
                        panorama_mapping["pano_up_vec"],
                        panorama_mapping["pano_width"],
                        panorama_mapping["pano_height"],
                    )
                    polygon_xy.append([float(pano_u), float(pano_v)])
                unwrapped_polygon_x = unwrap_panorama_x_coordinates(
                    [point[0] for point in polygon_xy],
                    float(panorama_mapping["pano_width"]),
                    reference_x=(bbox_xyxy[0] + bbox_xyxy[2]) * 0.5,
                )
                polygon_xy = [
                    [unwrapped_x, point[1]]
                    for unwrapped_x, point in zip(unwrapped_polygon_x, polygon_xy)
                ]
            panorama_width = float(panorama_mapping["pano_width"])

        candidates.append(
            {
                "class_id": class_id,
                "class_name": class_name,
                "confidence": float(confidences[index - 1]),
                "bbox_xyxy": bbox_xyxy,
                "mask_polygon": polygon_xy,
                "detection_sources": [detection_source],
                "panorama_width": panorama_width,
            }
        )
    return candidates


def merge_detection_candidates(
    candidates: list[dict[str, Any]],
    iou_threshold: float,
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    kept: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item["confidence"], reverse=True):
        suppress = False
        for existing in kept:
            if existing["class_id"] != candidate["class_id"]:
                continue
            panorama_width = existing.get("panorama_width") or candidate.get("panorama_width")
            if circular_bbox_iou_xyxy(
                existing["bbox_xyxy"], candidate["bbox_xyxy"], panorama_width
            ) >= iou_threshold:
                existing_sources = set(existing.get("detection_sources", []))
                existing_sources.update(candidate.get("detection_sources", []))
                existing["detection_sources"] = sorted(existing_sources)
                suppress = True
                break
        if not suppress:
            kept.append(candidate)

    kept.sort(key=lambda item: (-item["confidence"], item["class_id"]))
    return kept


def _is_cuda_out_of_memory(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "cuda" in text and "out of memory" in text


def run_yolo_prediction(
    model: YOLO,
    runtime: dict[str, Any],
    logger,
    *,
    coordinator: MultiModelCoordinator | None = None,
    **predict_kwargs: Any,
):
    """Run one prediction with shared concurrency control and OOM downgrade."""

    def predict_once(*, clear_cuda_cache: bool = False):
        stage = f"yolo/{runtime.get('model_key') or 'model'}"
        timing = coordinator.timed(stage) if coordinator is not None else nullcontext()
        gate = (
            coordinator.inference_gate.slot()
            if coordinator is not None
            else nullcontext()
        )
        with gate, timing:
            if clear_cuda_cache:
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    logger.debug(
                        "Could not clear the CUDA allocator after OOM.",
                        exc_info=True,
                    )
            return model.predict(**predict_kwargs)

    try:
        return predict_once()
    except RuntimeError as exc:
        if coordinator is None or not _is_cuda_out_of_memory(exc):
            raise
        changed = coordinator.downgrade_inference_after_oom()
        logger.warning(
            "Concurrent CUDA inference ran out of memory for %s; "
            "retrying with serialized model inference%s.",
            runtime.get("model_name") or runtime.get("model_key") or "model",
            " and keeping that mode" if changed else "",
        )

    # Retry only after leaving the first exception handler. Its traceback may
    # retain CUDA tensors; ending that scope lets reference counting release
    # them before empty_cache runs under the serialized inference gate.
    try:
        return predict_once(clear_cuda_cache=True)
    except RuntimeError as retry_exc:
        if not _is_cuda_out_of_memory(retry_exc):
            raise
        model_name = (
            runtime.get("model_name")
            or runtime.get("model_key")
            or "model"
        )
        raise PersistentCudaOutOfMemoryError(
            f"Serialized CUDA inference still ran out of memory for {model_name}; "
            "the model was circuit-broken for the remainder of this run."
        ) from retry_exc


@lru_cache(maxsize=4)
def _cached_forward_view_geometry(
    image_width: int,
    image_height: int,
    view_size: int,
    hfov_deg: float,
    vfov_deg: float,
    yaw_offset_deg: float,
    pitch_offset_deg: float,
) -> tuple[dict[str, Any], tuple[np.ndarray, np.ndarray]]:
    """Cache the fixed 1280² ray/remap grid instead of rebuilding it per frame."""

    mapping = build_forward_detection_mapping(
        image_width,
        image_height,
        view_size=view_size,
        hfov_deg=hfov_deg,
        vfov_deg=vfov_deg,
        yaw_offset_deg=yaw_offset_deg,
        pitch_offset_deg=pitch_offset_deg,
    )
    remap_xy = build_perspective_panorama_remap(
        image_width,
        image_height,
        mapping["pano_forward_vec"],
        mapping["pano_right_vec"],
        mapping["pano_up_vec"],
        mapping["view_forward_vec"],
        mapping["view_right_vec"],
        mapping["view_up_vec"],
        mapping["output_width"],
        mapping["output_height"],
        mapping["hfov_deg"],
        mapping["vfov_deg"],
    )
    for grid in remap_xy:
        grid.flags.writeable = False
    return mapping, remap_xy


def render_forward_detection_view(
    image_rgb: np.ndarray,
    runtime: dict[str, Any],
) -> tuple[np.ndarray, dict[str, Any]]:
    """Render the immutable forward RGB array shared by every model."""

    image_height, image_width = image_rgb.shape[:2]
    mapping, remap_xy = _cached_forward_view_geometry(
        int(image_width),
        int(image_height),
        int(runtime["forward_view_size"]),
        float(runtime["forward_view_hfov_deg"]),
        float(runtime["forward_view_vfov_deg"]),
        float(runtime.get("panorama_yaw_offset_deg", 0.0)),
        float(runtime.get("panorama_pitch_offset_deg", 0.0)),
    )
    forward_rgb = render_perspective_view_from_panorama(
        image_rgb,
        mapping["pano_forward_vec"],
        mapping["pano_right_vec"],
        mapping["pano_up_vec"],
        mapping["view_forward_vec"],
        mapping["view_right_vec"],
        mapping["view_up_vec"],
        mapping["output_width"],
        mapping["output_height"],
        mapping["hfov_deg"],
        mapping["vfov_deg"],
        remap_xy=remap_xy,
    )
    return forward_rgb, mapping


def run_forward_detection_on_view(
    forward_rgb: np.ndarray,
    mapping: dict[str, Any],
    runtime: dict[str, Any],
    model: YOLO,
    logger,
    *,
    coordinator: MultiModelCoordinator | None = None,
) -> list[dict[str, Any]]:
    """Apply one model to an already-rendered shared forward view."""

    prediction = run_yolo_prediction(
        model,
        runtime,
        logger,
        coordinator=coordinator,
        source=Image.fromarray(forward_rgb),
        imgsz=runtime["imgsz"],
        conf=runtime["conf"],
        iou=runtime["iou"],
        max_det=runtime["max_det"],
        device=runtime["device"],
        verbose=False,
        retina_masks=True,
    )[0]
    candidates = extract_detection_candidates_from_prediction(
        prediction,
        model,
        detection_source="forward",
        panorama_mapping=mapping,
    )
    logger.info("Forward perspective detection produced %d detections.", len(candidates))
    return candidates


def run_full_panorama_detection(
    image_rgb: np.ndarray,
    runtime: dict[str, Any],
    model: YOLO,
    logger,
    *,
    coordinator: MultiModelCoordinator | None = None,
) -> list[dict[str, Any]]:
    logger.info("Running full panorama detection.")
    prediction = run_yolo_prediction(
        model,
        runtime,
        logger,
        coordinator=coordinator,
        source=Image.fromarray(image_rgb),
        imgsz=runtime["imgsz"],
        conf=runtime["conf"],
        iou=runtime["iou"],
        max_det=runtime["max_det"],
        device=runtime["device"],
        verbose=False,
        retina_masks=True,
    )[0]
    candidates = extract_detection_candidates_from_prediction(
        prediction,
        model,
        detection_source="panorama",
        tile_offset_xy=(0, 0),
    )
    logger.info("Full panorama detection produced %d detections.", len(candidates))
    return candidates


def run_tiled_panorama_detection(
    image_rgb: np.ndarray,
    runtime: dict[str, Any],
    model: YOLO,
    logger,
    *,
    coordinator: MultiModelCoordinator | None = None,
) -> list[dict[str, Any]]:

    image_height, image_width = image_rgb.shape[:2]
    raw_forward_vec = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    raw_right_vec = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
    raw_up_vec = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    pano_forward_vec, pano_right_vec, pano_up_vec = apply_panorama_angular_offsets(
        raw_forward_vec,
        raw_right_vec,
        raw_up_vec,
        yaw_offset_deg=float(runtime.get("panorama_yaw_offset_deg", 0.0)),
        pitch_offset_deg=float(runtime.get("panorama_pitch_offset_deg", 0.0)),
    )
    tiles = build_panorama_tiles(
        image_width,
        image_height,
        runtime["tile_width_px"],
        runtime["tile_height_px"],
        runtime["tile_overlap_px"],
    )
    logger.info(
        "Running tiled perspective detection on %d panorama tiles (%dx%d, overlap=%d).",
        len(tiles),
        tiles[0]["output_width"] if tiles else 0,
        tiles[0]["output_height"] if tiles else 0,
        runtime["tile_overlap_px"],
    )

    for tile in tiles:
        center_ray = pixel_to_world_ray(
            tile["center_x"],
            image_height * 0.5,
            image_width,
            image_height,
            pano_forward_vec,
            pano_right_vec,
            pano_up_vec,
        )
        view_forward_vec, view_right_vec, view_up_vec = build_view_axes(
            center_ray,
            pano_up_vec,
            pano_right_vec,
        )
        tile["view_forward_vec"] = view_forward_vec
        tile["view_right_vec"] = view_right_vec
        tile["view_up_vec"] = view_up_vec
        tile["pano_forward_vec"] = pano_forward_vec
        tile["pano_right_vec"] = pano_right_vec
        tile["pano_up_vec"] = pano_up_vec
        tile["pano_width"] = image_width
        tile["pano_height"] = image_height

    candidates: list[dict[str, Any]] = []
    batch_size = runtime["tile_batch_size"]
    for batch_start in range(0, len(tiles), batch_size):
        batch_tiles = tiles[batch_start:batch_start + batch_size]
        batch_sources = [
            Image.fromarray(
                render_perspective_view_from_panorama(
                    image_rgb,
                    tile["pano_forward_vec"],
                    tile["pano_right_vec"],
                    tile["pano_up_vec"],
                    tile["view_forward_vec"],
                    tile["view_right_vec"],
                    tile["view_up_vec"],
                    tile["output_width"],
                    tile["output_height"],
                    tile["hfov_deg"],
                    tile["vfov_deg"],
                )
            )
            for tile in batch_tiles
        ]
        batch_predictions = run_yolo_prediction(
            model,
            runtime,
            logger,
            coordinator=coordinator,
            source=batch_sources,
            imgsz=runtime["imgsz"],
            conf=runtime["conf"],
            iou=runtime["iou"],
            max_det=runtime["max_det"],
            device=runtime["device"],
            verbose=False,
            retina_masks=True,
        )
        for tile, prediction in zip(batch_tiles, batch_predictions):
            candidates.extend(
                extract_detection_candidates_from_prediction(
                    prediction,
                    model,
                    detection_source="tiled",
                    panorama_mapping=tile,
                )
            )

    merged = merge_detection_candidates(candidates, runtime["tile_merge_iou"])
    logger.info(
        "Tiled detection merged %d raw detections into %d tiled detections.",
        len(candidates),
        len(merged),
    )
    return merged


def run_forward_panorama_detection(
    image_rgb: np.ndarray,
    runtime: dict[str, Any],
    model: YOLO,
    logger,
    *,
    forward_view_output_path: Path | None = None,
    coordinator: MultiModelCoordinator | None = None,
) -> list[dict[str, Any]]:
    """Run YOLO only on a distortion-reduced vehicle-forward perspective view."""

    forward_rgb, mapping = render_forward_detection_view(image_rgb, runtime)
    logger.info(
        "Running forward perspective detection (%dx%d, hfov=%.1f, vfov=%.1f).",
        mapping["output_width"],
        mapping["output_height"],
        mapping["hfov_deg"],
        mapping["vfov_deg"],
    )
    if forward_view_output_path is not None:
        save_forward_detection_qa_image(
            forward_rgb,
            forward_view_output_path,
            hfov_deg=float(mapping["hfov_deg"]),
            vfov_deg=float(mapping["vfov_deg"]),
            max_center_ray_angle_deg=float(runtime["max_center_ray_angle_deg"]),
        )
        logger.info("Saved annotated forward YOLO view: %s", forward_view_output_path)
    return run_forward_detection_on_view(
        forward_rgb,
        mapping,
        runtime,
        model,
        logger,
        coordinator=coordinator,
    )


def run_yolo_detection_on_panorama(
    image_rgb: np.ndarray,
    runtime: dict[str, Any],
    model: YOLO,
    logger,
    *,
    forward_view_output_path: Path | None = None,
    coordinator: MultiModelCoordinator | None = None,
) -> list[dict[str, Any]]:
    if runtime.get("detection_view_mode") == "forward":
        return run_forward_panorama_detection(
            image_rgb=image_rgb,
            runtime=runtime,
            model=model,
            logger=logger,
            forward_view_output_path=forward_view_output_path,
            coordinator=coordinator,
        )

    full_candidates: list[dict[str, Any]] = []
    tiled_candidates: list[dict[str, Any]] = []

    if runtime["use_full_panorama_detection"]:
        full_candidates = run_full_panorama_detection(
            image_rgb=image_rgb,
            runtime=runtime,
            model=model,
            logger=logger,
            coordinator=coordinator,
        )
    if runtime["use_tiled_detection"]:
        tiled_candidates = run_tiled_panorama_detection(
            image_rgb=image_rgb,
            runtime=runtime,
            model=model,
            logger=logger,
            coordinator=coordinator,
        )

    if not full_candidates and not tiled_candidates:
        logger.info("Enabled detection passes produced 0 detections.")
        return []
    if not tiled_candidates:
        return full_candidates
    if not full_candidates:
        return tiled_candidates

    merged = merge_detection_candidates(
        full_candidates + tiled_candidates,
        runtime["tile_merge_iou"],
    )
    logger.info(
        "Combined detection merged %d panorama + %d tiled detections into %d global detections.",
        len(full_candidates),
        len(tiled_candidates),
        len(merged),
    )
    return merged


def expand_bbox(
    bbox_xyxy: tuple[float, float, float, float],
    padding_px: int,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox_xyxy
    return (
        max(0, int(math.floor(x1 - padding_px))),
        max(0, int(math.floor(y1 - padding_px))),
        min(image_width, int(math.ceil(x2 + padding_px))),
        min(image_height, int(math.ceil(y2 + padding_px))),
    )


def save_debug_crop(
    image_rgb: np.ndarray,
    polygon_xy: list[list[float]] | None,
    bbox_xyxy: tuple[float, float, float, float],
    crop_path: Path,
    padding_px: int,
    mask_alpha: int,
    label: str,
    marker_xy: tuple[float, float] | None = None,
    point_pixels_xy: np.ndarray | None = None,
    info_lines: list[str] | None = None,
) -> None:
    image_height, image_width = image_rgb.shape[:2]
    crop_x1, crop_y1, crop_x2, crop_y2 = expand_bbox(
        bbox_xyxy,
        padding_px,
        image_width,
        image_height,
    )

    crop = Image.fromarray(image_rgb[crop_y1:crop_y2, crop_x1:crop_x2]).convert("RGBA")
    if polygon_xy:
        shifted = [(point[0] - crop_x1, point[1] - crop_y1) for point in polygon_xy]
        if mask_alpha > 0:
            overlay = Image.new("RGBA", crop.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay, "RGBA")
            overlay_draw.polygon(
                shifted,
                fill=(255, 255, 0, max(0, min(255, mask_alpha))),
            )
            crop = Image.alpha_composite(crop, overlay)

    draw = ImageDraw.Draw(crop, "RGBA")
    draw.rectangle(
        (
            bbox_xyxy[0] - crop_x1,
            bbox_xyxy[1] - crop_y1,
            bbox_xyxy[2] - crop_x1,
            bbox_xyxy[3] - crop_y1,
        ),
        outline=(80, 255, 80, 255),
        width=4,
    )
    if polygon_xy:
        draw.line([*shifted, shifted[0]], fill=(255, 200, 0, 255), width=2)

    if point_pixels_xy is not None and point_pixels_xy.size > 0:
        step = max(1, int(math.ceil(point_pixels_xy.shape[0] / 500)))
        sampled = point_pixels_xy[::step]
        point_list = [
            (int(point[0]) - crop_x1, int(point[1]) - crop_y1)
            for point in sampled
        ]
        draw.point(point_list, fill=(0, 185, 220, 255))

    if marker_xy is not None:
        marker_x = float(marker_xy[0]) - crop_x1
        marker_y = float(marker_xy[1]) - crop_y1
        radius = 8
        draw.line(
            ((marker_x - radius, marker_y), (marker_x + radius, marker_y)),
            fill=(255, 40, 40, 255),
            width=3,
        )
        draw.line(
            ((marker_x, marker_y - radius), (marker_x, marker_y + radius)),
            fill=(255, 40, 40, 255),
            width=3,
        )
        draw.ellipse(
            (
                marker_x - radius,
                marker_y - radius,
                marker_x + radius,
                marker_y + radius,
            ),
            outline=(255, 40, 40, 255),
            width=2,
        )

    line_texts = [label]
    if info_lines:
        line_texts.extend(info_lines)

    text_left = 12
    text_top = 12
    line_spacing = 8
    current_top = text_top
    for line_text in line_texts:
        text_bbox = draw.textbbox((text_left, current_top), line_text)
        draw.rounded_rectangle(
            (
                text_bbox[0] - 8,
                text_bbox[1] - 6,
                text_bbox[2] + 8,
                text_bbox[3] + 6,
            ),
            radius=8,
            fill=(0, 0, 0, 170),
        )
        draw.text((text_left, current_top), line_text, fill=(255, 255, 255, 255))
        current_top = text_bbox[3] + line_spacing

    crop_path.parent.mkdir(parents=True, exist_ok=True)
    crop.convert("RGB").save(crop_path, quality=95)


def project_representative_point_pixel(
    point_xyz: np.ndarray,
    origin_xyz: np.ndarray,
    rectified_view: dict[str, Any],
) -> tuple[float, float] | None:
    u, v, _distances, valid = project_points_perspective(
        point_xyz.reshape(1, 3).astype(np.float64),
        origin_xyz,
        rectified_view["view_forward_vec"],
        rectified_view["view_right_vec"],
        rectified_view["view_up_vec"],
        rectified_view["view_width"],
        rectified_view["view_height"],
        rectified_view["hfov_deg"],
        rectified_view["vfov_deg"],
    )
    if not bool(valid[0]) or not math.isfinite(float(u[0])) or not math.isfinite(float(v[0])):
        return None
    return float(u[0]), float(v[0])


def make_projection_panel(
    points_uv: np.ndarray,
    colors_rgb: np.ndarray,
    title: str,
    panel_size: int,
) -> np.ndarray:
    margin = 20
    canvas = np.full((panel_size, panel_size, 3), 255, dtype=np.uint8)
    cv2.rectangle(canvas, (0, 0), (panel_size - 1, panel_size - 1), (210, 210, 210), 1)

    if points_uv.size > 0:
        step = max(1, int(math.ceil(points_uv.shape[0] / 8000)))
        sampled_points = points_uv[::step]
        sampled_colors = colors_rgb[::step]
        finite_mask = np.isfinite(sampled_points).all(axis=1)
        sampled_points = sampled_points[finite_mask]
        sampled_colors = sampled_colors[finite_mask]

        if sampled_points.size > 0:
            span = np.max(np.abs(sampled_points), axis=0)
            span = np.maximum(span, 1e-3)
            scale = min(
                (panel_size - (margin * 2)) / max(span[0] * 2.0, 1e-3),
                (panel_size - (margin * 2)) / max(span[1] * 2.0, 1e-3),
            )
            pixel_x = np.rint((panel_size * 0.5) + (sampled_points[:, 0] * scale)).astype(np.int32)
            pixel_y = np.rint((panel_size * 0.5) - (sampled_points[:, 1] * scale)).astype(np.int32)

            in_bounds = (
                (pixel_x >= 0)
                & (pixel_x < panel_size)
                & (pixel_y >= 0)
                & (pixel_y < panel_size)
            )
            pixel_x = pixel_x[in_bounds]
            pixel_y = pixel_y[in_bounds]
            sampled_colors = sampled_colors[in_bounds]
            canvas[pixel_y, pixel_x] = sampled_colors

    center = panel_size // 2
    cv2.drawMarker(
        canvas,
        (center, center),
        (40, 40, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=16,
        thickness=2,
        line_type=cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        title,
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (40, 40, 40),
        2,
        cv2.LINE_AA,
    )
    return canvas


def save_point_cloud_preview(
    points_xyz: np.ndarray,
    colors_rgb: np.ndarray,
    representative_xyz: np.ndarray,
    rectified_view: dict[str, Any],
    output_path: Path,
    *,
    panel_size: int,
    point_count: int,
    raw_point_count: int,
    cluster_count: int,
    center_ray_angle_deg: float,
    representative_mode: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    centered = points_xyz.astype(np.float64) - representative_xyz[None, :]

    front_uv = np.column_stack(
        (
            centered @ rectified_view["view_right_vec"],
            centered @ rectified_view["view_up_vec"],
        )
    )
    top_uv = np.column_stack(
        (
            centered @ rectified_view["view_right_vec"],
            centered @ rectified_view["view_forward_vec"],
        )
    )
    side_uv = np.column_stack(
        (
            centered @ rectified_view["view_forward_vec"],
            centered @ rectified_view["view_up_vec"],
        )
    )

    front_panel = make_projection_panel(front_uv, colors_rgb, "Front R/U", panel_size)
    top_panel = make_projection_panel(top_uv, colors_rgb, "Top R/F", panel_size)
    side_panel = make_projection_panel(side_uv, colors_rgb, "Side F/U", panel_size)

    preview = np.full((panel_size * 2, panel_size * 2, 3), 255, dtype=np.uint8)
    preview[:panel_size, :panel_size] = front_panel
    preview[:panel_size, panel_size:] = top_panel
    preview[panel_size:, :panel_size] = side_panel

    info_panel = preview[panel_size:, panel_size:]
    info_panel[:] = 245
    cv2.rectangle(info_panel, (0, 0), (panel_size - 1, panel_size - 1), (210, 210, 210), 1)
    info_lines = [
        "Representative point",
        f"mode: {representative_mode}",
        f"raw/cluster: {raw_point_count}/{point_count}",
        f"clusters: {cluster_count}",
        f"ray angle: {center_ray_angle_deg:.1f} deg",
        f"hfov/vfov: {rectified_view['hfov_deg']:.1f}/{rectified_view['vfov_deg']:.1f}",
        "cross = saved SHP point",
    ]
    line_y = 34
    for line in info_lines:
        cv2.putText(
            info_panel,
            line,
            (14, line_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (40, 40, 40),
            1,
            cv2.LINE_AA,
        )
        line_y += 34

    Image.fromarray(preview).save(output_path)


def points_to_bbox(points_xy: list[list[float]]) -> tuple[float, float, float, float] | None:
    if not points_xy:
        return None
    x_values = [float(point[0]) for point in points_xy]
    y_values = [float(point[1]) for point in points_xy]
    return (
        min(x_values),
        min(y_values),
        max(x_values),
        max(y_values),
    )


def clamp_bbox(
    bbox_xyxy: tuple[float, float, float, float],
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox_xyxy
    x1 = float(max(0.0, min(float(image_width - 1), x1)))
    y1 = float(max(0.0, min(float(image_height - 1), y1)))
    x2 = float(max(x1 + 1.0, min(float(image_width), x2)))
    y2 = float(max(y1 + 1.0, min(float(image_height), y2)))
    return x1, y1, x2, y2


def iter_detection_support_pixels(
    polygon_xy: list[list[float]] | None,
    bbox_xyxy: tuple[float, float, float, float],
) -> list[tuple[float, float]]:
    x1, y1, x2, y2 = bbox_xyxy
    points = [
        (x1, y1),
        (x1, y2),
        (x2, y1),
        (x2, y2),
        ((x1 + x2) * 0.5, y1),
        ((x1 + x2) * 0.5, y2),
        (x1, (y1 + y2) * 0.5),
        (x2, (y1 + y2) * 0.5),
        ((x1 + x2) * 0.5, (y1 + y2) * 0.5),
    ]
    if polygon_xy:
        step = max(1, len(polygon_xy) // 64)
        points.extend((float(point[0]), float(point[1])) for point in polygon_xy[::step])
    return points


def transform_panorama_points_to_perspective(
    points_xy: list[tuple[float, float]],
    pano_width: int,
    pano_height: int,
    pano_forward_vec: np.ndarray,
    pano_right_vec: np.ndarray,
    pano_up_vec: np.ndarray,
    view_forward_vec: np.ndarray,
    view_right_vec: np.ndarray,
    view_up_vec: np.ndarray,
    view_width: int,
    view_height: int,
    hfov_deg: float,
    vfov_deg: float,
) -> list[list[float]]:
    transformed: list[list[float]] = []
    for pixel_x, pixel_y in points_xy:
        ray = pixel_to_world_ray(
            pixel_x,
            pixel_y,
            pano_width,
            pano_height,
            pano_forward_vec,
            pano_right_vec,
            pano_up_vec,
        )
        projected_x, projected_y, local_z = world_ray_to_perspective_pixel(
            ray,
            view_forward_vec,
            view_right_vec,
            view_up_vec,
            view_width,
            view_height,
            hfov_deg,
            vfov_deg,
        )
        if local_z <= 1e-6 or not math.isfinite(projected_x) or not math.isfinite(projected_y):
            continue
        transformed.append([float(projected_x), float(projected_y)])
    return transformed


def estimate_rectified_fovs(
    polygon_xy: list[list[float]] | None,
    bbox_xyxy: tuple[float, float, float, float],
    pano_width: int,
    pano_height: int,
    pano_forward_vec: np.ndarray,
    pano_right_vec: np.ndarray,
    pano_up_vec: np.ndarray,
    view_forward_vec: np.ndarray,
    view_right_vec: np.ndarray,
    view_up_vec: np.ndarray,
    margin_deg: float,
    min_fov_deg: float,
    max_fov_deg: float,
) -> tuple[float, float]:
    max_h_angle = 0.0
    max_v_angle = 0.0
    for pixel_x, pixel_y in iter_detection_support_pixels(polygon_xy, bbox_xyxy):
        ray = pixel_to_world_ray(
            pixel_x,
            pixel_y,
            pano_width,
            pano_height,
            pano_forward_vec,
            pano_right_vec,
            pano_up_vec,
        )
        local_x = float(np.dot(ray, view_right_vec))
        local_y = float(np.dot(ray, view_up_vec))
        local_z = float(np.dot(ray, view_forward_vec))
        if local_z <= 1e-6:
            continue
        max_h_angle = max(max_h_angle, abs(math.atan2(local_x, local_z)))
        max_v_angle = max(max_v_angle, abs(math.atan2(local_y, local_z)))

    full_hfov_deg = math.degrees(max_h_angle * 2.0) + (margin_deg * 2.0)
    full_vfov_deg = math.degrees(max_v_angle * 2.0) + (margin_deg * 2.0)
    hfov_deg = min(max(max(min_fov_deg, full_hfov_deg), 1.0), max_fov_deg)
    vfov_deg = min(max(max(min_fov_deg, full_vfov_deg), 1.0), max_fov_deg)
    return hfov_deg, vfov_deg


def build_rectified_detection_view(
    image_task: dict[str, Any],
    image_rgb: np.ndarray,
    detection_payload: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    raw_forward_vec, raw_right_vec, raw_up_vec = build_camera_axes(
        tuple(image_task["direction"]),
        tuple(image_task["up"]),
    )
    pano_forward_vec, pano_right_vec, pano_up_vec = apply_panorama_angular_offsets(
        raw_forward_vec,
        raw_right_vec,
        raw_up_vec,
        yaw_offset_deg=float(runtime.get("panorama_yaw_offset_deg", 0.0)),
        pitch_offset_deg=float(runtime.get("panorama_pitch_offset_deg", 0.0)),
    )
    pano_height, pano_width = image_rgb.shape[:2]
    bbox_xyxy = tuple(float(value) for value in detection_payload["bbox_xyxy"])
    polygon_xy = detection_payload["mask_polygon"]

    center_ray, detection_angle = angular_radius_from_bbox(
        bbox_xyxy,
        pano_width,
        pano_height,
        pano_forward_vec,
        pano_right_vec,
        pano_up_vec,
    )
    center_ray_angle_deg = math.degrees(
        math.acos(float(np.clip(np.dot(center_ray, raw_forward_vec), -1.0, 1.0)))
    )
    view_forward_vec, view_right_vec, view_up_vec = build_view_axes(
        center_ray,
        pano_up_vec,
        pano_right_vec,
    )
    hfov_deg, vfov_deg = estimate_rectified_fovs(
        polygon_xy,
        bbox_xyxy,
        pano_width,
        pano_height,
        pano_forward_vec,
        pano_right_vec,
        pano_up_vec,
        view_forward_vec,
        view_right_vec,
        view_up_vec,
        runtime["perspective_margin_deg"],
        runtime["perspective_min_fov_deg"],
        runtime["perspective_max_fov_deg"],
    )
    view_size = int(runtime["perspective_view_size"])
    rectified_rgb = render_perspective_view_from_panorama(
        image_rgb,
        pano_forward_vec,
        pano_right_vec,
        pano_up_vec,
        view_forward_vec,
        view_right_vec,
        view_up_vec,
        view_size,
        view_size,
        hfov_deg,
        vfov_deg,
    )

    rectified_polygon = None
    if polygon_xy:
        rectified_polygon = transform_panorama_points_to_perspective(
            [(float(point[0]), float(point[1])) for point in polygon_xy],
            pano_width,
            pano_height,
            pano_forward_vec,
            pano_right_vec,
            pano_up_vec,
            view_forward_vec,
            view_right_vec,
            view_up_vec,
            view_size,
            view_size,
            hfov_deg,
            vfov_deg,
        )
        if len(rectified_polygon) < 3:
            rectified_polygon = None

    transformed_bbox_points = transform_panorama_points_to_perspective(
        [
            (bbox_xyxy[0], bbox_xyxy[1]),
            (bbox_xyxy[0], bbox_xyxy[3]),
            (bbox_xyxy[2], bbox_xyxy[1]),
            (bbox_xyxy[2], bbox_xyxy[3]),
        ],
        pano_width,
        pano_height,
        pano_forward_vec,
        pano_right_vec,
        pano_up_vec,
        view_forward_vec,
        view_right_vec,
        view_up_vec,
        view_size,
        view_size,
        hfov_deg,
        vfov_deg,
    )
    rectified_bbox = points_to_bbox(rectified_polygon or transformed_bbox_points) or (
        view_size * 0.25,
        view_size * 0.25,
        view_size * 0.75,
        view_size * 0.75,
    )
    rectified_bbox = clamp_bbox(rectified_bbox, view_size, view_size)

    return {
        "center_ray": center_ray,
        "detection_angle": detection_angle,
        "center_ray_angle_deg": center_ray_angle_deg,
        "rectified_rgb": rectified_rgb,
        "rectified_polygon": rectified_polygon,
        "rectified_bbox": rectified_bbox,
        "view_forward_vec": view_forward_vec,
        "view_right_vec": view_right_vec,
        "view_up_vec": view_up_vec,
        "view_width": view_size,
        "view_height": view_size,
        "hfov_deg": hfov_deg,
        "vfov_deg": vfov_deg,
        "pano_forward_vec": pano_forward_vec,
        "pano_right_vec": pano_right_vec,
        "pano_up_vec": pano_up_vec,
        "raw_forward_vec": raw_forward_vec,
        "raw_right_vec": raw_right_vec,
        "raw_up_vec": raw_up_vec,
    }


def build_detection_mask(
    polygon_xy: list[list[float]] | None,
    bbox_xyxy: tuple[float, float, float, float],
    point_padding_px: int,
    image_width: int,
    image_height: int,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    crop_x1, crop_y1, crop_x2, crop_y2 = expand_bbox(
        bbox_xyxy,
        point_padding_px,
        image_width,
        image_height,
    )

    crop_width = max(1, crop_x2 - crop_x1)
    crop_height = max(1, crop_y2 - crop_y1)
    mask = np.zeros((crop_height, crop_width), dtype=np.uint8)

    if polygon_xy:
        polygon = np.asarray(
            [[point[0] - crop_x1, point[1] - crop_y1] for point in polygon_xy],
            dtype=np.int32,
        )
        cv2.fillPoly(mask, [polygon], color=255)
        if point_padding_px > 0:
            kernel_size = (point_padding_px * 2) + 1
            kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
            mask = cv2.dilate(mask, kernel, iterations=1)
    else:
        local_x1 = max(0, int(math.floor(bbox_xyxy[0])) - crop_x1)
        local_y1 = max(0, int(math.floor(bbox_xyxy[1])) - crop_y1)
        local_x2 = min(crop_width, int(math.ceil(bbox_xyxy[2])) - crop_x1)
        local_y2 = min(crop_height, int(math.ceil(bbox_xyxy[3])) - crop_y1)
        mask[local_y1:local_y2, local_x1:local_x2] = 255

    return mask, (crop_x1, crop_y1, crop_x2, crop_y2)


def write_las(
    points_xyz: np.ndarray,
    colors_rgb: np.ndarray,
    output_path: Path,
    *,
    crs_wkt: str | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Point format 2 avoids implying that source GPS time was preserved.  This
    # file is deliberately a visualization-oriented derived point set.
    header = laspy.LasHeader(point_format=2, version="1.4")
    header.system_identifier = "MMS_SIGN_DERIVED"
    header.generating_software = "mms_shp_detection"
    if crs_wkt:
        header.vlrs.append(WktCoordinateSystemVlr(crs_wkt.replace("\x00", "").strip()))
        header.global_encoding.wkt = True
    mins = points_xyz.min(axis=0)
    header.offsets = mins.tolist()
    header.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(header)
    las.x = points_xyz[:, 0]
    las.y = points_xyz[:, 1]
    las.z = points_xyz[:, 2]
    scaled = colors_rgb.astype(np.uint16) * 257
    las.red = scaled[:, 0]
    las.green = scaled[:, 1]
    las.blue = scaled[:, 2]
    las.write(str(output_path))


def write_pole_las(
    records: dict[str, np.ndarray],
    point_indices: np.ndarray,
    output_path: Path,
    *,
    crs_wkt: str | None = None,
) -> None:
    """Write pole-axis inliers while retaining available LAS core attributes."""

    indices = np.asarray(point_indices, dtype=np.int64)
    points_xyz = np.asarray(records["xyz"], dtype=np.float64)[indices]
    if points_xyz.shape[0] == 0:
        raise ValueError("Cannot write an empty pole point crop")

    gps_time_type: int | None = None
    source_gps_time_types = records.get("gps_time_type")
    if source_gps_time_types is not None:
        all_gps_time_types = np.asarray(source_gps_time_types, dtype=np.int64)
        if (
            all_gps_time_types.ndim != 1
            or all_gps_time_types.shape[0] != len(records["xyz"])
        ):
            raise ValueError("gps_time_type must contain one value per source point")
        selected_gps_time_types = all_gps_time_types[indices]
        if np.any(~np.isin(selected_gps_time_types, (-1, 0, 1))):
            raise ValueError(
                "LAS GPS time type must be -1 (unknown), 0 (week time), or 1 (standard time)"
            )
        has_unknown_gps_time_type = bool(np.any(selected_gps_time_types < 0))
        known_gps_time_types = np.unique(
            selected_gps_time_types[selected_gps_time_types >= 0]
        )
        if has_unknown_gps_time_type and known_gps_time_types.size:
            raise ValueError(
                "Cannot combine pole points with known and unknown LAS GPS time encodings"
            )
        if known_gps_time_types.size > 1:
            raise ValueError(
                "Cannot combine pole points with different LAS GPS time encodings"
            )
        if known_gps_time_types.size == 1:
            gps_time_type = int(known_gps_time_types[0])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    header = laspy.LasHeader(point_format=7, version="1.4")
    header.system_identifier = "MMS_POLE_DERIVED"
    header.generating_software = "mms_shp_detection"
    if gps_time_type is not None:
        header.global_encoding.gps_time_type = gps_time_type
    if crs_wkt:
        header.vlrs.append(WktCoordinateSystemVlr(crs_wkt.replace("\x00", "").strip()))
        header.global_encoding.wkt = True
    header.offsets = points_xyz.min(axis=0).tolist()
    header.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(header)
    las.x = points_xyz[:, 0]
    las.y = points_xyz[:, 1]
    las.z = points_xyz[:, 2]
    rgb8 = np.asarray(records["rgb"], dtype=np.uint8)[indices]
    rgb16 = rgb8.astype(np.uint16) * 257
    las.red = rgb16[:, 0]
    las.green = rgb16[:, 1]
    las.blue = rgb16[:, 2]
    las.intensity = np.asarray(records["intensity"], dtype=np.uint16)[indices]
    classes = np.asarray(records["classification"], dtype=np.int16)[indices]
    las.classification = np.clip(np.where(classes >= 0, classes, 0), 0, 255).astype(np.uint8)
    gps_time = np.asarray(records["gps_time"], dtype=np.float64)[indices]
    las.gps_time = np.nan_to_num(gps_time, nan=0.0, posinf=0.0, neginf=0.0)
    las.return_number = np.clip(
        np.asarray(records["return_number"], dtype=np.uint8)[indices], 0, 15
    )
    las.number_of_returns = np.clip(
        np.asarray(records["number_of_returns"], dtype=np.uint8)[indices], 0, 15
    )
    las.write(str(output_path))


def build_pole_search_parameters(runtime: dict[str, Any]) -> PoleSearchParameters:
    parameters = PoleSearchParameters(
        search_radius_m=float(runtime["pole_search_radius_m"]),
        max_drop_m=float(runtime["pole_max_drop_m"]),
        top_margin_m=float(runtime["pole_top_margin_m"]),
        xy_voxel_m=float(runtime["pole_xy_voxel_m"]),
        z_bin_m=float(runtime["pole_z_bin_m"]),
        axis_cluster_radius_m=float(runtime["pole_axis_cluster_radius_m"]),
        axis_inlier_radius_m=float(runtime["pole_axis_inlier_radius_m"]),
        min_vertical_span_m=float(runtime["pole_min_vertical_span_m"]),
        min_vertical_bins=int(runtime["pole_min_vertical_bins"]),
        min_consecutive_vertical_bins=int(runtime["pole_min_consecutive_vertical_bins"]),
        max_observed_z_gap_m=float(runtime["pole_max_observed_z_gap_m"]),
        min_vertical_occupancy_ratio=float(runtime["pole_min_vertical_occupancy_ratio"]),
        middle_support_start_fraction=float(runtime["pole_middle_support_start_fraction"]),
        min_middle_support_coverage_ratio=float(
            runtime["pole_min_middle_support_coverage_ratio"]
        ),
        preferred_min_completeness_ratio=float(
            runtime["pole_preferred_min_completeness_ratio"]
        ),
        geometry_ground_clearance_m=float(
            runtime["pole_geometry_ground_clearance_m"]
        ),
        geometry_remote_min_completeness_ratio=float(
            runtime["pole_geometry_remote_min_completeness_ratio"]
        ),
        geometry_remote_max_axis_rmse_m=float(
            runtime["pole_geometry_remote_max_axis_rmse_m"]
        ),
        geometry_remote_max_ground_rmse_m=float(
            runtime["pole_geometry_remote_max_ground_rmse_m"]
        ),
        min_points=int(runtime["pole_min_points"]),
        max_axis_tilt_deg=float(runtime["pole_max_axis_tilt_deg"]),
        axis_plumb_max_tilt_deg=float(
            runtime["pole_axis_plumb_max_tilt_deg"]
        ),
        axis_plumb_full_tilt_deg=float(
            runtime["pole_axis_plumb_full_tilt_deg"]
        ),
        axis_plumb_endpoint_fraction=float(
            runtime["pole_axis_plumb_endpoint_fraction"]
        ),
        direct_max_axis_sign_distance_m=float(
            runtime["pole_direct_max_axis_sign_distance_m"]
        ),
        max_axis_sign_distance_m=float(runtime["pole_max_axis_sign_distance_m"]),
        horizontal_connection_radius_m=float(
            runtime["pole_horizontal_connection_radius_m"]
        ),
        horizontal_connection_z_tolerance_m=float(
            runtime["pole_horizontal_connection_z_tolerance_m"]
        ),
        horizontal_connection_above_tolerance_m=float(
            runtime["pole_horizontal_connection_above_tolerance_m"]
        ),
        horizontal_connection_bin_m=float(
            runtime["pole_horizontal_connection_bin_m"]
        ),
        horizontal_connection_min_points_per_bin=int(
            runtime["pole_horizontal_connection_min_points_per_bin"]
        ),
        horizontal_connection_coherence_radius_m=float(
            runtime["pole_horizontal_connection_coherence_radius_m"]
        ),
        min_horizontal_connection_coverage=float(
            runtime["pole_min_horizontal_connection_coverage"]
        ),
        min_horizontal_connection_coherent_ratio=float(
            runtime["pole_min_horizontal_connection_coherent_ratio"]
        ),
        min_horizontal_connection_coherent_point_fraction=float(
            runtime[
                "pole_min_horizontal_connection_coherent_point_fraction"
            ]
        ),
        remote_max_endpoint_tilt_deg=float(
            runtime["pole_remote_max_endpoint_tilt_deg"]
        ),
        long_remote_distance_m=float(runtime["pole_long_remote_distance_m"]),
        long_remote_transition_m=float(runtime["pole_long_remote_transition_m"]),
        long_remote_min_vertical_span_m=float(
            runtime["pole_long_remote_min_vertical_span_m"]
        ),
        long_remote_min_completeness_ratio=float(
            runtime["pole_long_remote_min_completeness_ratio"]
        ),
        long_remote_min_connection_coverage_ratio=float(
            runtime["pole_long_remote_min_connection_coverage_ratio"]
        ),
        max_ground_class_fraction=float(runtime["pole_max_ground_class_fraction"]),
        min_ground_drop_m=float(runtime["pole_min_ground_drop_m"]),
        require_ground=bool(runtime["pole_require_ground"]),
        ground_search_radius_m=float(runtime["pole_ground_search_radius_m"]),
        ground_core_radius_m=float(runtime["pole_ground_core_radius_m"]),
        ground_exclusion_radius_m=float(runtime["pole_ground_exclusion_radius_m"]),
        ground_cell_size_m=float(runtime["pole_ground_cell_size_m"]),
        ground_cell_quantile=float(runtime["pole_ground_cell_quantile"]),
        ground_min_cells=int(runtime["pole_ground_min_cells"]),
        ground_max_rmse_m=float(runtime["pole_ground_max_rmse_m"]),
        ground_geometry_preference_margin_m=float(
            runtime["pole_ground_geometry_preference_margin_m"]
        ),
        occlusion_gap_m=float(runtime["pole_occlusion_gap_m"]),
        max_ground_penetration_m=float(
            runtime["pole_max_ground_penetration_m"]
        ),
        max_ground_support_distance_m=float(
            runtime["pole_max_ground_support_distance_m"]
        ),
        ground_class_ids=tuple(runtime["pole_ground_class_ids"]),
        pole_class_ids=tuple(runtime["pole_class_ids"]),
        excluded_pole_class_ids=tuple(runtime["pole_excluded_pole_class_ids"]),
    )
    parameters.validate()
    return parameters


def build_pole_fallback_parameters(
    runtime: dict[str, Any],
    strict: PoleSearchParameters,
) -> PoleSearchParameters | None:
    """Build the wider physical pole-search envelope used only after rejection."""

    if not runtime.get("pole_range_fallback_enabled", False):
        return None
    fallback = replace(
        strict,
        search_radius_m=float(runtime["pole_fallback_search_radius_m"]),
        max_drop_m=float(runtime["pole_fallback_max_drop_m"]),
        top_margin_m=float(runtime["pole_fallback_top_margin_m"]),
        max_axis_sign_distance_m=float(
            runtime["pole_fallback_max_axis_sign_distance_m"]
        ),
        min_vertical_span_m=float(
            runtime["pole_fallback_min_vertical_span_m"]
        ),
        horizontal_connection_radius_m=float(
            runtime["pole_fallback_horizontal_connection_radius_m"]
        ),
        horizontal_connection_z_tolerance_m=float(
            runtime["pole_fallback_horizontal_connection_z_tolerance_m"]
        ),
        horizontal_connection_above_tolerance_m=float(
            runtime[
                "pole_fallback_horizontal_connection_above_tolerance_m"
            ]
        ),
        horizontal_connection_bin_m=float(
            runtime["pole_fallback_horizontal_connection_bin_m"]
        ),
        min_horizontal_connection_coverage=float(
            runtime["pole_fallback_min_horizontal_connection_coverage"]
        ),
    )
    fallback.validate()
    comparisons = (
        (
            "pole_fallback_search_radius_m",
            fallback.search_radius_m,
            "pole_search_radius_m",
            strict.search_radius_m,
        ),
        (
            "pole_fallback_max_drop_m",
            fallback.max_drop_m,
            "pole_max_drop_m",
            strict.max_drop_m,
        ),
        (
            "pole_fallback_top_margin_m",
            fallback.top_margin_m,
            "pole_top_margin_m",
            strict.top_margin_m,
        ),
        (
            "pole_fallback_max_axis_sign_distance_m",
            fallback.max_axis_sign_distance_m,
            "pole_max_axis_sign_distance_m",
            strict.max_axis_sign_distance_m,
        ),
    )
    smaller = [
        f"{fallback_name}={fallback_value:g} < {strict_name}={strict_value:g}"
        for fallback_name, fallback_value, strict_name, strict_value in comparisons
        if fallback_value < strict_value
    ]
    if smaller:
        raise ValueError(
            "Pole fallback bounds cannot be smaller than strict bounds: "
            + ", ".join(smaller)
        )
    if all(
        math.isclose(fallback_value, strict_value)
        for _, fallback_value, _, strict_value in comparisons
    ):
        raise ValueError(
            "Pole range fallback is enabled but none of its bounds expands the strict search"
        )
    return fallback


def pole_cross_profile_candidate_key(
    candidate: Any,
    *,
    preferred_min_completeness_ratio: float,
    direct_max_axis_sign_distance_m: float,
) -> tuple[float, ...]:
    """Compare strict/fallback candidates without profile-dependent bin counts."""

    completeness = float(
        getattr(candidate, "completeness_ratio", 0.0) or 0.0
    )
    association_distance = float(
        getattr(candidate, "association_distance_m", math.inf)
    )
    direct = association_distance <= direct_max_axis_sign_distance_m
    connection_coverage = (
        1.0
        if direct
        else pole_connection_coverage(candidate)
    )
    axis_rmse = float(getattr(candidate, "radial_rmse_m", math.inf))
    ground_rmse_value = getattr(candidate, "ground_rmse_m", None)
    ground_rmse = (
        float(ground_rmse_value)
        if ground_rmse_value is not None
        else math.inf
    )
    has_ground = getattr(candidate, "ground_z", None) is not None
    status = str(getattr(candidate, "status", "REVIEW") or "REVIEW")
    common = (
        0.0
        if completeness >= preferred_min_completeness_ratio
        else 1.0,
        0.0 if has_ground else 1.0,
        0.0 if status == "AUTO" else 1.0,
        0.0 if direct else 1.0,
    )
    if direct:
        return (
            *common,
            -completeness,
            axis_rmse,
            ground_rmse,
            association_distance,
            -float(getattr(candidate, "point_count", 0) or 0),
        )
    return (
        *common,
        remote_pole_junction_cost(candidate),
        -connection_coverage,
        -completeness,
        axis_rmse,
        ground_rmse,
        association_distance,
        -float(getattr(candidate, "point_count", 0) or 0),
    )


def select_cross_profile_pole_candidate(
    candidates: tuple[Any, ...],
    parameters: PoleSearchParameters,
) -> Any:
    """Select across strict/fallback envelopes without bypassing side tie-breaks."""

    if not candidates:
        raise ValueError(
            "select_cross_profile_pole_candidate requires at least one candidate"
        )

    def rank_key(candidate: Any) -> tuple[float, ...]:
        return pole_cross_profile_candidate_key(
            candidate,
            preferred_min_completeness_ratio=(
                parameters.preferred_min_completeness_ratio
            ),
            direct_max_axis_sign_distance_m=(
                parameters.direct_max_axis_sign_distance_m
            ),
        )

    ordered = sorted(candidates, key=rank_key)
    initial_tier = rank_key(ordered[0])[:4]
    same_tier = tuple(
        candidate
        for candidate in ordered
        if rank_key(candidate)[:4] == initial_tier
    )
    return select_pole_candidate(
        same_tier,
        parameters,
        rank_key=rank_key,
    )


def _project_points_to_rectified_view(
    points_xyz: np.ndarray,
    origin_xyz: np.ndarray,
    rectified_view: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    if points_xyz.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64), np.empty((0,), dtype=bool)
    u, v, _distance, valid = project_points_perspective(
        np.asarray(points_xyz, dtype=np.float64),
        origin_xyz,
        rectified_view["view_forward_vec"],
        rectified_view["view_right_vec"],
        rectified_view["view_up_vec"],
        rectified_view["view_width"],
        rectified_view["view_height"],
        rectified_view["hfov_deg"],
        rectified_view["vfov_deg"],
    )
    valid &= (
        np.isfinite(u)
        & np.isfinite(v)
        & (u >= 0.0)
        & (u < rectified_view["view_width"])
        & (v >= 0.0)
        & (v < rectified_view["view_height"])
    )
    return np.column_stack((u, v)), valid


def build_pole_debug_axis_segments(
    candidate: Any,
) -> dict[str, tuple[np.ndarray, np.ndarray] | None]:
    """Split a fitted pole axis into observed and ground-extrapolated segments."""

    base_xyz = np.asarray(candidate.base_xyz, dtype=np.float64)
    axis_direction = np.asarray(candidate.axis_direction, dtype=np.float64)
    if base_xyz.shape != (3,) or axis_direction.shape != (3,):
        raise ValueError("Pole debug axis vectors must contain three coordinates")
    if abs(float(axis_direction[2])) < 1e-9:
        return {"observed": None, "ground_extrapolated": None}

    observed_z_min = getattr(candidate, "observed_z_min", None)
    if observed_z_min is None:
        observed_z_min = getattr(candidate, "lowest_observed_z", base_xyz[2])
    observed_z_min = float(observed_z_min)

    observed_z_max = getattr(candidate, "observed_z_max", None)
    if observed_z_max is None:
        observed_z_max = observed_z_min + float(
            max(getattr(candidate, "vertical_span_m", 1.0), 1e-6)
        )
    observed_z_max = float(observed_z_max)
    if observed_z_max < observed_z_min:
        observed_z_min, observed_z_max = observed_z_max, observed_z_min

    def point_at_z(z_value: float) -> np.ndarray:
        distance_along_axis = (z_value - float(base_xyz[2])) / float(axis_direction[2])
        return base_xyz + (axis_direction * distance_along_axis)

    observed_start = point_at_z(observed_z_min)
    observed_end = point_at_z(observed_z_max)
    extrapolated = None
    if observed_z_min > float(base_xyz[2]) + 1e-6:
        extrapolated = (base_xyz, observed_start)
    return {
        "observed": (observed_start, observed_end),
        "ground_extrapolated": extrapolated,
    }


def build_pole_corridor_bbox(
    rectified_view: dict[str, Any],
    *,
    side_expand_ratio: float,
    top_margin_ratio: float,
) -> tuple[float, float, float, float]:
    """Build the image-space support corridor for a rectified sign view."""

    x1, y1, x2, y2 = rectified_view["rectified_bbox"]
    bbox_width = max(1.0, float(x2) - float(x1))
    bbox_height = max(1.0, float(y2) - float(y1))
    return (
        max(0.0, float(x1) - (bbox_width * float(side_expand_ratio))),
        max(0.0, float(y1) - (bbox_height * float(top_margin_ratio))),
        min(
            float(rectified_view["view_width"] - 1),
            float(x2) + (bbox_width * float(side_expand_ratio)),
        ),
        float(rectified_view["view_height"] - 1),
    )


def build_pole_search_corridor_masks(
    neighborhood_xyz: np.ndarray,
    projected_pixels: np.ndarray,
    valid_projection_mask: np.ndarray,
    sign_xyz: np.ndarray,
    corridor_bbox: tuple[float, float, float, float],
    parameters: PoleSearchParameters,
) -> tuple[np.ndarray, np.ndarray]:
    """Build strict and remote-support image masks for pole-axis fitting.

    The strict mask stays tied to the detected sign and is always searched
    first.  A side-mounted sign can, however, be several metres from its one
    physical support, placing the support outside a bbox-width-based corridor.
    The fallback therefore opens the image laterally below the sign, but only
    for points farther than the direct-support threshold.  ``find_pole_bases``
    still requires 3-D horizontal connection coverage before accepting any
    such remote axis.
    """

    points = np.asarray(neighborhood_xyz, dtype=np.float64)
    pixels = np.asarray(projected_pixels, dtype=np.float64)
    valid = np.asarray(valid_projection_mask, dtype=bool)
    sign = np.asarray(sign_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("neighborhood_xyz must have shape (N, 3)")
    if pixels.shape != (points.shape[0], 2):
        raise ValueError("projected_pixels must have shape (N, 2)")
    if valid.shape != (points.shape[0],):
        raise ValueError("valid_projection_mask must have one value per point")
    if sign.shape != (3,) or not np.all(np.isfinite(sign)):
        raise ValueError("sign_xyz must be a finite three-vector")

    x1, y1, x2, y2 = (float(value) for value in corridor_bbox)
    vertical_band = valid & (pixels[:, 1] >= y1) & (pixels[:, 1] <= y2)
    strict = (
        vertical_band
        & (pixels[:, 0] >= x1)
        & (pixels[:, 0] <= x2)
    )
    radial_to_sign = np.linalg.norm(points[:, :2] - sign[None, :2], axis=1)
    remote_axis = vertical_band & (
        radial_to_sign > parameters.direct_max_axis_sign_distance_m
    )
    return strict, strict | remote_axis


def find_pole_bases_with_corridor_fallback(
    neighborhood_xyz: np.ndarray,
    projected_pixels: np.ndarray,
    valid_projection_mask: np.ndarray,
    sign_xyz: np.ndarray,
    corridor_bbox: tuple[float, float, float, float],
    parameters: PoleSearchParameters,
    classifications: np.ndarray | None = None,
    return_numbers: np.ndarray | None = None,
    number_of_returns: np.ndarray | None = None,
    *,
    workspace: PoleSearchWorkspace | None = None,
    ground_classifications: np.ndarray | None = None,
    travel_forward_xy: np.ndarray | None = None,
    travel_right_xy: np.ndarray | None = None,
    rejected_support_hypotheses: list[dict[str, Any]] | None = None,
) -> tuple[Any | None, np.ndarray, str, int, int]:
    """Search the strict sign corridor, then retry connected remote supports."""

    strict_mask, expanded_mask = build_pole_search_corridor_masks(
        neighborhood_xyz,
        projected_pixels,
        valid_projection_mask,
        sign_xyz,
        corridor_bbox,
        parameters,
    )
    search_workspace = workspace or PoleSearchWorkspace(neighborhood_xyz)
    result = find_pole_bases(
        neighborhood_xyz,
        strict_mask,
        sign_xyz,
        parameters,
        classifications,
        return_numbers,
        number_of_returns,
        workspace=search_workspace,
        ground_classifications=ground_classifications,
        travel_forward_xy=travel_forward_xy,
        travel_right_xy=travel_right_xy,
        rejected_support_hypotheses=rejected_support_hypotheses,
    )
    mode = "strict"
    searched_mask = strict_mask
    strict_candidate = (
        result.candidates[0]
        if result is not None and getattr(result, "candidates", ())
        else None
    )
    strict_is_direct = bool(
        strict_candidate is not None
        and float(strict_candidate.association_distance_m)
        <= parameters.direct_max_axis_sign_distance_m
    )
    if not strict_is_direct and np.any(expanded_mask & ~strict_mask):
        searched_mask = expanded_mask
        mode = "remote_expanded"
        expanded_result = find_pole_bases(
            neighborhood_xyz,
            expanded_mask,
            sign_xyz,
            parameters,
            classifications,
            return_numbers,
            number_of_returns,
            workspace=search_workspace,
            ground_classifications=ground_classifications,
            travel_forward_xy=travel_forward_xy,
            travel_right_xy=travel_right_xy,
            rejected_support_hypotheses=rejected_support_hypotheses,
        )
        if expanded_result is not None:
            if result is None:
                result = expanded_result
            else:
                preferred = select_pole_candidate(
                    (
                        result.candidates[0],
                        expanded_result.candidates[0],
                    ),
                    parameters,
                )
                if preferred is expanded_result.candidates[0]:
                    result = expanded_result
    return (
        result,
        searched_mask,
        mode,
        int(strict_mask.sum()),
        int(expanded_mask.sum()),
    )


def _world_points_to_overview_rays(
    points_xyz: list[np.ndarray] | np.ndarray,
    origin_xyz: np.ndarray,
) -> np.ndarray | None:
    """Convert finite non-origin world points into normalized camera rays."""

    points = np.asarray(points_xyz, dtype=np.float64)
    if points.size == 0:
        return None
    points = points.reshape(-1, 3)
    vectors = points - np.asarray(origin_xyz, dtype=np.float64)[None, :]
    norms = np.linalg.norm(vectors, axis=1)
    valid = np.all(np.isfinite(vectors), axis=1) & np.isfinite(norms) & (norms > 1e-9)
    if not np.any(valid):
        return None
    return vectors[valid] / norms[valid, None]


def build_pole_debug_overview_view(
    *,
    image_rgb: np.ndarray,
    detection_payload: dict[str, Any],
    pole_result: Any,
    origin_xyz: np.ndarray,
    fallback_view: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Render a result-focused QA view from the pole top to its base evidence.

    This function is deliberately used only after pole fitting.  The original
    rectified view and its corridor remain the inputs to pole search, while the
    returned view is a result-aware visualization.  Search can require a very
    wide FOV (especially for traffic-signal mast arms), but that search FOV must
    not force the result crop to include the camera body or unrelated road.
    """

    pano_height, pano_width = image_rgb.shape[:2]
    bbox_xyxy = tuple(float(value) for value in detection_payload["bbox_xyxy"])
    polygon_xy = detection_payload.get("mask_polygon")
    sign_pixels = iter_detection_support_pixels(polygon_xy, bbox_xyxy)
    sign_rays = np.asarray(
        [
            pixel_to_world_ray(
                pixel_x,
                pixel_y,
                pano_width,
                pano_height,
                fallback_view["pano_forward_vec"],
                fallback_view["pano_right_vec"],
                fallback_view["pano_up_vec"],
            )
            for pixel_x, pixel_y in sign_pixels
        ],
        dtype=np.float64,
    )

    base_points: list[np.ndarray] = []
    axis_points: list[np.ndarray] = []
    ground_points: list[np.ndarray] = []
    for candidate in pole_result.candidates:
        base_points.append(np.asarray(candidate.base_xyz, dtype=np.float64))
        observed_segment = build_pole_debug_axis_segments(candidate)["observed"]
        if observed_segment is not None:
            axis_points.extend(np.asarray(point, dtype=np.float64) for point in observed_segment)
        ground = getattr(candidate, "ground_estimate", None)
        if ground is not None and np.asarray(ground.support_xyz).size:
            ground_points.extend(
                np.asarray(point, dtype=np.float64)
                for point in np.asarray(ground.support_xyz).reshape(-1, 3)
            )
    if not base_points:
        base_points.append(np.asarray(pole_result.representative_xyz, dtype=np.float64))

    base_rays = _world_points_to_overview_rays(base_points, origin_xyz)
    axis_rays = _world_points_to_overview_rays(axis_points, origin_xyz)
    ground_rays = _world_points_to_overview_rays(ground_points, origin_xyz)
    configured_max_fov = float(runtime["perspective_max_fov_deg"])
    debug_min_fov = float(
        runtime.get(
            "pole_debug_min_fov_deg",
            runtime.get("perspective_min_fov_deg", 18.0),
        )
    )
    safe_max_fov = min(179.0, max(configured_max_fov, debug_min_fov))
    if safe_max_fov <= 0.0:
        raise ValueError("Pole debug overview requires a positive maximum FOV")

    view_forward_vec, view_right_vec, view_up_vec, hfov_deg, vfov_deg = (
        fit_perspective_overview(
            sign_rays,
            base_rays,
            axis_rays,
            ground_rays,
            fallback_view["pano_up_vec"],
            padding_deg=4.0,
            max_fov_deg=safe_max_fov,
            output_aspect_ratio=1.0,
            reference_right_vec=fallback_view["pano_right_vec"],
        )
    )
    # The debug raster is square.  A common FOV retains square pixels, and the
    # result-specific minimum keeps enough context without inheriting the much
    # wider pole-search FOV.
    overview_fov = min(
        safe_max_fov,
        max(float(hfov_deg), float(vfov_deg), min(debug_min_fov, safe_max_fov)),
    )
    hfov_deg = overview_fov
    vfov_deg = overview_fov
    view_size = int(runtime["perspective_view_size"])
    rectified_rgb = render_perspective_view_from_panorama(
        image_rgb,
        fallback_view["pano_forward_vec"],
        fallback_view["pano_right_vec"],
        fallback_view["pano_up_vec"],
        view_forward_vec,
        view_right_vec,
        view_up_vec,
        view_size,
        view_size,
        hfov_deg,
        vfov_deg,
    )

    rectified_polygon = None
    if polygon_xy:
        rectified_polygon = transform_panorama_points_to_perspective(
            [(float(point[0]), float(point[1])) for point in polygon_xy],
            pano_width,
            pano_height,
            fallback_view["pano_forward_vec"],
            fallback_view["pano_right_vec"],
            fallback_view["pano_up_vec"],
            view_forward_vec,
            view_right_vec,
            view_up_vec,
            view_size,
            view_size,
            hfov_deg,
            vfov_deg,
        )
        if len(rectified_polygon) < 3:
            rectified_polygon = None

    transformed_bbox_points = transform_panorama_points_to_perspective(
        [
            (bbox_xyxy[0], bbox_xyxy[1]),
            (bbox_xyxy[0], bbox_xyxy[3]),
            (bbox_xyxy[2], bbox_xyxy[1]),
            (bbox_xyxy[2], bbox_xyxy[3]),
        ],
        pano_width,
        pano_height,
        fallback_view["pano_forward_vec"],
        fallback_view["pano_right_vec"],
        fallback_view["pano_up_vec"],
        view_forward_vec,
        view_right_vec,
        view_up_vec,
        view_size,
        view_size,
        hfov_deg,
        vfov_deg,
    )
    rectified_bbox = points_to_bbox(rectified_polygon or transformed_bbox_points)
    if rectified_bbox is None:
        raise ValueError("Pole debug overview could not reproject the sign geometry")

    return {
        **fallback_view,
        "rectified_rgb": rectified_rgb,
        "rectified_polygon": rectified_polygon,
        "rectified_bbox": clamp_bbox(rectified_bbox, view_size, view_size),
        "view_forward_vec": view_forward_vec,
        "view_right_vec": view_right_vec,
        "view_up_vec": view_up_vec,
        "view_width": view_size,
        "view_height": view_size,
        "hfov_deg": float(hfov_deg),
        "vfov_deg": float(vfov_deg),
        "debug_overview_adaptive": True,
    }


def _draw_dashed_debug_line(
    draw: ImageDraw.ImageDraw,
    start_xy: np.ndarray,
    end_xy: np.ndarray,
    *,
    fill: tuple[int, int, int, int],
    width: int,
    dash_px: float = 10.0,
    gap_px: float = 7.0,
) -> None:
    start = np.asarray(start_xy, dtype=np.float64)
    end = np.asarray(end_xy, dtype=np.float64)
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length <= 1e-6:
        return
    direction = vector / length
    offset = 0.0
    while offset < length:
        dash_end = min(length, offset + dash_px)
        first = start + (direction * offset)
        second = start + (direction * dash_end)
        draw.line([tuple(first), tuple(second)], fill=fill, width=width)
        offset += dash_px + gap_px


def save_pole_debug_image(
    rectified_view: dict[str, Any],
    output_path: Path,
    *,
    corridor_bbox: tuple[float, float, float, float],
    sign_points_xyz: np.ndarray,
    neighborhood_records: dict[str, np.ndarray],
    pole_result: Any | None,
    origin_xyz: np.ndarray,
    mask_alpha: int,
    label: str,
    reason: str | None = None,
) -> None:
    """Save one wide QA view containing the sign, support points, axes and base."""

    canvas = Image.fromarray(rectified_view["rectified_rgb"]).convert("RGBA")
    x1, y1, x2, y2 = rectified_view["rectified_bbox"]
    polygon = rectified_view.get("rectified_polygon")
    if polygon:
        if mask_alpha > 0:
            overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay, "RGBA")
            overlay_draw.polygon(
                [tuple(point) for point in polygon],
                fill=(255, 220, 0, max(0, min(255, mask_alpha))),
            )
            canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((x1, y1, x2, y2), outline=(80, 255, 80, 255), width=3)
    if polygon:
        outline_points = [tuple(point) for point in polygon]
        draw.line([*outline_points, outline_points[0]], fill=(255, 220, 0, 255), width=2)
    draw.rectangle(corridor_bbox, outline=(255, 180, 0, 220), width=2)

    sign_pixels, sign_valid = _project_points_to_rectified_view(
        sign_points_xyz,
        origin_xyz,
        rectified_view,
    )
    if np.any(sign_valid):
        sampled = sign_pixels[sign_valid][:: max(1, int(math.ceil(sign_valid.sum() / 400)))]
        draw.point([(int(p[0]), int(p[1])) for p in sampled], fill=(0, 185, 220, 255))

    status_text = reason or "pole not found"
    ground_info_lines: list[str] = []
    connection_info_lines: list[str] = []
    if pole_result is not None:
        pole_points = neighborhood_records["xyz"][pole_result.point_indices]
        pole_pixels, pole_valid = _project_points_to_rectified_view(
            pole_points,
            origin_xyz,
            rectified_view,
        )
        if np.any(pole_valid):
            sampled = pole_pixels[pole_valid][:: max(1, int(math.ceil(pole_valid.sum() / 1600)))]
            draw.point([(int(p[0]), int(p[1])) for p in sampled], fill=(255, 30, 220, 235))

        for candidate_index, candidate in enumerate(pole_result.candidates, start=1):
            association_distance = float(candidate.association_distance_m)
            connection_coverage = candidate.horizontal_connection_coverage_ratio
            completeness = float(candidate.completeness_ratio or 0.0)
            shaft_summary = (
                f"complete={completeness:.2f} span={candidate.vertical_span_m:.2f}m"
            )
            if candidate.axis_endpoint_tilt_deg is not None:
                shaft_summary += (
                    f" end-tilt={candidate.axis_endpoint_tilt_deg:.2f}deg"
                    f" plumb={int(candidate.axis_plumb_adjusted)}"
                )
            if candidate.multi_return_fraction is not None:
                shaft_summary += (
                    f" multi-return={candidate.multi_return_fraction:.2f}"
                )
            if connection_coverage is None:
                connection_info_lines.append(
                    f"support{candidate_index}=direct assoc={association_distance:.3f}m "
                    + shaft_summary
                )
            else:
                occupied_bins = int(candidate.horizontal_connection_bin_count or 0)
                expected_bins = int(
                    candidate.horizontal_connection_expected_bin_count or 0
                )
                connection_info_lines.append(
                    f"arm{candidate_index}={occupied_bins}/{expected_bins} "
                    f"coverage={float(connection_coverage):.2f} "
                    f"assoc={association_distance:.3f}m "
                    + shaft_summary
                )
                if (
                    candidate.horizontal_connection_ridge_density_points_per_m
                    is not None
                ):
                    connection_info_lines.append(
                        f"arm{candidate_index}-ridge="
                        f"{candidate.horizontal_connection_ridge_point_count or 0}pts "
                        f"{candidate.horizontal_connection_ridge_density_points_per_m:.1f}pts/m "
                        f"side={candidate.support_side or 'UNKNOWN'}"
                    )
                if candidate.horizontal_connection_coherent_ratio is not None:
                    connection_info_lines.append(
                        f"arm{candidate_index}-3d="
                        f"{candidate.horizontal_connection_coherent_bin_count or 0}/"
                        f"{expected_bins} "
                        f"raw-ratio={candidate.horizontal_connection_coherent_ratio:.2f} "
                        f"point-frac="
                        f"{candidate.horizontal_connection_coherent_point_fraction or 0.0:.2f} "
                        f"anchored={int(bool(candidate.horizontal_connection_endpoint_anchored))}"
                    )
                if sign_points_xyz.size and abs(float(candidate.axis_direction[2])) > 1e-9:
                    sign_anchor = np.median(sign_points_xyz, axis=0)
                    axis_distance = (
                        float(sign_anchor[2]) - float(candidate.base_xyz[2])
                    ) / float(candidate.axis_direction[2])
                    attachment_xyz = (
                        np.asarray(candidate.base_xyz, dtype=np.float64)
                        + np.asarray(candidate.axis_direction, dtype=np.float64)
                        * axis_distance
                    )
                    connection_pixels, connection_valid = _project_points_to_rectified_view(
                        np.vstack((sign_anchor, attachment_xyz)),
                        origin_xyz,
                        rectified_view,
                    )
                    if np.all(connection_valid):
                        draw.line(
                            [tuple(connection_pixels[0]), tuple(connection_pixels[1])],
                            fill=(255, 145, 0, 255),
                            width=3,
                        )
            ground = candidate.ground_estimate
            if ground is not None and ground.support_xyz.size:
                ground_pixels, ground_valid = _project_points_to_rectified_view(
                    ground.support_xyz,
                    origin_xyz,
                    rectified_view,
                )
                visible_ground = ground_pixels[ground_valid]
                for point in visible_ground:
                    px, py = float(point[0]), float(point[1])
                    draw.ellipse(
                        (px - 3, py - 3, px + 3, py + 3),
                        fill=(30, 145, 255, 235),
                        outline=(190, 230, 255, 255),
                    )
                if visible_ground.shape[0] >= 3:
                    hull = cv2.convexHull(visible_ground.astype(np.float32)).reshape(-1, 2)
                    hull_points = [tuple(float(value) for value in point) for point in hull]
                    draw.line(
                        [*hull_points, hull_points[0]],
                        fill=(30, 145, 255, 230),
                        width=2,
                    )
                short_method = ground.method.removeprefix("robust_low_cell_plane_")
                ground_info_lines.append(
                    f"ground{candidate_index}={short_method} "
                    f"cells={ground.cell_count}/{ground.candidate_cell_count} "
                    f"rmse={ground.rmse_m:.3f}m z={ground.z:.3f}"
                )
            else:
                ground_info_lines.append(f"ground{candidate_index}=unavailable")
            axis_segments = build_pole_debug_axis_segments(candidate)
            observed_segment = axis_segments["observed"]
            if observed_segment is not None:
                observed_pixels, observed_valid = _project_points_to_rectified_view(
                    np.vstack(observed_segment),
                    origin_xyz,
                    rectified_view,
                )
                if np.all(observed_valid):
                    draw.line(
                        [tuple(observed_pixels[0]), tuple(observed_pixels[1])],
                        fill=(255, 40, 220, 255),
                        width=4,
                    )
            extrapolated_segment = axis_segments["ground_extrapolated"]
            if extrapolated_segment is not None:
                extrapolated_pixels, extrapolated_valid = _project_points_to_rectified_view(
                    np.vstack(extrapolated_segment),
                    origin_xyz,
                    rectified_view,
                )
                if np.all(extrapolated_valid):
                    _draw_dashed_debug_line(
                        draw,
                        extrapolated_pixels[0],
                        extrapolated_pixels[1],
                        fill=(255, 170, 225, 255),
                        width=3,
                    )

        base_pixels, base_valid = _project_points_to_rectified_view(
            pole_result.representative_xyz[None, :],
            origin_xyz,
            rectified_view,
        )
        if bool(base_valid[0]):
            px, py = float(base_pixels[0, 0]), float(base_pixels[0, 1])
            radius = 10
            draw.ellipse(
                (px - radius, py - radius, px + radius, py + radius),
                outline=(50, 255, 50, 255),
                width=4,
            )
            draw.line((px - radius, py, px + radius, py), fill=(50, 255, 50, 255), width=3)
            draw.line((px, py - radius, px, py + radius), fill=(50, 255, 50, 255), width=3)
        status_text = (
            f"pole={pole_result.pole_type} | {pole_result.method} | "
            f"{pole_result.status} | points={pole_result.point_indices.size}"
        )

    lines = [
        label,
        status_text,
        "cyan=sign magenta=pole orange=verified arm green=base blue=ground cells",
        "pole axis: solid=observed dashed=ground extrapolation",
        *connection_info_lines,
        *ground_info_lines,
    ]
    top = 12
    for line in lines:
        bounds = draw.textbbox((12, top), line)
        draw.rounded_rectangle(
            (bounds[0] - 7, bounds[1] - 5, bounds[2] + 7, bounds[3] + 5),
            radius=7,
            fill=(0, 0, 0, 175),
        )
        draw.text((12, top), line, fill=(255, 255, 255, 255))
        top = bounds[3] + 9
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, quality=95)


def extract_pole_for_detection(
    *,
    image_task: dict[str, Any],
    image_rgb: np.ndarray,
    detection_index: int,
    detection_payload: dict[str, Any],
    sign_points_xyz: np.ndarray,
    sign_xyz: np.ndarray,
    nearest_pointcloud_files: list[dict[str, Any]],
    runtime: dict[str, Any],
    pointcloud_cache: PointCloudReaderCache,
    logger: Any,
) -> dict[str, Any]:
    """Extract a support axis and extrapolate it to classified/local ground."""

    if not runtime["pole_detection"]:
        return {"enabled": False, "found": False, "reason": "disabled"}

    parameters = build_pole_search_parameters(runtime)
    fallback_parameters = build_pole_fallback_parameters(runtime, parameters)
    classification_policy = runtime.get("pole_classification_policy") or {
        "requested_mode": str(runtime.get("pole_classification_mode", "auto")),
        "effective_mode": "HYBRID",
        "uses_classification": True,
        "reason": "legacy_runtime_without_catalog_policy",
        "configured": {
            "ground_class_ids": list(parameters.ground_class_ids),
            "pole_class_ids": list(parameters.pole_class_ids),
            "excluded_pole_class_ids": list(parameters.excluded_pole_class_ids),
        },
    }
    classification_common = {
        "classification_mode_requested": str(
            classification_policy.get("requested_mode") or "auto"
        ),
        "classification_mode": str(
            classification_policy.get("effective_mode") or "GEOMETRY"
        ),
        "classification_reason": str(
            classification_policy.get("reason") or "unknown"
        ),
    }
    wide_runtime = dict(runtime)
    wide_runtime["perspective_min_fov_deg"] = max(
        float(runtime["perspective_min_fov_deg"]),
        float(runtime["pole_min_fov_deg"]),
    )
    wide_runtime["perspective_max_fov_deg"] = max(
        float(runtime["perspective_max_fov_deg"]),
        wide_runtime["perspective_min_fov_deg"],
    )
    rectified_view = build_rectified_detection_view(
        image_task=image_task,
        image_rgb=image_rgb,
        detection_payload=detection_payload,
        runtime=wide_runtime,
    )

    class_tag = sanitize_name(f"{detection_payload['class_id']:03d}_{detection_payload['class_name']}")
    base_name = f"{image_task['image_stem']}__det{detection_index:04d}__{class_tag}"
    debug_path = (
        Path(runtime["pole_debug_dir"]) / image_task["record_name"] / f"{base_name}.jpg"
    )
    corridor_bbox = build_pole_corridor_bbox(
        rectified_view,
        side_expand_ratio=runtime["pole_corridor_side_expand_ratio"],
        top_margin_ratio=runtime["pole_corridor_top_margin_ratio"],
    )
    configured_class_ids = {
        int(value)
        for values in (classification_policy.get("configured") or {}).values()
        for value in values
    }
    origin_xyz = np.asarray(image_task["origin"], dtype=np.float64)
    pole_search_attempts: list[dict[str, Any]] = []

    def load_and_search(
        search_parameters: PoleSearchParameters,
    ) -> dict[str, Any] | None:
        neighborhood_radius = (
            search_parameters.search_radius_m
            + search_parameters.ground_search_radius_m
        )
        minimum = sign_xyz - np.asarray(
            [
                neighborhood_radius,
                neighborhood_radius,
                search_parameters.max_drop_m,
            ],
            dtype=np.float64,
        )
        maximum = sign_xyz + np.asarray(
            [
                neighborhood_radius,
                neighborhood_radius,
                search_parameters.top_margin_m,
            ],
            dtype=np.float64,
        )
        selected_blocks = blocks_intersecting_bounds(
            nearest_pointcloud_files,
            minimum,
            maximum,
        )
        attempt = {
            "mode": (
                "physical_fallback"
                if search_parameters is fallback_parameters
                else "strict"
            ),
            "minimum_xyz": [float(value) for value in minimum],
            "maximum_xyz": [float(value) for value in maximum],
            "intersected_block_count": len(selected_blocks),
            "intersected_pointcloud_files": sorted(
                {
                    str(pointcloud_file["path"])
                    for pointcloud_file, _ in selected_blocks
                }
            ),
            "retained_point_count": 0,
        }
        pole_search_attempts.append(attempt)
        record_parts: list[dict[str, np.ndarray]] = []
        selected_files: set[str] = set()
        for pointcloud_file, block in selected_blocks:
            block_records = pointcloud_cache.read_block_records(
                pointcloud_file,
                block,
            )
            xyz = block_records["xyz"]
            if xyz.shape[0] == 0:
                continue
            radial = np.linalg.norm(xyz[:, :2] - sign_xyz[None, :2], axis=1)
            keep = (
                np.all(np.isfinite(xyz), axis=1)
                & (radial <= neighborhood_radius)
                & (xyz[:, 2] >= minimum[2])
                & (xyz[:, 2] <= maximum[2])
            )
            if not np.any(keep):
                continue
            record_parts.append(
                {key: value[keep] for key, value in block_records.items()}
            )
            selected_files.add(str(pointcloud_file["path"]))
        if not record_parts:
            return None
        attempt["retained_point_count"] = int(
            sum(part["xyz"].shape[0] for part in record_parts)
        )

        keys = tuple(record_parts[0])
        selected_records = {
            key: np.concatenate(
                [part[key] for part in record_parts],
                axis=0,
            )
            for key in keys
        }
        selected_classes = np.asarray(
            selected_records["classification"],
            dtype=np.int16,
        )
        selected_algorithm_classes = pole_classifications_for_policy(
            selected_classes,
            classification_policy,
        )
        pole_workspace = PoleSearchWorkspace(selected_records["xyz"])
        selected_pixels, selected_valid = _project_points_to_rectified_view(
            selected_records["xyz"],
            origin_xyz,
            rectified_view,
        )
        support_hypotheses: list[dict[str, Any]] = []
        (
            selected_result,
            selected_corridor_mask,
            selected_corridor_mode,
            selected_strict_count,
            selected_expanded_count,
        ) = find_pole_bases_with_corridor_fallback(
            selected_records["xyz"],
            selected_pixels,
            selected_valid,
            sign_xyz,
            corridor_bbox,
            search_parameters,
            selected_algorithm_classes,
            np.asarray(selected_records["return_number"], dtype=np.int16),
            np.asarray(
                selected_records["number_of_returns"],
                dtype=np.int16,
            ),
            workspace=pole_workspace,
            ground_classifications=selected_algorithm_classes,
            travel_forward_xy=np.asarray(
                rectified_view["raw_forward_vec"][:2],
                dtype=np.float64,
            ),
            travel_right_xy=np.asarray(
                rectified_view["raw_right_vec"][:2],
                dtype=np.float64,
            ),
            rejected_support_hypotheses=support_hypotheses,
        )
        return {
            "records": selected_records,
            "classes": selected_classes,
            "result": selected_result,
            "corridor_mask": selected_corridor_mask,
            "corridor_mode": selected_corridor_mode,
            "strict_count": selected_strict_count,
            "expanded_count": selected_expanded_count,
            "used_files": selected_files,
            "used_block_count": len(selected_blocks),
            "support_hypotheses": support_hypotheses,
        }

    strict_state = load_and_search(parameters)
    selected_state = strict_state
    result = strict_state["result"] if strict_state is not None else None
    all_support_hypotheses = (
        list(strict_state["support_hypotheses"])
        if strict_state is not None
        else []
    )
    strict_corridor_point_count = (
        int(strict_state["strict_count"]) if strict_state is not None else 0
    )
    expanded_corridor_point_count = (
        int(strict_state["expanded_count"]) if strict_state is not None else 0
    )
    pole_fallback_attempted = False
    pole_fallback_used = False
    fallback_strict_corridor_point_count: int | None = None
    fallback_expanded_corridor_point_count: int | None = None
    if (
        fallback_parameters is not None
        and (result is None or result.status != "AUTO")
    ):
        pole_fallback_attempted = True
        fallback_state = load_and_search(fallback_parameters)
        if fallback_state is not None:
            all_support_hypotheses.extend(
                fallback_state["support_hypotheses"]
            )
            fallback_strict_corridor_point_count = int(
                fallback_state["strict_count"]
            )
            fallback_expanded_corridor_point_count = int(
                fallback_state["expanded_count"]
            )
            fallback_result = fallback_state["result"]
            fallback_state["corridor_mode"] = (
                f"physical_fallback_{fallback_state['corridor_mode']}"
            )
        else:
            fallback_result = None
        if result is None and fallback_state is not None:
            # Keep the wider search state for failure QA even when no pole was
            # accepted, because it reflects the last physical envelope tested.
            selected_state = fallback_state
        if fallback_result is not None:
            # Wider distance/drop and more tolerant arm gates are deliberately
            # exposed as REVIEW results even when the geometric fitter itself
            # rates the candidate AUTO.
            reviewed_fallback_result = replace(
                fallback_result,
                status="REVIEW",
            )
            fallback_is_better = (
                result is None
                or select_cross_profile_pole_candidate(
                    (
                        result.candidates[0],
                        fallback_result.candidates[0],
                    ),
                    parameters,
                )
                is fallback_result.candidates[0]
            )
            if fallback_is_better:
                result = reviewed_fallback_result
                selected_state = fallback_state
                selected_state["result"] = result
                pole_fallback_used = True

    ordered_hypotheses = sorted(
        all_support_hypotheses,
        key=lambda item: (
            float(item.get("axis_rmse_m") or math.inf),
            -float(item.get("vertical_span_m") or 0.0),
            float(item.get("association_distance_m") or math.inf),
            float(item.get("axis_x") or 0.0),
            float(item.get("axis_y") or 0.0),
        ),
    )
    support_hypotheses: list[dict[str, Any]] = []
    for hypothesis in ordered_hypotheses:
        try:
            hypothesis_xy = np.asarray(
                [float(hypothesis["axis_x"]), float(hypothesis["axis_y"])],
                dtype=np.float64,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not np.all(np.isfinite(hypothesis_xy)):
            continue
        if any(
            float(
                np.linalg.norm(
                    hypothesis_xy
                    - np.asarray(
                        [kept["axis_x"], kept["axis_y"]],
                        dtype=np.float64,
                    )
                )
            )
            <= 0.15
            for kept in support_hypotheses
        ):
            continue
        support_hypotheses.append(dict(hypothesis))
        if len(support_hypotheses) >= 64:
            break

    if selected_state is None:
        empty_records = {
            "xyz": np.empty((0, 3), dtype=np.float64),
            "rgb": np.empty((0, 3), dtype=np.uint8),
        }
        if not runtime["disable_pole_debug"]:
            save_pole_debug_image(
                rectified_view,
                debug_path,
                corridor_bbox=corridor_bbox,
                sign_points_xyz=sign_points_xyz,
                neighborhood_records=empty_records,
                pole_result=None,
                origin_xyz=origin_xyz,
                mask_alpha=runtime["debug_mask_alpha"],
                label=(
                    f"{detection_payload['class_name']} "
                    f"{detection_payload['confidence']:.3f} | "
                    f"class={classification_common['classification_mode']}"
                ),
                reason="no neighborhood points",
            )
        return {
            **classification_common,
            "enabled": True,
            "found": False,
            "reason": "no_neighborhood_points",
            "classification_neighborhood_point_count": 0,
            "classification_matched_point_count": 0,
            "neighborhood_point_count": 0,
            "corridor_point_count": 0,
            "strict_corridor_point_count": 0,
            "expanded_corridor_point_count": 0,
            "fallback_strict_corridor_point_count": None,
            "fallback_expanded_corridor_point_count": None,
            "corridor_mode": (
                "physical_fallback_no_points"
                if pole_fallback_attempted
                else "strict_no_points"
            ),
            "pole_fallback_enabled": fallback_parameters is not None,
            "pole_fallback_attempted": pole_fallback_attempted,
            "pole_fallback_used": False,
            "support_hypotheses": support_hypotheses,
            "pole_search_attempts": pole_search_attempts,
            "used_pointcloud_files": (
                pole_search_attempts[-1]["intersected_pointcloud_files"]
                if pole_search_attempts
                else []
            ),
            "used_block_count": (
                pole_search_attempts[-1]["intersected_block_count"]
                if pole_search_attempts
                else 0
            ),
            "debug_image_path": str(debug_path.resolve())
            if not runtime["disable_pole_debug"]
            else None,
        }

    records = selected_state["records"]
    neighborhood_classes = selected_state["classes"]
    classification_matched_point_count = int(
        np.isin(neighborhood_classes, tuple(configured_class_ids)).sum()
    )
    corridor_mask = selected_state["corridor_mask"]
    corridor_mode = str(selected_state["corridor_mode"])
    used_files = set(selected_state["used_files"])
    used_block_count = int(selected_state["used_block_count"])

    if not runtime["disable_pole_debug"]:
        debug_view = rectified_view
        debug_corridor_bbox = corridor_bbox
        if result is not None:
            try:
                debug_view = build_pole_debug_overview_view(
                    image_rgb=image_rgb,
                    detection_payload=detection_payload,
                    pole_result=result,
                    origin_xyz=origin_xyz,
                    fallback_view=rectified_view,
                    runtime=runtime,
                )
                debug_corridor_bbox = build_pole_corridor_bbox(
                    debug_view,
                    side_expand_ratio=runtime["pole_corridor_side_expand_ratio"],
                    top_margin_ratio=runtime["pole_corridor_top_margin_ratio"],
                )
            except ValueError as exc:
                logger.warning(
                    "Adaptive pole debug overview failed for %s detection %d; "
                    "using the search view: %s",
                    image_task["image_name"],
                    detection_index,
                    exc,
                )
        if corridor_mode.endswith("remote_expanded"):
            debug_corridor_bbox = (
                0.0,
                debug_corridor_bbox[1],
                float(debug_view["view_width"] - 1),
                debug_corridor_bbox[3],
            )
        save_pole_debug_image(
            debug_view,
            debug_path,
            corridor_bbox=debug_corridor_bbox,
            sign_points_xyz=sign_points_xyz,
            neighborhood_records=records,
            pole_result=result,
            origin_xyz=origin_xyz,
            mask_alpha=runtime["debug_mask_alpha"],
            label=(
                f"{detection_payload['class_name']} "
                f"{detection_payload['confidence']:.3f} | "
                f"class={classification_common['classification_mode']} | "
                f"search={corridor_mode}"
            ),
            reason="no valid support-ground candidate" if result is None else None,
        )

    common = {
        **classification_common,
        "enabled": True,
        "classification_neighborhood_point_count": int(neighborhood_classes.size),
        "classification_matched_point_count": classification_matched_point_count,
        "neighborhood_point_count": int(records["xyz"].shape[0]),
        "corridor_point_count": int(corridor_mask.sum()),
        "strict_corridor_point_count": strict_corridor_point_count,
        "expanded_corridor_point_count": expanded_corridor_point_count,
        "fallback_strict_corridor_point_count": (
            fallback_strict_corridor_point_count
        ),
        "fallback_expanded_corridor_point_count": (
            fallback_expanded_corridor_point_count
        ),
        "corridor_mode": corridor_mode,
        "pole_fallback_enabled": fallback_parameters is not None,
        "pole_fallback_attempted": pole_fallback_attempted,
        "pole_fallback_used": pole_fallback_used,
        "support_hypotheses": support_hypotheses,
        "pole_search_attempts": pole_search_attempts,
        "used_pointcloud_files": sorted(used_files),
        "used_block_count": used_block_count,
        "wide_hfov_deg": float(rectified_view["hfov_deg"]),
        "wide_vfov_deg": float(rectified_view["vfov_deg"]),
        "debug_image_path": str(debug_path.resolve())
        if not runtime["disable_pole_debug"]
        else None,
    }
    if result is None:
        logger.info(
            "No pole axis accepted for %s detection %d (classification=%s).",
            image_task["image_name"],
            detection_index,
            classification_common["classification_mode"],
        )
        return {**common, "found": False, "reason": "no_support_ground_candidate"}

    point_crop_path: Path | None = None
    if not runtime["disable_pole_point_crop"]:
        point_crop_path = (
            Path(runtime["pole_crops_dir"])
            / image_task["record_name"]
            / f"{base_name}.las"
        )
        write_pole_las(
            records,
            result.point_indices,
            point_crop_path,
            crs_wkt=runtime.get("crs_wkt"),
        )

    primary = result.candidates[0]
    axis_rmse = float(np.mean([item.radial_rmse_m for item in result.candidates]))
    ground_rmses = [
        float(item.ground_rmse_m)
        for item in result.candidates
        if item.ground_rmse_m is not None
    ]
    ground_rmse = float(np.mean(ground_rmses)) if ground_rmses else None
    coverage = (
        float(primary.completeness_ratio)
        if primary.completeness_ratio is not None
        else min(1.0, primary.vertical_span_m / 2.0)
    )
    ground_penalty_rmse = (
        ground_rmse if ground_rmse is not None else parameters.ground_max_rmse_m
    )
    quality = float(detection_payload["confidence"]) * coverage * math.exp(
        -(5.0 * axis_rmse) - (2.0 * ground_penalty_rmse)
    )
    if result.status != "AUTO":
        quality *= 0.75
    candidates_payload = [
        {
            "base_xyz": [float(value) for value in item.base_xyz],
            "axis_direction": [float(value) for value in item.axis_direction],
            "point_count": int(item.point_count),
            "vertical_span_m": float(item.vertical_span_m),
            "vertical_bin_count": int(item.vertical_bin_count),
            "observed_z_min": float(item.observed_z_min),
            "observed_z_max": float(item.observed_z_max),
            "longest_consecutive_bin_count": int(item.longest_consecutive_bin_count),
            "max_observed_z_gap_m": float(item.max_observed_z_gap_m),
            "vertical_occupancy_ratio": float(item.vertical_occupancy_ratio),
            "middle_support_bin_count": (
                None if item.middle_support_bin_count is None else int(item.middle_support_bin_count)
            ),
            "middle_expected_bin_count": (
                None
                if item.middle_expected_bin_count is None
                else int(item.middle_expected_bin_count)
            ),
            "middle_support_coverage_ratio": (
                None
                if item.middle_support_coverage_ratio is None
                else float(item.middle_support_coverage_ratio)
            ),
            "completeness_ratio": (
                None
                if item.completeness_ratio is None
                else float(item.completeness_ratio)
            ),
            "association_distance_m": float(item.association_distance_m),
            "horizontal_connection_bin_count": (
                None
                if item.horizontal_connection_bin_count is None
                else int(item.horizontal_connection_bin_count)
            ),
            "horizontal_connection_expected_bin_count": (
                None
                if item.horizontal_connection_expected_bin_count is None
                else int(item.horizontal_connection_expected_bin_count)
            ),
            "horizontal_connection_coverage_ratio": (
                None
                if item.horizontal_connection_coverage_ratio is None
                else float(item.horizontal_connection_coverage_ratio)
            ),
            "horizontal_connection_point_count": (
                None
                if item.horizontal_connection_point_count is None
                else int(item.horizontal_connection_point_count)
            ),
            "horizontal_connection_ridge_point_count": (
                None
                if item.horizontal_connection_ridge_point_count is None
                else int(item.horizontal_connection_ridge_point_count)
            ),
            "horizontal_connection_ridge_density_points_per_m": (
                None
                if item.horizontal_connection_ridge_density_points_per_m is None
                else float(
                    item.horizontal_connection_ridge_density_points_per_m
                )
            ),
            "horizontal_connection_coherent_bin_count": (
                None
                if item.horizontal_connection_coherent_bin_count is None
                else int(item.horizontal_connection_coherent_bin_count)
            ),
            "horizontal_connection_coherent_coverage_ratio": (
                None
                if item.horizontal_connection_coherent_coverage_ratio is None
                else float(item.horizontal_connection_coherent_coverage_ratio)
            ),
            "horizontal_connection_coherent_ratio": (
                None
                if item.horizontal_connection_coherent_ratio is None
                else float(item.horizontal_connection_coherent_ratio)
            ),
            "horizontal_connection_coherent_point_fraction": (
                None
                if item.horizontal_connection_coherent_point_fraction is None
                else float(
                    item.horizontal_connection_coherent_point_fraction
                )
            ),
            "horizontal_connection_endpoint_anchored": (
                item.horizontal_connection_endpoint_anchored
            ),
            "travel_longitudinal_offset_m": item.travel_longitudinal_offset_m,
            "travel_lateral_offset_m": item.travel_lateral_offset_m,
            "support_side": item.support_side,
            "crossroad_alignment_ratio": item.crossroad_alignment_ratio,
            "axis_rmse_m": float(item.radial_rmse_m),
            "axis_stabilized": bool(item.axis_stabilized),
            "axis_bin_inlier_count": (
                None
                if item.axis_bin_inlier_count is None
                else int(item.axis_bin_inlier_count)
            ),
            "axis_bin_count": (
                None if item.axis_bin_count is None else int(item.axis_bin_count)
            ),
            "axis_plumb_adjusted": bool(item.axis_plumb_adjusted),
            "axis_endpoint_tilt_deg": (
                None
                if item.axis_endpoint_tilt_deg is None
                else float(item.axis_endpoint_tilt_deg)
            ),
            "axis_endpoint_drift_m": (
                None
                if item.axis_endpoint_drift_m is None
                else float(item.axis_endpoint_drift_m)
            ),
            "lowest_observed_z": float(item.lowest_observed_z),
            "ground_z": None if item.ground_z is None else float(item.ground_z),
            "ground_rmse_m": None
            if item.ground_rmse_m is None
            else float(item.ground_rmse_m),
            "bottom_gap_m": None if item.bottom_gap_m is None else float(item.bottom_gap_m),
            "ground_support_distance_m": (
                None
                if item.ground_support_distance_m is None
                else float(item.ground_support_distance_m)
            ),
            "occluded_bottom": item.occluded_bottom,
            "occlusion_status": item.occlusion_status,
            "method": item.method,
            "status": item.status,
            "score": float(item.score),
            "dominant_class_id": item.dominant_class_id,
            "dominant_class_fraction": item.dominant_class_fraction,
            "multi_return_fraction": item.multi_return_fraction,
            "ground_fit_method": (
                item.ground_estimate.method if item.ground_estimate is not None else None
            ),
            "ground_cell_count": (
                item.ground_estimate.cell_count if item.ground_estimate is not None else 0
            ),
            "ground_candidate_cell_count": (
                item.ground_estimate.candidate_cell_count
                if item.ground_estimate is not None
                else 0
            ),
        }
        for item in result.candidates
    ]
    logger.info(
        "Pole accepted for %s detection %d: type=%s method=%s status=%s "
        "classification=%s xyz=(%.3f, %.3f, %.3f)",
        image_task["image_name"],
        detection_index,
        result.pole_type,
        result.method,
        result.status,
        classification_common["classification_mode"],
        result.representative_xyz[0],
        result.representative_xyz[1],
        result.representative_xyz[2],
    )
    return {
        **common,
        "found": True,
        "reason": None,
        "x": float(result.representative_xyz[0]),
        "y": float(result.representative_xyz[1]),
        "z": float(result.representative_xyz[2]),
        "type": result.pole_type,
        "method": result.method,
        "status": result.status,
        "occluded_bottom": result.occluded_bottom,
        "occlusion_status": result.occlusion_status,
        "pole_count": len(result.candidates),
        "point_count": int(result.point_indices.size),
        "quality": quality,
        "axis_rmse_m": axis_rmse,
        "ground_rmse_m": ground_rmse,
        "axis_stabilized": bool(primary.axis_stabilized),
        "axis_bin_inlier_count": primary.axis_bin_inlier_count,
        "axis_bin_count": primary.axis_bin_count,
        "axis_plumb_adjusted": bool(primary.axis_plumb_adjusted),
        "axis_endpoint_tilt_deg": primary.axis_endpoint_tilt_deg,
        "axis_endpoint_drift_m": primary.axis_endpoint_drift_m,
        "bottom_gap_m": primary.bottom_gap_m,
        "ground_support_distance_m": primary.ground_support_distance_m,
        "dominant_class_id": primary.dominant_class_id,
        "dominant_class_fraction": primary.dominant_class_fraction,
        "multi_return_fraction": primary.multi_return_fraction,
        "association_distance_m": float(primary.association_distance_m),
        "horizontal_connection_coverage_ratio": (
            None
            if primary.horizontal_connection_coverage_ratio is None
            else float(primary.horizontal_connection_coverage_ratio)
        ),
        "horizontal_connection_point_count": (
            primary.horizontal_connection_point_count
        ),
        "horizontal_connection_ridge_point_count": (
            primary.horizontal_connection_ridge_point_count
        ),
        "horizontal_connection_ridge_density_points_per_m": (
            primary.horizontal_connection_ridge_density_points_per_m
        ),
        "horizontal_connection_coherent_bin_count": (
            primary.horizontal_connection_coherent_bin_count
        ),
        "horizontal_connection_coherent_coverage_ratio": (
            primary.horizontal_connection_coherent_coverage_ratio
        ),
        "horizontal_connection_coherent_ratio": (
            primary.horizontal_connection_coherent_ratio
        ),
        "horizontal_connection_coherent_point_fraction": (
            primary.horizontal_connection_coherent_point_fraction
        ),
        "horizontal_connection_endpoint_anchored": (
            primary.horizontal_connection_endpoint_anchored
        ),
        "travel_longitudinal_offset_m": primary.travel_longitudinal_offset_m,
        "travel_lateral_offset_m": primary.travel_lateral_offset_m,
        "support_side": primary.support_side,
        "crossroad_alignment_ratio": primary.crossroad_alignment_ratio,
        "completeness_ratio": (
            None
            if primary.completeness_ratio is None
            else float(primary.completeness_ratio)
        ),
        "point_crop_path": str(point_crop_path.resolve()) if point_crop_path else None,
        "candidates": candidates_payload,
    }


def crop_points_for_las_export(
    points_xyz: np.ndarray,
    colors_rgb: np.ndarray,
    representative_xyz: np.ndarray,
    half_extent_m: float,
) -> tuple[np.ndarray, np.ndarray, bool]:
    if points_xyz.shape[0] == 0 or half_extent_m <= 0.0:
        return points_xyz, colors_rgb, False

    representative_xyz = np.asarray(representative_xyz, dtype=np.float64)
    axis_offsets = np.abs(points_xyz.astype(np.float64) - representative_xyz[None, :])
    in_extent = np.all(axis_offsets <= half_extent_m, axis=1)
    if np.any(in_extent):
        return points_xyz[in_extent], colors_rgb[in_extent], False

    nearest_index = int(
        np.argmin(
            np.linalg.norm(points_xyz.astype(np.float64) - representative_xyz[None, :], axis=1)
        )
    )
    return (
        points_xyz[nearest_index:nearest_index + 1],
        colors_rgb[nearest_index:nearest_index + 1],
        True,
    )


def robust_front_surface_distance(
    distances: np.ndarray,
    *,
    quantile: float,
    min_support: int,
) -> float:
    """Choose a near-surface anchor that is not controlled by one stray point."""
    values = np.asarray(distances, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("Cannot estimate a front surface from no finite distances")
    quantile = min(1.0, max(0.0, float(quantile)))
    rank = max(int(min_support), int(math.ceil(values.size * quantile))) - 1
    rank = min(max(0, rank), values.size - 1)
    return float(np.partition(values, rank)[rank])


def cluster_extracted_points(
    points_xyz: np.ndarray,
    pixels_xy: np.ndarray,
    distances: np.ndarray,
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    if points_xyz.shape[0] == 0:
        return None

    cluster_radius_m = float(runtime["cluster_radius_m"])
    cluster_min_neighbors = max(1, int(runtime["cluster_min_neighbors"]))
    cluster_trim_radius_multiplier = float(runtime["cluster_trim_radius_multiplier"])

    if points_xyz.shape[0] == 1:
        cluster_points = points_xyz
        cluster_pixels = pixels_xy
        cluster_distances = distances
        representative_xyz = np.median(cluster_points.astype(np.float64), axis=0)
        return {
            "points_xyz": cluster_points,
            "pixels_xy": cluster_pixels,
            "distances": cluster_distances,
            "representative_xyz": representative_xyz,
            "raw_point_count": 1,
            "cluster_point_count": 1,
            "cluster_count": 1,
            "reason": None,
        }

    tree = cKDTree(points_xyz.astype(np.float64))
    neighbor_lists = tree.query_ball_point(points_xyz.astype(np.float64), r=cluster_radius_m)
    core_mask = np.asarray(
        [len(neighbors) >= cluster_min_neighbors for neighbors in neighbor_lists],
        dtype=bool,
    )

    if not np.any(core_mask):
        return {
            "points_xyz": np.empty((0, 3), dtype=np.float32),
            "pixels_xy": np.empty((0, 2), dtype=np.int32),
            "distances": np.empty((0,), dtype=np.float64),
            "representative_xyz": None,
            "raw_point_count": int(points_xyz.shape[0]),
            "cluster_point_count": 0,
            "cluster_count": 0,
            "reason": "no_dense_cluster",
        }

    parents = np.arange(points_xyz.shape[0], dtype=np.int32)

    def find(node: int) -> int:
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def union(node_a: int, node_b: int) -> None:
        root_a = find(node_a)
        root_b = find(node_b)
        if root_a != root_b:
            parents[root_b] = root_a

    core_indices = np.flatnonzero(core_mask)
    for core_index in core_indices:
        for neighbor_index in neighbor_lists[core_index]:
            if neighbor_index <= core_index or not core_mask[neighbor_index]:
                continue
            union(int(core_index), int(neighbor_index))

    root_to_cluster: dict[int, int] = {}
    point_cluster_ids = np.full(points_xyz.shape[0], -1, dtype=np.int32)
    for core_index in core_indices:
        root = find(int(core_index))
        cluster_id = root_to_cluster.setdefault(root, len(root_to_cluster))
        point_cluster_ids[int(core_index)] = cluster_id

    non_core_indices = np.flatnonzero(~core_mask)
    for point_index in non_core_indices:
        candidate_core_neighbors = [
            neighbor_index
            for neighbor_index in neighbor_lists[int(point_index)]
            if core_mask[neighbor_index]
        ]
        if not candidate_core_neighbors:
            continue

        candidate_clusters = point_cluster_ids[np.asarray(candidate_core_neighbors, dtype=np.int32)]
        candidate_clusters = candidate_clusters[candidate_clusters >= 0]
        if candidate_clusters.size == 0:
            continue

        unique_clusters = np.unique(candidate_clusters)
        best_cluster_id = -1
        best_distance = None
        point_xyz = points_xyz[int(point_index)].astype(np.float64)
        for cluster_id in unique_clusters.tolist():
            cluster_member_mask = point_cluster_ids == cluster_id
            cluster_member_points = points_xyz[cluster_member_mask].astype(np.float64)
            if cluster_member_points.size == 0:
                continue
            cluster_distance = float(
                np.linalg.norm(cluster_member_points - point_xyz[None, :], axis=1).min()
            )
            if best_distance is None or cluster_distance < best_distance:
                best_distance = cluster_distance
                best_cluster_id = int(cluster_id)

        if best_cluster_id >= 0:
            point_cluster_ids[int(point_index)] = best_cluster_id

    valid_cluster_ids = point_cluster_ids[point_cluster_ids >= 0]
    if valid_cluster_ids.size == 0:
        return {
            "points_xyz": np.empty((0, 3), dtype=np.float32),
            "pixels_xy": np.empty((0, 2), dtype=np.int32),
            "distances": np.empty((0,), dtype=np.float64),
            "representative_xyz": None,
            "raw_point_count": int(points_xyz.shape[0]),
            "cluster_point_count": 0,
            "cluster_count": 0,
            "reason": "no_assigned_cluster",
        }

    cluster_summaries: list[dict[str, Any]] = []
    for cluster_id in np.unique(valid_cluster_ids).tolist():
        cluster_mask = point_cluster_ids == cluster_id
        cluster_points = points_xyz[cluster_mask]
        cluster_pixels = pixels_xy[cluster_mask]
        cluster_distances = distances[cluster_mask]
        cluster_median_xyz = np.median(cluster_points.astype(np.float64), axis=0)

        radial_offsets = np.linalg.norm(
            cluster_points.astype(np.float64) - cluster_median_xyz[None, :],
            axis=1,
        )
        trim_radius_m = max(
            cluster_radius_m * cluster_trim_radius_multiplier,
            float(np.median(radial_offsets)) * 3.0,
        )
        trimmed_mask = radial_offsets <= trim_radius_m
        if np.any(trimmed_mask):
            cluster_points = cluster_points[trimmed_mask]
            cluster_pixels = cluster_pixels[trimmed_mask]
            cluster_distances = cluster_distances[trimmed_mask]
            cluster_median_xyz = np.median(cluster_points.astype(np.float64), axis=0)

        cluster_summaries.append(
            {
                "cluster_id": int(cluster_id),
                "points_xyz": cluster_points,
                "pixels_xy": cluster_pixels,
                "distances": cluster_distances,
                "representative_xyz": cluster_median_xyz,
                "point_count": int(cluster_points.shape[0]),
                "median_distance_m": float(np.median(cluster_distances)),
                "min_distance_m": float(cluster_distances.min()),
            }
        )

    cluster_summaries = [item for item in cluster_summaries if item["point_count"] > 0]
    if not cluster_summaries:
        return {
            "points_xyz": np.empty((0, 3), dtype=np.float32),
            "pixels_xy": np.empty((0, 2), dtype=np.int32),
            "distances": np.empty((0,), dtype=np.float64),
            "representative_xyz": None,
            "raw_point_count": int(points_xyz.shape[0]),
            "cluster_point_count": 0,
            "cluster_count": 0,
            "reason": "all_clusters_trimmed",
        }

    cluster_summaries.sort(
        key=lambda item: (
            item["median_distance_m"],
            item["min_distance_m"],
            -item["point_count"],
        )
    )
    selected = cluster_summaries[0]
    return {
        "points_xyz": selected["points_xyz"],
        "pixels_xy": selected["pixels_xy"],
        "distances": selected["distances"],
        "representative_xyz": selected["representative_xyz"],
        "raw_point_count": int(points_xyz.shape[0]),
        "cluster_point_count": int(selected["point_count"]),
        "cluster_count": len(cluster_summaries),
        "reason": None,
    }


def collect_detection_points_at_range(
    nearest_pointcloud_files: list[dict[str, Any]],
    pointcloud_cache: PointCloudReaderCache,
    origin_xyz: np.ndarray,
    rectified_view: dict[str, Any],
    detection_mask: np.ndarray,
    crop_bbox: tuple[int, int, int, int],
    *,
    maximum_range_m: float,
    angle_margin_rad: float,
) -> dict[str, Any]:
    """Collect projected points inside one detection mask at an exact range."""

    selected_points: list[np.ndarray] = []
    selected_pixels: list[np.ndarray] = []
    selected_distances: list[np.ndarray] = []
    used_pointcloud_files: set[str] = set()
    selected_block_count = 0
    candidate_block_count = 0

    for pointcloud_file in nearest_pointcloud_files:
        blocks = select_candidate_blocks(
            pointcloud_file,
            origin_xyz,
            rectified_view["center_ray"],
            rectified_view["detection_angle"],
            maximum_range_m,
            angle_margin_rad,
        )
        candidate_block_count += len(blocks)
        for block in blocks:
            coords_xyz, _raw_rgb, _intensity = pointcloud_cache.read_block_points(
                pointcloud_file["path"],
                block["name"],
            )
            if coords_xyz.size == 0:
                continue

            vectors = coords_xyz.astype(np.float64) - origin_xyz[None, :]
            distances = np.linalg.norm(vectors, axis=1)
            range_mask = (distances > 0.1) & (distances <= maximum_range_m)
            if not np.any(range_mask):
                continue

            coords_xyz = coords_xyz[range_mask]
            distances = distances[range_mask]
            u, v, _projected_distances, valid_projection = project_points_perspective(
                coords_xyz.astype(np.float64),
                origin_xyz,
                rectified_view["view_forward_vec"],
                rectified_view["view_right_vec"],
                rectified_view["view_up_vec"],
                rectified_view["view_width"],
                rectified_view["view_height"],
                rectified_view["hfov_deg"],
                rectified_view["vfov_deg"],
            )
            if not np.any(valid_projection):
                continue

            coords_xyz = coords_xyz[valid_projection]
            distances = distances[valid_projection]
            u = u[valid_projection]
            v = v[valid_projection]
            finite_projection = np.isfinite(u) & np.isfinite(v)
            if not np.any(finite_projection):
                continue

            coords_xyz = coords_xyz[finite_projection]
            distances = distances[finite_projection]
            u = u[finite_projection]
            v = v[finite_projection]
            crop_x1, crop_y1, crop_x2, crop_y2 = crop_bbox
            in_crop = (
                (u >= crop_x1)
                & (u < crop_x2)
                & (v >= crop_y1)
                & (v < crop_y2)
            )
            if not np.any(in_crop):
                continue

            coords_xyz = coords_xyz[in_crop]
            distances = distances[in_crop]
            u_int = np.rint(u[in_crop]).astype(np.int32)
            v_int = np.rint(v[in_crop]).astype(np.int32)
            u_int = np.clip(u_int, 0, rectified_view["view_width"] - 1)
            v_int = np.clip(v_int, 0, rectified_view["view_height"] - 1)
            local_x = np.clip(u_int - crop_x1, 0, detection_mask.shape[1] - 1)
            local_y = np.clip(v_int - crop_y1, 0, detection_mask.shape[0] - 1)
            in_mask = detection_mask[local_y, local_x] > 0
            if not np.any(in_mask):
                continue

            selected_points.append(coords_xyz[in_mask])
            selected_pixels.append(
                np.column_stack((u_int[in_mask], v_int[in_mask]))
            )
            selected_distances.append(distances[in_mask])
            used_pointcloud_files.add(pointcloud_file["path"])
            selected_block_count += 1

    if not selected_points:
        return {
            "points_xyz": np.empty((0, 3), dtype=np.float64),
            "pixels_xy": np.empty((0, 2), dtype=np.int32),
            "distances": np.empty((0,), dtype=np.float64),
            "used_pointcloud_files": set(),
            "selected_block_count": 0,
            "candidate_block_count": int(candidate_block_count),
        }
    return {
        "points_xyz": np.concatenate(selected_points, axis=0),
        "pixels_xy": np.concatenate(selected_pixels, axis=0),
        "distances": np.concatenate(selected_distances, axis=0),
        "used_pointcloud_files": used_pointcloud_files,
        "selected_block_count": int(selected_block_count),
        "candidate_block_count": int(candidate_block_count),
    }


def _pixels_inside_detection_mask(
    pixels_xy: np.ndarray,
    mask: np.ndarray,
    crop_bbox: tuple[int, int, int, int],
) -> np.ndarray:
    pixels = np.asarray(pixels_xy, dtype=np.int64).reshape(-1, 2)
    crop_x1, crop_y1, crop_x2, crop_y2 = crop_bbox
    inside_crop = (
        (pixels[:, 0] >= crop_x1)
        & (pixels[:, 0] < crop_x2)
        & (pixels[:, 1] >= crop_y1)
        & (pixels[:, 1] < crop_y2)
    )
    result = np.zeros(pixels.shape[0], dtype=bool)
    if not np.any(inside_crop):
        return result
    indices = np.flatnonzero(inside_crop)
    local_x = pixels[indices, 0] - crop_x1
    local_y = pixels[indices, 1] - crop_y1
    result[indices] = mask[local_y, local_x] > 0
    return result


def evaluate_point_range_fallback_quality(
    cluster_result: dict[str, Any],
    representative_pixel_xy: tuple[float, float] | None,
    rectified_view: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Require one compact cluster supported by the original, unpadded mask."""

    core_mask, core_bbox = build_detection_mask(
        rectified_view["rectified_polygon"],
        rectified_view["rectified_bbox"],
        0,
        rectified_view["view_width"],
        rectified_view["view_height"],
    )
    point_count = int(cluster_result.get("cluster_point_count", 0))
    raw_point_count = int(cluster_result.get("raw_point_count", 0))
    cluster_count = int(cluster_result.get("cluster_count", 0))
    cluster_fraction = float(point_count / max(1, raw_point_count))
    pixels_xy = np.asarray(
        cluster_result.get("pixels_xy", np.empty((0, 2))),
        dtype=np.int64,
    ).reshape(-1, 2)
    core_membership = _pixels_inside_detection_mask(
        pixels_xy,
        core_mask,
        core_bbox,
    )
    core_mask_fraction = float(np.mean(core_membership)) if pixels_xy.size else 0.0
    representative_inside_core_mask = False
    if representative_pixel_xy is not None:
        representative_pixel = np.rint(
            np.asarray(representative_pixel_xy, dtype=np.float64)
        ).astype(np.int64).reshape(1, 2)
        representative_inside_core_mask = bool(
            _pixels_inside_detection_mask(
                representative_pixel,
                core_mask,
                core_bbox,
            )[0]
        )
    distances = np.asarray(
        cluster_result.get("distances", np.empty((0,))),
        dtype=np.float64,
    )
    finite_distances = distances[np.isfinite(distances)]
    depth_span_m = (
        float(
            np.quantile(finite_distances, 0.95)
            - np.quantile(finite_distances, 0.05)
        )
        if finite_distances.size
        else None
    )

    minimum_points = int(runtime["point_range_fallback_min_point_count"])
    minimum_cluster_fraction = float(
        runtime["point_range_fallback_min_cluster_fraction"]
    )
    minimum_core_fraction = float(
        runtime["point_range_fallback_min_core_mask_fraction"]
    )
    maximum_depth_span_m = float(
        runtime["point_range_fallback_max_depth_span_m"]
    )
    failures: list[str] = []
    if point_count < minimum_points:
        failures.append(f"point_count_lt_{minimum_points}")
    if cluster_count != 1:
        failures.append("cluster_count_ne_1")
    if cluster_fraction < minimum_cluster_fraction:
        failures.append(
            f"cluster_fraction_lt_{minimum_cluster_fraction:.2f}"
        )
    if not representative_inside_core_mask:
        failures.append("representative_outside_core_mask")
    if core_mask_fraction < minimum_core_fraction:
        failures.append(
            f"core_mask_fraction_lt_{minimum_core_fraction:.2f}"
        )
    if depth_span_m is None or depth_span_m > maximum_depth_span_m:
        failures.append(f"depth_span_gt_{maximum_depth_span_m:.2f}m")
    return {
        "accepted": not failures,
        "reason": "accepted" if not failures else ";".join(failures),
        "minimum_point_count": minimum_points,
        "cluster_fraction": cluster_fraction,
        "core_mask_fraction": core_mask_fraction,
        "representative_inside_core_mask": representative_inside_core_mask,
        "depth_span_m": depth_span_m,
    }


def extract_points_for_detection(
    image_task: dict[str, Any],
    image_rgb: np.ndarray,
    detection_index: int,
    detection_payload: dict[str, Any],
    nearest_pointcloud_files: list[dict[str, Any]],
    runtime: dict[str, Any],
    pointcloud_cache: PointCloudReaderCache,
    logger,
    *,
    coordinator: MultiModelCoordinator | None = None,
) -> dict[str, Any]:
    origin_xyz = np.asarray(image_task["origin"], dtype=np.float64)
    rectified_view = build_rectified_detection_view(
        image_task=image_task,
        image_rgb=image_rgb,
        detection_payload=detection_payload,
        runtime=runtime,
    )

    class_tag = sanitize_name(f"{detection_payload['class_id']:03d}_{detection_payload['class_name']}")
    image_crop_dir = Path(runtime["image_crops_dir"]) / image_task["record_name"]
    image_crop_name = f"{image_task['image_stem']}__det{detection_index:04d}__{class_tag}.jpg"
    image_crop_path = image_crop_dir / image_crop_name
    point_preview_dir = Path(runtime["point_previews_dir"]) / image_task["record_name"]
    point_preview_name = f"{image_task['image_stem']}__det{detection_index:04d}__{class_tag}.png"
    point_preview_path = point_preview_dir / point_preview_name

    base_label = f"{detection_payload['class_name']} {detection_payload['confidence']:.3f}"
    base_info_lines = [
        f"ray={rectified_view['center_ray_angle_deg']:.1f}deg",
        f"fov={rectified_view['hfov_deg']:.1f}/{rectified_view['vfov_deg']:.1f}",
    ]
    strict_range_m = float(runtime["max_range_m"])
    range_fallback_enabled = bool(
        runtime.get("point_range_fallback_enabled", False)
    )
    fallback_range_m = float(
        runtime.get("point_range_fallback_max_range_m", strict_range_m)
    )

    if rectified_view["center_ray_angle_deg"] > runtime["max_center_ray_angle_deg"]:
        logger.info(
            "Excluded rear-facing detection for %s detection %d because center_ray_angle_deg=%.1f > %.1f",
            image_task["image_name"],
            detection_index,
            rectified_view["center_ray_angle_deg"],
            runtime["max_center_ray_angle_deg"],
        )
        return {
            "x": None,
            "y": None,
            "z": None,
            "candidate_x": None,
            "candidate_y": None,
            "candidate_z": None,
            "point_count": 0,
            "point_crop_point_count": 0,
            "point_crop_half_extent_m": float(runtime["las_crop_half_extent_m"]),
            "point_crop_fallback_used": False,
            "point_crop_path": None,
            "point_preview_path": None,
            # Rejected context detections remain in TXT/log metadata, but are
            # intentionally omitted from the accepted-candidate crop folder.
            # This prevents a side-facing context detection from looking like
            # a georeferenced result during visual review.
            "image_crop_path": None,
            "used_pointcloud_files": [],
            "used_pcdb_files": [],
            "used_block_count": 0,
            "min_distance_m": None,
            "median_distance_m": None,
            "representative_point_mode": "nearest_cluster_median",
            "accepted_for_shp": False,
            "exclude_reason": f"center_ray_angle_gt_{runtime['max_center_ray_angle_deg']:.1f}",
            "rectified_hfov_deg": float(rectified_view["hfov_deg"]),
            "rectified_vfov_deg": float(rectified_view["vfov_deg"]),
            "center_ray_angle_deg": float(rectified_view["center_ray_angle_deg"]),
            "representative_pixel_x": None,
            "representative_pixel_y": None,
            "raw_point_count": 0,
            "cluster_point_count": 0,
            "cluster_count": 0,
            "point_match_mode": "angle_rejected",
            "point_match_max_range_m": strict_range_m,
            "point_match_min_point_count": int(runtime["min_point_count"]),
            "point_range_fallback_attempted": False,
            "point_range_fallback_used": False,
            "point_range_fallback_quality_reason": "not_attempted",
            "point_range_fallback_cluster_fraction": None,
            "point_range_fallback_core_mask_fraction": None,
            "point_range_fallback_representative_inside_core_mask": None,
            "point_range_fallback_depth_span_m": None,
        }

    mask, crop_bbox = build_detection_mask(
        rectified_view["rectified_polygon"],
        rectified_view["rectified_bbox"],
        runtime["point_padding_px"],
        rectified_view["view_width"],
        rectified_view["view_height"],
    )

    angle_margin_rad = math.radians(runtime["block_angle_margin_deg"])
    collection = collect_detection_points_at_range(
        nearest_pointcloud_files,
        pointcloud_cache,
        origin_xyz,
        rectified_view,
        mask,
        crop_bbox,
        maximum_range_m=strict_range_m,
        angle_margin_rad=angle_margin_rad,
    )
    range_fallback_attempted = False
    range_fallback_used = False
    point_match_max_range_m = strict_range_m
    if (
        collection["points_xyz"].shape[0] == 0
        and range_fallback_enabled
        and fallback_range_m > strict_range_m
    ):
        range_fallback_attempted = True
        point_match_max_range_m = fallback_range_m
        logger.info(
            "Retrying empty point match for %s detection %d at %.1f m (strict %.1f m).",
            image_task["image_name"],
            detection_index,
            fallback_range_m,
            strict_range_m,
        )
        fallback_collection = collect_detection_points_at_range(
            nearest_pointcloud_files,
            pointcloud_cache,
            origin_xyz,
            rectified_view,
            mask,
            crop_bbox,
            maximum_range_m=fallback_range_m,
            angle_margin_rad=angle_margin_rad,
        )
        if fallback_collection["points_xyz"].shape[0] > 0:
            collection = fallback_collection
            range_fallback_used = True

    used_pcdb_files = set(collection["used_pointcloud_files"])
    used_block_count = int(collection["selected_block_count"])
    point_match_mode = "range_fallback" if range_fallback_used else "strict"
    point_match_min_point_count = int(
        runtime["point_range_fallback_min_point_count"]
        if range_fallback_used
        else runtime["min_point_count"]
    )
    range_info_lines = (
        [f"range=fallback {strict_range_m:.1f}->{fallback_range_m:.1f}m"]
        if range_fallback_used
        else []
    )
    if collection["points_xyz"].shape[0] == 0:
        empty_reason = (
            "no matched points after range fallback"
            if range_fallback_attempted
            else "no matched points"
        )
        save_debug_crop(
            image_rgb=rectified_view["rectified_rgb"],
            polygon_xy=rectified_view["rectified_polygon"],
            bbox_xyxy=rectified_view["rectified_bbox"],
            crop_path=image_crop_path,
            padding_px=runtime["debug_crop_padding_px"],
            mask_alpha=runtime["debug_mask_alpha"],
            label=base_label,
            info_lines=base_info_lines + [empty_reason],
        )
        logger.warning(
            "No point-cloud points were selected for %s detection %d (%s); "
            "fallback_attempted=%s max_range=%.1f m.",
            image_task["image_name"],
            detection_index,
            detection_payload["class_name"],
            range_fallback_attempted,
            point_match_max_range_m,
        )
        return {
            "x": None,
            "y": None,
            "z": None,
            "candidate_x": None,
            "candidate_y": None,
            "candidate_z": None,
            "point_count": 0,
            "point_crop_point_count": 0,
            "point_crop_half_extent_m": float(runtime["las_crop_half_extent_m"]),
            "point_crop_fallback_used": False,
            "point_crop_path": None,
            "point_preview_path": None,
            "image_crop_path": str(image_crop_path.resolve()),
            "used_pointcloud_files": [],
            "used_pcdb_files": [],
            "used_block_count": used_block_count,
            "min_distance_m": None,
            "median_distance_m": None,
            "representative_point_mode": "nearest_cluster_median",
            "accepted_for_shp": False,
            "exclude_reason": "no_points",
            "rectified_hfov_deg": float(rectified_view["hfov_deg"]),
            "rectified_vfov_deg": float(rectified_view["vfov_deg"]),
            "center_ray_angle_deg": float(rectified_view["center_ray_angle_deg"]),
            "representative_pixel_x": None,
            "representative_pixel_y": None,
            "raw_point_count": 0,
            "cluster_point_count": 0,
            "cluster_count": 0,
            "point_match_mode": (
                "range_fallback_empty"
                if range_fallback_attempted
                else "strict_empty"
            ),
            "point_match_max_range_m": point_match_max_range_m,
            "point_match_min_point_count": int(runtime["min_point_count"]),
            "point_range_fallback_attempted": range_fallback_attempted,
            "point_range_fallback_used": False,
            "point_range_fallback_quality_reason": (
                "no_points" if range_fallback_attempted else "not_attempted"
            ),
            "point_range_fallback_cluster_fraction": None,
            "point_range_fallback_core_mask_fraction": None,
            "point_range_fallback_representative_inside_core_mask": None,
            "point_range_fallback_depth_span_m": None,
        }

    points_xyz = np.asarray(collection["points_xyz"])
    pixels_xy = np.asarray(collection["pixels_xy"])
    distances = np.asarray(collection["distances"])

    front_surface_anchor_m = robust_front_surface_distance(
        distances,
        quantile=runtime["front_surface_quantile"],
        min_support=runtime["front_surface_min_support"],
    )
    front_surface_mask = distances <= (front_surface_anchor_m + runtime["depth_window_m"])
    if np.any(front_surface_mask):
        points_xyz = points_xyz[front_surface_mask]
        pixels_xy = pixels_xy[front_surface_mask]
        distances = distances[front_surface_mask]

    cluster_result = cluster_extracted_points(
        points_xyz=points_xyz,
        pixels_xy=pixels_xy,
        distances=distances,
        runtime=runtime,
    )
    if cluster_result is None or cluster_result["cluster_point_count"] <= 0:
        save_debug_crop(
            image_rgb=rectified_view["rectified_rgb"],
            polygon_xy=rectified_view["rectified_polygon"],
            bbox_xyxy=rectified_view["rectified_bbox"],
            crop_path=image_crop_path,
            padding_px=runtime["debug_crop_padding_px"],
            mask_alpha=runtime["debug_mask_alpha"],
            label=base_label,
            point_pixels_xy=pixels_xy,
            info_lines=base_info_lines + range_info_lines + ["no dense cluster"],
        )
        return {
            "x": None,
            "y": None,
            "z": None,
            "candidate_x": None,
            "candidate_y": None,
            "candidate_z": None,
            "point_count": 0,
            "point_crop_point_count": 0,
            "point_crop_half_extent_m": float(runtime["las_crop_half_extent_m"]),
            "point_crop_fallback_used": False,
            "point_crop_path": None,
            "point_preview_path": None,
            "image_crop_path": str(image_crop_path.resolve()),
            "used_pointcloud_files": sorted(used_pcdb_files),
            "used_pcdb_files": sorted(used_pcdb_files),
            "used_block_count": used_block_count,
            "min_distance_m": None,
            "median_distance_m": None,
            "representative_point_mode": "nearest_cluster_median",
            "accepted_for_shp": False,
            "exclude_reason": cluster_result["reason"] if cluster_result is not None else "no_dense_cluster",
            "rectified_hfov_deg": float(rectified_view["hfov_deg"]),
            "rectified_vfov_deg": float(rectified_view["vfov_deg"]),
            "center_ray_angle_deg": float(rectified_view["center_ray_angle_deg"]),
            "representative_pixel_x": None,
            "representative_pixel_y": None,
            "raw_point_count": int(points_xyz.shape[0]),
            "cluster_point_count": 0,
            "cluster_count": 0 if cluster_result is None else int(cluster_result["cluster_count"]),
            "front_surface_anchor_m": front_surface_anchor_m,
            "point_match_mode": point_match_mode,
            "point_match_max_range_m": point_match_max_range_m,
            "point_match_min_point_count": point_match_min_point_count,
            "point_range_fallback_attempted": range_fallback_attempted,
            "point_range_fallback_used": range_fallback_used,
            "point_range_fallback_quality_reason": (
                (
                    str(cluster_result.get("reason") or "no_dense_cluster")
                    if cluster_result is not None
                    else "no_dense_cluster"
                )
                if range_fallback_used
                else "not_attempted"
            ),
            "point_range_fallback_cluster_fraction": None,
            "point_range_fallback_core_mask_fraction": None,
            "point_range_fallback_representative_inside_core_mask": None,
            "point_range_fallback_depth_span_m": None,
        }

    points_xyz = cluster_result["points_xyz"]
    pixels_xy = cluster_result["pixels_xy"]
    distances = cluster_result["distances"]
    representative_xyz = np.asarray(cluster_result["representative_xyz"], dtype=np.float64)
    pixel_colors = rectified_view["rectified_rgb"][pixels_xy[:, 1], pixels_xy[:, 0]]
    las_points_xyz, las_pixel_colors, las_crop_fallback_used = crop_points_for_las_export(
        points_xyz,
        pixel_colors,
        representative_xyz,
        float(runtime["las_crop_half_extent_m"]),
    )
    point_crop_dir = Path(runtime["point_crops_dir"]) / image_task["record_name"]
    point_crop_name = f"{image_task['image_stem']}__det{detection_index:04d}__{class_tag}.las"
    point_crop_path = point_crop_dir / point_crop_name
    write_las(
        las_points_xyz,
        las_pixel_colors,
        point_crop_path,
        crs_wkt=runtime.get("crs_wkt"),
    )

    representative_pixel_xy = project_representative_point_pixel(
        representative_xyz,
        origin_xyz,
        rectified_view,
    )
    save_point_cloud_preview(
        points_xyz,
        pixel_colors,
        representative_xyz,
        rectified_view,
        point_preview_path,
        panel_size=int(runtime["point_preview_size"]),
        point_count=int(points_xyz.shape[0]),
        raw_point_count=int(cluster_result["raw_point_count"]),
        cluster_count=int(cluster_result["cluster_count"]),
        center_ray_angle_deg=float(rectified_view["center_ray_angle_deg"]),
        representative_mode="nearest_cluster_median",
    )
    point_count = int(points_xyz.shape[0])
    fallback_quality: dict[str, Any] | None = None
    if range_fallback_used:
        fallback_quality = evaluate_point_range_fallback_quality(
            cluster_result,
            representative_pixel_xy,
            rectified_view,
            runtime,
        )
        accepted_for_shp = bool(fallback_quality["accepted"])
        point_match_min_point_count = int(
            fallback_quality["minimum_point_count"]
        )
    else:
        accepted_for_shp = point_count >= runtime["min_point_count"]
    exclude_reason = None
    if not accepted_for_shp:
        exclude_reason = (
            f"point_range_fallback_rejected:{fallback_quality['reason']}"
            if fallback_quality is not None
            else f"point_count_lt_{runtime['min_point_count']}"
        )
        logger.info(
            "Excluded from SHP for %s detection %d: %s (point_count=%d).",
            image_task["image_name"],
            detection_index,
            exclude_reason,
            point_count,
        )
    elif fallback_quality is not None:
        logger.info(
            "Accepted range fallback for %s detection %d: points=%d, "
            "cluster_fraction=%.3f, core_mask_fraction=%.3f, depth_span=%.3f m.",
            image_task["image_name"],
            detection_index,
            point_count,
            float(fallback_quality["cluster_fraction"]),
            float(fallback_quality["core_mask_fraction"]),
            float(fallback_quality["depth_span_m"]),
        )

    policy = runtime.get("pole_classification_policy") or {}
    pole_classification_defaults = {
        "classification_mode_requested": str(
            policy.get("requested_mode") or runtime.get("pole_classification_mode", "auto")
        ),
        "classification_mode": str(policy.get("effective_mode") or "GEOMETRY"),
        "classification_reason": str(policy.get("reason") or "unknown"),
    }
    pole_payload: dict[str, Any]
    if runtime["pole_detection"] and accepted_for_shp:
        try:
            pole_slot = (
                coordinator.pole_gate.slot()
                if coordinator is not None
                else nullcontext()
            )
            pole_timing = (
                coordinator.timed(
                    f"pole/{runtime.get('model_key') or 'model'}"
                )
                if coordinator is not None
                else nullcontext()
            )
            with pole_slot, pole_timing:
                pole_payload = extract_pole_for_detection(
                    image_task=image_task,
                    image_rgb=image_rgb,
                    detection_index=detection_index,
                    detection_payload=detection_payload,
                    sign_points_xyz=points_xyz,
                    sign_xyz=representative_xyz,
                    nearest_pointcloud_files=nearest_pointcloud_files,
                    runtime=runtime,
                    pointcloud_cache=pointcloud_cache,
                    logger=logger,
                )
        except Exception as exc:
            logger.exception(
                "Pole extraction failed for %s detection %d.",
                image_task["image_name"],
                detection_index,
            )
            pole_payload = {
                **pole_classification_defaults,
                "enabled": True,
                "found": False,
                "reason": "processing_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    elif runtime["pole_detection"]:
        pole_payload = {
            **pole_classification_defaults,
            "enabled": True,
            "found": False,
            "reason": "sign_not_accepted",
        }
    else:
        pole_payload = {
            **pole_classification_defaults,
            "enabled": False,
            "found": False,
            "reason": "disabled",
        }

    info_lines = base_info_lines + range_info_lines + [
        f"raw={int(cluster_result['raw_point_count'])}",
        f"cluster={point_count}",
        f"clusters={int(cluster_result['cluster_count'])}",
    ]
    if fallback_quality is not None:
        info_lines.append(
            "fallback="
            + ("accepted" if fallback_quality["accepted"] else "rejected")
            + f" core={float(fallback_quality['core_mask_fraction']):.2f}"
            + f" depth={float(fallback_quality['depth_span_m']):.2f}m"
        )
    if not accepted_for_shp:
        info_lines.append(f"excluded={exclude_reason}")
    if pole_payload.get("enabled"):
        info_lines.append(
            "pole="
            + (
                f"{pole_payload.get('type')} / {pole_payload.get('status')}"
                if pole_payload.get("found")
                else str(pole_payload.get("reason") or "not found")
            )
        )
    save_debug_crop(
        image_rgb=rectified_view["rectified_rgb"],
        polygon_xy=rectified_view["rectified_polygon"],
        bbox_xyxy=rectified_view["rectified_bbox"],
        crop_path=image_crop_path,
        padding_px=runtime["debug_crop_padding_px"],
        mask_alpha=runtime["debug_mask_alpha"],
        label=base_label,
        marker_xy=representative_pixel_xy,
        point_pixels_xy=pixels_xy,
        info_lines=info_lines,
    )

    return {
        "x": float(representative_xyz[0]) if accepted_for_shp else None,
        "y": float(representative_xyz[1]) if accepted_for_shp else None,
        "z": float(representative_xyz[2]) if accepted_for_shp else None,
        "candidate_x": float(representative_xyz[0]),
        "candidate_y": float(representative_xyz[1]),
        "candidate_z": float(representative_xyz[2]),
        "point_count": point_count,
        "point_crop_point_count": int(las_points_xyz.shape[0]),
        "point_crop_half_extent_m": float(runtime["las_crop_half_extent_m"]),
        "point_crop_fallback_used": bool(las_crop_fallback_used),
        "point_crop_path": str(point_crop_path.resolve()),
        "point_preview_path": str(point_preview_path.resolve()),
        "image_crop_path": str(image_crop_path.resolve()),
        "used_pointcloud_files": sorted(used_pcdb_files),
        "used_pcdb_files": sorted(used_pcdb_files),
        "used_block_count": used_block_count,
        "min_distance_m": float(distances.min()),
        "median_distance_m": float(np.median(distances)),
        "representative_point_mode": "nearest_cluster_median",
        "accepted_for_shp": accepted_for_shp,
        "exclude_reason": exclude_reason,
        "rectified_hfov_deg": float(rectified_view["hfov_deg"]),
        "rectified_vfov_deg": float(rectified_view["vfov_deg"]),
        "center_ray_angle_deg": float(rectified_view["center_ray_angle_deg"]),
        "representative_pixel_x": None if representative_pixel_xy is None else float(representative_pixel_xy[0]),
        "representative_pixel_y": None if representative_pixel_xy is None else float(representative_pixel_xy[1]),
        "raw_point_count": int(cluster_result["raw_point_count"]),
        "cluster_point_count": int(cluster_result["cluster_point_count"]),
        "cluster_count": int(cluster_result["cluster_count"]),
        "front_surface_anchor_m": front_surface_anchor_m,
        "point_match_mode": point_match_mode,
        "point_match_max_range_m": point_match_max_range_m,
        "point_match_min_point_count": point_match_min_point_count,
        "point_range_fallback_attempted": range_fallback_attempted,
        "point_range_fallback_used": range_fallback_used,
        "point_range_fallback_quality_reason": (
            str(fallback_quality["reason"])
            if fallback_quality is not None
            else "not_attempted"
        ),
        "point_range_fallback_cluster_fraction": (
            None
            if fallback_quality is None
            else float(fallback_quality["cluster_fraction"])
        ),
        "point_range_fallback_core_mask_fraction": (
            None
            if fallback_quality is None
            else float(fallback_quality["core_mask_fraction"])
        ),
        "point_range_fallback_representative_inside_core_mask": (
            None
            if fallback_quality is None
            else bool(fallback_quality["representative_inside_core_mask"])
        ),
        "point_range_fallback_depth_span_m": (
            None
            if fallback_quality is None
            else fallback_quality["depth_span_m"]
        ),
        "pole": pole_payload,
    }


def compatible_existing_result_summary(
    image_task: dict[str, Any],
    runtime: dict[str, Any],
    logger,
) -> dict[str, int] | None:
    """Return a completed-image summary when skip-existing is safe."""

    if not runtime["skip_existing"]:
        return None
    txt_path = (
        Path(runtime["txt_dir"])
        / image_task["record_name"]
        / f"{image_task['image_stem']}.txt"
    )
    if not txt_path.exists():
        return None
    try:
        existing_payload = json.loads(txt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing_payload = {}
    missing_artifacts = missing_result_artifacts(existing_payload)
    if (
        existing_payload.get("schema_version") == RESULT_SCHEMA_VERSION
        and existing_payload.get("run_fingerprint") == runtime["run_fingerprint"]
        and not missing_artifacts
    ):
        logger.info("Skipping compatible existing result: %s", txt_path)
        existing_detections = existing_payload["detections"]
        return {
            "images": 1,
            "detections": len(existing_detections),
            "points": sum(
                int(item.get("point_count") or 0)
                for item in existing_detections
                if isinstance(item, dict)
            ),
            "failures": 0,
        }
    if missing_artifacts:
        logger.info(
            "Reprocessing result with %d missing referenced artifact(s): %s",
            len(missing_artifacts),
            txt_path,
        )
    else:
        logger.info("Reprocessing stale result with a different input/config: %s", txt_path)
    return None


def process_image_task(
    image_task: dict[str, Any],
    runtime: dict[str, Any],
    model: YOLO | None,
    pointcloud_catalog: dict[str, Any],
    pointcloud_cache: PointCloudReaderCache,
    logger,
    *,
    image_rgb_override: np.ndarray | None = None,
    detection_candidates_override: list[dict[str, Any]] | None = None,
    forward_view_output_path_override: Path | None = None,
    skip_existing_checked: bool = False,
    coordinator: MultiModelCoordinator | None = None,
) -> dict[str, int]:
    txt_dir = Path(runtime["txt_dir"]) / image_task["record_name"]
    txt_dir.mkdir(parents=True, exist_ok=True)
    txt_path = txt_dir / f"{image_task['image_stem']}.txt"

    if not skip_existing_checked:
        existing_summary = compatible_existing_result_summary(
            image_task,
            runtime,
            logger,
        )
        if existing_summary is not None:
            return existing_summary

    image_path = Path(image_task["image_path"])
    logger.info("Running detection for %s", image_path.name)

    image_rgb = (
        image_rgb_override
        if image_rgb_override is not None
        else load_panorama_rgb(image_path, logger)
    )
    validate_panorama_image(image_task, image_rgb)
    forward_view_output_path = forward_view_output_path_override
    if (
        forward_view_output_path is None
        and runtime.get("detection_view_mode") == "forward"
    ):
        forward_view_output_path = (
            Path(runtime["forward_views_dir"])
            / image_task["record_name"]
            / f"{image_task['image_stem']}__forward.jpg"
        )
    if detection_candidates_override is None:
        if model is None:
            raise ValueError("A YOLO model is required when detections are not precomputed")
        detection_candidates = run_yolo_detection_on_panorama(
            image_rgb=image_rgb,
            runtime=runtime,
            model=model,
            logger=logger,
            forward_view_output_path=forward_view_output_path,
            coordinator=coordinator,
        )
    else:
        detection_candidates = detection_candidates_override
    nearest_pointcloud_files = match_nearest_pointcloud_files(
        image_task,
        pointcloud_catalog,
        runtime["pointcloud_neighbor_count"],
    )

    detections: list[dict[str, Any]] = []
    if detection_candidates:
        for index, candidate in enumerate(detection_candidates, start=1):
            detection_payload = {
                "detection_index": index,
                "class_id": int(candidate["class_id"]),
                "class_name": str(candidate["class_name"]),
                "confidence": float(candidate["confidence"]),
                "bbox_xyxy": [float(value) for value in candidate["bbox_xyxy"]],
                "mask_polygon": candidate["mask_polygon"],
                "detection_sources": list(candidate.get("detection_sources", [])),
                "image_crop_path": None,
                "image_name": image_task["image_name"],
                "timestamp_iso": image_task["timestamp_iso"],
            }

            point_payload = extract_points_for_detection(
                image_task=image_task,
                image_rgb=image_rgb,
                detection_index=index,
                detection_payload=detection_payload,
                nearest_pointcloud_files=nearest_pointcloud_files,
                runtime=runtime,
                pointcloud_cache=pointcloud_cache,
                logger=logger,
                coordinator=coordinator,
            )
            point_payload.setdefault(
                "pole",
                {
                    "classification_mode_requested": str(
                        runtime["pole_classification_policy"].get("requested_mode")
                        or runtime.get("pole_classification_mode", "auto")
                    ),
                    "classification_mode": str(
                        runtime["pole_classification_policy"].get("effective_mode")
                        or "GEOMETRY"
                    ),
                    "classification_reason": str(
                        runtime["pole_classification_policy"].get("reason") or "unknown"
                    ),
                    "enabled": bool(runtime["pole_detection"]),
                    "found": False,
                    "reason": "sign_not_georeferenced"
                    if runtime["pole_detection"]
                    else "disabled",
                },
            )
            detection_payload.update(point_payload)
            detections.append(detection_payload)

    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_fingerprint": runtime["run_fingerprint"],
        "model_name": runtime.get("model_name"),
        "model_key": runtime.get("model_key"),
        "model_profile": runtime.get("model_profile"),
        "model_object_type": runtime.get("model_object_type"),
        "model_sha256": runtime.get("model_sha256"),
        "dataset_signature": runtime["dataset_signature"],
        "image_path": str(image_path.resolve()),
        "image_name": image_task["image_name"],
        "timestamp_iso": image_task["timestamp_iso"],
        "record_name": image_task["record_name"],
        "route_id": image_task["route_id"],
        "pose_csv_path": image_task["pose_csv_path"],
        "origin": image_task["origin"],
        "direction": image_task["direction"],
        "up": image_task["up"],
        "right": image_task.get("right"),
        "rotation_local_to_world": image_task.get("rotation_local_to_world"),
        "pose_format": image_task.get("pose_format"),
        "pose_row_number": image_task.get("pose_row_number"),
        "gps_sow_seconds": image_task.get("gps_sow_seconds"),
        "gps_week": image_task.get("gps_week"),
        "gps_week_source": image_task.get("gps_week_source"),
        "panorama": image_task.get("panorama"),
        "panorama_detection": {
            "mode": runtime.get("detection_view_mode", "panorama"),
            "forward_view_path": (
                str(forward_view_output_path.resolve())
                if forward_view_output_path is not None
                else None
            ),
            "forward_view_width_px": (
                int(runtime["forward_view_size"])
                if forward_view_output_path is not None
                else None
            ),
            "forward_view_height_px": (
                int(runtime["forward_view_size"])
                if forward_view_output_path is not None
                else None
            ),
            "forward_view_hfov_deg": (
                float(runtime["forward_view_hfov_deg"])
                if forward_view_output_path is not None
                else None
            ),
            "forward_view_vfov_deg": (
                float(runtime["forward_view_vfov_deg"])
                if forward_view_output_path is not None
                else None
            ),
            "max_center_ray_angle_deg": float(runtime["max_center_ray_angle_deg"]),
            "inference_pixels_annotated": False,
        },
        "panorama_alignment": {
            "model": "pose_local_yaw_then_pitch_v1",
            "source": "manual_yaml",
            "yaw_offset_deg": float(runtime.get("panorama_yaw_offset_deg", 0.0)),
            "pitch_offset_deg": float(runtime.get("panorama_pitch_offset_deg", 0.0)),
            "sign_convention": {
                "positive_yaw": "projected points move right",
                "positive_pitch": "projected points move down",
            },
            "qa_report_path": (runtime.get("alignment_qa") or {}).get("report_path"),
            "qa_status": (runtime.get("alignment_qa") or {}).get("status"),
            "recommended_total_yaw_offset_deg": (
                runtime.get("alignment_qa") or {}
            ).get("recommended_total_yaw_offset_deg"),
            "recommended_total_pitch_offset_deg": (
                runtime.get("alignment_qa") or {}
            ).get("recommended_total_pitch_offset_deg"),
        },
        "calibration": image_task.get("calibration"),
        "pointcloud_source": runtime["pointcloud_source"],
        "pole_classification": public_pole_classification_policy(
            runtime["pole_classification_policy"]
        ),
        "crs_wkt": runtime.get("crs_wkt"),
        "point_crop_semantics": POINT_CROP_SEMANTICS,
        "pole_crop_semantics": POLE_CROP_SEMANTICS,
        "matched_pointcloud_files": [item["path"] for item in nearest_pointcloud_files],
        "matched_pcdb_files": [item["path"] for item in nearest_pointcloud_files],
        "detections": detections,
    }
    atomic_write_text(txt_path, json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info(
        "Saved %d detections for %s to %s",
        len(detections),
        image_task["image_name"],
        txt_path,
    )
    return {
        "images": 1,
        "detections": len(detections),
        "points": sum(item["point_count"] for item in detections if item["point_count"]),
        "failures": sum(
            1
            for item in detections
            if (item.get("pole") or {}).get("reason") == "processing_error"
        ),
    }


def _update_progress_bar(
    progress_bar,
    totals: dict[str, int],
    event: dict[str, int],
) -> None:
    completed = max(0, int(event.get("images", 0)))
    for key in ("images", "detections", "points", "failures"):
        totals[key] += max(0, int(event.get(key, 0)))
    if completed:
        progress_bar.update(completed)
    progress_bar.set_postfix(
        signs=totals["detections"],
        points=totals["points"],
        errors=totals["failures"],
        refresh=False,
    )


def _drain_progress_queue(
    progress_queue,
    progress_bar,
    totals: dict[str, int],
) -> int:
    drained = 0
    while True:
        try:
            event = progress_queue.get_nowait()
        except queue.Empty:
            break
        _update_progress_bar(progress_bar, totals, event)
        drained += 1
    return drained


def worker_process(
    chunk: list[dict[str, Any]],
    runtime: dict[str, Any],
    progress_queue=None,
    progress_callback=None,
) -> dict[str, int]:
    worker_log_path = (
        Path(runtime["workers_log_dir"])
        / f"{mp.current_process().name}_{os.getpid()}.log"
    )
    logger = setup_logging(
        worker_log_path,
        file_mode="w",
        logger_name=f"mms_shp_detection_worker_{os.getpid()}",
        level=runtime.get("log_level", "INFO"),
    )
    logger.info("Worker starting with %d images.", len(chunk))
    model = YOLO(runtime["model_path"])
    pointcloud_catalog = json.loads(
        Path(runtime["pointcloud_catalog_path"]).read_text(encoding="utf-8")
    )
    pointcloud_cache = PointCloudReaderCache()

    summary = {"images": 0, "detections": 0, "points": 0, "failures": 0}
    try:
        total_images = len(chunk)
        for index, image_task in enumerate(chunk, start=1):
            try:
                result = process_image_task(
                    image_task=image_task,
                    runtime=runtime,
                    model=model,
                    pointcloud_catalog=pointcloud_catalog,
                    pointcloud_cache=pointcloud_cache,
                    logger=logger,
                )
                for key in summary:
                    summary[key] += result[key]
            except Exception:
                logger.exception("Worker failed on image %s", image_task["image_path"])
                result = {"images": 1, "detections": 0, "points": 0, "failures": 1}
                summary["images"] += result["images"]
                summary["failures"] += result["failures"]

            if progress_callback is not None:
                progress_callback(result)
            elif progress_queue is not None:
                progress_queue.put(result)

            if index % runtime["worker_progress_every"] == 0 or index == total_images:
                logger.info(
                    "Worker progress: %d/%d images processed, detections=%d, points=%d",
                    index,
                    total_images,
                    summary["detections"],
                    summary["points"],
                )
    finally:
        pointcloud_cache.close()

    logger.info(
        "Worker finished: images=%d detections=%d points=%d",
        summary["images"],
        summary["detections"],
        summary["points"],
    )
    return summary


def run_panorama_alignment_qa(
    image_tasks: list[dict[str, Any]],
    pointcloud_catalog: dict[str, Any],
    args: argparse.Namespace,
    report_path: Path,
    logger: Any,
    *,
    dataset_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a report-only RGB reprojection recommendation.

    Automatic application is intentionally avoided: a constant colour shift can
    also be caused by lever-arm, exposure-time, or panorama stitching parallax.
    The recommended total is written for review and can then be copied into the
    two explicit YAML offset fields, which are part of the processing fingerprint.
    """

    enabled = bool(getattr(args, "alignment_qa_enabled", False))
    cache_fingerprint = (
        build_panorama_alignment_qa_fingerprint(
            image_tasks,
            pointcloud_catalog,
            args,
            dataset_signature=dataset_signature,
        )
        if enabled
        else None
    )
    if enabled and report_path.is_file():
        try:
            cached = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Ignoring unreadable panorama alignment QA cache %s: %s",
                report_path,
                exc,
            )
        else:
            cache_valid = (
                isinstance(cached, dict)
                and cached.get("cache_version")
                == PANORAMA_ALIGNMENT_QA_CACHE_VERSION
                and cached.get("estimator_version")
                == PANORAMA_ALIGNMENT_QA_ESTIMATOR_VERSION
                and cached.get("cache_fingerprint") == cache_fingerprint
                and cached.get("status") in PANORAMA_ALIGNMENT_QA_FINAL_STATUSES
                and cached.get("report_only") is True
            )
            if cache_valid:
                report = dict(cached)
                report["cache_hit"] = True
                report["report_path"] = str(report_path.resolve())
                atomic_write_text(
                    report_path,
                    json.dumps(report, ensure_ascii=False, indent=2),
                )
                logger.info(
                    "Panorama alignment QA cache hit: status=%s fingerprint=%s",
                    report["status"],
                    cache_fingerprint[:12],
                )
                return report
            logger.info(
                "Panorama alignment QA cache miss: input fingerprint changed or "
                "the previous report is incomplete."
            )

    report: dict[str, Any] = {
        "estimator_version": PANORAMA_ALIGNMENT_QA_ESTIMATOR_VERSION,
        "cache_version": PANORAMA_ALIGNMENT_QA_CACHE_VERSION,
        "cache_fingerprint": cache_fingerprint,
        "cache_hit": False,
        "status": "disabled" if not enabled else "running",
        "report_only": True,
        "applied_yaw_offset_deg": float(getattr(args, "panorama_yaw_offset_deg", 0.0)),
        "applied_pitch_offset_deg": float(getattr(args, "panorama_pitch_offset_deg", 0.0)),
        "report_path": str(report_path.resolve()),
    }
    if enabled:
        estimated = estimate_panorama_alignment(
            image_tasks,
            pointcloud_catalog,
            logger,
            neighbor_count=max(1, int(args.pointcloud_neighbor_count)),
            sample_images=int(args.alignment_qa_sample_images),
            max_points_per_image=int(args.alignment_qa_max_points_per_image),
            search_radius_px=int(args.alignment_qa_search_radius_px),
            trim_fraction=float(args.alignment_qa_trim_fraction),
            minimum_range_m=float(args.alignment_qa_min_range_m),
            maximum_range_m=float(args.alignment_qa_max_range_m),
            base_yaw_offset_deg=float(args.panorama_yaw_offset_deg),
            base_pitch_offset_deg=float(args.panorama_pitch_offset_deg),
            show_progress=not bool(getattr(args, "disable_console_progress", False)),
        )
        report.update(estimated)
        valid_count = int(report.get("valid_sample_count") or 0)
        dx_mad_value = report.get("dx_mad_px")
        dy_mad_value = report.get("dy_mad_px")
        dx_mad = float(dx_mad_value) if dx_mad_value is not None else math.inf
        dy_mad = float(dy_mad_value) if dy_mad_value is not None else math.inf
        search_radius = int(args.alignment_qa_search_radius_px)
        boundary_hit = any(
            abs(int(item.get("dx_px") or 0)) >= search_radius
            or abs(int(item.get("dy_px") or 0)) >= search_radius
            for item in report.get("samples", [])
        )
        stable = (
            report.get("status") == "ok"
            and valid_count >= int(args.alignment_qa_min_valid_samples)
            and dx_mad <= float(args.alignment_qa_max_mad_px)
            and dy_mad <= float(args.alignment_qa_max_mad_px)
            and not boundary_hit
        )
        report["boundary_hit"] = boundary_hit
        report["stable_recommendation"] = stable
        if stable:
            report["status"] = "recommendation"
            report["recommended_additional_yaw_offset_deg"] = float(
                report["estimated_yaw_residual_deg"]
            )
            report["recommended_additional_pitch_offset_deg"] = float(
                report["estimated_pitch_residual_deg"]
            )
            report["recommended_total_yaw_offset_deg"] = float(
                args.panorama_yaw_offset_deg
            ) + float(report["estimated_yaw_residual_deg"])
            report["recommended_total_pitch_offset_deg"] = float(
                args.panorama_pitch_offset_deg
            ) + float(report["estimated_pitch_residual_deg"])
        elif valid_count < int(args.alignment_qa_min_valid_samples):
            report["status"] = "insufficient_data"
        else:
            report["status"] = "ambiguous"
        logger.info(
            "Panorama alignment QA: status=%s valid=%d residual=(%+.4f, %+.4f) deg",
            report["status"],
            valid_count,
            float(report.get("estimated_yaw_residual_deg") or 0.0),
            float(report.get("estimated_pitch_residual_deg") or 0.0),
        )

    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _natural_frame_id_key(value: str) -> tuple[tuple[int, Any], ...]:
    """Return a deterministic, case-insensitive natural-order image-stem key."""

    return tuple(
        (1, int(part)) if part.isdecimal() else (0, part.casefold())
        for part in re.split(r"([0-9]+)", value)
        if part
    )


def select_image_tasks_for_scope(
    image_tasks: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    logger=None,
) -> list[dict[str, Any]]:
    """Apply stable record/job/track and inclusive image-stem work bounds."""

    name_filters: dict[str, tuple[str, ...] | None] = {}
    for option_name in (
        "include_record_names",
        "include_job_names",
        "include_track_names",
    ):
        raw_value = getattr(args, option_name, None)
        if raw_value is None:
            name_filters[option_name] = None
            continue
        try:
            name_filters[option_name] = parse_name_list(raw_value)
        except argparse.ArgumentTypeError as exc:
            raise ValueError(f"Invalid {option_name}: {exc}") from exc

    frame_bounds: dict[str, str | None] = {}
    for option_name in ("frame_id_from", "frame_id_to"):
        raw_value = getattr(args, option_name, None)
        if raw_value is None:
            frame_bounds[option_name] = None
            continue
        value = str(raw_value).strip()
        if not value:
            raise ValueError(f"{option_name} must be a non-empty image stem")
        frame_bounds[option_name] = value

    frame_from = frame_bounds["frame_id_from"]
    frame_to = frame_bounds["frame_id_to"]
    frame_from_key = _natural_frame_id_key(frame_from) if frame_from is not None else None
    frame_to_key = _natural_frame_id_key(frame_to) if frame_to is not None else None
    if (
        frame_from_key is not None
        and frame_to_key is not None
        and frame_from_key > frame_to_key
    ):
        raise ValueError(
            "frame_id_from must not sort after frame_id_to in natural image-stem order "
            f"({frame_from!r} > {frame_to!r})"
        )

    metadata_fields = {
        "include_record_names": "record_name",
        "include_job_names": "job_name",
        "include_track_names": "track_name",
    }
    folded_filters = {
        option_name: (
            {name.casefold() for name in names}
            if names is not None
            else None
        )
        for option_name, names in name_filters.items()
    }

    selected: list[dict[str, Any]] = []
    for task in image_tasks:
        if any(
            allowed_names is not None
            and (
                task.get(metadata_fields[option_name]) is None
                or str(task[metadata_fields[option_name]]).casefold()
                not in allowed_names
            )
            for option_name, allowed_names in folded_filters.items()
        ):
            continue

        if frame_from_key is not None or frame_to_key is not None:
            raw_stem = task.get("image_stem")
            if raw_stem is None:
                raw_stem = Path(
                    str(task.get("image_name") or task.get("image_path") or "")
                ).stem
            stem_key = _natural_frame_id_key(str(raw_stem))
            if frame_from_key is not None and stem_key < frame_from_key:
                continue
            if frame_to_key is not None and stem_key > frame_to_key:
                continue
        selected.append(task)

    active_scope = {
        **{
            option_name: list(names)
            for option_name, names in name_filters.items()
            if names is not None
        },
        **{
            option_name: value
            for option_name, value in frame_bounds.items()
            if value is not None
        },
    }
    if not selected:
        scope_description = (
            ", ".join(
                f"{option_name}={value!r}"
                for option_name, value in active_scope.items()
            )
            or "no explicit filters"
        )
        raise ValueError(
            "Work-scope selection contains no MMS image tasks; "
            f"check {scope_description}."
        )

    if active_scope and logger is not None:
        logger.info(
            "Work-scope filters selected %d/%d MMS image tasks: %s",
            len(selected),
            len(image_tasks),
            ", ".join(
                f"{option_name}={value!r}"
                for option_name, value in active_scope.items()
            ),
        )
    return selected


def _leica_catalog_job_names(
    image_tasks: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Return the canonical display names that bound LAS discovery."""

    names_by_key: dict[str, str] = {}
    for task in image_tasks:
        if task.get("pose_format") not in {"leica-sphere", "leica-delivery"}:
            continue
        raw_name = task.get("job_name")
        if raw_name is None:
            continue
        name = str(raw_name).strip()
        if name:
            names_by_key.setdefault(name.casefold(), name)
    return tuple(names_by_key[key] for key in sorted(names_by_key))


def _scoped_pointcloud_catalog_path(
    base_path: Path,
    *,
    all_image_tasks: list[dict[str, Any]],
    selected_image_tasks: list[dict[str, Any]],
    logger=None,
) -> tuple[Path, tuple[str, ...]]:
    """Keep different selected Leica job sets in stable, reusable cache files.

    The unfiltered path remains unchanged for backward compatibility.  A new
    scoped cache is seeded from that full cache when available: the catalog
    builder can then reuse unchanged per-file block indexes instead of reading
    the selected LAS files again.
    """

    all_jobs = _leica_catalog_job_names(all_image_tasks)
    selected_jobs = _leica_catalog_job_names(selected_image_tasks)
    if {name.casefold() for name in selected_jobs} == {
        name.casefold() for name in all_jobs
    }:
        return base_path, selected_jobs

    canonical = json.dumps(
        sorted(name.casefold() for name in selected_jobs),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    suffix = base_path.suffix or ".json"
    stem = base_path.name[: -len(base_path.suffix)] if base_path.suffix else base_path.name
    scoped_path = base_path.with_name(f"{stem}.jobs-{digest}{suffix}")

    if not scoped_path.exists() and base_path.is_file():
        temporary_path = scoped_path.with_name(
            f".{scoped_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            scoped_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_bytes(base_path.read_bytes())
            try:
                # A hard link publishes without overwriting a cache another
                # process may have completed while this seed was being copied.
                os.link(temporary_path, scoped_path)
            except FileExistsError:
                pass
            if logger is not None:
                logger.info(
                    "Seeded scoped point-cloud catalog from %s: %s",
                    base_path,
                    scoped_path,
                )
        except OSError as exc:
            if logger is not None:
                logger.warning(
                    "Could not seed scoped point-cloud catalog %s: %s",
                    scoped_path,
                    exc,
                )
        finally:
            temporary_path.unlink(missing_ok=True)
    return scoped_path, selected_jobs


def prepare_shared_pipeline_context(
    args: argparse.Namespace,
    *,
    alignment_report_path: Path,
    logger,
) -> dict[str, Any]:
    """Prepare immutable dataset/catalog inputs once for every model."""

    all_image_tasks = scan_image_tasks(
        args.data_root,
        logger,
        pose_format=args.pose_format,
        gps_week=args.gps_week,
        gps_utc_offset_seconds=args.gps_utc_offset_seconds,
    )
    image_tasks = select_image_tasks_for_scope(
        all_image_tasks,
        args,
        logger=logger,
    )
    calibration_bundle = attach_calibration_metadata(
        image_tasks,
        args.calibration_path,
        logger,
        require_calibration=args.require_calibration,
    )
    image_tasks.sort(key=lambda item: (item["timestamp_iso"], item["image_path"]))
    dataset_signature = build_dataset_signature(image_tasks)
    catalog_path, included_leica_jobs = _scoped_pointcloud_catalog_path(
        Path(args.pointcloud_cache_path),
        all_image_tasks=all_image_tasks,
        selected_image_tasks=image_tasks,
        logger=logger,
    )
    args.pointcloud_cache_path = catalog_path
    pointcloud_catalog = build_pointcloud_catalog(
        args.data_root,
        catalog_path,
        logger,
        source=args.point_source,
        las_chunk_size=max(10_000, args.las_index_chunk_points),
        # An empty tuple intentionally excludes LAS for legacy-only scopes.
        include_jobs=included_leica_jobs,
    )
    crs_wkt = (
        validate_crs_wkt(
            args.crs_wkt_path.read_text(encoding="utf-8-sig"),
            label=str(args.crs_wkt_path),
        )
        if args.crs_wkt_path is not None
        else resolve_matched_crs_wkt(
            image_tasks,
            pointcloud_catalog,
            max(1, args.pointcloud_neighbor_count),
        )
    )
    pointcloud_catalog["resolved_crs_wkt"] = crs_wkt
    if pointcloud_catalog.get("selected_source_type") in {"las", "mixed"} and not crs_wkt:
        raise ValueError(
            "LAS files have missing or inconsistent CRS WKT; refuse to write a mislabeled SHP."
        )
    maximum_pose_separation = validate_pose_pointcloud_proximity(
        image_tasks,
        pointcloud_catalog,
        max(1, args.pointcloud_neighbor_count),
        args.max_pose_pointcloud_separation_m,
    )
    alignment_report = run_panorama_alignment_qa(
        image_tasks,
        pointcloud_catalog,
        args,
        alignment_report_path,
        logger,
        dataset_signature=dataset_signature,
    )
    return {
        "image_tasks": image_tasks,
        "calibration_bundle": calibration_bundle,
        "dataset_signature": dataset_signature,
        "catalog_path": catalog_path,
        "pointcloud_catalog": pointcloud_catalog,
        "crs_wkt": crs_wkt,
        "maximum_pose_separation": maximum_pose_separation,
        "alignment_report": alignment_report,
    }


def finalize_prepared_model_run(
    prepared_run: dict[str, Any],
    summary: dict[str, int],
) -> dict[str, Any]:
    """Publish one model's SHP bundles after queued processing completes."""

    output_dirs = prepared_run["output_dirs"]
    logger = prepared_run["logger"]
    runtime = prepared_run["runtime"]
    run_fingerprint = prepared_run["run_fingerprint"]
    crs_wkt = prepared_run["crs_wkt"]
    if summary["failures"]:
        logger.error(
            "Run has %d failed image/pole operation(s); final SHP publication was withheld. "
            "Any available partial features remain under *.in_progress.*.",
            summary["failures"],
        )
        raise RuntimeError(
            f"Pipeline completed with {summary['failures']} failed image/pole operation(s); "
            "see worker logs."
        )

    records = collect_detection_records(
        output_dirs["txt"],
        logger=logger,
        run_fingerprint=run_fingerprint,
    )
    shp_path = output_dirs["shp"] / "detected_signs.shp"
    stage_id = f"{run_fingerprint[:12]}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    staged_shp_path = output_dirs["shp"] / f"detected_signs.{stage_id}.ready.shp"
    pole_shp_path = output_dirs["shp"] / "pole_bottoms.shp"
    staged_pole_shp_path = output_dirs["shp"] / f"pole_bottoms.{stage_id}.ready.shp"
    merged_poles: list[dict[str, Any]] = []
    unique_frame_observations = 0
    if runtime["pole_detection"]:
        pole_observations = collect_pole_records(
            output_dirs["txt"],
            logger=logger,
            run_fingerprint=run_fingerprint,
        )
        pole_observations = reconcile_remote_supports_from_direct_anchors(
            records,
            pole_observations,
            direct_distance_m=float(
                runtime["pole_direct_max_axis_sign_distance_m"]
            ),
            max_link_distance_m=max(
                float(runtime["pole_max_axis_sign_distance_m"]),
                float(runtime["pole_fallback_max_axis_sign_distance_m"]),
            ),
        )
        merged_poles = (
            cluster_pole_observations(
                pole_observations,
                radius_m=float(runtime["pole_observation_merge_radius_m"]),
            )
            if pole_observations
            else []
        )
        unique_frame_observations = sum(
            int(item.get("obs_count") or 1)
            for item in {
                str(relation.get("support_id")): relation
                for relation in merged_poles
            }.values()
        )
        merged_poles = [
            item
            for item in merged_poles
            if int(item.get("obs_count") or 1)
            >= int(runtime["pole_min_observations"])
        ]
    attach_support_ids_to_detection_records(records, merged_poles)
    records, merged_poles = deduplicate_sign_and_pole_observations(
        records,
        merged_poles,
        supported_xy_radius_m=float(runtime["sign_observation_merge_xy_radius_m"]),
        supported_z_radius_m=float(runtime["sign_observation_merge_z_radius_m"]),
        unsupported_xy_radius_m=float(
            runtime["sign_observation_fallback_xy_radius_m"]
        ),
        unsupported_z_radius_m=float(
            runtime["sign_observation_fallback_z_radius_m"]
        ),
    )

    publication_pairs = [(staged_shp_path, shp_path)]
    try:
        write_shapefile(records, staged_shp_path, crs_wkt=crs_wkt)
        if runtime["pole_detection"]:
            write_pole_shapefile(
                merged_poles,
                staged_pole_shp_path,
                crs_wkt=crs_wkt,
            )
            publication_pairs.append((staged_pole_shp_path, pole_shp_path))
        publish_shapefile_bundles(publication_pairs)
    except BaseException as exc:
        recovery_preserved = any(
            "Recovery components were preserved" in note
            for note in getattr(exc, "__notes__", [])
        )
        if recovery_preserved:
            logger.error(
                "Final SHP rollback was incomplete; ready/backup components were kept "
                "for recovery. Do not open the final bundle until the paths in the "
                "exception notes have been inspected."
            )
        else:
            remove_generated_shapefile_bundle(staged_shp_path, logger)
            remove_generated_shapefile_bundle(staged_pole_shp_path, logger)
        raise

    logger.info("Saved shapefile with %d features to %s", len(records), shp_path)
    if runtime["pole_detection"]:
        logger.info(
            "Saved pole shapefile with %d features from %d unique frame observations to %s",
            len(merged_poles),
            unique_frame_observations,
            pole_shp_path,
        )
    else:
        existing_components = [
            pole_shp_path.with_suffix(suffix)
            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2")
            if pole_shp_path.with_suffix(suffix).exists()
        ]
        if existing_components:
            logger.warning(
                "Pole detection is disabled; existing pole_bottoms files were left unchanged "
                "and are not outputs of run %s: %s",
                run_fingerprint[:12],
                ", ".join(str(path) for path in existing_components),
            )

    remove_generated_shapefile_bundle(
        output_dirs["shp"] / "detected_signs.in_progress.shp",
        logger,
    )
    remove_generated_shapefile_bundle(
        output_dirs["shp"] / "pole_bottoms.in_progress.shp",
        logger,
    )
    return {
        "run_fingerprint": run_fingerprint,
        "final_shapefiles": {
            "detections": str(shp_path.resolve()),
            "poles": (
                str(pole_shp_path.resolve())
                if runtime["pole_detection"]
                else None
            ),
        },
        "feature_counts": {
            "detections": len(records),
            "poles": len(merged_poles) if runtime["pole_detection"] else 0,
        },
    }


def _run_single_model_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    validate_point_range_fallback_arguments(args)
    shared_forward_views_dir = getattr(args, "_shared_forward_views_dir", None)
    output_dirs = ensure_output_dirs(
        args.output_dir,
        shared_forward_views_dir=shared_forward_views_dir,
    )
    log_path = output_dirs["logs"] / "run.log"
    model_logger_key = sanitize_name(
        getattr(args, "model_key", None)
        or getattr(getattr(args, "model_path", None), "stem", "model")
    )
    logger = setup_logging(
        log_path,
        file_mode="w",
        logger_name=f"mms_shp_detection_main_{model_logger_key}",
        level=getattr(args, "log_level", "INFO"),
        capture_root=not bool(getattr(args, "_parallel_prepare", False)),
    )
    effective_config_path = output_dirs["logs"] / "effective_config.json"
    atomic_write_text(
        effective_config_path,
        json.dumps(serializable_config(args), ensure_ascii=False, indent=2),
    )
    logger.info("Configuration source: %s", getattr(args, "_config_path", None) or "CLI/defaults")
    logger.info("Effective configuration: %s", effective_config_path.resolve())

    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass

    device = resolve_device(args.device)
    actual_num_workers = (
        1
        if getattr(args, "_parallel_prepare", False)
        else resolve_num_workers(args, device, logger)
    )
    logger.info("Using device: %s", device)
    logger.info("Data root: %s", args.data_root)
    logger.info("Model path: %s", args.model_path)
    logger.info(
        "Model profile: %s | object type: %s",
        getattr(args, "model_profile", "<base>"),
        getattr(args, "model_object_type", "generic"),
    )
    logger.info("Output root: %s", args.output_dir)
    logger.info("Point-cloud cache path: %s", args.pointcloud_cache_path)
    logger.info("Requested workers: %d | Effective workers: %d", args.num_workers, actual_num_workers)

    shared_context = getattr(args, "_shared_pipeline_context", None)
    if shared_context is None:
        shared_context = prepare_shared_pipeline_context(
            args,
            alignment_report_path=(
                output_dirs["logs"] / "panorama_alignment_qa.json"
            ),
            logger=logger,
        )
    image_tasks = list(shared_context["image_tasks"])
    calibration_bundle = shared_context["calibration_bundle"]
    dataset_signature = shared_context["dataset_signature"]
    catalog_path = Path(shared_context["catalog_path"])
    pointcloud_catalog = shared_context["pointcloud_catalog"]
    crs_wkt = shared_context["crs_wkt"]
    maximum_pose_separation = shared_context["maximum_pose_separation"]
    alignment_report = shared_context["alignment_report"]
    if args.pole_detection:
        pole_classification_policy = resolve_pole_classification_policy(
            pointcloud_catalog,
            requested_mode=args.pole_classification_mode,
            ground_class_ids=tuple(args.pole_ground_class_ids),
            pole_class_ids=tuple(args.pole_class_ids),
            excluded_pole_class_ids=tuple(args.pole_excluded_pole_class_ids),
        )
    else:
        pole_classification_policy = {
            "requested_mode": str(args.pole_classification_mode),
            "effective_mode": "GEOMETRY",
            "uses_classification": False,
            "reason": "pole_detection_disabled",
            "source_type": str(pointcloud_catalog.get("selected_source_type") or "unknown"),
            "configured": {
                "ground_class_ids": sorted(set(args.pole_ground_class_ids)),
                "pole_class_ids": sorted(set(args.pole_class_ids)),
                "excluded_pole_class_ids": sorted(
                    set(args.pole_excluded_pole_class_ids)
                ),
            },
            "observed_class_ids": [],
            "matched_class_ids": [],
            "matched_point_count": 0,
            "source_file_count": len(pointcloud_catalog.get("files") or []),
            "files_with_classification_dimension": 0,
            "files_with_semantic_classes": 0,
            "files_without_semantic_classes": 0,
            "_files_without_semantic_class_paths": [],
        }
    logger.info(
        "Pole classification policy: requested=%s effective=%s reason=%s "
        "matched_ids=%s matched_points=%d files=%d/%d",
        pole_classification_policy["requested_mode"],
        pole_classification_policy["effective_mode"],
        pole_classification_policy["reason"],
        pole_classification_policy["matched_class_ids"],
        pole_classification_policy["matched_point_count"],
        pole_classification_policy["files_with_semantic_classes"],
        pole_classification_policy["source_file_count"],
    )
    missing_semantic_paths = pole_classification_policy.get(
        "_files_without_semantic_class_paths"
    ) or []
    if args.pole_detection and missing_semantic_paths:
        logger.warning(
            "Configured semantic classes were absent from %d selected point-cloud file(s): %s",
            len(missing_semantic_paths),
            ", ".join(missing_semantic_paths),
        )
    classification_policy_path = output_dirs["logs"] / "pole_classification_policy.json"
    atomic_write_text(
        classification_policy_path,
        json.dumps(
            public_pole_classification_policy(pole_classification_policy),
            ensure_ascii=False,
            indent=2,
        ),
    )
    logger.info("Pole classification policy report: %s", classification_policy_path.resolve())
    model_sha256 = _sha256_file(args.model_path.resolve())
    run_fingerprint = build_run_fingerprint(
        args,
        pointcloud_catalog,
        calibration_bundle,
        dataset_signature,
        model_sha256=model_sha256,
    )
    logger.info(
        "Dataset signature: tasks=%d images=%d sha256=%s",
        dataset_signature["task_count"],
        dataset_signature["image_file_count"],
        dataset_signature["sha256"][:12],
    )
    logger.info(
        "Point-cloud source: %s | files=%d | run fingerprint=%s",
        pointcloud_catalog.get("selected_source_type"),
        len(pointcloud_catalog.get("files", [])),
        run_fingerprint[:12],
    )
    logger.info("Model SHA256: %s", model_sha256)
    if maximum_pose_separation is not None:
        logger.info(
            "Pose/point-cloud XY bbox sanity check passed (maximum nearest separation %.3f m).",
            maximum_pose_separation,
        )

    # Slicing is intentionally applied only after the immutable input scope and
    # run fingerprint have been resolved.  Separate batches of the same dataset
    # therefore share one fingerprint and can safely contribute to one SHP.
    if args.start_index > 0 or args.limit_images > 0:
        start = max(0, args.start_index)
        stop = None if args.limit_images <= 0 else start + args.limit_images
        image_tasks = image_tasks[start:stop]
        logger.info(
            "Applied image slicing: start_index=%d limit_images=%d -> %d tasks",
            args.start_index,
            args.limit_images,
            len(image_tasks),
        )

    runtime = {
        "model_path": str(args.model_path.resolve()),
        "model_name": args.model_path.name,
        "model_key": sanitize_name(args.model_path.stem),
        "model_profile": str(getattr(args, "model_profile", "<base>")),
        "model_object_type": str(getattr(args, "model_object_type", "generic")),
        "model_sha256": model_sha256,
        "imgsz": args.imgsz,
        "detection_view_mode": str(args.detection_view_mode),
        "forward_view_size": max(256, int(args.forward_view_size)),
        "forward_view_hfov_deg": float(args.forward_view_hfov_deg),
        "forward_view_vfov_deg": float(args.forward_view_vfov_deg),
        "panorama_yaw_offset_deg": float(args.panorama_yaw_offset_deg),
        "panorama_pitch_offset_deg": float(args.panorama_pitch_offset_deg),
        "alignment_qa": alignment_report,
        "use_full_panorama_detection": not args.disable_full_panorama_detection,
        "use_tiled_detection": not args.disable_tiled_detection,
        "tile_width_px": 0 if args.tile_width_px <= 0 else max(256, args.tile_width_px),
        "tile_height_px": 0 if args.tile_height_px <= 0 else max(256, args.tile_height_px),
        "tile_overlap_px": max(0, args.tile_overlap_px),
        "tile_batch_size": max(1, args.tile_batch_size),
        "tile_merge_iou": min(0.99, max(0.0, args.tile_merge_iou)),
        "conf": args.conf,
        "iou": args.iou,
        "max_det": args.max_det,
        "device": device,
        "pointcloud_neighbor_count": args.pointcloud_neighbor_count,
        "point_padding_px": args.point_padding_px,
        "debug_crop_padding_px": args.debug_crop_padding_px,
        "debug_mask_alpha": args.debug_mask_alpha,
        "max_range_m": args.max_range_m,
        "point_range_fallback_enabled": bool(args.point_range_fallback_enabled),
        "point_range_fallback_max_range_m": float(
            args.point_range_fallback_max_range_m
        ),
        "point_range_fallback_min_point_count": int(
            args.point_range_fallback_min_point_count
        ),
        "point_range_fallback_min_cluster_fraction": float(
            args.point_range_fallback_min_cluster_fraction
        ),
        "point_range_fallback_min_core_mask_fraction": float(
            args.point_range_fallback_min_core_mask_fraction
        ),
        "point_range_fallback_max_depth_span_m": float(
            args.point_range_fallback_max_depth_span_m
        ),
        "depth_window_m": args.depth_window_m,
        "front_surface_quantile": min(1.0, max(0.0, args.front_surface_quantile)),
        "front_surface_min_support": max(1, args.front_surface_min_support),
        "block_angle_margin_deg": args.block_angle_margin_deg,
        "max_center_ray_angle_deg": min(180.0, max(0.0, args.max_center_ray_angle_deg)),
        "min_point_count": max(1, args.min_point_count),
        "perspective_view_size": max(256, args.perspective_view_size),
        "perspective_margin_deg": max(0.0, args.perspective_margin_deg),
        "perspective_min_fov_deg": max(1.0, args.perspective_min_fov_deg),
        "perspective_max_fov_deg": max(args.perspective_min_fov_deg, args.perspective_max_fov_deg),
        "cluster_radius_m": max(0.01, args.cluster_radius_m),
        "cluster_min_neighbors": max(1, args.cluster_min_neighbors),
        "cluster_trim_radius_multiplier": max(1.0, args.cluster_trim_radius_multiplier),
        "point_preview_size": max(192, args.point_preview_size),
        "las_crop_half_extent_m": float(args.las_crop_half_extent_m),
        "pole_detection": bool(args.pole_detection),
        "pole_classification_mode": str(args.pole_classification_mode),
        "pole_classification_policy": pole_classification_policy,
        "pole_min_fov_deg": float(args.pole_min_fov_deg),
        "pole_debug_min_fov_deg": float(args.pole_debug_min_fov_deg),
        "pole_corridor_side_expand_ratio": float(args.pole_corridor_side_expand_ratio),
        "pole_corridor_top_margin_ratio": float(args.pole_corridor_top_margin_ratio),
        "pole_search_radius_m": float(args.pole_search_radius_m),
        "pole_max_drop_m": float(args.pole_max_drop_m),
        "pole_top_margin_m": float(args.pole_top_margin_m),
        "pole_range_fallback_enabled": bool(args.pole_range_fallback_enabled),
        "pole_fallback_search_radius_m": float(
            args.pole_fallback_search_radius_m
        ),
        "pole_fallback_max_drop_m": float(args.pole_fallback_max_drop_m),
        "pole_fallback_top_margin_m": float(args.pole_fallback_top_margin_m),
        "pole_fallback_max_axis_sign_distance_m": float(
            args.pole_fallback_max_axis_sign_distance_m
        ),
        "pole_fallback_min_vertical_span_m": float(
            args.pole_fallback_min_vertical_span_m
        ),
        "pole_fallback_horizontal_connection_radius_m": float(
            args.pole_fallback_horizontal_connection_radius_m
        ),
        "pole_fallback_horizontal_connection_z_tolerance_m": float(
            args.pole_fallback_horizontal_connection_z_tolerance_m
        ),
        "pole_fallback_horizontal_connection_above_tolerance_m": float(
            args.pole_fallback_horizontal_connection_above_tolerance_m
        ),
        "pole_fallback_horizontal_connection_bin_m": float(
            args.pole_fallback_horizontal_connection_bin_m
        ),
        "pole_fallback_min_horizontal_connection_coverage": float(
            args.pole_fallback_min_horizontal_connection_coverage
        ),
        "pole_xy_voxel_m": float(args.pole_xy_voxel_m),
        "pole_z_bin_m": float(args.pole_z_bin_m),
        "pole_axis_cluster_radius_m": float(args.pole_axis_cluster_radius_m),
        "pole_axis_inlier_radius_m": float(args.pole_axis_inlier_radius_m),
        "pole_min_vertical_span_m": float(args.pole_min_vertical_span_m),
        "pole_min_vertical_bins": int(args.pole_min_vertical_bins),
        "pole_min_consecutive_vertical_bins": int(args.pole_min_consecutive_vertical_bins),
        "pole_max_observed_z_gap_m": float(args.pole_max_observed_z_gap_m),
        "pole_min_vertical_occupancy_ratio": float(args.pole_min_vertical_occupancy_ratio),
        "pole_middle_support_start_fraction": float(args.pole_middle_support_start_fraction),
        "pole_min_middle_support_coverage_ratio": float(
            args.pole_min_middle_support_coverage_ratio
        ),
        "pole_preferred_min_completeness_ratio": float(
            args.pole_preferred_min_completeness_ratio
        ),
        "pole_geometry_ground_clearance_m": float(
            args.pole_geometry_ground_clearance_m
        ),
        "pole_geometry_remote_min_completeness_ratio": float(
            args.pole_geometry_remote_min_completeness_ratio
        ),
        "pole_geometry_remote_max_axis_rmse_m": float(
            args.pole_geometry_remote_max_axis_rmse_m
        ),
        "pole_geometry_remote_max_ground_rmse_m": float(
            args.pole_geometry_remote_max_ground_rmse_m
        ),
        "pole_min_points": int(args.pole_min_points),
        "pole_max_axis_tilt_deg": float(args.pole_max_axis_tilt_deg),
        "pole_axis_plumb_max_tilt_deg": float(
            args.pole_axis_plumb_max_tilt_deg
        ),
        "pole_axis_plumb_full_tilt_deg": float(
            args.pole_axis_plumb_full_tilt_deg
        ),
        "pole_axis_plumb_endpoint_fraction": float(
            args.pole_axis_plumb_endpoint_fraction
        ),
        "pole_direct_max_axis_sign_distance_m": float(
            args.pole_direct_max_axis_sign_distance_m
        ),
        "pole_max_axis_sign_distance_m": float(args.pole_max_axis_sign_distance_m),
        "pole_horizontal_connection_radius_m": float(
            args.pole_horizontal_connection_radius_m
        ),
        "pole_horizontal_connection_z_tolerance_m": float(
            args.pole_horizontal_connection_z_tolerance_m
        ),
        "pole_horizontal_connection_above_tolerance_m": float(
            args.pole_horizontal_connection_above_tolerance_m
        ),
        "pole_horizontal_connection_bin_m": float(
            args.pole_horizontal_connection_bin_m
        ),
        "pole_horizontal_connection_min_points_per_bin": int(
            args.pole_horizontal_connection_min_points_per_bin
        ),
        "pole_horizontal_connection_coherence_radius_m": float(
            args.pole_horizontal_connection_coherence_radius_m
        ),
        "pole_min_horizontal_connection_coverage": float(
            args.pole_min_horizontal_connection_coverage
        ),
        "pole_min_horizontal_connection_coherent_ratio": float(
            args.pole_min_horizontal_connection_coherent_ratio
        ),
        "pole_min_horizontal_connection_coherent_point_fraction": float(
            args.pole_min_horizontal_connection_coherent_point_fraction
        ),
        "pole_remote_max_endpoint_tilt_deg": float(
            args.pole_remote_max_endpoint_tilt_deg
        ),
        "pole_long_remote_distance_m": float(
            args.pole_long_remote_distance_m
        ),
        "pole_long_remote_transition_m": float(
            args.pole_long_remote_transition_m
        ),
        "pole_long_remote_min_vertical_span_m": float(
            args.pole_long_remote_min_vertical_span_m
        ),
        "pole_long_remote_min_completeness_ratio": float(
            args.pole_long_remote_min_completeness_ratio
        ),
        "pole_long_remote_min_connection_coverage_ratio": float(
            args.pole_long_remote_min_connection_coverage_ratio
        ),
        "pole_max_ground_class_fraction": float(args.pole_max_ground_class_fraction),
        "pole_min_ground_drop_m": float(args.pole_min_ground_drop_m),
        "pole_require_ground": bool(args.pole_require_ground),
        "pole_ground_search_radius_m": float(args.pole_ground_search_radius_m),
        "pole_ground_core_radius_m": float(args.pole_ground_core_radius_m),
        "pole_ground_exclusion_radius_m": float(args.pole_ground_exclusion_radius_m),
        "pole_ground_cell_size_m": float(args.pole_ground_cell_size_m),
        "pole_ground_cell_quantile": float(args.pole_ground_cell_quantile),
        "pole_ground_min_cells": int(args.pole_ground_min_cells),
        "pole_ground_max_rmse_m": float(args.pole_ground_max_rmse_m),
        "pole_ground_geometry_preference_margin_m": float(
            args.pole_ground_geometry_preference_margin_m
        ),
        "pole_occlusion_gap_m": float(args.pole_occlusion_gap_m),
        "pole_max_ground_penetration_m": float(
            args.pole_max_ground_penetration_m
        ),
        "pole_max_ground_support_distance_m": float(
            args.pole_max_ground_support_distance_m
        ),
        "pole_ground_class_ids": tuple(args.pole_ground_class_ids),
        "pole_class_ids": tuple(args.pole_class_ids),
        "pole_excluded_pole_class_ids": tuple(args.pole_excluded_pole_class_ids),
        "pole_observation_merge_radius_m": float(args.pole_observation_merge_radius_m),
        "pole_min_observations": int(args.pole_min_observations),
        "sign_observation_merge_xy_radius_m": float(
            args.sign_observation_merge_xy_radius_m
        ),
        "sign_observation_merge_z_radius_m": float(
            args.sign_observation_merge_z_radius_m
        ),
        "sign_observation_fallback_xy_radius_m": float(
            args.sign_observation_fallback_xy_radius_m
        ),
        "sign_observation_fallback_z_radius_m": float(
            args.sign_observation_fallback_z_radius_m
        ),
        "disable_pole_debug": bool(args.disable_pole_debug),
        "disable_pole_point_crop": bool(args.disable_pole_point_crop),
        "skip_existing": args.skip_existing,
        "txt_dir": str(output_dirs["txt"].resolve()),
        "forward_views_dir": str(output_dirs["forward_views"].resolve()),
        "image_crops_dir": str(output_dirs["image_crops"].resolve()),
        "point_crops_dir": str(output_dirs["point_crops"].resolve()),
        "point_previews_dir": str(output_dirs["point_previews"].resolve()),
        "pole_crops_dir": str(output_dirs["pole_crops"].resolve()),
        "pole_debug_dir": str(output_dirs["pole_debug"].resolve()),
        "workers_log_dir": str((output_dirs["logs"] / "workers").resolve()),
        "pointcloud_catalog_path": str(catalog_path.resolve()),
        "pointcloud_source": pointcloud_catalog.get("selected_source_type"),
        "crs_wkt": crs_wkt,
        "dataset_signature": dataset_signature,
        "run_fingerprint": run_fingerprint,
        "log_path": str(log_path.resolve()),
        "worker_progress_every": max(1, args.worker_progress_every),
        "log_level": getattr(args, "log_level", "INFO"),
    }

    if (
        runtime["detection_view_mode"] == "panorama"
        and not runtime["use_full_panorama_detection"]
        and not runtime["use_tiled_detection"]
    ):
        raise ValueError(
            "At least one detection pass must remain enabled. "
            "Do not use --disable-full-panorama-detection and --disable-tiled-detection together."
        )
    if runtime["detection_view_mode"] == "forward":
        if not 1.0 <= runtime["forward_view_hfov_deg"] < 180.0:
            raise ValueError("forward_view_hfov_deg must be in [1, 180) degrees")
        if not 1.0 <= runtime["forward_view_vfov_deg"] < 180.0:
            raise ValueError("forward_view_vfov_deg must be in [1, 180) degrees")
        if not math.isclose(
            runtime["forward_view_hfov_deg"],
            runtime["forward_view_vfov_deg"],
            abs_tol=1.0e-6,
        ):
            raise ValueError(
                "The square forward YOLO input requires equal horizontal and vertical "
                "FOV values; unequal values anisotropically distort sign shapes."
            )
    if runtime["pole_detection"]:
        strict_pole_parameters = build_pole_search_parameters(runtime)
        build_pole_fallback_parameters(runtime, strict_pole_parameters)
        if not 1.0 <= runtime["pole_min_fov_deg"] <= 180.0:
            raise ValueError("pole_min_fov_deg must be between 1 and 180")
        if not 1.0 <= runtime["pole_debug_min_fov_deg"] <= 180.0:
            raise ValueError("pole_debug_min_fov_deg must be between 1 and 180")
        if runtime["pole_debug_min_fov_deg"] > runtime["perspective_max_fov_deg"]:
            raise ValueError(
                "pole_debug_min_fov_deg cannot exceed perspective_max_fov_deg"
            )
        if runtime["pole_corridor_side_expand_ratio"] < 0.0:
            raise ValueError("pole_corridor_side_expand_ratio cannot be negative")
        if runtime["pole_corridor_top_margin_ratio"] < 0.0:
            raise ValueError("pole_corridor_top_margin_ratio cannot be negative")
        if runtime["pole_observation_merge_radius_m"] <= 0.0:
            raise ValueError("pole_observation_merge_radius_m must be positive")
        if runtime["pole_min_observations"] < 1:
            raise ValueError("pole_min_observations must be at least 1")

    prepared_run = {
        "args": args,
        "output_dirs": output_dirs,
        "logger": logger,
        "log_path": log_path,
        "image_tasks": image_tasks,
        "pointcloud_catalog": pointcloud_catalog,
        "runtime": runtime,
        "run_fingerprint": run_fingerprint,
        "crs_wkt": crs_wkt,
        "actual_num_workers": actual_num_workers,
    }
    if getattr(args, "_parallel_prepare", False):
        return prepared_run

    progress_totals = {"images": 0, "detections": 0, "points": 0, "failures": 0}
    progress_disabled = bool(getattr(args, "disable_console_progress", False))
    with tqdm(
        total=len(image_tasks),
        desc="MMS processing",
        unit="image",
        dynamic_ncols=True,
        disable=progress_disabled,
        file=sys.stderr,
        bar_format=(
            "{l_bar}{bar}| {n_fmt}/{total_fmt} "
            "[{elapsed}<{remaining}, {rate_fmt}{postfix}]"
        ),
    ) as progress_bar:
        if not image_tasks:
            summary = {"images": 0, "detections": 0, "points": 0, "failures": 0}
            logger.warning("Selected image range is empty; no worker was launched.")
        elif actual_num_workers <= 1:
            summary = worker_process(
                image_tasks,
                runtime,
                progress_callback=lambda event: _update_progress_bar(
                    progress_bar,
                    progress_totals,
                    event,
                ),
            )
            logger.info(
                "Single-worker run finished: images=%d detections=%d points=%d",
                summary["images"],
                summary["detections"],
                summary["points"],
            )
            if not args.disable_intermediate_shp:
                safely_refresh_shapefile_from_txt(
                    output_dirs["txt"],
                    output_dirs["shp"] / "detected_signs.in_progress.shp",
                    logger,
                    reason="single-worker completion",
                    run_fingerprint=run_fingerprint,
                    crs_wkt=crs_wkt,
                    pole_shp_path=(output_dirs["shp"] / "pole_bottoms.in_progress.shp")
                    if runtime["pole_detection"]
                    else None,
                    pole_merge_radius_m=runtime["pole_observation_merge_radius_m"],
                    pole_min_observations=runtime["pole_min_observations"],
                    sign_merge_xy_radius_m=runtime["sign_observation_merge_xy_radius_m"],
                    sign_merge_z_radius_m=runtime["sign_observation_merge_z_radius_m"],
                    sign_fallback_xy_radius_m=runtime[
                        "sign_observation_fallback_xy_radius_m"
                    ],
                    sign_fallback_z_radius_m=runtime[
                        "sign_observation_fallback_z_radius_m"
                    ],
                )
        else:
            chunks = split_chunks(image_tasks, actual_num_workers)
            summary = {"images": 0, "detections": 0, "points": 0, "failures": 0}
            logger.info("Launching %d worker processes.", len(chunks))
            with mp.Manager() as manager:
                progress_queue = manager.Queue()
                with ProcessPoolExecutor(max_workers=len(chunks)) as executor:
                    pending = {
                        executor.submit(worker_process, chunk, runtime, progress_queue)
                        for chunk in chunks
                    }
                    completed_workers = 0
                    heartbeat_interval = max(1, args.progress_log_interval_sec)
                    next_heartbeat = time.monotonic() + heartbeat_interval
                    while pending:
                        done, pending = wait(
                            pending,
                            timeout=0.25,
                            return_when=FIRST_COMPLETED,
                        )
                        _drain_progress_queue(progress_queue, progress_bar, progress_totals)

                        now = time.monotonic()
                        if now >= next_heartbeat:
                            logger.info(
                                "Waiting for workers: completed=%d/%d, pending=%d, processed=%d/%d",
                                completed_workers,
                                len(chunks),
                                len(pending),
                                progress_totals["images"],
                                len(image_tasks),
                            )
                            if (
                                not args.disable_intermediate_shp
                                and count_txt_files(output_dirs["txt"]) > 0
                            ):
                                safely_refresh_shapefile_from_txt(
                                    output_dirs["txt"],
                                    output_dirs["shp"] / "detected_signs.in_progress.shp",
                                    logger,
                                    reason="heartbeat",
                                    run_fingerprint=run_fingerprint,
                                    crs_wkt=crs_wkt,
                                    pole_shp_path=(output_dirs["shp"] / "pole_bottoms.in_progress.shp")
                                    if runtime["pole_detection"]
                                    else None,
                                    pole_merge_radius_m=runtime["pole_observation_merge_radius_m"],
                                    pole_min_observations=runtime["pole_min_observations"],
                                    sign_merge_xy_radius_m=runtime[
                                        "sign_observation_merge_xy_radius_m"
                                    ],
                                    sign_merge_z_radius_m=runtime[
                                        "sign_observation_merge_z_radius_m"
                                    ],
                                    sign_fallback_xy_radius_m=runtime[
                                        "sign_observation_fallback_xy_radius_m"
                                    ],
                                    sign_fallback_z_radius_m=runtime[
                                        "sign_observation_fallback_z_radius_m"
                                    ],
                                )
                            next_heartbeat = now + heartbeat_interval

                        for future in done:
                            result = future.result()
                            completed_workers += 1
                            for key in summary:
                                summary[key] += result[key]
                            logger.info(
                                "Worker completion received: completed=%d/%d, detections=%d, points=%d",
                                completed_workers,
                                len(chunks),
                                summary["detections"],
                                summary["points"],
                            )
                            if not args.disable_intermediate_shp:
                                safely_refresh_shapefile_from_txt(
                                    output_dirs["txt"],
                                    output_dirs["shp"] / "detected_signs.in_progress.shp",
                                    logger,
                                    reason=f"worker {completed_workers} completion",
                                    run_fingerprint=run_fingerprint,
                                    crs_wkt=crs_wkt,
                                    pole_shp_path=(output_dirs["shp"] / "pole_bottoms.in_progress.shp")
                                    if runtime["pole_detection"]
                                    else None,
                                    pole_merge_radius_m=runtime["pole_observation_merge_radius_m"],
                                    pole_min_observations=runtime["pole_min_observations"],
                                    sign_merge_xy_radius_m=runtime[
                                        "sign_observation_merge_xy_radius_m"
                                    ],
                                    sign_merge_z_radius_m=runtime[
                                        "sign_observation_merge_z_radius_m"
                                    ],
                                    sign_fallback_xy_radius_m=runtime[
                                        "sign_observation_fallback_xy_radius_m"
                                    ],
                                    sign_fallback_z_radius_m=runtime[
                                        "sign_observation_fallback_z_radius_m"
                                    ],
                                )
                    _drain_progress_queue(progress_queue, progress_bar, progress_totals)
            logger.info(
                "Multi-worker run finished: images=%d detections=%d points=%d",
                summary["images"],
                summary["detections"],
                summary["points"],
            )

        # Test doubles or an abruptly closed progress queue may not emit events;
        # reconcile the display with the authoritative worker summary.
        if progress_totals["images"] < summary["images"]:
            missing_images = summary["images"] - progress_totals["images"]
            progress_bar.update(missing_images)
        progress_bar.set_postfix(
            signs=summary["detections"],
            points=summary["points"],
            errors=summary["failures"],
            refresh=True,
        )

    return finalize_prepared_model_run(prepared_run, summary)


def _accumulate_summary(
    target: dict[str, int],
    result: dict[str, int],
) -> None:
    for key in ("images", "detections", "points", "failures"):
        target[key] += int(result.get(key, 0))


def _parallel_forward_spec(runtime: dict[str, Any]) -> tuple[Any, ...]:
    return (
        runtime.get("detection_view_mode"),
        int(runtime["forward_view_size"]),
        float(runtime["forward_view_hfov_deg"]),
        float(runtime["forward_view_vfov_deg"]),
        float(runtime.get("panorama_yaw_offset_deg", 0.0)),
        float(runtime.get("panorama_pitch_offset_deg", 0.0)),
        float(runtime["max_center_ray_angle_deg"]),
    )


def run_parallel_multi_model_pipeline(
    prepared: list[tuple[Path, argparse.Namespace, str, str, str]],
    *,
    base_output_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    """Run shared-frame inference with bounded model post-processing queues."""

    logs_dir = base_output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    orchestrator_logger = setup_logging(
        logs_dir / "orchestrator.log",
        file_mode="w",
        logger_name="mms_shp_detection_orchestrator",
        level=getattr(prepared[0][1], "log_level", "INFO"),
    )
    shared_forward_views_dir = base_output_dir / "forward_views"
    shared_forward_views_dir.mkdir(parents=True, exist_ok=True)
    first_args = prepared[0][1]
    manifest["execution_mode"] = "frame_parallel_shared_forward"
    manifest["shared_artifacts"] = {
        "forward_views": str(shared_forward_views_dir.resolve()),
        "panorama_alignment_qa": str(
            (logs_dir / "panorama_alignment_qa.json").resolve()
        ),
    }
    manifest["scheduler"] = {
        "inference_workers_requested": int(
            first_args.multi_model_inference_workers
        ),
        "pole_workers": int(first_args.multi_model_pole_workers),
        "per_model_queue_depth": int(first_args.multi_model_queue_depth),
        "cuda_oom_policy": "retry_serialized_then_circuit_break_model",
    }
    for entry in manifest["models"]:
        entry["status"] = "preparing"
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    orchestrator_logger.info(
        "Preparing one shared dataset context for %d models.", len(prepared)
    )
    try:
        shared_context = prepare_shared_pipeline_context(
            first_args,
            alignment_report_path=logs_dir / "panorama_alignment_qa.json",
            logger=orchestrator_logger,
        )
    except BaseException as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        error_text = None if interrupted else f"{type(exc).__name__}: {exc}"
        for entry in manifest["models"]:
            entry["status"] = "interrupted" if interrupted else "failed"
            entry["error"] = error_text
            entry["failure_log"] = str(
                (logs_dir / "orchestrator.log").resolve()
            )
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        raise

    states: list[dict[str, Any]] = []
    entry_by_key = {
        str(entry["model_key"]): entry for entry in manifest["models"]
    }
    preparation_failures: list[tuple[str, str]] = []
    for _model_path, effective, model_key, _profile_name, _object_type in prepared:
        entry = entry_by_key[model_key]
        entry["status"] = "preparing"
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        effective._shared_pipeline_context = shared_context
        effective._shared_forward_views_dir = shared_forward_views_dir
        effective._parallel_prepare = True
        try:
            state = _run_single_model_pipeline(effective)
        except KeyboardInterrupt:
            for candidate_entry in manifest["models"]:
                if candidate_entry["status"] in {"pending", "preparing", "running"}:
                    candidate_entry["status"] = "interrupted"
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise
        except Exception as exc:
            logger = setup_logging(
                Path(effective.output_dir) / "logs" / "run.log",
                file_mode="a",
                logger_name=f"mms_shp_detection_prepare_failure_{model_key}",
                level=getattr(effective, "log_level", "INFO"),
                capture_root=False,
            )
            logger.exception("Parallel model preparation failed for %s.", model_key)
            error_text = f"{type(exc).__name__}: {exc}"
            entry["status"] = "failed"
            entry["error"] = error_text
            entry["failure_log"] = str(
                (Path(effective.output_dir) / "logs" / "run.log").resolve()
            )
            preparation_failures.append((model_key, error_text))
        else:
            state["model_key"] = model_key
            states.append(state)
            entry["status"] = "running"
            entry["run_fingerprint"] = state["run_fingerprint"]
        finally:
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

    if not states:
        summary = "; ".join(f"{key}: {error}" for key, error in preparation_failures)
        raise RuntimeError(f"Every model failed during parallel preparation: {summary}")

    specs = {_parallel_forward_spec(state["runtime"]) for state in states}
    if len(specs) != 1 or next(iter(specs))[0] != "forward":
        error_text = (
            "Parallel shared-view execution requires identical forward view, "
            "alignment, and center-ray settings for every model."
        )
        for state in states:
            entry = entry_by_key[state["model_key"]]
            entry["status"] = "failed"
            entry["error"] = error_text
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        raise ValueError(
            error_text
        )
    reference_tasks = states[0]["image_tasks"]
    reference_task_keys = [
        (str(item["image_path"]), str(item["timestamp_iso"])) for item in reference_tasks
    ]
    for state in states[1:]:
        task_keys = [
            (str(item["image_path"]), str(item["timestamp_iso"]))
            for item in state["image_tasks"]
        ]
        if task_keys != reference_task_keys:
            error_text = "Parallel models resolved different image task scopes"
            for candidate_state in states:
                entry = entry_by_key[candidate_state["model_key"]]
                entry["status"] = "failed"
                entry["error"] = error_text
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise ValueError(error_text)

    first_args = states[0]["args"]
    coordinator = MultiModelCoordinator(
        inference_workers=min(
            len(states),
            max(1, int(first_args.multi_model_inference_workers)),
        ),
        pole_workers=max(1, int(first_args.multi_model_pole_workers)),
        queue_depth=max(1, int(first_args.multi_model_queue_depth)),
    )
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )

    models: dict[str, YOLO] = {}
    active_states: list[dict[str, Any]] = []
    model_failures: list[tuple[str, str]] = list(preparation_failures)
    for state in states:
        model_key = state["model_key"]
        entry = entry_by_key[model_key]
        try:
            with coordinator.timed(f"model_load/{model_key}"):
                models[model_key] = YOLO(state["runtime"]["model_path"])
        except KeyboardInterrupt:
            for candidate_entry in manifest["models"]:
                if candidate_entry["status"] in {"pending", "preparing", "running"}:
                    candidate_entry["status"] = "interrupted"
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise
        except Exception as exc:
            state["logger"].exception("Could not load model %s.", model_key)
            error_text = f"{type(exc).__name__}: {exc}"
            entry["status"] = "failed"
            entry["error"] = error_text
            entry["failure_log"] = str(Path(state["log_path"]).resolve())
            model_failures.append((model_key, error_text))
        else:
            active_states.append(state)
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    if not active_states:
        summary = "; ".join(f"{key}: {error}" for key, error in model_failures)
        raise RuntimeError(f"Every model failed to load: {summary}")

    work_queues = {
        state["model_key"]: queue.Queue(maxsize=coordinator.queue_depth)
        for state in active_states
    }
    stop_token = object()
    cancel_event = threading.Event()
    consumer_errors: queue.Queue[tuple[str, BaseException]] = queue.Queue()
    terminal_model_errors: dict[str, str] = {}
    summaries = {
        state["model_key"]: {
            "images": 0,
            "detections": 0,
            "points": 0,
            "failures": 0,
        }
        for state in active_states
    }
    summary_lock = threading.Lock()
    progress_disabled = bool(first_args.disable_console_progress)
    progress_bar = tqdm(
        total=len(reference_tasks) * len(active_states),
        desc="MMS multi-model",
        unit="model-frame",
        dynamic_ncols=True,
        disable=progress_disabled,
        file=sys.stderr,
    )

    def record_result(model_key: str, result: dict[str, int]) -> None:
        with summary_lock:
            _accumulate_summary(summaries[model_key], result)
            progress_bar.update(max(0, int(result.get("images", 0))))
            total_detections = sum(item["detections"] for item in summaries.values())
            total_failures = sum(item["failures"] for item in summaries.values())
            progress_bar.set_postfix(
                detections=total_detections,
                errors=total_failures,
                refresh=False,
            )

    def raise_pending_consumer_error() -> None:
        try:
            model_key, exc = consumer_errors.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError(
            f"Post-processing consumer for {model_key} stopped unexpectedly: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    def enqueue_consumer_job(model_key: str, job: Any) -> None:
        work_queue = work_queues[model_key]
        while True:
            raise_pending_consumer_error()
            if cancel_event.is_set():
                raise RuntimeError(
                    "Parallel post-processing was cancelled before a queued job "
                    f"could be delivered to {model_key}."
                )
            try:
                work_queue.put(job, timeout=0.2)
                return
            except queue.Full:
                continue

    def enqueue_frame_jobs_round_robin(
        frame_jobs: list[tuple[str, Any]],
    ) -> None:
        """Deliver ready model jobs without letting one full queue block its peers."""

        pending = list(frame_jobs)
        while pending:
            raise_pending_consumer_error()
            if cancel_event.is_set():
                raise RuntimeError(
                    "Parallel post-processing was cancelled while frame jobs "
                    "were waiting for bounded queues."
                )
            remaining: list[tuple[str, Any]] = []
            made_progress = False
            for model_key, job in pending:
                try:
                    work_queues[model_key].put_nowait(job)
                except queue.Full:
                    remaining.append((model_key, job))
                else:
                    made_progress = True
            pending = remaining
            if pending and not made_progress:
                cancel_event.wait(0.05)

    shared_pointcloud_cache = PointCloudReaderCache()

    def consume_model_frames(state: dict[str, Any]) -> None:
        model_key = state["model_key"]
        runtime = state["runtime"]
        logger = state["logger"]
        work_queue = work_queues[model_key]
        try:
            while True:
                if cancel_event.is_set():
                    return
                try:
                    job = work_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if job is stop_token:
                        return
                    image_task, image_rgb, candidates, forward_path = job
                    try:
                        with coordinator.timed(f"postprocess/{model_key}"):
                            result = process_image_task(
                                image_task=image_task,
                                runtime=runtime,
                                model=None,
                                pointcloud_catalog=state["pointcloud_catalog"],
                                pointcloud_cache=shared_pointcloud_cache,
                                logger=logger,
                                image_rgb_override=image_rgb,
                                detection_candidates_override=candidates,
                                forward_view_output_path_override=forward_path,
                                skip_existing_checked=True,
                                coordinator=coordinator,
                            )
                    except MemoryError:
                        raise
                    except Exception:
                        logger.exception(
                            "Queued post-processing failed on image %s.",
                            image_task["image_path"],
                        )
                        result = {
                            "images": 1,
                            "detections": 0,
                            "points": 0,
                            "failures": 1,
                        }
                    record_result(model_key, result)
                finally:
                    work_queue.task_done()
        except BaseException as exc:
            consumer_errors.put((model_key, exc))
            cancel_event.set()

    consumer_threads = [
        threading.Thread(
            target=consume_model_frames,
            args=(state,),
            name=f"postprocess-{state['model_key']}",
            daemon=False,
        )
        for state in active_states
    ]
    started_consumer_threads: list[threading.Thread] = []
    producer_error: BaseException | None = None
    try:
        for thread in consumer_threads:
            thread.start()
            started_consumer_threads.append(thread)
        with ThreadPoolExecutor(
            max_workers=len(active_states),
            thread_name_prefix="yolo-model",
        ) as inference_executor:
            for image_task in reference_tasks:
                raise_pending_consumer_error()
                needed_states: list[dict[str, Any]] = []
                for state in active_states:
                    if state["model_key"] in terminal_model_errors:
                        with summary_lock:
                            progress_bar.update(1)
                        continue
                    existing = compatible_existing_result_summary(
                        image_task,
                        state["runtime"],
                        state["logger"],
                    )
                    if existing is None:
                        needed_states.append(state)
                    else:
                        record_result(state["model_key"], existing)
                if not needed_states:
                    continue

                image_path = Path(image_task["image_path"])
                try:
                    with coordinator.timed("shared_panorama_decode"):
                        image_rgb = load_panorama_rgb(image_path, orchestrator_logger)
                        validate_panorama_image(image_task, image_rgb)
                    with coordinator.timed("shared_forward_render"):
                        forward_rgb, mapping = render_forward_detection_view(
                            image_rgb,
                            needed_states[0]["runtime"],
                        )
                    forward_path = (
                        shared_forward_views_dir
                        / image_task["record_name"]
                        / f"{image_task['image_stem']}__forward.jpg"
                    )
                    with coordinator.timed("shared_forward_qa_write"):
                        save_forward_detection_qa_image(
                            forward_rgb,
                            forward_path,
                            hfov_deg=float(mapping["hfov_deg"]),
                            vfov_deg=float(mapping["vfov_deg"]),
                            max_center_ray_angle_deg=float(
                                needed_states[0]["runtime"][
                                    "max_center_ray_angle_deg"
                                ]
                            ),
                        )
                except MemoryError:
                    raise
                except Exception:
                    orchestrator_logger.exception(
                        "Shared panorama preparation failed for %s; "
                        "recording one failure per required model and continuing.",
                        image_task["image_path"],
                    )
                    for state in needed_states:
                        state["logger"].error(
                            "Shared panorama preparation failed for %s.",
                            image_task["image_path"],
                            exc_info=True,
                        )
                        record_result(
                            state["model_key"],
                            {
                                "images": 1,
                                "detections": 0,
                                "points": 0,
                                "failures": 1,
                            },
                        )
                    continue

                future_states = {
                    inference_executor.submit(
                        run_forward_detection_on_view,
                        forward_rgb,
                        mapping,
                        state["runtime"],
                        models[state["model_key"]],
                        state["logger"],
                        coordinator=coordinator,
                    ): state
                    for state in needed_states
                }
                frame_jobs: list[tuple[str, Any]] = []
                for future in as_completed(future_states):
                    state = future_states[future]
                    model_key = state["model_key"]
                    try:
                        candidates = future.result()
                    except PersistentCudaOutOfMemoryError as exc:
                        state["logger"].exception(
                            "Model was disabled after serialized CUDA OOM on image %s.",
                            image_task["image_path"],
                        )
                        error_text = f"{type(exc).__name__}: {exc}"
                        terminal_model_errors[model_key] = error_text
                        entry = entry_by_key[model_key]
                        entry["status"] = "failed"
                        entry["error"] = error_text
                        entry["failure_log"] = str(Path(state["log_path"]).resolve())
                        model_failures.append((model_key, error_text))
                        try:
                            # Wait until every other serialized prediction has
                            # left the GPU before moving failed weights.
                            with coordinator.inference_gate.slot():
                                models[model_key].to("cpu")
                                try:
                                    import torch

                                    if torch.cuda.is_available():
                                        torch.cuda.empty_cache()
                                except Exception:
                                    state["logger"].debug(
                                        "Could not clear CUDA cache after model offload.",
                                        exc_info=True,
                                    )
                        except Exception:
                            state["logger"].debug(
                                "Could not offload the failed model to CPU.",
                                exc_info=True,
                            )
                        record_result(
                            model_key,
                            {
                                "images": 1,
                                "detections": 0,
                                "points": 0,
                                "failures": 1,
                            },
                        )
                        atomic_write_text(
                            manifest_path,
                            json.dumps(manifest, ensure_ascii=False, indent=2),
                        )
                        continue
                    except MemoryError:
                        raise
                    except Exception:
                        state["logger"].exception(
                            "Model inference failed on image %s.",
                            image_task["image_path"],
                        )
                        record_result(
                            model_key,
                            {
                                "images": 1,
                                "detections": 0,
                                "points": 0,
                                "failures": 1,
                            },
                        )
                        continue
                    frame_jobs.append(
                        (
                            model_key,
                            (image_task, image_rgb, candidates, forward_path),
                        )
                    )
                enqueue_frame_jobs_round_robin(frame_jobs)
    except BaseException as exc:
        producer_error = exc
        cancel_event.set()
        orchestrator_logger.exception("Parallel frame producer stopped.")
    finally:
        if producer_error is None and not cancel_event.is_set():
            try:
                for state in active_states:
                    enqueue_consumer_job(state["model_key"], stop_token)
            except BaseException as exc:
                producer_error = exc
                cancel_event.set()
                orchestrator_logger.exception(
                    "Could not finish the parallel post-processing queues cleanly."
                )
        else:
            cancel_event.set()
        for thread in started_consumer_threads:
            while thread.is_alive():
                thread.join(timeout=0.2)
                if producer_error is None:
                    try:
                        raise_pending_consumer_error()
                    except BaseException as exc:
                        producer_error = exc
                        cancel_event.set()
                        orchestrator_logger.exception(
                            "Parallel post-processing consumer stopped."
                        )
        try:
            shared_pointcloud_cache.close()
        except BaseException as exc:
            if producer_error is None:
                producer_error = exc
            cancel_event.set()
            orchestrator_logger.exception(
                "Could not close the shared point-cloud block cache."
            )
        if producer_error is None:
            try:
                raise_pending_consumer_error()
            except BaseException as exc:
                producer_error = exc
                cancel_event.set()
                orchestrator_logger.exception(
                    "Parallel post-processing consumer failed during shutdown."
                )
        progress_bar.close()

    performance = coordinator.snapshot()
    atomic_write_text(
        logs_dir / "performance.json",
        json.dumps(performance, ensure_ascii=False, indent=2),
    )
    manifest["performance"] = performance
    if producer_error is not None:
        interrupted = isinstance(producer_error, KeyboardInterrupt)
        error_text = (
            None
            if interrupted
            else f"{type(producer_error).__name__}: {producer_error}"
        )
        for state in active_states:
            entry = entry_by_key[state["model_key"]]
            if entry["status"] == "running":
                entry["status"] = "interrupted" if interrupted else "failed"
                entry["error"] = error_text
                entry["failure_log"] = str(
                    (logs_dir / "orchestrator.log").resolve()
                )
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        raise producer_error

    for state in active_states:
        model_key = state["model_key"]
        entry = entry_by_key[model_key]
        expected_paths = list(entry["expected_final_shapefiles"].values())
        entry["preexisting_final_shapefiles"] = [
            path for path in expected_paths if Path(path).is_file()
        ]
        if model_key in terminal_model_errors:
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            continue
        try:
            run_result = finalize_prepared_model_run(
                state,
                summaries[model_key],
            )
        except KeyboardInterrupt:
            for candidate_entry in manifest["models"]:
                if candidate_entry["status"] == "running":
                    candidate_entry["status"] = "interrupted"
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise
        except Exception as exc:
            state["logger"].exception("Model finalization failed for %s.", model_key)
            error_text = f"{type(exc).__name__}: {exc}"
            entry["status"] = "failed"
            entry["error"] = error_text
            entry["failure_log"] = str(Path(state["log_path"]).resolve())
            entry["existing_final_shapefiles_after_failure"] = [
                path for path in expected_paths if Path(path).is_file()
            ]
            model_failures.append((model_key, error_text))
        else:
            entry["status"] = "completed"
            entry["published_current_run"] = True
            entry["run_fingerprint"] = run_result.get("run_fingerprint")
            entry["final_shapefiles"] = run_result.get("final_shapefiles")
            entry["feature_counts"] = run_result.get("feature_counts")
        finally:
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

    if model_failures:
        summary = "; ".join(
            f"{model_key}: {error_text}"
            for model_key, error_text in model_failures
        )
        raise RuntimeError(
            f"{len(model_failures)} model run(s) failed after all models were attempted: "
            f"{summary}"
        )


def run_pipeline(args: argparse.Namespace) -> None:
    """Run one checkpoint or every configured checkpoint in isolated outputs."""

    configured_model_dir = getattr(args, "model_dir", None)
    model_paths = discover_model_paths(
        configured_model_dir,
        getattr(args, "model_path", None),
    )
    multi_model = configured_model_dir is not None
    base_output_dir = Path(args.output_dir).resolve()

    prepared: list[tuple[Path, argparse.Namespace, str, str, str]] = []
    for model_path in model_paths:
        effective, profile_name, object_type = apply_model_filter(
            args,
            model_path,
            require_profile=multi_model,
        )
        validate_point_range_fallback_arguments(effective)
        if effective.pole_detection:
            strict_pole_parameters = build_pole_search_parameters(vars(effective))
            build_pole_fallback_parameters(
                vars(effective),
                strict_pole_parameters,
            )
        model_key = sanitize_name(model_path.stem)
        effective.output_dir = (
            base_output_dir / model_key if multi_model else base_output_dir
        )
        prepared.append(
            (model_path, effective, model_key, profile_name, object_type)
        )

    if not multi_model:
        _run_single_model_pipeline(prepared[0][1])
        return

    base_output_dir.mkdir(parents=True, exist_ok=True)
    (base_output_dir / "logs").mkdir(parents=True, exist_ok=True)
    shared_forward_views_dir = base_output_dir / "forward_views"
    shared_forward_views_dir.mkdir(parents=True, exist_ok=True)
    for _model_path, effective, _model_key, _profile_name, _object_type in prepared:
        effective._shared_forward_views_dir = shared_forward_views_dir
    manifest_path = base_output_dir / "models_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "model_dir": str(Path(configured_model_dir).resolve()),
        "output_dir": str(base_output_dir),
        "execution_mode": "sequential",
        "shared_artifacts": {
            "forward_views": str(shared_forward_views_dir.resolve()),
        },
        "models": [
            {
                "model_name": model_path.name,
                "model_key": model_key,
                "model_path": str(model_path),
                "model_profile": profile_name,
                "model_object_type": object_type,
                "output_dir": str(effective.output_dir),
                "expected_final_shapefiles": {
                    "detections": str(
                        Path(effective.output_dir).resolve()
                        / "shp"
                        / "detected_signs.shp"
                    ),
                    "poles": str(
                        Path(effective.output_dir).resolve()
                        / "shp"
                        / "pole_bottoms.shp"
                    ),
                },
                "status": "pending",
                "error": None,
                "failure_log": None,
                "published_current_run": False,
                "run_fingerprint": None,
            }
            for model_path, effective, model_key, profile_name, object_type in prepared
        ],
    }
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )

    if bool(getattr(args, "multi_model_parallel", False)) and len(prepared) > 1:
        try:
            run_parallel_multi_model_pipeline(
                prepared,
                base_output_dir=base_output_dir,
                manifest=manifest,
                manifest_path=manifest_path,
            )
        except KeyboardInterrupt:
            for entry in manifest["models"]:
                if entry["status"] in {"pending", "preparing", "running"}:
                    entry["status"] = "interrupted"
                    entry["error"] = None
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise
        return

    failures: list[tuple[str, str]] = []
    for index, (_, effective, model_key, _, _) in enumerate(prepared):
        entry = manifest["models"][index]
        expected_paths = list(entry["expected_final_shapefiles"].values())
        entry["preexisting_final_shapefiles"] = [
            path for path in expected_paths if Path(path).is_file()
        ]
        entry["status"] = "running"
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        try:
            run_result = _run_single_model_pipeline(effective)
        except KeyboardInterrupt:
            entry["status"] = "interrupted"
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            raise
        except Exception as exc:
            failure_log_path = Path(effective.output_dir) / "logs" / "run.log"
            failure_logger = setup_logging(
                failure_log_path,
                file_mode="a",
                logger_name="mms_shp_detection_main",
                level=getattr(effective, "log_level", "INFO"),
            )
            failure_logger.exception(
                "Model pipeline failed for %s.",
                effective.model_path,
            )
            error_text = f"{type(exc).__name__}: {exc}"
            entry["status"] = "failed"
            entry["error"] = error_text
            entry["failure_log"] = str(failure_log_path.resolve())
            entry["existing_final_shapefiles_after_failure"] = [
                path for path in expected_paths if Path(path).is_file()
            ]
            failures.append((model_key, error_text))
        else:
            entry["status"] = "completed"
            entry["published_current_run"] = True
            if isinstance(run_result, dict):
                entry["run_fingerprint"] = run_result.get("run_fingerprint")
                entry["final_shapefiles"] = run_result.get("final_shapefiles")
                entry["feature_counts"] = run_result.get("feature_counts")
        finally:
            atomic_write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

    if failures:
        summary = "; ".join(
            f"{model_key}: {error_text}"
            for model_key, error_text in failures
        )
        raise RuntimeError(
            f"{len(failures)} model run(s) failed after all models were attempted: {summary}"
        )


def main() -> None:
    parser = build_arg_parser()
    args = parse_args_with_config(
        parser,
        default_config_path=Path(__file__).resolve().parents[1] / "config.yaml",
    )
    try:
        run_pipeline(args)
    except KeyboardInterrupt:
        log_path = Path(args.output_dir) / "logs" / "run.log"
        logger = setup_logging(
            log_path,
            file_mode="a",
            logger_name="mms_shp_detection_main",
            level=getattr(args, "log_level", "INFO"),
        )
        logger.warning("Pipeline interrupted by the user.")
        sys.stderr.write(f"\nInterrupted. Details: {log_path.resolve()}\n")
        raise SystemExit(130) from None
    except Exception:
        log_path = Path(args.output_dir) / "logs" / "run.log"
        logger = setup_logging(
            log_path,
            file_mode="a",
            logger_name="mms_shp_detection_main",
            level=getattr(args, "log_level", "INFO"),
        )
        logger.exception("Pipeline failed.")
        sys.stderr.write(f"\nFailed. Details: {log_path.resolve()}\n")
        raise SystemExit(1) from None
