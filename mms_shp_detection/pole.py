from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
from scipy.spatial import cKDTree


@dataclass(frozen=True)
class PoleSearchParameters:
    """Tunable LiDAR pole/base extraction parameters in metres."""

    search_radius_m: float = 8.0
    max_drop_m: float = 8.0
    top_margin_m: float = 3.0
    xy_voxel_m: float = 0.10
    z_bin_m: float = 0.15
    axis_cluster_radius_m: float = 0.24
    axis_inlier_radius_m: float = 0.18
    min_vertical_span_m: float = 0.75
    min_vertical_bins: int = 5
    min_consecutive_vertical_bins: int = 4
    max_observed_z_gap_m: float = 1.0
    min_vertical_occupancy_ratio: float = 0.35
    middle_support_start_fraction: float = 0.20
    min_middle_support_coverage_ratio: float = 0.30
    preferred_min_completeness_ratio: float = 0.75
    geometry_ground_clearance_m: float = 0.20
    geometry_remote_min_completeness_ratio: float = 0.75
    geometry_remote_max_axis_rmse_m: float = 0.095
    geometry_remote_max_ground_rmse_m: float = 0.15
    min_points: int = 18
    max_axis_tilt_deg: float = 15.0
    direct_max_axis_sign_distance_m: float = 0.75
    max_axis_sign_distance_m: float = 8.0
    horizontal_connection_radius_m: float = 0.25
    horizontal_connection_z_tolerance_m: float = 0.30
    horizontal_connection_above_tolerance_m: float = 0.30
    horizontal_connection_bin_m: float = 0.25
    horizontal_connection_min_points_per_bin: int = 2
    min_horizontal_connection_coverage: float = 0.50
    max_ground_class_fraction: float = 0.35
    min_ground_drop_m: float = 1.8
    require_ground: bool = True
    ground_search_radius_m: float = 1.5
    ground_core_radius_m: float = 0.75
    ground_exclusion_radius_m: float = 0.24
    ground_cell_size_m: float = 0.25
    ground_cell_quantile: float = 0.10
    ground_min_cells: int = 6
    ground_max_rmse_m: float = 0.20
    ground_geometry_preference_margin_m: float = 0.10
    occlusion_gap_m: float = 0.35
    max_ground_penetration_m: float = 0.10
    max_ground_support_distance_m: float = 0.35
    ground_class_ids: tuple[int, ...] = (2, 11)
    pole_class_ids: tuple[int, ...] = ()
    excluded_pole_class_ids: tuple[int, ...] = (3, 4, 5)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "PoleSearchParameters":
        names = cls.__dataclass_fields__.keys()
        normalized = {name: values[name] for name in names if name in values}
        for name in ("ground_class_ids", "pole_class_ids", "excluded_pole_class_ids"):
            if name in normalized:
                normalized[name] = tuple(int(value) for value in normalized[name])
        return cls(**normalized)

    def validate(self) -> None:
        positive = {
            "search_radius_m": self.search_radius_m,
            "max_drop_m": self.max_drop_m,
            "xy_voxel_m": self.xy_voxel_m,
            "z_bin_m": self.z_bin_m,
            "axis_cluster_radius_m": self.axis_cluster_radius_m,
            "axis_inlier_radius_m": self.axis_inlier_radius_m,
            "min_vertical_span_m": self.min_vertical_span_m,
            "max_observed_z_gap_m": self.max_observed_z_gap_m,
            "ground_search_radius_m": self.ground_search_radius_m,
            "ground_core_radius_m": self.ground_core_radius_m,
            "ground_cell_size_m": self.ground_cell_size_m,
            "ground_max_rmse_m": self.ground_max_rmse_m,
            "ground_geometry_preference_margin_m": (
                self.ground_geometry_preference_margin_m
            ),
            "max_ground_penetration_m": self.max_ground_penetration_m,
            "max_ground_support_distance_m": self.max_ground_support_distance_m,
            "geometry_remote_max_axis_rmse_m": self.geometry_remote_max_axis_rmse_m,
            "geometry_remote_max_ground_rmse_m": self.geometry_remote_max_ground_rmse_m,
            "direct_max_axis_sign_distance_m": self.direct_max_axis_sign_distance_m,
            "max_axis_sign_distance_m": self.max_axis_sign_distance_m,
            "horizontal_connection_radius_m": self.horizontal_connection_radius_m,
            "horizontal_connection_z_tolerance_m": (
                self.horizontal_connection_z_tolerance_m
            ),
            "horizontal_connection_above_tolerance_m": (
                self.horizontal_connection_above_tolerance_m
            ),
            "horizontal_connection_bin_m": self.horizontal_connection_bin_m,
        }
        invalid = [name for name, value in positive.items() if not math.isfinite(value) or value <= 0]
        if invalid:
            raise ValueError(f"Pole parameters must be finite and positive: {', '.join(invalid)}")
        if (
            self.min_vertical_bins < 2
            or self.min_consecutive_vertical_bins < 2
            or self.min_points < 3
        ):
            raise ValueError("Pole min_vertical_bins/min_points are too small")
        if not 0.0 <= self.min_vertical_occupancy_ratio <= 1.0:
            raise ValueError("min_vertical_occupancy_ratio must be between 0 and 1")
        if not 0.0 <= self.middle_support_start_fraction < 1.0:
            raise ValueError("middle_support_start_fraction must be in [0, 1)")
        if not 0.0 <= self.min_middle_support_coverage_ratio <= 1.0:
            raise ValueError("min_middle_support_coverage_ratio must be between 0 and 1")
        if not 0.0 <= self.preferred_min_completeness_ratio <= 1.0:
            raise ValueError("preferred_min_completeness_ratio must be between 0 and 1")
        if (
            not math.isfinite(self.geometry_ground_clearance_m)
            or self.geometry_ground_clearance_m < 0.0
        ):
            raise ValueError(
                "geometry_ground_clearance_m must be finite and non-negative"
            )
        if (
            not math.isfinite(self.geometry_remote_min_completeness_ratio)
            or not 0.0 <= self.geometry_remote_min_completeness_ratio <= 1.0
        ):
            raise ValueError(
                "geometry_remote_min_completeness_ratio must be between 0 and 1"
            )
        if not 0.0 <= self.min_horizontal_connection_coverage <= 1.0:
            raise ValueError("min_horizontal_connection_coverage must be between 0 and 1")
        if self.horizontal_connection_min_points_per_bin < 1:
            raise ValueError("horizontal_connection_min_points_per_bin must be positive")
        if self.direct_max_axis_sign_distance_m > self.max_axis_sign_distance_m:
            raise ValueError(
                "direct_max_axis_sign_distance_m cannot exceed max_axis_sign_distance_m"
            )
        if not 0.0 <= self.ground_cell_quantile <= 1.0:
            raise ValueError("ground_cell_quantile must be between 0 and 1")
        if self.ground_core_radius_m > self.ground_search_radius_m:
            raise ValueError("ground_core_radius_m cannot exceed ground_search_radius_m")
        if self.ground_exclusion_radius_m < 0.0:
            raise ValueError("ground_exclusion_radius_m cannot be negative")
        if self.ground_exclusion_radius_m >= self.ground_core_radius_m:
            raise ValueError("ground_exclusion_radius_m must be smaller than ground_core_radius_m")
        if not 0.0 <= self.max_axis_tilt_deg <= 90.0:
            raise ValueError("max_axis_tilt_deg must be between 0 and 90")
        if not 0.0 <= self.max_ground_class_fraction <= 1.0:
            raise ValueError("max_ground_class_fraction must be between 0 and 1")
        if not math.isfinite(self.min_ground_drop_m) or self.min_ground_drop_m < 0.0:
            raise ValueError("min_ground_drop_m must be finite and non-negative")
        for class_id in (
            *self.ground_class_ids,
            *self.pole_class_ids,
            *self.excluded_pole_class_ids,
        ):
            if class_id < 0 or class_id > 255:
                raise ValueError("LAS class IDs must be between 0 and 255")


class PoleSearchWorkspace:
    """Reusable spatial index and invariant masks for one pole neighborhood."""

    def __init__(self, neighborhood_xyz: np.ndarray) -> None:
        points = np.asarray(neighborhood_xyz, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("neighborhood_xyz must have shape (N, 3)")
        self.points = points
        self._finite_xy_indices = np.flatnonzero(
            np.all(np.isfinite(points[:, :2]), axis=1)
        ).astype(np.int64, copy=False)
        self._xy_tree = (
            cKDTree(points[self._finite_xy_indices, :2])
            if self._finite_xy_indices.size
            else None
        )
        self._terrain_masks: dict[
            tuple[tuple[float, float, float], PoleSearchParameters],
            np.ndarray,
        ] = {}

    def _check_points(self, points: np.ndarray) -> None:
        same_layout = (
            points.__array_interface__["data"][0]
            == self.points.__array_interface__["data"][0]
            and points.strides == self.points.strides
        )
        if (
            points.shape != self.points.shape
            or not same_layout
        ):
            raise ValueError("PoleSearchWorkspace does not match neighborhood_xyz")

    def query_radius(
        self,
        points: np.ndarray,
        center_xy: np.ndarray,
        radius_m: float,
    ) -> np.ndarray:
        """Return original point indices in an exact XY-radius query."""

        self._check_points(points)
        if self._xy_tree is None:
            return np.empty((0,), dtype=np.int64)
        local = np.asarray(
            self._xy_tree.query_ball_point(
                np.asarray(center_xy, dtype=np.float64),
                float(radius_m),
            ),
            dtype=np.int64,
        )
        return self._finite_xy_indices[local]

    def query_connection_capsule(
        self,
        points: np.ndarray,
        start_xy: np.ndarray,
        end_xy: np.ndarray,
        radius_m: float,
    ) -> np.ndarray:
        """Return an exact superset of an XY line-segment capsule."""

        self._check_points(points)
        if self._xy_tree is None:
            return np.empty((0,), dtype=np.int64)
        start = np.asarray(start_xy, dtype=np.float64)
        end = np.asarray(end_xy, dtype=np.float64)
        distance = float(np.linalg.norm(end - start))
        if distance <= 1e-9:
            return self.query_radius(points, start, radius_m)

        # Every point in the capsule lies within this radius of its nearest
        # sample. The original analytic mask is still applied afterwards, so
        # this query changes only the amount of data scanned, not the result.
        sample_count = max(2, int(math.ceil(distance / max(radius_m, 1e-6))) + 1)
        fractions = np.linspace(0.0, 1.0, sample_count, dtype=np.float64)
        samples = start[None, :] + (fractions[:, None] * (end - start)[None, :])
        sample_spacing = distance / float(sample_count - 1)
        query_radius = math.hypot(radius_m, sample_spacing * 0.5)
        groups = self._xy_tree.query_ball_point(samples, query_radius)
        nonempty = [
            np.asarray(group, dtype=np.int64)
            for group in groups
            if len(group)
        ]
        if not nonempty:
            return np.empty((0,), dtype=np.int64)
        local = np.unique(np.concatenate(nonempty))
        return self._finite_xy_indices[local]

    def terrain_clearance_mask(
        self,
        points: np.ndarray,
        sign_xyz: np.ndarray,
        parameters: PoleSearchParameters,
    ) -> np.ndarray:
        """Cache the expensive geometry-only terrain mask across corridor retries."""

        self._check_points(points)
        sign_key = tuple(float(value) for value in np.asarray(sign_xyz, dtype=np.float64))
        key = (sign_key, parameters)
        cached = self._terrain_masks.get(key)
        if cached is None:
            cached = _geometry_terrain_clearance_mask(points, sign_xyz, parameters)
            cached.setflags(write=False)
            self._terrain_masks[key] = cached
        return cached


@dataclass(frozen=True)
class GroundEstimate:
    z: float
    rmse_m: float
    cell_count: int
    candidate_cell_count: int
    method: str
    support_xyz: np.ndarray
    plane_coefficients: np.ndarray
    reference_xy: np.ndarray


@dataclass(frozen=True)
class PoleAxisCandidate:
    base_xyz: np.ndarray
    axis_direction: np.ndarray
    point_indices: np.ndarray
    point_count: int
    vertical_span_m: float
    vertical_bin_count: int
    observed_z_min: float
    observed_z_max: float
    longest_consecutive_bin_count: int
    max_observed_z_gap_m: float
    vertical_occupancy_ratio: float
    middle_support_bin_count: int | None
    middle_expected_bin_count: int | None
    middle_support_coverage_ratio: float | None
    completeness_ratio: float | None
    association_distance_m: float
    horizontal_connection_bin_count: int | None
    horizontal_connection_expected_bin_count: int | None
    horizontal_connection_coverage_ratio: float | None
    radial_rmse_m: float
    lowest_observed_z: float
    ground_z: float | None
    ground_rmse_m: float | None
    bottom_gap_m: float | None
    occluded_bottom: bool | None
    occlusion_status: str
    method: str
    status: str
    score: float
    dominant_class_id: int | None
    dominant_class_fraction: float | None
    multi_return_fraction: float | None
    ground_estimate: GroundEstimate | None
    axis_stabilized: bool = False
    axis_bin_inlier_count: int | None = None
    axis_bin_count: int | None = None
    ground_support_distance_m: float | None = None


@dataclass(frozen=True)
class PoleSearchResult:
    representative_xyz: np.ndarray
    candidates: tuple[PoleAxisCandidate, ...]
    pole_type: str
    method: str
    status: str
    occluded_bottom: bool | None
    occlusion_status: str

    @property
    def point_indices(self) -> np.ndarray:
        if not self.candidates:
            return np.empty((0,), dtype=np.int64)
        return np.unique(np.concatenate([item.point_indices for item in self.candidates])).astype(
            np.int64,
            copy=False,
        )


@dataclass(frozen=True)
class _VerticalContinuity:
    observed_z_min: float
    observed_z_max: float
    vertical_bin_count: int
    longest_consecutive_bin_count: int
    max_observed_z_gap_m: float
    vertical_occupancy_ratio: float


@dataclass(frozen=True)
class _AxisFitResult:
    coefficients: np.ndarray
    axis_direction: np.ndarray
    inlier_indices: np.ndarray
    radial_rmse_m: float
    z_reference: float
    continuity: _VerticalContinuity
    stabilized: bool
    bin_inlier_count: int
    bin_count: int


@dataclass(frozen=True)
class _HorizontalConnection:
    occupied_bin_count: int
    expected_bin_count: int
    coverage_ratio: float


def blocks_intersecting_bounds(
    pointcloud_files: Iterable[dict[str, Any]],
    minimum_xyz: np.ndarray,
    maximum_xyz: np.ndarray,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return catalog blocks whose 3D AABB overlaps the requested bounds."""

    minimum = np.asarray(minimum_xyz, dtype=np.float64)
    maximum = np.asarray(maximum_xyz, dtype=np.float64)
    if minimum.shape != (3,) or maximum.shape != (3,) or np.any(minimum > maximum):
        raise ValueError("minimum_xyz and maximum_xyz must be ordered three-vectors")

    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pointcloud_file in pointcloud_files:
        for block in pointcloud_file.get("blocks", []):
            block_min = block.get("min")
            block_max = block.get("max")
            if not block_min or not block_max or any(value is None for value in [*block_min, *block_max]):
                continue
            block_min_array = np.asarray(block_min, dtype=np.float64)
            block_max_array = np.asarray(block_max, dtype=np.float64)
            if np.all(block_max_array >= minimum) and np.all(block_min_array <= maximum):
                matches.append((pointcloud_file, block))
    return matches


def _connected_components(points_xy: np.ndarray, radius: float) -> list[np.ndarray]:
    if points_xy.shape[0] == 0:
        return []
    parents = np.arange(points_xy.shape[0], dtype=np.int32)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = int(parents[index])
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first, second in cKDTree(points_xy).query_pairs(radius):
        union(int(first), int(second))

    groups: dict[int, list[int]] = {}
    for index in range(points_xy.shape[0]):
        groups.setdefault(find(index), []).append(index)
    return [np.asarray(indices, dtype=np.int64) for indices in groups.values()]


def _vertical_continuity(
    z_values: np.ndarray,
    z_bin_m: float,
) -> _VerticalContinuity:
    """Summarize whether observations form a genuinely supported vertical column."""

    observed = np.sort(np.asarray(z_values, dtype=np.float64))
    if observed.ndim != 1 or observed.size == 0 or not np.all(np.isfinite(observed)):
        raise ValueError("z_values must contain finite observations")

    observed_z_min = float(observed[0])
    observed_z_max = float(observed[-1])
    bin_ids = np.unique(
        np.floor(((observed - observed_z_min) / z_bin_m) + 1e-9).astype(np.int64)
    )
    longest_run = 1
    current_run = 1
    for difference in np.diff(bin_ids):
        if int(difference) == 1:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 1

    expected_bins = int(bin_ids[-1] - bin_ids[0] + 1)
    max_gap = float(np.max(np.diff(observed))) if observed.size > 1 else 0.0
    return _VerticalContinuity(
        observed_z_min=observed_z_min,
        observed_z_max=observed_z_max,
        vertical_bin_count=int(bin_ids.size),
        longest_consecutive_bin_count=int(longest_run),
        max_observed_z_gap_m=max_gap,
        vertical_occupancy_ratio=float(bin_ids.size / expected_bins),
    )


def _middle_support_coverage(
    z_values: np.ndarray,
    ground_z: float,
    sign_z: float,
    parameters: PoleSearchParameters,
) -> tuple[int, int, float]:
    """Measure vertical-bin support through the middle of the expected pole height.

    The lowest part of a pole is deliberately omitted so a vehicle, guard rail,
    vegetation, or another foreground object can hide its base.  The remaining
    interval must still contain enough LiDAR evidence to justify extrapolating
    the fitted axis to ground.
    """

    middle_z_min = ground_z + (
        (sign_z - ground_z) * parameters.middle_support_start_fraction
    )
    middle_z_max = sign_z
    if middle_z_max <= middle_z_min:
        return 0, 0, 1.0

    expected_bins = max(
        1,
        int(math.floor((middle_z_max - middle_z_min) / parameters.z_bin_m)) + 1,
    )
    observed = np.asarray(z_values, dtype=np.float64)
    observed = observed[(observed >= middle_z_min) & (observed <= middle_z_max)]
    if observed.size == 0:
        return 0, expected_bins, 0.0
    bin_ids = np.floor(
        ((observed - middle_z_min) / parameters.z_bin_m) + 1e-9
    ).astype(np.int64)
    bin_ids = np.unique(bin_ids[(bin_ids >= 0) & (bin_ids < expected_bins)])
    observed_bins = int(bin_ids.size)
    return observed_bins, expected_bins, float(observed_bins / expected_bins)


def _horizontal_connection_coverage(
    points_xyz: np.ndarray,
    sign_xyz: np.ndarray,
    attachment_xy: np.ndarray,
    parameters: PoleSearchParameters,
    classifications: np.ndarray | None,
) -> _HorizontalConnection:
    """Measure 3-D evidence along a horizontal sign-to-support connection."""

    delta_xy = np.asarray(attachment_xy, dtype=np.float64) - sign_xyz[:2]
    distance = float(np.linalg.norm(delta_xy))
    if distance <= 1e-9:
        return _HorizontalConnection(1, 1, 1.0)
    direction = delta_xy / distance
    relative_xy = points_xyz[:, :2] - sign_xyz[None, :2]
    along = relative_xy @ direction
    perpendicular = np.abs(
        (relative_xy[:, 0] * direction[1])
        - (relative_xy[:, 1] * direction[0])
    )
    mask = (
        np.all(np.isfinite(points_xyz), axis=1)
        & (along >= 0.0)
        & (along <= distance)
        & (perpendicular <= parameters.horizontal_connection_radius_m)
        & (
            points_xyz[:, 2]
            >= float(sign_xyz[2]) - parameters.horizontal_connection_z_tolerance_m
        )
        & (
            points_xyz[:, 2]
            <= float(sign_xyz[2]) + parameters.horizontal_connection_above_tolerance_m
        )
    )
    if classifications is not None and parameters.ground_class_ids:
        mask &= ~np.isin(classifications, parameters.ground_class_ids)

    expected_bins = max(1, int(math.ceil(distance / parameters.horizontal_connection_bin_m)))
    if not np.any(mask):
        return _HorizontalConnection(0, expected_bins, 0.0)
    bin_ids = np.minimum(
        expected_bins - 1,
        np.floor(along[mask] / parameters.horizontal_connection_bin_m).astype(np.int64),
    )
    counts = np.bincount(bin_ids, minlength=expected_bins)
    occupied_bins = int(
        np.count_nonzero(counts >= parameters.horizontal_connection_min_points_per_bin)
    )
    return _HorizontalConnection(
        occupied_bin_count=occupied_bins,
        expected_bin_count=expected_bins,
        coverage_ratio=float(occupied_bins / expected_bins),
    )


def _stable_axis_bin_indices(
    bin_medians: np.ndarray,
    parameters: PoleSearchParameters,
) -> np.ndarray:
    """Select the longest physically continuous section of a shaft.

    A vehicle, vegetation, or a horizontal arm can dominate the median in only
    one height range. A real tilted pole moves smoothly from one z bin to the
    next, whereas that contamination normally introduces an abrupt lateral
    step. Keeping the longest coherent section avoids extrapolating that bend
    to the ground without forcing genuinely tilted poles to be vertical.
    """

    if bin_medians.shape[0] <= parameters.min_vertical_bins:
        return np.arange(bin_medians.shape[0], dtype=np.int64)
    delta_z = np.diff(bin_medians[:, 2])
    delta_xy = np.linalg.norm(np.diff(bin_medians[:, :2], axis=0), axis=1)
    noise_allowance = min(
        parameters.axis_inlier_radius_m,
        max(0.06, parameters.xy_voxel_m * 0.75),
    )
    smooth_tilt = math.tan(math.radians(parameters.max_axis_tilt_deg)) * np.maximum(
        delta_z,
        0.0,
    )
    breaks = np.flatnonzero(
        (delta_z <= 0.0)
        | (delta_z > parameters.max_observed_z_gap_m)
        | (delta_xy > noise_allowance + smooth_tilt)
    )
    starts = np.concatenate((np.asarray([0]), breaks + 1))
    stops = np.concatenate((breaks + 1, np.asarray([bin_medians.shape[0]])))
    sections: list[np.ndarray] = []
    for start, stop in zip(starts, stops):
        indices = np.arange(int(start), int(stop), dtype=np.int64)
        if (
            indices.size >= parameters.min_vertical_bins
            and float(np.ptp(bin_medians[indices, 2]))
            >= parameters.min_vertical_span_m
        ):
            sections.append(indices)
    if not sections:
        return np.arange(bin_medians.shape[0], dtype=np.int64)
    return max(
        sections,
        key=lambda indices: (
            float(np.ptp(bin_medians[indices, 2])),
            int(indices.size),
            float(np.median(bin_medians[indices, 2])),
        ),
    )


def _robust_bin_axis_coefficients(
    bin_medians: np.ndarray,
    initial_indices: np.ndarray,
    parameters: PoleSearchParameters,
) -> tuple[np.ndarray, float, np.ndarray] | None:
    """Use a Theil-Sen seed and capped MAD refits for x(z), y(z)."""

    initial = bin_medians[np.asarray(initial_indices, dtype=np.int64)]
    z_reference = float(np.median(initial[:, 2]))
    pair_i, pair_j = np.triu_indices(initial.shape[0], k=1)
    pair_dz = initial[pair_j, 2] - initial[pair_i, 2]
    minimum_pair_span = min(
        1.0,
        max(parameters.z_bin_m * 4.0, parameters.min_vertical_span_m * 0.5),
    )
    valid_pairs = pair_dz >= minimum_pair_span
    if not np.any(valid_pairs):
        valid_pairs = pair_dz > 0.0
    if not np.any(valid_pairs):
        return None
    slopes = (
        initial[pair_j[valid_pairs], :2] - initial[pair_i[valid_pairs], :2]
    ) / pair_dz[valid_pairs, None]
    slope = np.median(slopes, axis=0)
    intercept = np.median(
        initial[:, :2] - ((initial[:, 2] - z_reference)[:, None] * slope[None, :]),
        axis=0,
    )
    coefficients = np.vstack((slope, intercept))
    design_all = np.column_stack(
        (
            bin_medians[:, 2] - z_reference,
            np.ones(bin_medians.shape[0], dtype=np.float64),
        )
    )
    residual_floor = min(
        parameters.axis_inlier_radius_m,
        max(0.06, parameters.xy_voxel_m * 0.75),
    )
    seed_mask = np.zeros(bin_medians.shape[0], dtype=bool)
    seed_mask[np.asarray(initial_indices, dtype=np.int64)] = True
    keep = seed_mask
    for _ in range(5):
        residuals = np.linalg.norm(
            bin_medians[:, :2] - (design_all @ coefficients),
            axis=1,
        )
        seed_residuals = residuals[keep]
        median = float(np.median(seed_residuals))
        mad = float(np.median(np.abs(seed_residuals - median)))
        threshold = min(
            parameters.axis_inlier_radius_m,
            max(residual_floor, median + (3.0 * 1.4826 * mad)),
        )
        next_keep = residuals <= threshold
        if int(next_keep.sum()) < parameters.min_vertical_bins:
            next_keep = keep
        design = design_all[next_keep]
        coefficients, *_ = np.linalg.lstsq(
            design,
            bin_medians[next_keep, :2],
            rcond=None,
        )
        if np.array_equal(next_keep, keep):
            keep = next_keep
            break
        keep = next_keep
    return coefficients, z_reference, keep


def _robust_axis_fit(
    points_xyz: np.ndarray,
    source_indices: np.ndarray,
    parameters: PoleSearchParameters,
) -> _AxisFitResult | None:
    """Fit a height-stable x(z), y(z) shaft axis and return source inliers."""

    if points_xyz.shape[0] < parameters.min_points:
        return None
    z_min = float(points_xyz[:, 2].min())
    z_bin_ids = np.floor((points_xyz[:, 2] - z_min) / parameters.z_bin_m).astype(np.int64)
    unique_bins = np.unique(z_bin_ids)
    if unique_bins.size < parameters.min_vertical_bins:
        return None

    bin_medians = np.asarray(
        [np.median(points_xyz[z_bin_ids == bin_id], axis=0) for bin_id in unique_bins],
        dtype=np.float64,
    )
    if float(np.ptp(bin_medians[:, 2])) < parameters.min_vertical_span_m:
        return None

    stable_indices = _stable_axis_bin_indices(bin_medians, parameters)
    fitted = _robust_bin_axis_coefficients(
        bin_medians,
        stable_indices,
        parameters,
    )
    if fitted is None:
        return None
    coefficients, z_reference, kept_bins = fitted
    slope_xy = coefficients[0]
    tilt_deg = math.degrees(math.atan(float(np.linalg.norm(slope_xy))))
    if tilt_deg > parameters.max_axis_tilt_deg:
        return None

    design_points = np.column_stack(
        (points_xyz[:, 2] - z_reference, np.ones(points_xyz.shape[0], dtype=np.float64))
    )
    predicted_xy = design_points @ coefficients
    radial_residuals = np.linalg.norm(points_xyz[:, :2] - predicted_xy, axis=1)
    point_inlier_radius = min(
        parameters.axis_inlier_radius_m,
        max(0.10, parameters.xy_voxel_m * 1.5),
    )
    point_inliers = radial_residuals <= point_inlier_radius
    if int(point_inliers.sum()) < parameters.min_points:
        return None

    inlier_points = points_xyz[point_inliers]
    inlier_indices = source_indices[point_inliers]
    span = float(np.ptp(inlier_points[:, 2]))
    continuity = _vertical_continuity(inlier_points[:, 2], parameters.z_bin_m)
    if (
        span < parameters.min_vertical_span_m
        or continuity.vertical_bin_count < parameters.min_vertical_bins
        or continuity.longest_consecutive_bin_count
        < parameters.min_consecutive_vertical_bins
        or continuity.max_observed_z_gap_m > parameters.max_observed_z_gap_m
        or continuity.vertical_occupancy_ratio < parameters.min_vertical_occupancy_ratio
    ):
        return None

    quality_inliers = radial_residuals <= parameters.axis_inlier_radius_m
    radial_rmse = float(
        np.sqrt(np.mean(np.square(radial_residuals[quality_inliers])))
    )
    axis_direction = np.asarray([slope_xy[0], slope_xy[1], 1.0], dtype=np.float64)
    axis_direction /= np.linalg.norm(axis_direction)
    return _AxisFitResult(
        coefficients=coefficients,
        axis_direction=axis_direction,
        inlier_indices=inlier_indices,
        radial_rmse_m=radial_rmse,
        z_reference=z_reference,
        continuity=continuity,
        stabilized=bool(
            stable_indices.size < bin_medians.shape[0]
            or int(kept_bins.sum()) < bin_medians.shape[0]
        ),
        bin_inlier_count=int(kept_bins.sum()),
        bin_count=int(bin_medians.shape[0]),
    )


def _fit_local_ground_hypothesis(
    points: np.ndarray,
    pole_xy: np.ndarray,
    radial: np.ndarray,
    candidate_mask: np.ndarray,
    parameters: PoleSearchParameters,
    method: str,
) -> GroundEstimate | None:
    """Fit one ground hypothesis without silently relaxing its cell minimum."""

    mask = np.asarray(candidate_mask, dtype=bool).copy()
    core_min_radius = min(
        parameters.ground_core_radius_m,
        parameters.ground_exclusion_radius_m + (parameters.ground_cell_size_m * 0.5),
    )
    required_core_cells = parameters.ground_min_cells * (
        1 if method.startswith("classified") else 2
    )
    for core_radius in np.linspace(core_min_radius, parameters.ground_core_radius_m, 6):
        core_mask = mask & (radial <= float(core_radius))
        if not np.any(core_mask):
            continue
        core_cells = np.floor(
            (points[core_mask, :2] - pole_xy[None, :]) / parameters.ground_cell_size_m
        ).astype(np.int64)
        if np.unique(core_cells, axis=0).shape[0] >= required_core_cells:
            mask = core_mask
            method += "_adaptive_core"
            break

    ground_points = points[mask]
    if ground_points.shape[0] < parameters.ground_min_cells:
        return None
    cells = np.floor(
        (ground_points[:, :2] - pole_xy[None, :]) / parameters.ground_cell_size_m
    ).astype(np.int64)
    _unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    ordered_groups = inverse[order]
    group_starts = np.concatenate(
        (
            np.asarray([0], dtype=np.int64),
            np.flatnonzero(np.diff(ordered_groups)) + 1,
        )
    )
    group_ends = np.concatenate(
        (group_starts[1:], np.asarray([order.size], dtype=np.int64))
    )
    cell_quantile = (
        0.50 if method.startswith("classified") else parameters.ground_cell_quantile
    )
    cell_samples: list[np.ndarray] = []
    for start, end in zip(group_starts, group_ends):
        cell_points = ground_points[order[int(start):int(end)]]
        cell_samples.append(
            np.asarray(
                [
                    float(np.median(cell_points[:, 0])),
                    float(np.median(cell_points[:, 1])),
                    float(np.quantile(cell_points[:, 2], cell_quantile)),
                ],
                dtype=np.float64,
            )
        )
    samples = np.asarray(cell_samples, dtype=np.float64)
    if samples.shape[0] < parameters.ground_min_cells:
        return None

    if method.startswith("classified"):
        keep = np.ones(samples.shape[0], dtype=bool)
    else:
        upper_z = float(np.quantile(samples[:, 2], 0.65))
        lower_z = float(np.quantile(samples[:, 2], 0.03)) - parameters.ground_max_rmse_m
        keep = (samples[:, 2] <= upper_z) & (samples[:, 2] >= lower_z)
        if int(keep.sum()) < parameters.ground_min_cells:
            keep = np.ones(samples.shape[0], dtype=bool)

    for _ in range(5):
        design = np.column_stack(
            (
                samples[keep, 0] - pole_xy[0],
                samples[keep, 1] - pole_xy[1],
                np.ones(int(keep.sum()), dtype=np.float64),
            )
        )
        coefficients, *_ = np.linalg.lstsq(design, samples[keep, 2], rcond=None)
        all_design = np.column_stack(
            (
                samples[:, 0] - pole_xy[0],
                samples[:, 1] - pole_xy[1],
                np.ones(samples.shape[0], dtype=np.float64),
            )
        )
        residuals = samples[:, 2] - (all_design @ coefficients)
        median = float(np.median(residuals[keep]))
        mad = float(np.median(np.abs(residuals[keep] - median)))
        threshold = max(
            0.05,
            min(parameters.ground_max_rmse_m, 3.0 * 1.4826 * mad),
        )
        next_keep = np.abs(residuals - median) <= threshold
        if (
            int(next_keep.sum()) < parameters.ground_min_cells
            or np.array_equal(next_keep, keep)
        ):
            break
        keep = next_keep

    if int(keep.sum()) < parameters.ground_min_cells:
        return None
    final_design = np.column_stack(
        (
            samples[keep, 0] - pole_xy[0],
            samples[keep, 1] - pole_xy[1],
            np.ones(int(keep.sum()), dtype=np.float64),
        )
    )
    coefficients, *_ = np.linalg.lstsq(
        final_design,
        samples[keep, 2],
        rcond=None,
    )
    final_residuals = samples[keep, 2] - (final_design @ coefficients)
    rmse = float(np.sqrt(np.mean(np.square(final_residuals))))
    if not math.isfinite(rmse) or rmse > parameters.ground_max_rmse_m:
        return None
    return GroundEstimate(
        z=float(coefficients[2]),
        rmse_m=rmse,
        cell_count=int(keep.sum()),
        candidate_cell_count=int(samples.shape[0]),
        method=f"robust_low_cell_plane_{method}",
        support_xyz=np.asarray(samples[keep], dtype=np.float64),
        plane_coefficients=np.asarray(coefficients, dtype=np.float64),
        reference_xy=np.asarray(pole_xy, dtype=np.float64),
    )


def estimate_local_ground(
    neighborhood_xyz: np.ndarray,
    pole_xy: np.ndarray,
    sign_z: float,
    parameters: PoleSearchParameters,
    classifications: np.ndarray | None = None,
) -> GroundEstimate | None:
    """Compare classified and geometric local-ground hypotheses.

    Classified road points can lie across a kerb from a pole planted on an
    unclassified sidewalk.  Both surfaces are therefore fitted independently;
    a materially closer geometric surface wins, while classified ground remains
    the tie-breaker.
    """

    points = np.asarray(neighborhood_xyz, dtype=np.float64)
    pole_xy = np.asarray(pole_xy, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] == 0:
        return None
    radial = np.linalg.norm(points[:, :2] - pole_xy[None, :], axis=1)
    base_mask = (
        np.all(np.isfinite(points), axis=1)
        & (radial >= parameters.ground_exclusion_radius_m)
        & (radial <= parameters.ground_search_radius_m)
        & (points[:, 2] <= sign_z)
        & (points[:, 2] >= sign_z - parameters.max_drop_m)
    )
    geometry_mask = base_mask & (
        points[:, 2] <= sign_z - parameters.min_ground_drop_m
    )
    geometry = _fit_local_ground_hypothesis(
        points,
        pole_xy,
        radial,
        geometry_mask,
        parameters,
        "geometry",
    )

    classified = None
    if classifications is not None:
        classes = np.asarray(classifications, dtype=np.int16)
        if classes.shape != (points.shape[0],):
            raise ValueError("classifications must have one value per neighborhood point")
        if parameters.ground_class_ids:
            classified_mask = base_mask & np.isin(classes, parameters.ground_class_ids)
            classified = _fit_local_ground_hypothesis(
                points,
                pole_xy,
                radial,
                classified_mask,
                parameters,
                "classified",
            )

    if classified is None:
        return geometry
    if geometry is None:
        return classified
    geometry_radius = float(
        np.median(np.linalg.norm(geometry.support_xyz[:, :2] - pole_xy[None, :], axis=1))
    )
    classified_radius = float(
        np.median(
            np.linalg.norm(classified.support_xyz[:, :2] - pole_xy[None, :], axis=1)
        )
    )
    if (
        geometry_radius + parameters.ground_geometry_preference_margin_m
        < classified_radius
    ):
        return geometry
    return classified


def _geometry_terrain_clearance_mask(
    points_xyz: np.ndarray,
    sign_xyz: np.ndarray,
    parameters: PoleSearchParameters,
) -> np.ndarray:
    """Keep points sufficiently above the local low-cell surface.

    Semantic classes normally keep road returns out of the shaft search.  In
    geometry-only mode, estimate the same separation from the low quantile in
    each XY terrain cell.  This mask is used only to discover pole axes; ground
    fitting still receives the original, unfiltered neighborhood.
    """

    if parameters.geometry_ground_clearance_m <= 0.0:
        return np.ones(points_xyz.shape[0], dtype=bool)
    radial = np.linalg.norm(points_xyz[:, :2] - sign_xyz[None, :2], axis=1)
    support_mask = (
        np.all(np.isfinite(points_xyz), axis=1)
        & (radial <= parameters.search_radius_m)
        & (points_xyz[:, 2] >= sign_xyz[2] - parameters.max_drop_m)
        & (points_xyz[:, 2] <= sign_xyz[2] + parameters.top_margin_m)
    )
    support_indices = np.flatnonzero(support_mask)
    if support_indices.size == 0:
        return np.ones(points_xyz.shape[0], dtype=bool)

    support_points = points_xyz[support_indices]
    cells = np.floor(
        (support_points[:, :2] - sign_xyz[None, :2])
        / parameters.ground_cell_size_m
    ).astype(np.int64)
    unique_cells, inverse, counts = np.unique(
        cells,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    order = np.lexsort((support_points[:, 2], inverse))
    starts = np.cumsum(np.concatenate(([0], counts[:-1]))).astype(np.int64)
    quantile_offsets = np.floor(
        parameters.ground_cell_quantile * np.maximum(0, counts - 1)
    ).astype(np.int64)
    cell_low_z = support_points[order[starts + quantile_offsets], 2]
    cell_centers_xy = (
        (unique_cells.astype(np.float64) + 0.5)
        * parameters.ground_cell_size_m
        + sign_xyz[None, :2]
    )
    cell_tree = cKDTree(cell_centers_xy)
    terrain_z = np.empty_like(cell_low_z)
    for cell_index, neighbors in enumerate(
        cell_tree.query_ball_point(
            cell_centers_xy,
            parameters.ground_core_radius_m,
        )
    ):
        terrain_z[cell_index] = float(
            np.quantile(
                cell_low_z[np.asarray(neighbors, dtype=np.int64)],
                parameters.ground_cell_quantile,
            )
        )

    keep = np.ones(points_xyz.shape[0], dtype=bool)
    keep[support_indices] = (
        support_points[:, 2]
        > terrain_z[inverse] + parameters.geometry_ground_clearance_m
    )
    return keep


def _eligible_axis_cells(
    candidate_points: np.ndarray,
    sign_xyz: np.ndarray,
    parameters: PoleSearchParameters,
) -> tuple[np.ndarray, list[np.ndarray], np.ndarray]:
    """Aggregate vertical XY cells in O(N log N), without an N-by-cell scan."""

    xy_cells = np.floor(
        (candidate_points[:, :2] - sign_xyz[None, :2])
        / parameters.xy_voxel_m
    ).astype(np.int64)
    _unique_cells, inverse, counts = np.unique(
        xy_cells,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    order = np.argsort(inverse, kind="stable")
    starts = np.cumsum(np.concatenate(([0], counts[:-1]))).astype(np.int64)
    minimum_members = max(3, parameters.min_points // 5)
    minimum_bins = max(3, parameters.min_vertical_bins // 2)

    eligible_centers: list[np.ndarray] = []
    eligible_indices: list[np.ndarray] = []
    eligible_quality: list[float] = []
    for cell_index in np.flatnonzero(counts >= minimum_members):
        start = int(starts[cell_index])
        stop = start + int(counts[cell_index])
        members = order[start:stop]
        member_points = candidate_points[members]
        span = float(np.ptp(member_points[:, 2]))
        if span < parameters.min_vertical_span_m * 0.6:
            continue
        z_bins = np.unique(
            np.floor(
                (member_points[:, 2] - float(member_points[:, 2].min()))
                / parameters.z_bin_m
            ).astype(np.int64)
        )
        if z_bins.size < minimum_bins:
            continue
        longest_run = 1
        current_run = 1
        for difference in np.diff(z_bins):
            if int(difference) == 1:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 1
        expected_bins = max(1, int(z_bins[-1] - z_bins[0] + 1))
        occupancy = float(z_bins.size / expected_bins)
        eligible_centers.append(np.median(member_points[:, :2], axis=0))
        eligible_indices.append(members.astype(np.int64, copy=False))
        eligible_quality.append(
            float(longest_run)
            + occupancy
            + min(span, parameters.max_drop_m) / max(parameters.z_bin_m, 1e-9)
            + (0.05 * math.sqrt(members.size))
        )

    return (
        np.asarray(eligible_centers, dtype=np.float64).reshape(-1, 2),
        eligible_indices,
        np.asarray(eligible_quality, dtype=np.float64),
    )


def _coverage_seed_indices(
    eligible_centers: np.ndarray,
    eligible_quality: np.ndarray,
    sign_xy: np.ndarray,
    parameters: PoleSearchParameters,
) -> np.ndarray:
    """Choose spatially covering seeds without a per-component fixed cap."""

    if eligible_centers.shape[0] == 0:
        return np.empty((0,), dtype=np.int64)
    distances = np.linalg.norm(eligible_centers - sign_xy[None, :], axis=1)
    direct_tier = (
        distances > parameters.direct_max_axis_sign_distance_m
    ).astype(np.int8)
    order = np.lexsort((distances, -eligible_quality, direct_tier))
    tree = cKDTree(eligible_centers)
    covered = np.zeros(eligible_centers.shape[0], dtype=bool)
    selected: list[int] = []
    coverage_radius = max(
        parameters.xy_voxel_m * 1.5,
        parameters.axis_cluster_radius_m * 0.6,
    )
    for raw_index in order:
        index = int(raw_index)
        if covered[index]:
            continue
        selected.append(index)
        neighbors = tree.query_ball_point(eligible_centers[index], coverage_radius)
        covered[np.asarray(neighbors, dtype=np.int64)] = True
    return np.asarray(selected, dtype=np.int64)


def _discover_axis_fits(
    candidate_points: np.ndarray,
    candidate_indices: np.ndarray,
    eligible_centers: np.ndarray,
    eligible_indices: list[np.ndarray],
    eligible_quality: np.ndarray,
    sign_xy: np.ndarray,
    parameters: PoleSearchParameters,
    *,
    fit_only_eligible_points: bool,
) -> list[_AxisFitResult]:
    """Fit spatially covering local seeds without transitive-component starvation."""

    if fit_only_eligible_points:
        fit_positions = np.unique(np.concatenate(eligible_indices))
        fit_points = candidate_points[fit_positions]
        fit_source_indices = candidate_indices[fit_positions]
    else:
        fit_points = candidate_points
        fit_source_indices = candidate_indices
    point_tree = cKDTree(fit_points[:, :2])
    fits: list[_AxisFitResult] = []
    fit_centers: list[np.ndarray] = []
    dedup_radius = max(
        parameters.xy_voxel_m * 1.5,
        parameters.axis_cluster_radius_m * 0.5,
    )
    seed_indices = _coverage_seed_indices(
        eligible_centers,
        eligible_quality,
        sign_xy,
        parameters,
    )
    for seed_index in seed_indices:
        seed_center = eligible_centers[int(seed_index)]
        fit_members = np.asarray(
            point_tree.query_ball_point(
                seed_center,
                parameters.axis_cluster_radius_m,
            ),
            dtype=np.int64,
        )
        if fit_members.size < parameters.min_points:
            continue
        fit = _robust_axis_fit(
            fit_points[fit_members],
            fit_source_indices[fit_members],
            parameters,
        )
        if fit is None:
            continue
        fit_center = np.asarray(fit.coefficients[1], dtype=np.float64)
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(fit_centers)
                if float(np.linalg.norm(fit_center - existing)) < dedup_radius
            ),
            None,
        )
        if duplicate_index is None:
            fits.append(fit)
            fit_centers.append(fit_center)
            continue
        existing = fits[duplicate_index]
        fit_quality = (
            fit.continuity.longest_consecutive_bin_count,
            fit.continuity.vertical_occupancy_ratio,
            fit.inlier_indices.size,
            -fit.radial_rmse_m,
        )
        existing_quality = (
            existing.continuity.longest_consecutive_bin_count,
            existing.continuity.vertical_occupancy_ratio,
            existing.inlier_indices.size,
            -existing.radial_rmse_m,
        )
        if fit_quality > existing_quality:
            fits[duplicate_index] = fit
            fit_centers[duplicate_index] = fit_center
    return fits


def pole_candidate_rank_key(
    item: PoleAxisCandidate,
    parameters: PoleSearchParameters,
) -> tuple[float, ...]:
    """Return the shared physical-quality order for strict/expanded candidates."""

    completeness = float(getattr(item, "completeness_ratio", 0.0) or 0.0)
    association_distance = float(
        getattr(item, "association_distance_m", math.inf)
    )
    direct = (
        association_distance <= parameters.direct_max_axis_sign_distance_m
    )
    connection_coverage = float(
        getattr(item, "horizontal_connection_coverage_ratio", 0.0) or 0.0
    )
    connection_expected_bins = int(
        getattr(item, "horizontal_connection_expected_bin_count", 0) or 0
    )
    # A 4/4 connection immediately beside a sign is much easier to obtain by
    # chance than a 15/26 arm across several metres.  Weight coverage by the
    # square root of tested length so short fragments do not outrank a real
    # cantilever, while coverage still dominates very long incomplete clutter.
    connection_strength = connection_coverage * math.sqrt(
        max(1, connection_expected_bins)
    )
    radial_rmse = float(getattr(item, "radial_rmse_m", math.inf))
    multi_return_fraction = float(
        getattr(item, "multi_return_fraction", 0.0) or 0.0
    )
    score = float(getattr(item, "score", -math.inf))
    common = (
        0.0
        if completeness
        >= float(getattr(parameters, "preferred_min_completeness_ratio", 0.75))
        else 1.0,
        0.0 if direct else 1.0,
    )
    if direct:
        # Preserve the established direct-support behavior: once the shaft is
        # complete enough, the bounded physical score wins before tiny
        # completeness differences between adjacent vertical structures.
        return (
            *common,
            -score,
            -completeness,
            radial_rmse,
            multi_return_fraction,
            association_distance,
            -float(getattr(item, "point_count", 0)),
        )

    remote_evidence = (
        connection_strength
        + (2.0 * completeness)
        - (6.0 * radial_rmse)
        - multi_return_fraction
        - (0.05 * association_distance)
    )
    return (
        *common,
        -remote_evidence,
        -completeness,
        radial_rmse,
        multi_return_fraction,
        association_distance,
        -score,
        -float(getattr(item, "point_count", 0)),
    )


def _axis_ground_intersection(
    coefficients: np.ndarray,
    z_reference: float,
    ground: GroundEstimate,
) -> np.ndarray:
    """Intersect x(z), y(z) with the fitted local ground plane."""

    slope_xy = np.asarray(coefficients[0], dtype=np.float64)
    intercept_xy = np.asarray(coefficients[1], dtype=np.float64)
    plane = np.asarray(ground.plane_coefficients, dtype=np.float64)
    reference_xy = np.asarray(ground.reference_xy, dtype=np.float64)
    slope_coupling = float(np.dot(plane[:2], slope_xy))
    denominator = 1.0 - slope_coupling
    if abs(denominator) <= 1e-6:
        z_value = float(ground.z)
    else:
        constant = (
            float(np.dot(plane[:2], intercept_xy - reference_xy))
            + float(plane[2])
            - (slope_coupling * float(z_reference))
        )
        z_value = constant / denominator
    if not math.isfinite(z_value):
        z_value = float(ground.z)
    design = np.asarray([z_value - z_reference, 1.0], dtype=np.float64)
    xy_value = design @ coefficients
    return np.asarray([xy_value[0], xy_value[1], z_value], dtype=np.float64)


def find_pole_bases(
    neighborhood_xyz: np.ndarray,
    corridor_mask: np.ndarray,
    sign_xyz: np.ndarray,
    parameters: PoleSearchParameters,
    classifications: np.ndarray | None = None,
    return_numbers: np.ndarray | None = None,
    number_of_returns: np.ndarray | None = None,
    *,
    workspace: PoleSearchWorkspace | None = None,
    ground_classifications: np.ndarray | None = None,
) -> PoleSearchResult | None:
    """Find vertical pole axes and place their representative point on local ground.

    ``neighborhood_xyz`` contains every nearby point needed for both pole and
    ground estimation. ``corridor_mask`` marks points projected into the image
    corridor below the detected sign.  Returned indices address the original
    neighborhood array, which lets the caller save pole-only LAS/debug points.
    """

    parameters.validate()
    points = np.asarray(neighborhood_xyz, dtype=np.float64)
    sign = np.asarray(sign_xyz, dtype=np.float64)
    corridor = np.asarray(corridor_mask, dtype=bool)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("neighborhood_xyz must have shape (N, 3)")
    if corridor.shape != (points.shape[0],):
        raise ValueError("corridor_mask must have one value per neighborhood point")
    if sign.shape != (3,) or not np.all(np.isfinite(sign)):
        raise ValueError("sign_xyz must be a finite three-vector")
    search_workspace = workspace or PoleSearchWorkspace(points)
    search_workspace._check_points(points)
    classes = None
    if classifications is not None:
        classes = np.asarray(classifications, dtype=np.int16)
        if classes.shape != (points.shape[0],):
            raise ValueError("classifications must have one value per neighborhood point")
    ground_classes = classes
    if ground_classifications is not None:
        ground_classes = np.asarray(ground_classifications, dtype=np.int16)
        if ground_classes.shape != (points.shape[0],):
            raise ValueError(
                "ground_classifications must have one value per neighborhood point"
            )
    returns = None
    return_totals = None
    if return_numbers is not None or number_of_returns is not None:
        if return_numbers is None or number_of_returns is None:
            raise ValueError(
                "return_numbers and number_of_returns must be provided together"
            )
        returns = np.asarray(return_numbers, dtype=np.int16)
        return_totals = np.asarray(number_of_returns, dtype=np.int16)
        if returns.shape != (points.shape[0],) or return_totals.shape != (
            points.shape[0],
        ):
            raise ValueError("return metadata must have one value per neighborhood point")

    radial_to_sign = np.linalg.norm(points[:, :2] - sign[None, :2], axis=1)
    candidate_mask = (
        corridor
        & np.all(np.isfinite(points), axis=1)
        & (radial_to_sign <= parameters.search_radius_m)
        & (points[:, 2] >= sign[2] - parameters.max_drop_m)
        & (points[:, 2] <= sign[2] + parameters.top_margin_m)
    )
    if classes is not None:
        # Ground returns remain available to ``estimate_local_ground`` below,
        # but must never be allowed to bridge a sparse upper object into a
        # seemingly tall pole axis.
        if parameters.ground_class_ids:
            candidate_mask &= ~np.isin(classes, parameters.ground_class_ids)
        if parameters.pole_class_ids:
            candidate_mask &= np.isin(classes, parameters.pole_class_ids)
        elif parameters.excluded_pole_class_ids:
            candidate_mask &= ~np.isin(classes, parameters.excluded_pole_class_ids)
    else:
        candidate_mask &= search_workspace.terrain_clearance_mask(
            points,
            sign,
            parameters,
        )
    candidate_indices = np.flatnonzero(candidate_mask)
    candidate_points = points[candidate_indices]
    if candidate_points.shape[0] < parameters.min_points:
        return None

    cell_centers, eligible_indices, eligible_quality = _eligible_axis_cells(
        candidate_points,
        sign,
        parameters,
    )
    if cell_centers.shape[0] == 0:
        return None

    candidates: list[PoleAxisCandidate] = []
    for fit in _discover_axis_fits(
        candidate_points,
        candidate_indices,
        cell_centers,
        eligible_indices,
        eligible_quality,
        sign[:2],
        parameters,
        fit_only_eligible_points=classes is None,
    ):
        coefficients = fit.coefficients
        axis_direction = fit.axis_direction
        inlier_indices = fit.inlier_indices
        radial_rmse = fit.radial_rmse_m
        z_reference = fit.z_reference
        continuity = fit.continuity
        inlier_points = points[inlier_indices]
        shaft_quality_mask = (
            inlier_points[:, 2]
            < float(sign[2]) - parameters.horizontal_connection_z_tolerance_m
        )
        if int(shaft_quality_mask.sum()) >= parameters.min_points:
            shaft_design = np.column_stack(
                (
                    inlier_points[shaft_quality_mask, 2] - z_reference,
                    np.ones(int(shaft_quality_mask.sum()), dtype=np.float64),
                )
            )
            shaft_residuals = np.linalg.norm(
                inlier_points[shaft_quality_mask, :2]
                - (shaft_design @ coefficients),
                axis=1,
            )
            radial_rmse = max(
                radial_rmse,
                float(np.sqrt(np.mean(np.square(shaft_residuals)))),
            )
        lowest_observed_z = float(np.quantile(inlier_points[:, 2], 0.02))
        lowest_design = np.asarray([lowest_observed_z - z_reference, 1.0], dtype=np.float64)
        lowest_axis_xy = lowest_design @ coefficients
        ground_indices = search_workspace.query_radius(
            points,
            lowest_axis_xy,
            parameters.ground_search_radius_m,
        )
        ground = estimate_local_ground(
            points[ground_indices],
            lowest_axis_xy,
            float(sign[2]),
            parameters,
            (
                ground_classes[ground_indices]
                if ground_classes is not None
                else None
            ),
        )
        if ground is None and parameters.require_ground:
            continue
        if ground is not None and float(sign[2] - ground.z) < parameters.min_ground_drop_m:
            continue
        middle_support_bins: int | None = None
        middle_expected_bins: int | None = None
        middle_support_coverage: float | None = None
        completeness_ratio: float | None = None
        if ground is not None:
            (
                middle_support_bins,
                middle_expected_bins,
                middle_support_coverage,
            ) = _middle_support_coverage(
                inlier_points[:, 2],
                float(ground.z),
                float(sign[2]),
                parameters,
            )
            if (
                middle_support_coverage
                < parameters.min_middle_support_coverage_ratio
            ):
                continue
            expected_height = max(
                parameters.z_bin_m,
                float(sign[2] - ground.z),
            )
            observed_height = max(
                0.0,
                float(continuity.observed_z_max - continuity.observed_z_min),
            )
            height_coverage = min(1.0, observed_height / expected_height)
            completeness_ratio = min(
                float(middle_support_coverage),
                float(height_coverage),
            )
        if ground is None:
            base_z = lowest_observed_z
            base_design = np.asarray(
                [base_z - z_reference, 1.0],
                dtype=np.float64,
            )
            base_xy = base_design @ coefficients
            base_xyz = np.asarray(
                [base_xy[0], base_xy[1], base_z],
                dtype=np.float64,
            )
            method = "OBS_BOTTOM"
            status = "REVIEW"
            bottom_gap = None
            occluded = None
            occlusion_status = "UNKNOWN"
            ground_z = None
            ground_rmse = None
            ground_support_distance = None
        else:
            base_xyz = _axis_ground_intersection(
                coefficients,
                z_reference,
                ground,
            )
            base_z = float(base_xyz[2])
            bottom_gap = lowest_observed_z - base_z
            occluded = bottom_gap > parameters.occlusion_gap_m
            ground_conflict = bottom_gap < -parameters.max_ground_penetration_m
            occlusion_status = (
                "GROUND_CONFLICT"
                if ground_conflict
                else "OCCLUDED"
                if occluded
                else "VISIBLE"
            )
            method = "GROUND_EXTR" if occluded else "GROUND_SNAP"
            ground_support_distance = (
                float(
                    np.min(
                        np.linalg.norm(
                            ground.support_xyz[:, :2] - base_xyz[None, :2],
                            axis=1,
                        )
                    )
                )
                if ground.support_xyz.size
                else math.inf
            )
            status = (
                "REVIEW"
                if (
                    ground_conflict
                    or occluded
                    or ground.rmse_m > parameters.ground_max_rmse_m * 0.75
                    or float(completeness_ratio or 0.0)
                    < parameters.preferred_min_completeness_ratio
                    or ground_support_distance
                    > parameters.max_ground_support_distance_m
                )
                else "AUTO"
            )
            ground_z = base_z
            ground_rmse = ground.rmse_m

        vertical_span = float(np.ptp(inlier_points[:, 2]))
        attachment_design = np.asarray(
            [float(sign[2]) - z_reference, 1.0],
            dtype=np.float64,
        )
        attachment_xy = attachment_design @ coefficients
        association_distance = float(np.linalg.norm(attachment_xy - sign[:2]))
        if association_distance > parameters.max_axis_sign_distance_m:
            continue
        horizontal_connection_bins: int | None = None
        horizontal_connection_expected_bins: int | None = None
        horizontal_connection_coverage: float | None = None
        if association_distance > parameters.direct_max_axis_sign_distance_m:
            connection_indices = search_workspace.query_connection_capsule(
                points,
                sign[:2],
                attachment_xy,
                parameters.horizontal_connection_radius_m,
            )
            connection = _horizontal_connection_coverage(
                points[connection_indices],
                sign,
                attachment_xy,
                parameters,
                (
                    ground_classes[connection_indices]
                    if ground_classes is not None
                    else None
                ),
            )
            horizontal_connection_bins = connection.occupied_bin_count
            horizontal_connection_expected_bins = connection.expected_bin_count
            horizontal_connection_coverage = connection.coverage_ratio
            if (
                horizontal_connection_coverage
                < parameters.min_horizontal_connection_coverage
            ):
                continue
            if classes is None:
                if (
                    float(completeness_ratio or 0.0)
                    < parameters.geometry_remote_min_completeness_ratio
                    or radial_rmse > parameters.geometry_remote_max_axis_rmse_m
                    or (
                        ground_rmse is not None
                        and ground_rmse
                        > parameters.geometry_remote_max_ground_rmse_m
                    )
                ):
                    continue
        elif (
            classes is None
            and radial_rmse > parameters.geometry_remote_max_axis_rmse_m
        ):
            # A direct axis has stronger geometric association than a remote
            # one, so retain it for sibling/multi-frame comparison, but make
            # the high cross-section error explicit to QA and aggregation.
            status = "REVIEW"
        expected_shaft_height = (
            max(parameters.z_bin_m, float(sign[2] - ground_z))
            if ground_z is not None
            else parameters.max_drop_m
        )
        bounded_vertical_span = min(vertical_span, expected_shaft_height)
        multi_return_fraction: float | None = None
        if returns is not None and return_totals is not None:
            inlier_returns = returns[inlier_indices]
            inlier_totals = return_totals[inlier_indices]
            valid_returns = (
                (inlier_totals > 0)
                & (inlier_returns > 0)
                & (inlier_returns <= inlier_totals)
            )
            if np.any(valid_returns):
                multi_return_fraction = float(
                    np.mean(inlier_totals[valid_returns] > 1)
                )
        score = (
            bounded_vertical_span
            + (0.08 * continuity.vertical_bin_count)
            + (0.03 * math.sqrt(inlier_indices.size))
            - (5.0 * radial_rmse)
            - (0.15 * association_distance)
            + (0.50 * float(horizontal_connection_coverage or 0.0))
            - (0.50 * float(multi_return_fraction or 0.0))
        )
        dominant_class_id: int | None = None
        dominant_class_fraction: float | None = None
        if classes is not None:
            inlier_classes = classes[inlier_indices]
            known_classes = inlier_classes[inlier_classes >= 0]
            if known_classes.size:
                class_ids, class_counts = np.unique(known_classes, return_counts=True)
                dominant_index = int(np.argmax(class_counts))
                dominant_class_id = int(class_ids[dominant_index])
                dominant_class_fraction = float(class_counts[dominant_index] / known_classes.size)
        if (
            dominant_class_id in parameters.ground_class_ids
            and dominant_class_fraction is not None
            and dominant_class_fraction > parameters.max_ground_class_fraction
        ):
            continue
        candidates.append(
            PoleAxisCandidate(
                base_xyz=base_xyz,
                axis_direction=axis_direction,
                point_indices=inlier_indices.astype(np.int64, copy=False),
                point_count=int(inlier_indices.size),
                vertical_span_m=vertical_span,
                vertical_bin_count=continuity.vertical_bin_count,
                observed_z_min=continuity.observed_z_min,
                observed_z_max=continuity.observed_z_max,
                longest_consecutive_bin_count=(
                    continuity.longest_consecutive_bin_count
                ),
                max_observed_z_gap_m=continuity.max_observed_z_gap_m,
                vertical_occupancy_ratio=continuity.vertical_occupancy_ratio,
                middle_support_bin_count=middle_support_bins,
                middle_expected_bin_count=middle_expected_bins,
                middle_support_coverage_ratio=middle_support_coverage,
                completeness_ratio=completeness_ratio,
                association_distance_m=association_distance,
                horizontal_connection_bin_count=horizontal_connection_bins,
                horizontal_connection_expected_bin_count=(
                    horizontal_connection_expected_bins
                ),
                horizontal_connection_coverage_ratio=(
                    horizontal_connection_coverage
                ),
                radial_rmse_m=radial_rmse,
                lowest_observed_z=lowest_observed_z,
                ground_z=ground_z,
                ground_rmse_m=ground_rmse,
                bottom_gap_m=bottom_gap,
                occluded_bottom=occluded,
                occlusion_status=occlusion_status,
                method=method,
                status=status,
                score=float(score),
                dominant_class_id=dominant_class_id,
                dominant_class_fraction=dominant_class_fraction,
                multi_return_fraction=multi_return_fraction,
                ground_estimate=ground,
                axis_stabilized=fit.stabilized,
                axis_bin_inlier_count=fit.bin_inlier_count,
                axis_bin_count=fit.bin_count,
                ground_support_distance_m=ground_support_distance,
            )
        )

    if not candidates:
        return None
    # Prefer a physically complete shaft over a short sign edge or upper
    # appendage that happens to be closer to the sign centre.  Direct supports
    # still beat verified remote supports at the same completeness tier; a
    # remote axis can only enter the ranking after the 3-D arm gate above.
    candidates.sort(key=lambda item: pole_candidate_rank_key(item, parameters))
    selected = candidates[0]
    return PoleSearchResult(
        representative_xyz=selected.base_xyz,
        candidates=(selected,),
        pole_type="SINGLE",
        method=selected.method,
        status=selected.status,
        occluded_bottom=selected.occluded_bottom,
        occlusion_status=selected.occlusion_status,
    )


def _weighted_geometric_median(
    coordinates: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """Return a deterministic robust centre using Weiszfeld iterations."""

    if coordinates.shape[0] == 1:
        return coordinates[0].copy()
    current = np.average(coordinates, axis=0, weights=weights)
    for _ in range(40):
        distances = np.linalg.norm(coordinates - current[None, :], axis=1)
        coincident = np.flatnonzero(distances <= 1e-9)
        if coincident.size:
            return coordinates[int(coincident[np.argmax(weights[coincident])])].copy()
        scaled = weights / np.maximum(distances, 1e-9)
        updated = np.sum(coordinates * scaled[:, None], axis=0) / float(
            scaled.sum()
        )
        if float(np.linalg.norm(updated - current)) <= 1e-7:
            return updated
        current = updated
    return current


def cluster_pole_observations(
    observations: list[dict[str, Any]],
    radius_m: float,
) -> list[dict[str, Any]]:
    """Estimate one support coordinate, then emit one relation row per detection.

    A physical support can carry several signs.  Its aggregate coordinate and
    quality fields are therefore repeated for every attached sign detection so
    ``detected_signs.shp`` and ``pole_bottoms.shp`` can be joined by ``det_id``.
    """

    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        grouped.setdefault(str(item.get("record_name") or ""), []).append(item)

    merged: list[dict[str, Any]] = []
    for _, items in sorted(grouped.items(), key=lambda pair: pair[0]):
        coordinates = np.asarray(
            [[float(item["pole_x"]), float(item["pole_y"])] for item in items],
            dtype=np.float64,
        )
        for indices in _connected_components(coordinates, radius_m):
            members = [items[int(index)] for index in indices]
            frame_members: dict[str, dict[str, Any]] = {}
            for member_index, member in enumerate(members):
                frame_key = str(
                    member.get("image_name")
                    or member.get("timestamp_iso")
                    or f"__observation_{member_index}"
                )
                existing = frame_members.get(frame_key)
                if existing is None or float(member.get("pole_quality") or 0.0) > float(
                    existing.get("pole_quality") or 0.0
                ):
                    frame_members[frame_key] = member
            unique_members = list(frame_members.values())
            weights = np.asarray(
                [
                    max(
                        1e-6,
                        float(item.get("pole_quality") or item.get("confidence") or 1.0),
                    )
                    for item in unique_members
                ],
                dtype=np.float64,
            )
            xyz = np.asarray(
                [
                    [float(item["pole_x"]), float(item["pole_y"]), float(item["pole_z"])]
                    for item in unique_members
                ],
                dtype=np.float64,
            )
            physically_valid = np.asarray(
                [
                    str(item.get("pole_occlusion_status") or "")
                    != "GROUND_CONFLICT"
                    for item in unique_members
                ],
                dtype=bool,
            )
            seed_mask = (
                physically_valid
                if int(physically_valid.sum()) >= 2
                else np.ones(xyz.shape[0], dtype=bool)
            )
            seed_centre = _weighted_geometric_median(
                xyz[seed_mask],
                weights[seed_mask],
            )
            xy_distances = np.linalg.norm(
                xyz[:, :2] - seed_centre[None, :2],
                axis=1,
            )
            z_distances = np.abs(xyz[:, 2] - seed_centre[2])
            seed_xy = xy_distances[seed_mask]
            seed_z = z_distances[seed_mask]
            xy_median = float(np.median(seed_xy))
            z_median = float(np.median(seed_z))
            xy_mad = 1.4826 * float(np.median(np.abs(seed_xy - xy_median)))
            z_mad = 1.4826 * float(np.median(np.abs(seed_z - z_median)))
            consensus_mask = (
                physically_valid
                & (xy_distances <= max(0.20, xy_median + (3.0 * xy_mad)))
                & (z_distances <= max(0.20, z_median + (3.0 * z_mad)))
            )
            if not np.any(consensus_mask):
                consensus_mask = seed_mask
            consensus_xyz = xyz[consensus_mask]
            consensus_weights = weights[consensus_mask]
            representative = _weighted_geometric_median(
                consensus_xyz,
                consensus_weights,
            )
            consensus_members = [
                item
                for item, keep in zip(unique_members, consensus_mask)
                if bool(keep)
            ]
            consensus_outlier_count = int((~consensus_mask).sum())
            best = max(
                consensus_members,
                key=lambda item: float(item.get("pole_quality") or 0.0),
            )
            pole_types = {str(item.get("pole_type") or "") for item in members}
            all_auto = all(
                str(item.get("pole_status")) == "AUTO"
                for item in consensus_members
            )
            occlusion_states = {
                str(
                    item.get("pole_occlusion_status")
                    or (
                        "OCCLUDED"
                        if item.get("pole_occluded") is True
                        else "VISIBLE"
                        if item.get("pole_occluded") is False
                        else "UNKNOWN"
                    )
                )
                for item in unique_members
            }
            merged_occlusion_status = (
                next(iter(occlusion_states)) if len(occlusion_states) == 1 else "MIXED"
            )
            if "OCCLUDED" in occlusion_states:
                merged_occluded: bool | None = True
            elif occlusion_states == {"VISIBLE"}:
                merged_occluded = False
            else:
                merged_occluded = None
            fallback_attempted_count = sum(
                bool(item.get("pole_fallback_attempted"))
                for item in unique_members
            )
            fallback_used_count = sum(
                bool(item.get("pole_fallback_used"))
                for item in unique_members
            )
            search_modes = {
                str(item.get("pole_search_mode") or "").strip()
                for item in unique_members
                if str(item.get("pole_search_mode") or "").strip()
            }
            aggregate_search_mode = (
                next(iter(search_modes))
                if len(search_modes) == 1
                else "MIXED"
                if search_modes
                else ""
            )
            detection_ids: list[str] = []
            for member_index, member in enumerate(members):
                detection_id = str(member.get("detection_id") or "")
                if not detection_id:
                    identity = (
                        f"{member.get('record_name') or ''}|"
                        f"{member.get('image_name') or ''}|"
                        f"{member.get('detection_index') or member_index + 1}"
                    )
                    detection_id = "D" + hashlib.sha256(
                        identity.encode("utf-8")
                    ).hexdigest()[:19]
                    member["detection_id"] = detection_id
                detection_ids.append(detection_id)
            support_identity = (
                str(best.get("record_name") or "")
                + "|"
                + "|".join(sorted(detection_ids))
            )
            support_id = "P" + hashlib.sha256(
                support_identity.encode("utf-8")
            ).hexdigest()[:19]
            aggregate = {
                "pole_x": float(representative[0]),
                "pole_y": float(representative[1]),
                "pole_z": float(representative[2]),
                "support_id": support_id,
                "obs_count": len(unique_members),
                "detection_count": len(members),
                "occluded_count": sum(
                    item.get("pole_occluded") is True for item in unique_members
                ),
                "unknown_occlusion_count": sum(
                    item.get("pole_occluded") is None for item in unique_members
                ),
                "pole_occluded": merged_occluded,
                "pole_occlusion_status": merged_occlusion_status,
                "source_images": [
                    str(item.get("image_name") or "") for item in unique_members
                ],
                "source_class_ids": sorted(
                    {
                        int(item["class_id"])
                        if item.get("class_id") is not None
                        else -1
                        for item in members
                    }
                ),
                "pole_type": next(iter(pole_types)) if len(pole_types) == 1 else "MIXED",
                "pole_method": "MULTI_FRAME"
                if len(unique_members) > 1
                else str(best.get("pole_method") or ""),
                "pole_status": (
                    "AUTO"
                    if (
                        all_auto
                        and len(pole_types) == 1
                        and consensus_outlier_count == 0
                    )
                    else "REVIEW"
                ),
                "pole_search_mode": aggregate_search_mode,
                "pole_fallback_attempted": fallback_attempted_count > 0,
                "pole_fallback_attempted_count": fallback_attempted_count,
                "pole_fallback_used": fallback_used_count > 0,
                "pole_fallback_used_count": fallback_used_count,
                "xy_spread_m": float(
                    np.sqrt(
                        np.average(
                            np.sum(
                                np.square(
                                    consensus_xyz[:, :2]
                                    - representative[None, :2]
                                ),
                                axis=1,
                            ),
                            weights=consensus_weights,
                        )
                    )
                ),
                "z_spread_m": float(
                    np.sqrt(
                        np.average(
                            np.square(consensus_xyz[:, 2] - representative[2]),
                            weights=consensus_weights,
                        )
                    )
                ),
                "consensus_outlier_count": consensus_outlier_count,
            }
            for member in members:
                relation = dict(member)
                relation.update(aggregate)
                merged.append(relation)
    merged.sort(
        key=lambda item: (
            str(item.get("record_name") or ""),
            str(item.get("support_id") or ""),
            str(item.get("image_name") or ""),
            int(item.get("detection_index") or 0),
            str(item.get("detection_id") or ""),
        )
    )
    return merged
