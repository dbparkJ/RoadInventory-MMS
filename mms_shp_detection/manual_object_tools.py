"""Pure P1 manual-object templates and panorama bbox point inference."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .geometry import pixel_to_world_ray, project_points_equirectangular


@dataclass(frozen=True)
class ManualObjectTemplate:
    template_id: str
    class_name: str
    geometry_type: Literal["Point"]
    tool_id: str
    duplicate_radius_m: float
    continuous: bool
    required_semantics: tuple[str, ...]
    relation_semantics: tuple[str, ...] = ()
    fixed_values: tuple[tuple[str, object], ...] = ()
    default_values: tuple[tuple[str, object], ...] = ()
    domains: tuple[tuple[str, tuple[object, ...]], ...] = ()

    def public_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "class_name": self.class_name,
            "geometry_type": self.geometry_type,
            "tool_id": self.tool_id,
            "duplicate_radius_m": self.duplicate_radius_m,
            "continuous": self.continuous,
            "required_semantics": list(self.required_semantics),
            "relation_semantics": list(self.relation_semantics),
            "fixed_values": dict(self.fixed_values),
            "default_values": dict(self.default_values),
            "domains": {key: list(values) for key, values in self.domains},
        }


MANUAL_OBJECT_TEMPLATES: dict[str, ManualObjectTemplate] = {
    "TRAFFIC_SIGN": ManualObjectTemplate(
        template_id="TRAFFIC_SIGN",
        class_name="TRAFFIC_SIGN",
        geometry_type="Point",
        tool_id="panorama_bbox_point_v1",
        duplicate_radius_m=0.75,
        continuous=True,
        required_semantics=("class",),
        relation_semantics=("support_id",),
        fixed_values=(("class", "TRAFFIC_SIGN"),),
    ),
    "SIGN_SUPPORT_POLE": ManualObjectTemplate(
        template_id="SIGN_SUPPORT_POLE",
        class_name="SIGN_SUPPORT_POLE",
        geometry_type="Point",
        tool_id="manual_pole_base_v1",
        duplicate_radius_m=0.50,
        continuous=True,
        required_semantics=("class",),
        fixed_values=(("class", "SIGN_SUPPORT_POLE"),),
    ),
}


@dataclass(frozen=True)
class PanoramaBboxPointResult:
    status: Literal["auto", "review", "failed"]
    position: np.ndarray | None
    score: float
    support_point_count: int
    depth_spread_m: float | None
    reprojection_error_px: float | None
    cluster_count: int
    reason_codes: tuple[str, ...]
    seed_position: np.ndarray | None
    support_points: np.ndarray


def _validate_axes(
    origin: np.ndarray,
    forward: np.ndarray,
    right: np.ndarray,
    up: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    vectors = tuple(
        np.asarray(value, dtype=np.float64) for value in (origin, forward, right, up)
    )
    if any(value.shape != (3,) or not np.all(np.isfinite(value)) for value in vectors):
        raise ValueError("Panorama origin and axes must be finite three-vectors.")
    return vectors


def _bbox_center_u(u_intervals: tuple[tuple[float, float], ...]) -> float:
    if len(u_intervals) == 1:
        left, right = u_intervals[0]
        return (left + right) * 0.5
    ordered = sorted(u_intervals)
    low, high = ordered[0], ordered[-1]
    if math.isclose(low[0], 0.0, abs_tol=1e-9) and math.isclose(
        high[1], 1.0, abs_tol=1e-9
    ):
        return ((high[0] + (low[1] + 1.0)) * 0.5) % 1.0
    centers = np.asarray([(left + right) * 0.5 for left, right in ordered])
    widths = np.asarray([right - left for left, right in ordered])
    angles = centers * (2.0 * math.pi)
    vector = np.asarray(
        [
            float(np.sum(np.cos(angles) * widths)),
            float(np.sum(np.sin(angles) * widths)),
        ]
    )
    if float(np.linalg.norm(vector)) <= 1e-12:
        return float(centers[0])
    return (math.atan2(float(vector[1]), float(vector[0])) / (2.0 * math.pi)) % 1.0


def _depth_clusters(
    distances: np.ndarray,
    *,
    minimum_support: int,
) -> list[np.ndarray]:
    if distances.size == 0:
        return []
    order = np.argsort(distances, kind="stable")
    sorted_depth = distances[order]
    # Scanner spacing grows with range.  A bounded relative gap keeps a coherent
    # facade together while separating foreground vehicles and background signs.
    gaps = np.diff(sorted_depth)
    thresholds = np.maximum(0.35, np.minimum(1.25, sorted_depth[:-1] * 0.025))
    boundaries = np.flatnonzero(gaps > thresholds) + 1
    groups = np.split(order, boundaries)
    return [group for group in groups if group.size >= minimum_support]


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values, kind="stable")
    ordered_values = values[order]
    ordered_weights = weights[order]
    cutoff = float(np.sum(ordered_weights)) * 0.5
    index = int(np.searchsorted(np.cumsum(ordered_weights), cutoff, side="left"))
    return float(ordered_values[min(index, ordered_values.size - 1)])


def infer_panorama_bbox_point(
    points_xyz: np.ndarray,
    origin_xyz: np.ndarray,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    *,
    u_intervals: tuple[tuple[float, float], ...],
    v_min: float,
    v_max: float,
    image_width: int,
    image_height: int,
    max_range_m: float = 100.0,
    minimum_support: int = 5,
) -> PanoramaBboxPointResult:
    """Infer a deterministic PointZ proposal from a seam-safe panorama bbox."""

    points = np.asarray(points_xyz, dtype=np.float64)
    origin, forward, right, up = _validate_axes(
        origin_xyz, forward_vec, right_vec, up_vec
    )
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points_xyz must have shape (N, 3).")
    if not u_intervals or len(u_intervals) > 2:
        raise ValueError("u_intervals must contain one or two intervals.")
    if image_width <= 0 or image_height <= 0 or not 0.0 <= v_min < v_max <= 1.0:
        raise ValueError("Panorama bbox dimensions are invalid.")
    if not math.isfinite(max_range_m) or max_range_m <= 0.0:
        raise ValueError("max_range_m must be positive and finite.")

    finite = np.all(np.isfinite(points), axis=1)
    points = points[finite]
    if points.size == 0:
        return PanoramaBboxPointResult(
            "failed",
            None,
            0.0,
            0,
            None,
            None,
            0,
            ("NO_SUPPORTING_POINTS",),
            None,
            np.empty((0, 3), dtype=np.float64),
        )
    u_px, v_px, distance = project_points_equirectangular(
        points, origin, forward, right, up, image_width, image_height
    )
    u = np.mod(u_px / float(image_width), 1.0)
    v = v_px / float(image_height)
    in_u = np.zeros(points.shape[0], dtype=bool)
    for left, right_bound in u_intervals:
        if not 0.0 <= left < right_bound <= 1.0:
            raise ValueError("Each U interval must satisfy 0 <= left < right <= 1.")
        in_u |= (u >= left) & (u <= right_bound)
    angular_mask = in_u & (v >= v_min) & (v <= v_max) & (distance > 0.05)
    if not np.any(angular_mask):
        return PanoramaBboxPointResult(
            "failed",
            None,
            0.0,
            0,
            None,
            None,
            0,
            ("NO_SUPPORTING_POINTS",),
            None,
            np.empty((0, 3), dtype=np.float64),
        )
    if not np.any(angular_mask & (distance <= max_range_m)):
        return PanoramaBboxPointResult(
            "failed",
            None,
            0.0,
            0,
            None,
            None,
            0,
            ("MAX_RANGE_EXCEEDED",),
            None,
            np.empty((0, 3), dtype=np.float64),
        )

    selected_mask = angular_mask & (distance <= max_range_m)
    selected_points = points[selected_mask]
    selected_u = u[selected_mask]
    selected_v = v[selected_mask]
    selected_depth = distance[selected_mask]
    clusters = _depth_clusters(selected_depth, minimum_support=minimum_support)
    if not clusters:
        return PanoramaBboxPointResult(
            "failed",
            None,
            0.0,
            int(selected_depth.size),
            None,
            None,
            0,
            ("DEPTH_CLUSTER_WEAK",),
            None,
            selected_points[:256],
        )

    clusters.sort(
        key=lambda indices: (
            float(np.median(selected_depth[indices])),
            -int(indices.size),
        )
    )
    chosen = clusters[0]
    chosen_depth = selected_depth[chosen]
    depth_spread = float(
        np.quantile(chosen_depth, 0.90) - np.quantile(chosen_depth, 0.10)
    )
    bbox_center_u = _bbox_center_u(u_intervals)
    bbox_center_v = (v_min + v_max) * 0.5
    chosen_u = selected_u[chosen]
    chosen_v = selected_v[chosen]
    # Work in a seam-safe coordinate system centred on the bbox. Points near the
    # bbox centre carry more weight, so loose boxes do not pull the proposal to
    # background support at an edge.
    chosen_du = ((chosen_u - bbox_center_u + 0.5) % 1.0) - 0.5
    bbox_width = sum(right_bound - left for left, right_bound in u_intervals)
    half_width = max(bbox_width * 0.5, 1.0 / float(image_width))
    half_height = max((v_max - v_min) * 0.5, 1.0 / float(image_height))
    normalized_radius = np.hypot(
        chosen_du / half_width,
        (chosen_v - bbox_center_v) / half_height,
    )
    weights = np.exp(-0.5 * np.square(normalized_radius))
    weights = np.maximum(weights, 1e-6)
    depth = _weighted_median(chosen_depth, weights)
    center_u = (
        bbox_center_u + _weighted_median(chosen_du, weights)
    ) % 1.0
    center_v = _weighted_median(chosen_v, weights)
    ray = pixel_to_world_ray(center_u, center_v, 1, 1, forward, right, up)
    position = origin + (ray * depth)

    du = np.abs(chosen_u - center_u)
    du = np.minimum(du, 1.0 - du) * float(image_width)
    dv = np.abs(chosen_v - center_v) * float(image_height)
    reprojection_error = float(np.median(np.hypot(du, dv)))
    support = int(chosen.size)
    multiple = len(clusters) > 1
    weak = support < 12 or depth_spread > 0.75
    reasons = tuple(
        code
        for code, present in (
            ("MULTIPLE_DEPTH_CLUSTERS", multiple),
            ("DEPTH_CLUSTER_WEAK", weak),
        )
        if present
    )
    support_score = min(1.0, support / 30.0)
    spread_score = max(0.0, 1.0 - (depth_spread / 1.5))
    ambiguity_score = 0.70 if multiple else 1.0
    score = float(
        (0.45 * support_score) + (0.40 * spread_score) + (0.15 * ambiguity_score)
    )
    return PanoramaBboxPointResult(
        status="review" if reasons else "auto",
        position=np.asarray(position, dtype=np.float64),
        score=max(0.0, min(1.0, score)),
        support_point_count=support,
        depth_spread_m=depth_spread,
        reprojection_error_px=reprojection_error,
        cluster_count=len(clusters),
        reason_codes=reasons,
        seed_position=np.average(selected_points[chosen], axis=0, weights=weights),
        support_points=np.asarray(selected_points[chosen][:256], dtype=np.float64),
    )


__all__ = [
    "MANUAL_OBJECT_TEMPLATES",
    "ManualObjectTemplate",
    "PanoramaBboxPointResult",
    "infer_panorama_bbox_point",
]
