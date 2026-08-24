from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from .pole import (
    GroundEstimate,
    PoleAxisFit,
    PoleSearchParameters,
    fit_pole_axis,
    intersect_pole_axis_with_ground,
)

MANUAL_POLE_BASE_ALGORITHM = "manual_seed_axis_ground_intersection"
MANUAL_POLE_BASE_ALGORITHM_VERSION = "1"

REASON_CODES = (
    "INVALID_SEED",
    "METRIC_CRS_REQUIRED",
    "SEED_OUTSIDE_FRAME_WINDOW",
    "SEED_NOT_ON_SOURCE_POINT",
    "NO_LOCAL_POINTS",
    "LOCAL_POINT_LIMIT_EXCEEDED",
    "TOO_MANY_CANDIDATE_BLOCKS",
    "NO_VERTICAL_AXIS",
    "AXIS_TOO_SHORT",
    "AXIS_DISCONTINUOUS",
    "AXIS_RMSE_HIGH",
    "AXIS_TILT_EXCESS",
    "AMBIGUOUS_AXES",
    "NO_GROUND_SUPPORT",
    "GROUND_RMSE_HIGH",
    "GROUND_HYPOTHESES_CONFLICT",
    "GROUND_TOO_FAR",
    "GROUND_PENETRATION",
    "BOTTOM_EXTRAPOLATED",
    "BASE_OUTSIDE_LOCAL_WINDOW",
)
_REASON_ORDER = {code: index for index, code in enumerate(REASON_CODES)}
_WARNING_TEXT = {
    "AMBIGUOUS_AXES": "Multiple plausible pole axes are close to the clicked seed.",
    "GROUND_RMSE_HIGH": "The local ground plane has elevated residual error.",
    "GROUND_HYPOTHESES_CONFLICT": "Local ground hypotheses disagree near the pole.",
    "GROUND_TOO_FAR": "The nearest ground support is farther from the pole than preferred.",
    "GROUND_PENETRATION": "Observed shaft returns extend below the estimated ground.",
    "BOTTOM_EXTRAPOLATED": "The pole base is extrapolated below the lowest shaft return.",
}


def _clip(value: float) -> float:
    return float(np.clip(float(value), 0.0, 1.0))


def _ordered_reasons(*groups: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    unique = {code for group in groups for code in group}
    return tuple(sorted(unique, key=lambda code: (_REASON_ORDER.get(code, 10_000), code)))


@dataclass(frozen=True)
class ManualPoleBaseParameters:
    seed_snap_radius_m: float = 0.20

    axis_search_radius_m: float = 0.75
    axis_seed_gate_m: float = 0.30
    axis_cluster_radius_m: float = 0.24
    axis_inlier_radius_m: float = 0.18
    xy_voxel_m: float = 0.10
    z_bin_m: float = 0.15
    min_axis_points: int = 18
    min_vertical_span_m: float = 0.90
    min_vertical_bins: int = 5
    min_consecutive_vertical_bins: int = 4
    min_vertical_occupancy_ratio: float = 0.35
    max_observed_z_gap_m: float = 1.0
    max_axis_rmse_m: float = 0.12
    max_axis_tilt_deg: float = 15.0
    candidate_merge_radius_m: float = 0.18
    max_axis_hypotheses: int = 24
    ambiguity_score_margin: float = 0.08

    ground_search_radius_m: float = 1.50
    ground_core_radius_m: float = 0.75
    ground_exclusion_radius_m: float = 0.24
    ground_cell_size_m: float = 0.25
    ground_cell_quantile: float = 0.10
    ground_min_cells: int = 6
    ground_max_rmse_m: float = 0.20
    ground_surface_step_m: float = 0.22
    max_ground_support_distance_auto_m: float = 0.35
    max_ground_support_distance_review_m: float = 0.75

    max_ground_penetration_m: float = 0.10
    max_bottom_gap_auto_m: float = 0.35
    max_bottom_gap_review_m: float = 1.50
    max_bottom_gap_hard_m: float = 6.00
    max_base_seed_xy_distance_m: float = 1.0

    ground_class_ids: tuple[int, ...] = (2, 11)
    excluded_axis_class_ids: tuple[int, ...] = (2, 3, 4, 5, 11)

    def __post_init__(self) -> None:
        positive = (
            self.seed_snap_radius_m,
            self.axis_search_radius_m,
            self.axis_seed_gate_m,
            self.axis_cluster_radius_m,
            self.axis_inlier_radius_m,
            self.xy_voxel_m,
            self.z_bin_m,
            self.min_vertical_span_m,
            self.max_observed_z_gap_m,
            self.max_axis_rmse_m,
            self.candidate_merge_radius_m,
            self.ground_search_radius_m,
            self.ground_core_radius_m,
            self.ground_cell_size_m,
            self.ground_max_rmse_m,
            self.ground_surface_step_m,
            self.max_ground_support_distance_auto_m,
            self.max_ground_support_distance_review_m,
            self.max_bottom_gap_auto_m,
            self.max_bottom_gap_review_m,
            self.max_bottom_gap_hard_m,
            self.max_base_seed_xy_distance_m,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("Manual pole-base distance parameters must be finite and positive")
        if not 0.0 <= self.ground_exclusion_radius_m < self.ground_core_radius_m:
            raise ValueError("ground_exclusion_radius_m must be in the ground core")
        if self.ground_core_radius_m > self.ground_search_radius_m:
            raise ValueError("ground_core_radius_m cannot exceed ground_search_radius_m")
        if self.max_ground_support_distance_auto_m > self.max_ground_support_distance_review_m:
            raise ValueError("automatic ground distance cannot exceed review distance")
        if self.max_bottom_gap_auto_m > self.max_bottom_gap_review_m:
            raise ValueError("automatic bottom gap cannot exceed review gap")
        if self.max_bottom_gap_review_m > self.max_bottom_gap_hard_m:
            raise ValueError("review bottom gap cannot exceed the hard safety gap")
        if self.min_axis_points < 3 or self.min_vertical_bins < 2:
            raise ValueError("Manual pole-base point/bin limits are too small")
        if self.min_consecutive_vertical_bins < 2:
            raise ValueError("min_consecutive_vertical_bins must be at least two")
        if self.max_axis_hypotheses < 1 or self.ground_min_cells < 3:
            raise ValueError("Manual pole-base hypothesis/cell limits are too small")
        if not 0.0 <= self.min_vertical_occupancy_ratio <= 1.0:
            raise ValueError("min_vertical_occupancy_ratio must be in [0, 1]")
        if not 0.0 <= self.ground_cell_quantile <= 1.0:
            raise ValueError("ground_cell_quantile must be in [0, 1]")
        if not 0.0 <= self.max_axis_tilt_deg <= 90.0:
            raise ValueError("max_axis_tilt_deg must be in [0, 90]")

    def pole_parameters(self) -> PoleSearchParameters:
        """Map manual gates onto the shared robust axis fitter."""

        return PoleSearchParameters(
            search_radius_m=self.axis_search_radius_m,
            max_drop_m=12.0,
            top_margin_m=4.0,
            xy_voxel_m=self.xy_voxel_m,
            z_bin_m=self.z_bin_m,
            axis_cluster_radius_m=self.axis_cluster_radius_m,
            axis_inlier_radius_m=self.axis_inlier_radius_m,
            min_vertical_span_m=self.min_vertical_span_m,
            min_vertical_bins=self.min_vertical_bins,
            min_consecutive_vertical_bins=self.min_consecutive_vertical_bins,
            max_observed_z_gap_m=self.max_observed_z_gap_m,
            min_vertical_occupancy_ratio=self.min_vertical_occupancy_ratio,
            min_points=self.min_axis_points,
            max_axis_tilt_deg=self.max_axis_tilt_deg,
            ground_search_radius_m=self.ground_search_radius_m,
            ground_core_radius_m=self.ground_core_radius_m,
            ground_exclusion_radius_m=self.ground_exclusion_radius_m,
            ground_cell_size_m=self.ground_cell_size_m,
            ground_cell_quantile=self.ground_cell_quantile,
            ground_min_cells=self.ground_min_cells,
            ground_max_rmse_m=self.ground_max_rmse_m,
            max_ground_penetration_m=self.max_ground_penetration_m,
            max_ground_support_distance_m=self.max_ground_support_distance_auto_m,
            ground_class_ids=self.ground_class_ids,
            excluded_pole_class_ids=tuple(
                class_id
                for class_id in self.excluded_axis_class_ids
                if class_id not in self.ground_class_ids
            ),
        )


_DEFAULT_MANUAL_POLE_BASE_PARAMETERS = ManualPoleBaseParameters()


@dataclass(frozen=True)
class ManualPoleAxisResult:
    point: np.ndarray
    direction: np.ndarray
    point_count: int
    observed_z_min: float
    observed_z_max: float
    vertical_span_m: float
    vertical_bin_count: int
    longest_consecutive_bin_count: int
    occupancy_ratio: float
    rmse_m: float
    tilt_deg: float
    seed_distance_m: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point.astype(float).tolist(),
            "direction": self.direction.astype(float).tolist(),
            "point_count": self.point_count,
            "observed_z_min": self.observed_z_min,
            "observed_z_max": self.observed_z_max,
            "vertical_span_m": self.vertical_span_m,
            "vertical_bin_count": self.vertical_bin_count,
            "longest_consecutive_bin_count": self.longest_consecutive_bin_count,
            "occupancy_ratio": self.occupancy_ratio,
            "rmse_m": self.rmse_m,
            "tilt_deg": self.tilt_deg,
            "seed_distance_m": self.seed_distance_m,
        }


@dataclass(frozen=True)
class ManualPoleGroundResult:
    method: str
    z_at_base: float
    rmse_m: float
    cell_count: int
    candidate_cell_count: int
    nearest_support_distance_m: float
    plane_coefficients: np.ndarray
    reference_xy: np.ndarray
    support_xyz: np.ndarray = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "z_at_base": self.z_at_base,
            "rmse_m": self.rmse_m,
            "cell_count": self.cell_count,
            "candidate_cell_count": self.candidate_cell_count,
            "nearest_support_distance_m": self.nearest_support_distance_m,
            "plane_coefficients": self.plane_coefficients.astype(float).tolist(),
            "reference_xy": self.reference_xy.astype(float).tolist(),
        }


@dataclass(frozen=True)
class ManualPoleBaseQuality:
    score: float
    candidate_count: int
    ambiguous: bool
    bottom_gap_m: float | None
    components: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "candidate_count": self.candidate_count,
            "ambiguous": self.ambiguous,
            "bottom_gap_m": self.bottom_gap_m,
            "components": dict(self.components),
        }


@dataclass(frozen=True)
class ManualPoleBaseResult:
    status: Literal["auto", "review", "failed"]
    seed_position: np.ndarray
    snapped_seed_position: np.ndarray | None
    base_position: np.ndarray | None
    axis: ManualPoleAxisResult | None
    ground: ManualPoleGroundResult | None
    quality: ManualPoleBaseQuality
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    debug_points: np.ndarray | None = field(default=None, repr=False, compare=False)
    algorithm: str = MANUAL_POLE_BASE_ALGORITHM
    algorithm_version: str = MANUAL_POLE_BASE_ALGORITHM_VERSION
    coordinate_space: Literal["dataset"] = "dataset"

    def to_dict(self, *, debug: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "coordinate_space": self.coordinate_space,
            "seed_position": self.seed_position.astype(float).tolist(),
            "snapped_seed_position": (
                self.snapped_seed_position.astype(float).tolist()
                if self.snapped_seed_position is not None
                else None
            ),
            "base_position": (
                self.base_position.astype(float).tolist()
                if self.base_position is not None
                else None
            ),
            "axis": self.axis.to_dict() if self.axis is not None else None,
            "ground": self.ground.to_dict() if self.ground is not None else None,
            "quality": self.quality.to_dict(),
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "debug": None,
        }
        if debug and self.debug_points is not None:
            points = np.asarray(self.debug_points, dtype=np.float64)[:256]
            payload["debug"] = {"support_points": points.astype(float).tolist()}
        return payload

    def public_dict(self, *, debug: bool = False) -> dict[str, Any]:
        return self.to_dict(debug=debug)


@dataclass(frozen=True)
class _AxisCandidate:
    fit: PoleAxisFit
    seed_distance_m: float
    score: float


@dataclass(frozen=True)
class _GroundCandidate:
    estimate: GroundEstimate
    nearest_support_distance_m: float


@dataclass(frozen=True)
class _CrossSectionShape:
    radial_q90_m: float
    minor_major_ratio: float
    occupied_sector_count: int


def _empty_quality(
    *,
    candidate_count: int = 0,
    ambiguous: bool = False,
) -> ManualPoleBaseQuality:
    return ManualPoleBaseQuality(
        score=0.0,
        candidate_count=candidate_count,
        ambiguous=ambiguous,
        bottom_gap_m=None,
        components={
            "seed": 0.0,
            "axis": 0.0,
            "span": 0.0,
            "continuity": 0.0,
            "ground": 0.0,
            "bottom_gap": 0.0,
        },
    )


def failed_manual_pole_base(
    seed_xyz: np.ndarray,
    reason_codes: list[str] | tuple[str, ...],
    *,
    snapped_seed: np.ndarray | None = None,
    axis: ManualPoleAxisResult | None = None,
    ground: ManualPoleGroundResult | None = None,
    candidate_count: int = 0,
    ambiguous: bool = False,
    debug_points: np.ndarray | None = None,
) -> ManualPoleBaseResult:
    reasons = _ordered_reasons(tuple(reason_codes))
    return ManualPoleBaseResult(
        status="failed",
        seed_position=np.asarray(seed_xyz, dtype=np.float64),
        snapped_seed_position=(
            None
            if snapped_seed is None
            else np.asarray(snapped_seed, dtype=np.float64)
        ),
        base_position=None,
        axis=axis,
        ground=ground,
        quality=_empty_quality(
            candidate_count=candidate_count,
            ambiguous=ambiguous,
        ),
        reason_codes=reasons,
        warnings=tuple(_WARNING_TEXT[code] for code in reasons if code in _WARNING_TEXT),
        debug_points=debug_points,
    )


def _validate_inputs(
    points_xyz: np.ndarray,
    seed_xyz: np.ndarray,
    classifications: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    points = np.asarray(points_xyz, dtype=np.float64)
    seed = np.asarray(seed_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points_xyz must have shape (N, 3)")
    if seed.shape != (3,) or not np.all(np.isfinite(seed)):
        raise ValueError("seed_xyz must be a finite three-vector")
    classes = None
    if classifications is not None:
        classes = np.asarray(classifications, dtype=np.int16)
        if classes.shape != (points.shape[0],):
            raise ValueError("classifications must have one value per point")
    return points, seed, classes


def _snap_seed(
    points: np.ndarray,
    seed: np.ndarray,
    radius_m: float,
) -> tuple[np.ndarray | None, float | None]:
    finite_indices = np.flatnonzero(np.all(np.isfinite(points), axis=1))
    if finite_indices.size == 0:
        return None, None
    deltas = points[finite_indices] - seed[None, :]
    squared = np.einsum("ij,ij->i", deltas, deltas)
    local_index = int(np.argmin(squared))
    distance = math.sqrt(float(squared[local_index]))
    if distance > radius_m:
        return None, distance
    return np.asarray(points[int(finite_indices[local_index])], dtype=np.float64), distance


def _continuity_summary(z_values: np.ndarray, z_bin_m: float) -> tuple[float, int, int]:
    observed = np.sort(np.asarray(z_values, dtype=np.float64))
    if observed.size == 0:
        return 0.0, 0, 0
    span = float(observed[-1] - observed[0])
    bin_ids = np.unique(
        np.floor(((observed - observed[0]) / z_bin_m) + 1e-9).astype(np.int64)
    )
    longest = 1
    current = 1
    for difference in np.diff(bin_ids):
        if int(difference) == 1:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return span, int(bin_ids.size), int(longest)


def _axis_hypothesis_centres(
    axis_points: np.ndarray,
    snapped_seed: np.ndarray,
    parameters: ManualPoleBaseParameters,
) -> list[np.ndarray]:
    cell_ids = np.floor(axis_points[:, :2] / parameters.xy_voxel_m).astype(np.int64)
    groups: dict[tuple[int, int], list[int]] = {}
    for index, cell in enumerate(cell_ids):
        groups.setdefault((int(cell[0]), int(cell[1])), []).append(index)
    ranked: list[tuple[tuple[float, ...], np.ndarray]] = []
    minimum_cell_points = max(3, parameters.min_axis_points // 4)
    for cell, member_list in groups.items():
        members = np.asarray(member_list, dtype=np.int64)
        if members.size < minimum_cell_points:
            continue
        span, bin_count, consecutive = _continuity_summary(
            axis_points[members, 2],
            parameters.z_bin_m,
        )
        if (
            span < parameters.min_vertical_span_m * 0.5
            or bin_count < max(3, parameters.min_vertical_bins - 2)
            or consecutive < 2
        ):
            continue
        centre = np.median(axis_points[members, :2], axis=0)
        distance = float(np.linalg.norm(centre - snapped_seed[:2]))
        ranked.append(
            (
                (distance, -span, -float(bin_count), -float(members.size), cell[0], cell[1]),
                np.asarray(centre, dtype=np.float64),
            )
        )
    ranked.sort(key=lambda item: item[0])
    candidates = [np.asarray(snapped_seed[:2], dtype=np.float64)]
    for _key, centre in ranked:
        if any(
            float(np.linalg.norm(centre - existing))
            < parameters.candidate_merge_radius_m
            for existing in candidates
        ):
            continue
        candidates.append(centre)
        if len(candidates) >= parameters.max_axis_hypotheses:
            break
    return candidates


def _axis_candidate_score(
    fit: PoleAxisFit,
    seed_distance: float,
    parameters: ManualPoleBaseParameters,
) -> float:
    seed_score = _clip(1.0 - (seed_distance / parameters.axis_seed_gate_m))
    rmse_score = _clip(1.0 - (fit.radial_rmse_m / parameters.max_axis_rmse_m))
    span_score = _clip(
        (fit.vertical_span_m - parameters.min_vertical_span_m)
        / max(1e-9, 4.0 - parameters.min_vertical_span_m)
    )
    occupancy_score = _clip(fit.vertical_occupancy_ratio)
    consecutive_score = _clip(
        fit.longest_consecutive_bin_count / max(1, fit.vertical_bin_count)
    )
    tilt_score = _clip(1.0 - (fit.tilt_deg / parameters.max_axis_tilt_deg))
    return float(
        (0.30 * seed_score)
        + (0.20 * rmse_score)
        + (0.15 * span_score)
        + (0.15 * occupancy_score)
        + (0.10 * consecutive_score)
        + (0.10 * tilt_score)
    )


def _axis_cross_section_shape(
    points: np.ndarray,
    fit: PoleAxisFit,
) -> _CrossSectionShape | None:
    """Summarize the inlier cross-section without changing the shared fitter.

    A vertical wall can have an excellent line RMSE after a narrow cylinder is
    cut from it. Its residuals nevertheless occupy a wide, nearly one-
    dimensional strip. A shaft is either narrow or has support around several
    angular sectors. Robust quantiles and a median-centred covariance make
    the distinction insensitive to a small amount of nearby clutter.
    """

    indices = np.asarray(fit.inlier_indices, dtype=np.int64)
    if indices.size < 6:
        return None
    inliers = points[indices]
    design = np.column_stack(
        (
            inliers[:, 2] - fit.z_reference,
            np.ones(inliers.shape[0], dtype=np.float64),
        )
    )
    predicted_xy = design @ fit.coefficients
    residuals = np.asarray(inliers[:, :2] - predicted_xy, dtype=np.float64)
    if residuals.shape[0] < 6 or not np.all(np.isfinite(residuals)):
        return None

    radii = np.linalg.norm(residuals, axis=1)
    radial_q90 = float(np.quantile(radii, 0.90))
    centred = residuals - np.median(residuals, axis=0)[None, :]
    covariance = (centred.T @ centred) / float(max(1, centred.shape[0] - 1))
    eigenvalues = np.linalg.eigvalsh(covariance)
    major = max(0.0, float(eigenvalues[-1]))
    minor = max(0.0, float(eigenvalues[0]))
    minor_major_ratio = 1.0 if major <= 1e-12 else float(minor / major)

    angles = np.mod(np.arctan2(residuals[:, 1], residuals[:, 0]), 2.0 * math.pi)
    sector_ids = np.floor(angles * (8.0 / (2.0 * math.pi))).astype(np.int64)
    sector_ids = np.clip(sector_ids, 0, 7)
    sector_counts = np.bincount(sector_ids, minlength=8)
    minimum_sector_support = max(2, math.ceil(residuals.shape[0] * 0.02))
    occupied_sectors = int(np.count_nonzero(sector_counts >= minimum_sector_support))
    return _CrossSectionShape(
        radial_q90_m=radial_q90,
        minor_major_ratio=minor_major_ratio,
        occupied_sector_count=occupied_sectors,
    )


def _is_wide_planar_cross_section(
    points: np.ndarray,
    fit: PoleAxisFit,
    parameters: ManualPoleBaseParameters,
) -> bool:
    shape = _axis_cross_section_shape(points, fit)
    if shape is None:
        return False
    # Only apply the shape rejection to a genuinely wide section. This keeps
    # narrow and partially scanned shafts, while a wall strip admitted by the
    # 0.15 m shared inlier gate has q90 around 0.13 m.
    wide_radius = max(0.10, parameters.axis_inlier_radius_m * 0.60)
    return bool(
        shape.radial_q90_m >= wide_radius
        and (
            shape.minor_major_ratio < 0.18
            and shape.occupied_sector_count < 4
        )
    )


def _axis_candidates(
    points: np.ndarray,
    snapped_seed: np.ndarray,
    classes: np.ndarray | None,
    parameters: ManualPoleBaseParameters,
) -> list[_AxisCandidate]:
    finite = np.all(np.isfinite(points), axis=1)
    radial = np.linalg.norm(points[:, :2] - snapped_seed[None, :2], axis=1)
    eligible_mask = (
        finite
        & (points[:, 2] >= snapped_seed[2] - 12.0)
        & (points[:, 2] <= snapped_seed[2] + 4.0)
    )
    if classes is not None and parameters.excluded_axis_class_ids:
        eligible_mask &= ~np.isin(classes, parameters.excluded_axis_class_ids)

    # Discovery remains seed-local. Once it produces a line, expansion uses
    # the complete finite/class-filtered API crop so a tilted pole clicked near
    # its top can recover lower returns that have drifted outside 0.75 m XY.
    discovery_mask = eligible_mask & (radial <= parameters.axis_search_radius_m)
    discovery_indices = np.flatnonzero(discovery_mask).astype(np.int64, copy=False)
    axis_points = points[discovery_indices]
    if axis_points.shape[0] < parameters.min_axis_points:
        return []
    pool_indices = np.flatnonzero(eligible_mask).astype(np.int64, copy=False)
    pool_points = points[pool_indices]
    shared_parameters = parameters.pole_parameters()
    candidates: list[_AxisCandidate] = []
    for centre in _axis_hypothesis_centres(axis_points, snapped_seed, parameters):
        cylinder = (
            np.linalg.norm(axis_points[:, :2] - centre[None, :], axis=1)
            <= parameters.axis_cluster_radius_m
        )
        if int(np.count_nonzero(cylinder)) < parameters.min_axis_points:
            continue
        fit = fit_pole_axis(
            axis_points[cylinder],
            shared_parameters,
            source_indices=discovery_indices[cylinder],
        )
        if fit is None:
            continue
        # A fixed seed-centred cylinder sees only a middle slice of a genuinely
        # tilted shaft.  Grow along the first fitted line, then refit, so upper
        # clicks still recover lower returns without widening into nearby poles.
        for _ in range(2):
            design = np.column_stack(
                (
                    pool_points[:, 2] - fit.z_reference,
                    np.ones(pool_points.shape[0], dtype=np.float64),
                )
            )
            predicted_xy = design @ fit.coefficients
            expanded = (
                np.linalg.norm(pool_points[:, :2] - predicted_xy, axis=1)
                <= parameters.axis_cluster_radius_m
            )
            if int(np.count_nonzero(expanded)) < parameters.min_axis_points:
                break
            expanded_fit = fit_pole_axis(
                pool_points[expanded],
                shared_parameters,
                source_indices=pool_indices[expanded],
            )
            if expanded_fit is None:
                break
            unchanged = np.array_equal(
                np.sort(expanded_fit.inlier_indices),
                np.sort(fit.inlier_indices),
            )
            fit = expanded_fit
            if unchanged:
                break
        seed_axis_xy = fit.xy_at_z(float(snapped_seed[2]))
        seed_distance = float(np.linalg.norm(seed_axis_xy - snapped_seed[:2]))
        if (
            seed_distance > parameters.axis_seed_gate_m + 1e-9
            or fit.radial_rmse_m > parameters.max_axis_rmse_m
            or fit.tilt_deg > parameters.max_axis_tilt_deg
            or _is_wide_planar_cross_section(points, fit, parameters)
        ):
            continue
        candidate = _AxisCandidate(
            fit=fit,
            seed_distance_m=seed_distance,
            score=_axis_candidate_score(fit, seed_distance, parameters),
        )
        existing_index = next(
            (
                index
                for index, existing in enumerate(candidates)
                if float(
                    np.linalg.norm(
                        existing.fit.xy_at_z(float(snapped_seed[2])) - seed_axis_xy
                    )
                )
                < parameters.candidate_merge_radius_m
            ),
            None,
        )
        if existing_index is None:
            candidates.append(candidate)
        else:
            existing = candidates[existing_index]
            candidate_key = (
                candidate.score,
                -candidate.seed_distance_m,
                candidate.fit.vertical_span_m,
                -candidate.fit.radial_rmse_m,
            )
            existing_key = (
                existing.score,
                -existing.seed_distance_m,
                existing.fit.vertical_span_m,
                -existing.fit.radial_rmse_m,
            )
            if candidate_key > existing_key:
                candidates[existing_index] = candidate
    candidates.sort(
        key=lambda item: (
            -item.score,
            item.seed_distance_m,
            item.fit.radial_rmse_m,
            -item.fit.vertical_span_m,
            float(item.fit.point[0]),
            float(item.fit.point[1]),
        )
    )
    return candidates


def _public_axis(candidate: _AxisCandidate) -> ManualPoleAxisResult:
    fit = candidate.fit
    return ManualPoleAxisResult(
        point=np.asarray(fit.point, dtype=np.float64),
        direction=np.asarray(fit.direction, dtype=np.float64),
        point_count=fit.point_count,
        observed_z_min=fit.observed_z_min,
        observed_z_max=fit.observed_z_max,
        vertical_span_m=fit.vertical_span_m,
        vertical_bin_count=fit.vertical_bin_count,
        longest_consecutive_bin_count=fit.longest_consecutive_bin_count,
        occupancy_ratio=fit.vertical_occupancy_ratio,
        rmse_m=fit.radial_rmse_m,
        tilt_deg=fit.tilt_deg,
        seed_distance_m=candidate.seed_distance_m,
    )


def _cell_representatives(
    points: np.ndarray,
    mask: np.ndarray,
    reference_xy: np.ndarray,
    parameters: ManualPoleBaseParameters,
    *,
    classified: bool,
) -> tuple[np.ndarray, np.ndarray]:
    selected = points[np.asarray(mask, dtype=bool)]
    if selected.shape[0] < parameters.ground_min_cells:
        return np.empty((0, 3), dtype=np.float64), np.empty((0, 2), dtype=np.int64)
    cells = np.floor(
        (selected[:, :2] - reference_xy[None, :]) / parameters.ground_cell_size_m
    ).astype(np.int64)
    groups: dict[tuple[int, int], list[int]] = {}
    for index, cell in enumerate(cells):
        groups.setdefault((int(cell[0]), int(cell[1])), []).append(index)
    samples: list[np.ndarray] = []
    sample_cells: list[tuple[int, int]] = []
    quantile = 0.50 if classified else parameters.ground_cell_quantile
    for cell in sorted(groups):
        members = selected[np.asarray(groups[cell], dtype=np.int64)]
        samples.append(
            np.asarray(
                [
                    float(np.median(members[:, 0])),
                    float(np.median(members[:, 1])),
                    float(np.quantile(members[:, 2], quantile)),
                ],
                dtype=np.float64,
            )
        )
        sample_cells.append(cell)
    return np.asarray(samples, dtype=np.float64), np.asarray(sample_cells, dtype=np.int64)


def _surface_components(
    samples: np.ndarray,
    cells: np.ndarray,
    reference_xy: np.ndarray,
    parameters: ManualPoleBaseParameters,
) -> list[np.ndarray]:
    if samples.shape[0] == 0:
        return []
    cell_lookup = {(int(cell[0]), int(cell[1])): index for index, cell in enumerate(cells)}
    adjacency: list[list[int]] = [[] for _ in range(samples.shape[0])]
    slope_allowance = math.tan(math.radians(20.0))
    for index, cell in enumerate(cells):
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                if delta_x == 0 and delta_y == 0:
                    continue
                neighbor = cell_lookup.get((int(cell[0]) + delta_x, int(cell[1]) + delta_y))
                if neighbor is None or neighbor <= index:
                    continue
                xy_distance = float(np.linalg.norm(samples[index, :2] - samples[neighbor, :2]))
                height_limit = parameters.ground_surface_step_m + (
                    slope_allowance * xy_distance
                )
                if abs(float(samples[index, 2] - samples[neighbor, 2])) <= height_limit:
                    adjacency[index].append(neighbor)
                    adjacency[neighbor].append(index)
    order = np.lexsort(
        (
            samples[:, 2],
            samples[:, 1],
            samples[:, 0],
            np.linalg.norm(samples[:, :2] - reference_xy[None, :], axis=1),
        )
    )
    visited = np.zeros(samples.shape[0], dtype=bool)
    components: list[np.ndarray] = []
    for start in order:
        start_index = int(start)
        if visited[start_index]:
            continue
        stack = [start_index]
        visited[start_index] = True
        members: list[int] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbor in sorted(adjacency[current], reverse=True):
                if not visited[neighbor]:
                    visited[neighbor] = True
                    stack.append(neighbor)
        if len(members) >= parameters.ground_min_cells:
            components.append(np.asarray(sorted(members), dtype=np.int64))
    components.sort(
        key=lambda indices: (
            float(
                np.min(
                    np.linalg.norm(
                        samples[indices, :2] - reference_xy[None, :], axis=1
                    )
                )
            ),
            -int(indices.size),
            float(np.median(samples[indices, 2])),
        )
    )
    return components


def _fit_ground_component(
    samples: np.ndarray,
    reference_xy: np.ndarray,
    parameters: ManualPoleBaseParameters,
    *,
    method: str,
    candidate_cell_count: int,
) -> _GroundCandidate | None:
    if samples.shape[0] < parameters.ground_min_cells:
        return None
    keep = np.ones(samples.shape[0], dtype=bool)
    coefficients = np.zeros(3, dtype=np.float64)
    for _ in range(5):
        design = np.column_stack(
            (
                samples[keep, 0] - reference_xy[0],
                samples[keep, 1] - reference_xy[1],
                np.ones(int(np.count_nonzero(keep)), dtype=np.float64),
            )
        )
        coefficients, *_ = np.linalg.lstsq(design, samples[keep, 2], rcond=None)
        all_design = np.column_stack(
            (
                samples[:, 0] - reference_xy[0],
                samples[:, 1] - reference_xy[1],
                np.ones(samples.shape[0], dtype=np.float64),
            )
        )
        residuals = samples[:, 2] - (all_design @ coefficients)
        median = float(np.median(residuals[keep]))
        mad = float(np.median(np.abs(residuals[keep] - median)))
        threshold = max(
            0.03,
            min(parameters.ground_max_rmse_m, 3.0 * 1.4826 * mad),
        )
        next_keep = np.abs(residuals - median) <= threshold
        if int(np.count_nonzero(next_keep)) < parameters.ground_min_cells:
            break
        if np.array_equal(next_keep, keep):
            keep = next_keep
            break
        keep = next_keep
    selected = samples[keep]
    support_vectors = selected[:, :2] - reference_xy[None, :]
    support_angles = np.mod(
        np.arctan2(support_vectors[:, 1], support_vectors[:, 0]),
        2.0 * math.pi,
    )
    occupied_sector_count = int(
        np.unique(np.floor(support_angles / (2.0 * math.pi / 8.0)).astype(np.int64)).size
    )
    centered_xy = selected[:, :2] - np.mean(selected[:, :2], axis=0, keepdims=True)
    covariance = (centered_xy.T @ centered_xy) / float(selected.shape[0])
    eigenvalues = np.linalg.eigvalsh(covariance)
    minor_spread_m = math.sqrt(max(0.0, float(eigenvalues[0])))
    # A ground plane needs support in at least three azimuth sectors and enough
    # absolute width to constrain both slopes.  Absolute minor-axis spread, unlike
    # an eigenvalue ratio, keeps broad but elongated one-sided road/sidewalk
    # observations while rejecting a narrow guardrail ribbon.
    if (
        occupied_sector_count < 3
        or minor_spread_m < (0.5 * parameters.ground_cell_size_m)
    ):
        return None
    design = np.column_stack(
        (
            selected[:, 0] - reference_xy[0],
            selected[:, 1] - reference_xy[1],
            np.ones(selected.shape[0], dtype=np.float64),
        )
    )
    coefficients, *_ = np.linalg.lstsq(design, selected[:, 2], rcond=None)
    residuals = selected[:, 2] - (design @ coefficients)
    rmse = float(np.sqrt(np.mean(np.square(residuals))))
    if not math.isfinite(rmse) or rmse > parameters.ground_max_rmse_m:
        return None
    support_distance = max(
        0.0,
        float(
            np.min(
                np.linalg.norm(selected[:, :2] - reference_xy[None, :], axis=1)
            )
        )
        - (parameters.ground_cell_size_m / math.sqrt(2.0)),
    )
    estimate = GroundEstimate(
        z=float(coefficients[2]),
        rmse_m=rmse,
        cell_count=int(selected.shape[0]),
        candidate_cell_count=int(candidate_cell_count),
        method=f"{method}_anchored_plane",
        support_xyz=np.asarray(selected, dtype=np.float64),
        plane_coefficients=np.asarray(coefficients, dtype=np.float64),
        reference_xy=np.asarray(reference_xy, dtype=np.float64),
    )
    return _GroundCandidate(estimate, support_distance)


def _ground_hypothesis(
    points: np.ndarray,
    classes: np.ndarray | None,
    fit: PoleAxisFit,
    parameters: ManualPoleBaseParameters,
    *,
    classified: bool,
) -> _GroundCandidate | None:
    reference_xy = fit.xy_at_z(fit.observed_z_min)
    radial = np.linalg.norm(points[:, :2] - reference_xy[None, :], axis=1)
    mask = (
        np.all(np.isfinite(points), axis=1)
        & (radial >= parameters.ground_exclusion_radius_m)
        & (radial <= parameters.ground_search_radius_m)
        & (points[:, 2] <= fit.observed_z_min + parameters.max_ground_penetration_m)
    )
    if classified:
        if classes is None or not parameters.ground_class_ids:
            return None
        mask &= np.isin(classes, parameters.ground_class_ids)
    elif classes is not None:
        non_ground_class_ids = tuple(
            class_id
            for class_id in parameters.excluded_axis_class_ids
            if class_id not in parameters.ground_class_ids
        )
        if non_ground_class_ids:
            mask &= ~np.isin(classes, non_ground_class_ids)
    samples, cells = _cell_representatives(
        points,
        mask,
        reference_xy,
        parameters,
        classified=classified,
    )
    candidates: list[_GroundCandidate] = []
    for component in _surface_components(samples, cells, reference_xy, parameters):
        fitted = _fit_ground_component(
            samples[component],
            reference_xy,
            parameters,
            method="classified" if classified else "geometry",
            candidate_cell_count=int(samples.shape[0]),
        )
        if fitted is not None:
            candidates.append(fitted)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item.nearest_support_distance_m,
            item.estimate.rmse_m,
            -item.estimate.cell_count,
            item.estimate.z,
        )
    )
    nearest = candidates[0]
    nearest_bottom_gap = fit.observed_z_min - nearest.estimate.z
    dominant = [
        item
        for item in candidates
        if item.estimate.cell_count
        >= (0.50 * max(1, item.estimate.candidate_cell_count))
    ]
    # Geometry fallback can contain a compact floating canopy plus a much broader
    # outer ground surface.  Prefer the component that explains most ground cells
    # unless the nearest surface is in direct contact with the observed shaft
    # bottom; that contact exception preserves small upper installation surfaces.
    if dominant and not (
        -parameters.max_ground_penetration_m
        <= nearest_bottom_gap
        <= parameters.max_ground_penetration_m
    ):
        dominant.sort(
            key=lambda item: (
                item.nearest_support_distance_m,
                item.estimate.rmse_m,
                -item.estimate.cell_count,
                item.estimate.z,
            )
        )
        return dominant[0]
    return nearest


def _select_ground(
    points: np.ndarray,
    classes: np.ndarray | None,
    fit: PoleAxisFit,
    parameters: ManualPoleBaseParameters,
) -> tuple[_GroundCandidate | None, bool]:
    classified = _ground_hypothesis(
        points,
        classes,
        fit,
        parameters,
        classified=True,
    )
    geometry = _ground_hypothesis(
        points,
        classes,
        fit,
        parameters,
        classified=False,
    )
    if classified is None:
        return geometry, False
    if geometry is None:
        return classified, False
    height_difference = abs(classified.estimate.z - geometry.estimate.z)
    if height_difference < 0.15:
        return min(
            (classified, geometry),
            key=lambda item: (
                item.nearest_support_distance_m,
                item.estimate.rmse_m,
                0 if item.estimate.method.startswith("classified") else 1,
            ),
        ), False

    classified_gap = fit.observed_z_min - classified.estimate.z
    geometry_gap = fit.observed_z_min - geometry.estimate.z
    classified_valid = (
        -parameters.max_ground_penetration_m
        <= classified_gap
        <= parameters.max_bottom_gap_review_m
    )
    geometry_valid = (
        -parameters.max_ground_penetration_m
        <= geometry_gap
        <= parameters.max_bottom_gap_review_m
    )
    if classified_valid != geometry_valid:
        return (classified if classified_valid else geometry), False
    support_difference = (
        classified.nearest_support_distance_m - geometry.nearest_support_distance_m
    )
    geometry_component_share = geometry.estimate.cell_count / max(
        1,
        geometry.estimate.candidate_cell_count,
    )
    geometry_is_confident_contact_surface = (
        geometry_component_share >= 0.50
        and -parameters.max_ground_penetration_m
        <= geometry_gap
        <= parameters.max_ground_penetration_m
        and geometry.estimate.rmse_m
        <= max(0.04, classified.estimate.rmse_m + 0.03)
    )
    if support_difference <= -0.12:
        return classified, False
    if support_difference >= 0.12:
        if geometry_is_confident_contact_surface:
            return geometry, False
        return classified, True
    if (
        geometry.nearest_support_distance_m < classified.nearest_support_distance_m
        and geometry_is_confident_contact_surface
    ):
        return geometry, True
    return classified, True


def _public_ground(
    candidate: _GroundCandidate,
    base: np.ndarray,
    parameters: ManualPoleBaseParameters,
) -> ManualPoleGroundResult:
    estimate = candidate.estimate
    support_distance = max(
        0.0,
        float(
            np.min(
                np.linalg.norm(estimate.support_xyz[:, :2] - base[None, :2], axis=1)
            )
        )
        - (parameters.ground_cell_size_m / math.sqrt(2.0)),
    )
    return ManualPoleGroundResult(
        method=estimate.method,
        z_at_base=float(base[2]),
        rmse_m=float(estimate.rmse_m),
        cell_count=int(estimate.cell_count),
        candidate_cell_count=int(estimate.candidate_cell_count),
        nearest_support_distance_m=support_distance,
        plane_coefficients=np.asarray(estimate.plane_coefficients, dtype=np.float64),
        reference_xy=np.asarray(estimate.reference_xy, dtype=np.float64),
        support_xyz=np.asarray(estimate.support_xyz, dtype=np.float64),
    )


def _quality(
    candidate: _AxisCandidate,
    ground: ManualPoleGroundResult,
    bottom_gap: float,
    candidate_count: int,
    ambiguous: bool,
    parameters: ManualPoleBaseParameters,
) -> ManualPoleBaseQuality:
    fit = candidate.fit
    seed_score = _clip(1.0 - (candidate.seed_distance_m / parameters.axis_seed_gate_m))
    axis_score = _clip(1.0 - (fit.radial_rmse_m / parameters.max_axis_rmse_m))
    span_score = _clip(
        (fit.vertical_span_m - parameters.min_vertical_span_m)
        / max(1e-9, 4.0 - parameters.min_vertical_span_m)
    )
    continuity_score = _clip(fit.vertical_occupancy_ratio)
    support_score = _clip(
        1.0
        - (
            ground.nearest_support_distance_m
            / parameters.max_ground_support_distance_review_m
        )
    )
    ground_score = _clip(1.0 - (ground.rmse_m / parameters.ground_max_rmse_m)) * support_score
    if bottom_gap <= parameters.max_bottom_gap_auto_m:
        bottom_score = 1.0
    else:
        bottom_score = _clip(
            (parameters.max_bottom_gap_review_m - bottom_gap)
            / (
                parameters.max_bottom_gap_review_m
                - parameters.max_bottom_gap_auto_m
            )
        )
    components = {
        "seed": seed_score,
        "axis": axis_score,
        "span": span_score,
        "continuity": continuity_score,
        "ground": ground_score,
        "bottom_gap": bottom_score,
    }
    score = float(
        (0.20 * seed_score)
        + (0.20 * axis_score)
        + (0.15 * span_score)
        + (0.10 * continuity_score)
        + (0.25 * ground_score)
        + (0.10 * bottom_score)
    )
    return ManualPoleBaseQuality(
        score=score,
        candidate_count=candidate_count,
        ambiguous=ambiguous,
        bottom_gap_m=float(bottom_gap),
        components=components,
    )


def infer_pole_base_from_seed(
    points_xyz: np.ndarray,
    seed_xyz: np.ndarray,
    *,
    classifications: np.ndarray | None = None,
    parameters: ManualPoleBaseParameters = _DEFAULT_MANUAL_POLE_BASE_PARAMETERS,
) -> ManualPoleBaseResult:
    """Infer a pole's PointZ base from one clicked full-resolution return.

    The function is pure and deterministic.  It never reads files and never
    mutates an overlay; the web endpoint is responsible only for collecting a
    bounded local record set and serializing this result.
    """

    points, seed, classes = _validate_inputs(points_xyz, seed_xyz, classifications)
    if points.shape[0] == 0 or not np.any(np.all(np.isfinite(points), axis=1)):
        return failed_manual_pole_base(seed, ["NO_LOCAL_POINTS"])
    snapped_seed, _snap_distance = _snap_seed(
        points,
        seed,
        parameters.seed_snap_radius_m,
    )
    if snapped_seed is None:
        return failed_manual_pole_base(seed, ["SEED_NOT_ON_SOURCE_POINT"])

    candidates = _axis_candidates(points, snapped_seed, classes, parameters)
    if not candidates:
        finite = points[np.all(np.isfinite(points), axis=1)]
        reasons = ["NO_VERTICAL_AXIS"]
        if finite.shape[0] >= parameters.min_axis_points:
            radial = np.linalg.norm(finite[:, :2] - snapped_seed[None, :2], axis=1)
            local = finite[radial <= parameters.axis_search_radius_m]
            if local.shape[0] and float(np.ptp(local[:, 2])) < parameters.min_vertical_span_m:
                reasons.append("AXIS_TOO_SHORT")
        return failed_manual_pole_base(
            seed,
            reasons,
            snapped_seed=snapped_seed,
        )

    selected = candidates[0]
    ambiguous = False
    if len(candidates) > 1:
        runner_up = candidates[1]
        separation = float(
            np.linalg.norm(
                selected.fit.xy_at_z(float(snapped_seed[2]))
                - runner_up.fit.xy_at_z(float(snapped_seed[2]))
            )
        )
        ambiguous = (
            selected.score - runner_up.score < parameters.ambiguity_score_margin
            and separation >= parameters.candidate_merge_radius_m
        )
    public_axis = _public_axis(selected)

    ground_candidate, ground_conflict = _select_ground(
        points,
        classes,
        selected.fit,
        parameters,
    )
    axis_debug = points[selected.fit.inlier_indices[:128]]
    if ground_candidate is None:
        return failed_manual_pole_base(
            seed,
            ["NO_GROUND_SUPPORT"],
            snapped_seed=snapped_seed,
            axis=public_axis,
            candidate_count=len(candidates),
            ambiguous=ambiguous,
            debug_points=axis_debug,
        )

    base = intersect_pole_axis_with_ground(selected.fit, ground_candidate.estimate)
    if base is None or base.shape != (3,) or not np.all(np.isfinite(base)):
        return failed_manual_pole_base(
            seed,
            ["NO_GROUND_SUPPORT"],
            snapped_seed=snapped_seed,
            axis=public_axis,
            candidate_count=len(candidates),
            ambiguous=ambiguous,
            debug_points=axis_debug,
        )
    public_ground = _public_ground(ground_candidate, base, parameters)
    debug_points = np.vstack((axis_debug, public_ground.support_xyz[:128]))
    bottom_gap = float(selected.fit.observed_z_min - base[2])
    reasons: list[str] = []
    hard_failure = False
    if ambiguous:
        reasons.append("AMBIGUOUS_AXES")
    if ground_conflict:
        reasons.append("GROUND_HYPOTHESES_CONFLICT")
    if public_ground.nearest_support_distance_m > parameters.max_ground_support_distance_auto_m:
        reasons.append("GROUND_TOO_FAR")
    if public_ground.nearest_support_distance_m > parameters.max_ground_support_distance_review_m:
        hard_failure = True
    if bottom_gap > parameters.max_bottom_gap_auto_m:
        reasons.append("BOTTOM_EXTRAPOLATED")
    # A missing lower shaft is the reason this operator tool exists.  Keep a
    # trustworthy axis/ground intersection as a REVIEW proposal even when no
    # source return exists for several metres below the observed shaft.  The
    # separate hard cap still rejects implausibly remote intersections.
    if bottom_gap > parameters.max_bottom_gap_hard_m:
        hard_failure = True
    if bottom_gap < -parameters.max_ground_penetration_m:
        reasons.append("GROUND_PENETRATION")
    fitted_seed_xy = selected.fit.xy_at_z(float(snapped_seed[2]))
    fitted_base_xy = selected.fit.xy_at_z(float(base[2]))
    fitted_lateral_shift = float(np.linalg.norm(fitted_base_xy - fitted_seed_xy))
    base_seed_distance_limit = max(
        parameters.max_base_seed_xy_distance_m,
        fitted_lateral_shift + parameters.axis_seed_gate_m,
    )
    base_seed_distances = (
        float(np.linalg.norm(base[:2] - snapped_seed[:2])),
        float(np.linalg.norm(base[:2] - seed[:2])),
    )
    if max(base_seed_distances) > parameters.max_base_seed_xy_distance_m:
        reasons.append("BASE_OUTSIDE_LOCAL_WINDOW")
    if max(base_seed_distances) > base_seed_distance_limit:
        hard_failure = True

    quality = _quality(
        selected,
        public_ground,
        bottom_gap,
        len(candidates),
        ambiguous,
        parameters,
    )
    ordered_reasons = _ordered_reasons(reasons)
    warnings = tuple(
        _WARNING_TEXT[code] for code in ordered_reasons if code in _WARNING_TEXT
    )
    if hard_failure or quality.score < 0.55:
        return ManualPoleBaseResult(
            status="failed",
            seed_position=seed,
            snapped_seed_position=snapped_seed,
            base_position=None,
            axis=public_axis,
            ground=public_ground,
            quality=quality,
            reason_codes=ordered_reasons,
            warnings=warnings,
            debug_points=debug_points,
        )
    automatic = (
        quality.score >= 0.80
        and not ordered_reasons
        and public_ground.nearest_support_distance_m
        <= parameters.max_ground_support_distance_auto_m
        and bottom_gap <= parameters.max_bottom_gap_auto_m
        and not ambiguous
    )
    return ManualPoleBaseResult(
        status="auto" if automatic else "review",
        seed_position=seed,
        snapped_seed_position=snapped_seed,
        base_position=np.asarray(base, dtype=np.float64),
        axis=public_axis,
        ground=public_ground,
        quality=quality,
        reason_codes=ordered_reasons,
        warnings=warnings,
        debug_points=debug_points,
    )


__all__ = [
    "MANUAL_POLE_BASE_ALGORITHM",
    "MANUAL_POLE_BASE_ALGORITHM_VERSION",
    "REASON_CODES",
    "ManualPoleAxisResult",
    "ManualPoleBaseParameters",
    "ManualPoleBaseQuality",
    "ManualPoleBaseResult",
    "ManualPoleGroundResult",
    "failed_manual_pole_base",
    "infer_pole_base_from_seed",
]
