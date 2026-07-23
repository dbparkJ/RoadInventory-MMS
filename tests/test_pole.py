from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from mms_shp_detection.pole import (
    PoleSearchParameters,
    blocks_intersecting_bounds,
    cluster_pole_observations,
    estimate_local_ground,
    find_pole_bases,
    pole_candidate_rank_key,
)


def _pole_surface(
    x: float,
    y: float,
    z_min: float,
    z_max: float,
    *,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    for z in np.linspace(z_min, z_max, 36):
        for angle in np.linspace(0.0, 2.0 * np.pi, 6, endpoint=False):
            rows.append(
                [
                    x + (0.055 * np.cos(angle)) + rng.normal(0.0, 0.004),
                    y + (0.055 * np.sin(angle)) + rng.normal(0.0, 0.004),
                    z + rng.normal(0.0, 0.006),
                ]
            )
    return np.asarray(rows, dtype=np.float64)


def _ground_surface(center_x: float, center_y: float) -> np.ndarray:
    rows: list[list[float]] = []
    for dx in np.linspace(-1.4, 1.4, 15):
        for dy in np.linspace(-1.4, 1.4, 15):
            if np.hypot(dx, dy) < 0.28:
                continue
            x = center_x + dx
            y = center_y + dy
            z = 0.05 * x + 0.02 * y
            rows.append([x, y, z])
    return np.asarray(rows, dtype=np.float64)


def _horizontal_arm(
    x_start: float,
    x_end: float,
    y: float,
    z: float,
) -> np.ndarray:
    rows: list[list[float]] = []
    for x in np.linspace(x_start, x_end, 80):
        rows.extend(
            (
                [x, y - 0.02, z],
                [x, y, z + 0.02],
                [x, y + 0.02, z - 0.02],
            )
        )
    return np.asarray(rows, dtype=np.float64)


class PoleSearchTests(unittest.TestCase):
    def test_rank_balances_remote_arm_length_with_shaft_quality(self) -> None:
        parameters = PoleSearchParameters()
        actual = SimpleNamespace(
            completeness_ratio=1.0,
            association_distance_m=4.187,
            horizontal_connection_coverage_ratio=13 / 17,
            horizontal_connection_expected_bin_count=17,
            radial_rmse_m=0.0628,
            multi_return_fraction=0.0293,
            score=7.30,
            point_count=1673,
        )
        vegetation = SimpleNamespace(
            completeness_ratio=0.9524,
            association_distance_m=6.288,
            horizontal_connection_coverage_ratio=18 / 26,
            horizontal_connection_expected_bin_count=26,
            radial_rmse_m=0.0945,
            multi_return_fraction=0.1475,
            score=5.87,
            point_count=617,
        )

        self.assertLess(
            pole_candidate_rank_key(actual, parameters),
            pole_candidate_rank_key(vegetation, parameters),
        )

    def test_direct_rank_uses_bounded_score_before_tiny_completeness_delta(self) -> None:
        parameters = PoleSearchParameters()
        actual = SimpleNamespace(
            completeness_ratio=0.9091,
            association_distance_m=0.038,
            radial_rmse_m=0.0839,
            multi_return_fraction=0.008,
            score=3.66,
            point_count=880,
        )
        adjacent_structure = SimpleNamespace(
            completeness_ratio=0.9220,
            association_distance_m=0.704,
            radial_rmse_m=0.1041,
            multi_return_fraction=0.0,
            score=2.99,
            point_count=1002,
        )

        self.assertLess(
            pole_candidate_rank_key(actual, parameters),
            pole_candidate_rank_key(adjacent_structure, parameters),
        )

    def test_occluded_pole_axis_is_extended_to_local_ground(self) -> None:
        pole = _pole_surface(1.0, 0.2, 1.35, 4.8, seed=1)
        ground = _ground_surface(1.0, 0.2)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([1.0, 0.2, 5.0]),
            PoleSearchParameters(),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.pole_type, "SINGLE")
        self.assertTrue(result.occluded_bottom)
        self.assertEqual(result.method, "GROUND_EXTR")
        np.testing.assert_allclose(result.representative_xyz[:2], [1.0, 0.2], atol=0.06)
        self.assertAlmostEqual(result.representative_xyz[2], 0.054, delta=0.08)
        self.assertGreater(result.candidates[0].bottom_gap_m or 0.0, 1.0)
        candidate = result.candidates[0]
        self.assertLess(candidate.observed_z_min, 1.40)
        self.assertGreater(candidate.observed_z_max, 4.75)
        self.assertGreaterEqual(candidate.longest_consecutive_bin_count, 4)
        self.assertLess(candidate.max_observed_z_gap_m, 0.2)
        self.assertGreater(candidate.vertical_occupancy_ratio, 0.8)
        self.assertGreater(candidate.middle_support_coverage_ratio or 0.0, 0.7)

    def test_ground_class_points_are_excluded_from_axis_but_used_for_ground(self) -> None:
        pole = _pole_surface(0.4, 0.1, 1.2, 4.8, seed=111)
        ground = _ground_surface(0.4, 0.1)
        # Dense ground-class returns directly below the pole would previously
        # be eligible axis points whenever the corridor included the road.
        rng = np.random.default_rng(112)
        central_ground = np.column_stack(
            (
                0.4 + rng.normal(0.0, 0.03, 80),
                0.1 + rng.normal(0.0, 0.03, 80),
                0.02 + rng.normal(0.0, 0.01, 80),
            )
        )
        neighborhood = np.vstack((pole, central_ground, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0] + central_ground.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(central_ground.shape[0] + ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertTrue(np.all(classifications[candidate.point_indices] == 84))
        self.assertGreater(candidate.observed_z_min, 1.1)
        self.assertIsNotNone(candidate.ground_z)
        self.assertAlmostEqual(candidate.ground_z or 0.0, 0.02, delta=0.08)

    def test_road_and_sign_points_with_empty_middle_are_not_a_pole(self) -> None:
        upper_object = _pole_surface(0.4, 0.1, 4.05, 4.90, seed=121)
        ground = _ground_surface(0.4, 0.1)
        rng = np.random.default_rng(122)
        aligned_road = np.column_stack(
            (
                0.4 + rng.normal(0.0, 0.04, 81),
                0.1 + rng.normal(0.0, 0.04, 81),
                0.02 + rng.normal(0.0, 0.008, 81),
            )
        )
        neighborhood = np.vstack((upper_object, aligned_road, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: upper_object.shape[0] + aligned_road.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(upper_object.shape[0], 6, dtype=np.int16),
                np.full(aligned_road.shape[0] + ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 4.44]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNone(result)

    def test_moderately_occluded_middle_still_has_enough_continuity(self) -> None:
        pole = _pole_surface(0.6, -0.2, 0.9, 4.8, seed=131)
        # Preserve a real column while simulating two partial occlusions.  Both
        # gaps stay below the default one-metre observed-gap ceiling.
        pole = pole[
            ~(
                ((pole[:, 2] > 2.0) & (pole[:, 2] < 2.45))
                | ((pole[:, 2] > 3.25) & (pole[:, 2] < 3.60))
            )
        ]
        ground = _ground_surface(0.6, -0.2)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertLess(candidate.max_observed_z_gap_m, 1.0)
        self.assertGreater(candidate.vertical_occupancy_ratio, 0.35)
        self.assertGreater(candidate.middle_support_coverage_ratio or 0.0, 0.30)

    def test_missing_ground_is_reported_as_unknown_not_visible(self) -> None:
        pole = _pole_surface(1.0, 0.2, 1.35, 4.8, seed=101)

        result = find_pole_bases(
            pole,
            np.ones(pole.shape[0], dtype=bool),
            np.asarray([1.0, 0.2, 5.0]),
            PoleSearchParameters(require_ground=False),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result.occluded_bottom)
        self.assertEqual(result.occlusion_status, "UNKNOWN")
        self.assertEqual(result.status, "REVIEW")
        self.assertIsNone(result.candidates[0].occluded_bottom)
        self.assertEqual(result.candidates[0].occlusion_status, "UNKNOWN")

    def test_two_poles_emit_one_real_support_instead_of_a_midpoint(self) -> None:
        left = _pole_surface(-0.8, 0.1, 0.08, 4.5, seed=2)
        right = _pole_surface(0.8, 0.1, 0.08, 4.5, seed=3)
        ground = np.vstack((_ground_surface(-0.8, 0.1), _ground_surface(0.8, 0.1)))
        neighborhood = np.vstack((left, right, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: left.shape[0] + right.shape[0]] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(
                direct_max_axis_sign_distance_m=1.0,
            ),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.pole_type, "SINGLE")
        self.assertEqual(len(result.candidates), 1)
        self.assertAlmostEqual(abs(result.representative_xyz[0]), 0.8, delta=0.08)
        self.assertAlmostEqual(result.representative_xyz[1], 0.1, delta=0.08)

    def test_partly_occluded_second_support_does_not_create_double_output(self) -> None:
        left = _pole_surface(-0.8, 0.1, 0.08, 4.8, seed=20)
        right = _pole_surface(0.8, 0.1, 0.8, 4.0, seed=21)
        ground = np.vstack((_ground_surface(-0.8, 0.1), _ground_surface(0.8, 0.1)))
        neighborhood = np.vstack((left, right, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: left.shape[0] + right.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(left.shape[0] + right.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(direct_max_axis_sign_distance_m=1.0),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.pole_type, "SINGLE")
        self.assertEqual(len(result.candidates), 1)

    def test_classified_ground_uses_unbiased_cell_medians(self) -> None:
        rng = np.random.default_rng(30)
        rows: list[list[float]] = []
        for x in np.linspace(-0.7, 0.7, 8):
            for y in np.linspace(-0.7, 0.7, 8):
                if np.hypot(x, y) < 0.25:
                    continue
                for _ in range(9):
                    rows.append([x, y, 10.0 + rng.normal(0.0, 0.08)])
        points = np.asarray(rows, dtype=np.float64)
        ground = estimate_local_ground(
            points,
            np.asarray([0.0, 0.0]),
            15.0,
            PoleSearchParameters(max_drop_m=8.0),
            np.full(points.shape[0], 2, dtype=np.int16),
        )

        self.assertIsNotNone(ground)
        assert ground is not None
        self.assertIn("classified", ground.method)
        self.assertAlmostEqual(ground.z, 10.0, delta=0.03)
        self.assertEqual(ground.support_xyz.shape, (ground.cell_count, 3))
        self.assertGreaterEqual(ground.candidate_cell_count, ground.cell_count)
        residuals = (
            ground.support_xyz[:, 2]
            - (
                (ground.support_xyz[:, 0] - ground.reference_xy[0])
                * ground.plane_coefficients[0]
                + (ground.support_xyz[:, 1] - ground.reference_xy[1])
                * ground.plane_coefficients[1]
                + ground.plane_coefficients[2]
            )
        )
        self.assertAlmostEqual(
            float(np.sqrt(np.mean(np.square(residuals)))),
            ground.rmse_m,
            places=10,
        )

    def test_nearer_geometry_surface_wins_over_classified_road(self) -> None:
        sidewalk_rows: list[list[float]] = []
        road_rows: list[list[float]] = []
        for angle in np.linspace(0.0, 2.0 * np.pi, 16, endpoint=False):
            sidewalk_rows.append(
                [0.31 * np.cos(angle), 0.31 * np.sin(angle), 0.46]
            )
            road_rows.append(
                [0.58 * np.cos(angle), 0.58 * np.sin(angle), 0.00]
            )
        sidewalk = np.repeat(np.asarray(sidewalk_rows), 5, axis=0)
        road = np.repeat(np.asarray(road_rows), 5, axis=0)
        points = np.vstack((sidewalk, road))
        classifications = np.concatenate(
            (
                np.full(sidewalk.shape[0], 1, dtype=np.int16),
                np.full(road.shape[0], 2, dtype=np.int16),
            )
        )

        ground = estimate_local_ground(
            points,
            np.asarray([0.0, 0.0]),
            4.0,
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(ground)
        assert ground is not None
        self.assertIn("geometry", ground.method)
        self.assertAlmostEqual(ground.z, 0.46, delta=0.03)

    def test_remote_axis_requires_horizontal_3d_connection(self) -> None:
        pole = _pole_surface(2.5, 0.0, 0.08, 4.9, seed=141)
        ground = _ground_surface(2.5, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNone(result)

    def test_direct_axis_does_not_require_horizontal_connection(self) -> None:
        pole = _pole_surface(0.3, 0.0, 0.08, 4.9, seed=146)
        ground = _ground_surface(0.3, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertLess(candidate.association_distance_m, 0.4)
        self.assertIsNone(candidate.horizontal_connection_coverage_ratio)

    def test_remote_axis_with_continuous_horizontal_arm_is_accepted(self) -> None:
        pole = _pole_surface(2.5, 0.0, 0.08, 4.9, seed=151)
        arm = _horizontal_arm(0.0, 2.5, 0.0, 5.0)
        ground = _ground_surface(2.5, 0.0)
        neighborhood = np.vstack((pole, arm, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0] + arm.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0] + arm.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.pole_type, "SINGLE")
        self.assertAlmostEqual(result.representative_xyz[0], 2.5, delta=0.08)
        candidate = result.candidates[0]
        self.assertGreater(candidate.association_distance_m, 2.4)
        self.assertGreaterEqual(
            candidate.horizontal_connection_coverage_ratio or 0.0,
            0.9,
        )

    def test_geometry_mode_does_not_starve_pole_in_large_clutter_component(self) -> None:
        pole = _pole_surface(0.52, 0.0, 0.08, 4.9, seed=152)
        clutter_rows: list[list[float]] = []
        # More than 32 vertically eligible cells form a transitive XY chain.
        # Each individual clutter column is too short to pass the final axis
        # test, but the old per-component seed cap let this chain hide the pole.
        for column_index, x in enumerate(np.linspace(-4.5, 4.5, 61)):
            y = 0.32 + (0.04 * np.sin(column_index))
            for z in np.linspace(1.1, 1.7, 8):
                clutter_rows.append([x, y, z])
        clutter = np.asarray(clutter_rows, dtype=np.float64)
        ground = _ground_surface(0.52, 0.0)
        neighborhood = np.vstack((pole, clutter, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0] + clutter.shape[0]] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            None,
        )

        self.assertIsNotNone(result)
        assert result is not None
        np.testing.assert_allclose(
            result.representative_xyz[:2],
            [0.52, 0.0],
            atol=0.08,
        )
        self.assertGreaterEqual(result.candidates[0].completeness_ratio or 0.0, 0.9)

    def test_complete_remote_axis_with_stronger_arm_beats_taller_clutter(self) -> None:
        support = _pole_surface(2.5, 0.0, 0.08, 4.9, seed=153)
        taller_clutter = _pole_surface(5.0, 0.0, 0.08, 7.2, seed=154)
        full_arm = _horizontal_arm(0.0, 2.5, 0.0, 5.0)
        partial_arm = _horizontal_arm(2.5, 3.2, 0.0, 5.0)
        ground = np.vstack((_ground_surface(2.5, 0.0), _ground_surface(5.0, 0.0)))
        neighborhood = np.vstack(
            (support, taller_clutter, full_arm, partial_arm, ground)
        )
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        candidate_count = (
            support.shape[0]
            + taller_clutter.shape[0]
            + full_arm.shape[0]
            + partial_arm.shape[0]
        )
        corridor[:candidate_count] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            None,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.representative_xyz[0], 2.5, delta=0.08)
        self.assertGreaterEqual(
            result.candidates[0].horizontal_connection_coverage_ratio or 0.0,
            0.9,
        )

    def test_geometry_remote_quality_gate_rejects_noisy_branch_axis(self) -> None:
        rng = np.random.default_rng(155)
        branch = _pole_surface(2.5, 0.0, 0.08, 4.9, seed=156)
        branch[:, :2] += rng.normal(0.0, 0.105, size=(branch.shape[0], 2))
        arm = _horizontal_arm(0.0, 2.5, 0.0, 5.0)
        ground = _ground_surface(2.5, 0.0)
        neighborhood = np.vstack((branch, arm, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: branch.shape[0] + arm.shape[0]] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            None,
        )

        self.assertIsNone(result)

    def test_return_metadata_is_reported_without_becoming_a_class_requirement(self) -> None:
        pole = _pole_surface(0.3, 0.0, 0.08, 4.9, seed=157)
        ground = _ground_surface(0.3, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        return_numbers = np.ones(neighborhood.shape[0], dtype=np.uint8)
        number_of_returns = np.ones(neighborhood.shape[0], dtype=np.uint8)
        number_of_returns[: pole.shape[0] : 2] = 2

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            None,
            return_numbers,
            number_of_returns,
        )

        self.assertIsNotNone(result)
        assert result is not None
        fraction = result.candidates[0].multi_return_fraction
        self.assertIsNotNone(fraction)
        self.assertAlmostEqual(float(fraction), 0.5, delta=0.08)

    def test_near_axis_wins_over_taller_connected_remote_axis(self) -> None:
        near = _pole_surface(0.25, 0.0, 0.08, 4.3, seed=161)
        remote = _pole_surface(1.8, 0.0, 0.08, 5.8, seed=162)
        arm = _horizontal_arm(0.0, 1.8, 0.0, 5.0)
        ground = np.vstack((_ground_surface(0.25, 0.0), _ground_surface(1.8, 0.0)))
        neighborhood = np.vstack((near, remote, arm, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: near.shape[0] + remote.shape[0] + arm.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(
                    near.shape[0] + remote.shape[0] + arm.shape[0],
                    84,
                    dtype=np.int16,
                ),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.representative_xyz[0], 0.25, delta=0.08)
        self.assertLess(result.candidates[0].association_distance_m, 0.4)

    def test_complete_direct_shaft_wins_over_closer_upper_appendage(self) -> None:
        appendage = _pole_surface(0.05, 0.0, 2.45, 4.95, seed=166)
        full_shaft = _pole_surface(0.42, 0.0, 0.08, 4.95, seed=167)
        ground = np.vstack(
            (_ground_surface(0.05, 0.0), _ground_surface(0.42, 0.0))
        )
        neighborhood = np.vstack((appendage, full_shaft, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: appendage.shape[0] + full_shaft.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(
                    appendage.shape[0] + full_shaft.shape[0],
                    84,
                    dtype=np.int16,
                ),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(
                direct_max_axis_sign_distance_m=0.75,
                preferred_min_completeness_ratio=0.75,
            ),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertAlmostEqual(result.representative_xyz[0], 0.42, delta=0.08)
        self.assertGreaterEqual(candidate.completeness_ratio or 0.0, 0.75)
        self.assertGreater(candidate.association_distance_m, 0.30)

    def test_default_minimum_sign_ground_height_rejects_elevated_edge(self) -> None:
        pole = _pole_surface(0.3, 0.0, 3.55, 5.5, seed=171)
        elevated = _ground_surface(0.3, 0.0)
        elevated[:, 2] += 3.5
        neighborhood = np.vstack((pole, elevated))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True

        rejected = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
        )
        accepted_when_explicitly_relaxed = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(min_ground_drop_m=1.0),
        )

        self.assertIsNone(rejected)
        self.assertIsNotNone(accepted_when_explicitly_relaxed)

    def test_short_clutter_is_not_a_pole(self) -> None:
        rng = np.random.default_rng(4)
        clutter = rng.normal(size=(100, 3)) * np.asarray([0.2, 0.2, 0.08])
        clutter += np.asarray([0.5, 0.0, 2.0])
        result = find_pole_bases(
            clutter,
            np.ones(clutter.shape[0], dtype=bool),
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(),
        )
        self.assertIsNone(result)

    def test_elevated_surface_near_sign_is_not_accepted_as_ground(self) -> None:
        pole = _pole_surface(0.4, 0.1, 4.45, 5.6, seed=41)
        elevated_surface = _ground_surface(0.4, 0.1)
        elevated_surface[:, 2] += 4.4
        neighborhood = np.vstack((pole, elevated_surface))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(min_ground_drop_m=1.0),
        )
        self.assertIsNone(result)

        permissive = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(min_ground_drop_m=0.0),
        )
        self.assertIsNotNone(permissive)

    def test_mixed_class_noisy_secondary_is_not_treated_as_second_pole(self) -> None:
        primary = _pole_surface(1.0, 0.2, 0.05, 4.8, seed=10)
        secondary = _pole_surface(1.5, 1.9, 0.05, 4.0, seed=11)
        ground = _ground_surface(1.0, 0.2)
        neighborhood = np.vstack((primary, secondary, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: primary.shape[0] + secondary.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(primary.shape[0], 84, dtype=np.int16),
                np.resize(np.asarray([84, 65, 74], dtype=np.int16), secondary.shape[0]),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(direct_max_axis_sign_distance_m=1.1),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.pole_type, "SINGLE")
        self.assertGreater(result.candidates[0].dominant_class_fraction or 0.0, 0.95)

        permissive = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 5.0]),
            PoleSearchParameters(
                direct_max_axis_sign_distance_m=1.1,
                max_axis_sign_distance_m=10.0,
            ),
            None,
        )
        self.assertIsNotNone(permissive)
        assert permissive is not None
        self.assertEqual(permissive.pole_type, "SINGLE")
        self.assertEqual(len(permissive.candidates), 1)

    def test_block_bbox_filter(self) -> None:
        files = [
            {
                "path": "fixture.las",
                "blocks": [
                    {"name": "hit", "min": [0, 0, 0], "max": [2, 2, 2]},
                    {"name": "miss", "min": [10, 10, 10], "max": [12, 12, 12]},
                ],
            }
        ]
        matches = blocks_intersecting_bounds(files, np.asarray([1, 1, 1]), np.asarray([3, 3, 3]))
        self.assertEqual([block["name"] for _, block in matches], ["hit"])

    def test_adjacent_frame_observations_are_merged(self) -> None:
        common = {"record_name": "Job_A_Track01", "class_id": 65, "confidence": 0.9}
        observations = [
            {**common, "image_name": "a.jpg", "pole_x": 10.0, "pole_y": 20.0, "pole_z": 1.0, "pole_quality": 0.9, "pole_status": "AUTO"},
            {**common, "image_name": "b.jpg", "pole_x": 10.2, "pole_y": 20.1, "pole_z": 1.1, "pole_quality": 0.8, "pole_status": "AUTO", "pole_occluded": True},
            {**common, "image_name": "c.jpg", "pole_x": 30.0, "pole_y": 40.0, "pole_z": 2.0, "pole_quality": 0.7, "pole_status": "REVIEW"},
        ]
        merged = cluster_pole_observations(observations, radius_m=0.75)
        self.assertEqual(len(merged), 3)
        nearby = [item for item in merged if item["pole_x"] < 20.0]
        self.assertEqual(len(nearby), 2)
        self.assertEqual(len({item["support_id"] for item in nearby}), 1)
        first = nearby[0]
        self.assertEqual(first["obs_count"], 2)
        self.assertEqual(first["occluded_count"], 1)
        self.assertEqual(first["pole_status"], "AUTO")
        self.assertEqual(first["pole_method"], "MULTI_FRAME")

    def test_different_sign_classes_on_same_support_repeat_the_support_point(self) -> None:
        observations = [
            {
                "record_name": "Job_A_Track01",
                "class_id": 65,
                "image_name": "a.jpg",
                "pole_x": 10.0,
                "pole_y": 20.0,
                "pole_z": 1.0,
                "pole_quality": 0.9,
                "pole_status": "AUTO",
                "pole_type": "SINGLE",
            },
            {
                "record_name": "Job_A_Track01",
                "class_id": 72,
                "image_name": "b.jpg",
                "pole_x": 10.1,
                "pole_y": 20.0,
                "pole_z": 1.0,
                "pole_quality": 0.8,
                "pole_status": "AUTO",
                "pole_type": "SINGLE",
            },
        ]
        merged = cluster_pole_observations(observations, radius_m=0.75)
        self.assertEqual(len(merged), 2)
        self.assertEqual({item["class_id"] for item in merged}, {65, 72})
        self.assertEqual(len({item["support_id"] for item in merged}), 1)
        self.assertEqual(len({(item["pole_x"], item["pole_y"], item["pole_z"]) for item in merged}), 1)
        self.assertTrue(all(item["obs_count"] == 2 for item in merged))
        self.assertTrue(all(item["source_class_ids"] == [65, 72] for item in merged))

    def test_multiple_detections_in_one_image_get_one_row_each_at_same_support(self) -> None:
        observations = [
            {
                "record_name": "Job_A_Track01",
                "class_id": class_id,
                "image_name": "same.jpg",
                "pole_x": 10.0 + offset,
                "pole_y": 20.0,
                "pole_z": 1.0,
                "pole_quality": quality,
                "pole_status": "AUTO",
                "pole_type": "SINGLE",
            }
            for class_id, offset, quality in ((65, 0.0, 0.9), (72, 0.05, 0.8))
        ]
        merged = cluster_pole_observations(observations, radius_m=0.75)
        self.assertEqual(len(merged), 2)
        self.assertEqual(len({item["detection_id"] for item in merged}), 2)
        self.assertEqual(len({item["support_id"] for item in merged}), 1)
        self.assertTrue(all(item["obs_count"] == 1 for item in merged))
        self.assertTrue(all(item["detection_count"] == 2 for item in merged))
        self.assertTrue(all(item["source_class_ids"] == [65, 72] for item in merged))
        self.assertTrue(all(abs(item["pole_x"] - 10.0) < 1e-9 for item in merged))


if __name__ == "__main__":
    unittest.main()
