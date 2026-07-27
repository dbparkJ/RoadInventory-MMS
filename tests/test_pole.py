from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from mms_shp_detection.pole import (
    PoleSearchParameters,
    _group_integer_xy_cells,
    _horizontal_connection_coverage,
    blocks_intersecting_bounds,
    cluster_pole_observations,
    estimate_local_ground,
    find_pole_bases,
    pole_connection_coverage,
    pole_candidate_rank_key,
    remote_pole_junction_cost,
    select_pole_candidate,
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


def _rising_arm(
    x_start: float,
    x_end: float,
    y: float,
    z_start: float,
    z_end: float,
) -> np.ndarray:
    rows: list[list[float]] = []
    for fraction in np.linspace(0.0, 1.0, 100):
        x = x_start + ((x_end - x_start) * fraction)
        z = z_start + ((z_end - z_start) * fraction)
        rows.extend(
            (
                [x, y - 0.02, z],
                [x, y, z + 0.02],
                [x, y + 0.02, z - 0.02],
            )
        )
    return np.asarray(rows, dtype=np.float64)


class PoleSearchTests(unittest.TestCase):
    def test_coherent_arm_keeps_endpoint_anchors_across_large_middle_gap(
        self,
    ) -> None:
        parameters = PoleSearchParameters()
        distance = 5.38
        occupied = [*range(5), *range(14, 22)]
        rows: list[list[float]] = []
        for bin_index in occupied:
            along = min(
                distance - 0.01,
                (bin_index + 0.5) * parameters.horizontal_connection_bin_m,
            )
            rows.extend(
                (
                    [along, -0.02, 5.00],
                    [along, 0.00, 5.02],
                    [along, 0.02, 4.98],
                )
            )

        connection = _horizontal_connection_coverage(
            np.asarray(rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([distance, 0.0]),
            parameters,
            None,
        )

        self.assertEqual(connection.expected_bin_count, 22)
        self.assertEqual(connection.occupied_bin_count, 13)
        self.assertEqual(connection.coherent_bin_count, 13)
        self.assertTrue(connection.endpoint_anchored)
        self.assertGreaterEqual(connection.coherent_ratio, 0.80)
        self.assertGreaterEqual(connection.coherent_point_fraction, 0.20)

    def test_raw_coverage_without_one_endpoint_is_not_coherent_arm(self) -> None:
        parameters = PoleSearchParameters()
        distance = 5.0
        rows: list[list[float]] = []
        for bin_index in range(2, 18):
            along = (bin_index + 0.5) * parameters.horizontal_connection_bin_m
            rows.extend(
                (
                    [along, -0.02, 5.00],
                    [along, 0.00, 5.02],
                    [along, 0.02, 4.98],
                )
            )

        connection = _horizontal_connection_coverage(
            np.asarray(rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([distance, 0.0]),
            parameters,
            None,
        )

        self.assertGreaterEqual(connection.coverage_ratio, 0.75)
        self.assertFalse(connection.endpoint_anchored)

    def test_connection_density_is_invariant_to_mast_arm_slope(self) -> None:
        parameters = PoleSearchParameters(
            horizontal_connection_above_tolerance_m=1.0,
        )
        flat = _rising_arm(0.0, 5.0, 0.0, 5.0, 5.0)
        rising = _rising_arm(0.0, 5.0, 0.0, 5.0, 5.6)

        flat_connection = _horizontal_connection_coverage(
            flat,
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([5.0, 0.0]),
            parameters,
            None,
        )
        rising_connection = _horizontal_connection_coverage(
            rising,
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([5.0, 0.0]),
            parameters,
            None,
        )

        self.assertTrue(flat_connection.endpoint_anchored)
        self.assertTrue(rising_connection.endpoint_anchored)
        self.assertAlmostEqual(
            flat_connection.ridge_density_points_per_m,
            rising_connection.ridge_density_points_per_m,
            delta=1.0,
        )

    def test_thin_wire_with_one_extra_bin_fails_point_fraction_gate(self) -> None:
        parameters = PoleSearchParameters()
        distance = 5.0
        rows: list[list[float]] = []
        for bin_index in range(20):
            along = (bin_index + 0.5) * parameters.horizontal_connection_bin_m
            if bin_index != 10:
                for offset in np.linspace(-0.025, 0.025, 10):
                    rows.append([along, offset, 5.0])
            rows.extend(
                (
                    [along, 0.18, 5.20],
                    [along, 0.19, 5.20],
                )
            )

        connection = _horizontal_connection_coverage(
            np.asarray(rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([distance, 0.0]),
            parameters,
            None,
        )

        self.assertTrue(connection.endpoint_anchored)
        self.assertEqual(connection.coherent_bin_count, 20)
        self.assertLess(
            connection.coherent_point_fraction,
            parameters.min_horizontal_connection_coherent_point_fraction,
        )
        self.assertLess(connection.ridge_density_points_per_m, 10.0)

    def test_endpoint_mode_search_keeps_a_sparse_arm_among_dense_clutter(
        self,
    ) -> None:
        parameters = PoleSearchParameters()
        distance = 5.0
        rows: list[list[float]] = []
        for bin_index in range(20):
            along = (bin_index + 0.5) * parameters.horizontal_connection_bin_m
            rows.extend(
                (
                    [along, -0.02, 5.00],
                    [along, 0.00, 5.02],
                    [along, 0.02, 4.98],
                )
            )

        decoy_modes = [
            (perpendicular, height)
            for perpendicular in (-0.24, -0.12, 0.0, 0.12, 0.24)
            for height in (-0.24, -0.12, 0.0, 0.12, 0.24)
            if np.hypot(perpendicular, height) > 0.10
        ]
        for bin_index in (0, 1, 18, 19):
            along = (bin_index + 0.5) * parameters.horizontal_connection_bin_m
            for perpendicular, height in decoy_modes:
                for jitter in (-0.006, -0.002, 0.002, 0.006):
                    rows.append(
                        [
                            along,
                            -(perpendicular + jitter),
                            5.0 + height,
                        ]
                    )

        connection = _horizontal_connection_coverage(
            np.asarray(rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([distance, 0.0]),
            parameters,
            None,
        )

        self.assertTrue(connection.endpoint_anchored)
        self.assertEqual(connection.coherent_bin_count, 20)
        self.assertGreater(connection.coherent_point_fraction, 0.95)

    def test_stable_line_selection_separates_622_true_arm_from_wrong_side(
        self,
    ) -> None:
        parameters = PoleSearchParameters()

        true_distance = 5.29
        true_rows: list[list[float]] = []
        for bin_index in (*range(6), *range(16, 22)):
            along = min(
                true_distance - 0.01,
                (bin_index + 0.5) * parameters.horizontal_connection_bin_m,
            )
            for offset in np.linspace(0.006, 0.024, 6):
                true_rows.append([along, offset, 5.0 + offset])
        for bin_index in range(6, 12):
            along = (bin_index + 0.5) * parameters.horizontal_connection_bin_m
            true_rows.extend(
                (
                    [along, -0.18, 5.20],
                    [along, -0.17, 5.21],
                )
            )

        true_connection = _horizontal_connection_coverage(
            np.asarray(true_rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([true_distance, 0.0]),
            parameters,
            None,
        )

        wrong_distance = 8.73
        wrong_rows: list[list[float]] = []
        for bin_index in range(35):
            along = min(
                wrong_distance - 0.01,
                (bin_index + 0.5) * parameters.horizontal_connection_bin_m,
            )
            wrong_rows.extend(
                (
                    [along, 0.010, 5.010],
                    [along, 0.015, 5.015],
                )
            )
        dense_fragment_bins = (*range(11), *range(24, 35))
        for bin_index in dense_fragment_bins:
            along = min(
                wrong_distance - 0.01,
                (bin_index + 0.5) * parameters.horizontal_connection_bin_m,
            )
            for offset in np.linspace(0.0, 0.018, 10):
                wrong_rows.append([along, -0.18 + offset, 5.20 + offset])

        wrong_connection = _horizontal_connection_coverage(
            np.asarray(wrong_rows, dtype=np.float64),
            np.asarray([0.0, 0.0, 5.0]),
            np.asarray([wrong_distance, 0.0]),
            parameters,
            None,
        )

        self.assertEqual(true_connection.expected_bin_count, 22)
        self.assertEqual(true_connection.occupied_bin_count, 18)
        self.assertEqual(true_connection.coherent_bin_count, 12)
        self.assertTrue(true_connection.endpoint_anchored)
        self.assertGreaterEqual(
            true_connection.coherent_ratio,
            parameters.min_horizontal_connection_coherent_ratio,
        )
        self.assertGreaterEqual(
            true_connection.coherent_point_fraction,
            parameters.min_horizontal_connection_coherent_point_fraction,
        )

        # The wrong-side scene contains a thin line spanning every bin and a
        # denser but shorter fragment.  Coverage-first selection must keep the
        # full line as the measured centreline; its low share of interior
        # points then rejects the hypothesis.  A gate-priority/point-first key
        # would switch to the 22-bin fragment and report a misleadingly high
        # point fraction.
        self.assertEqual(wrong_connection.expected_bin_count, 35)
        self.assertEqual(wrong_connection.coherent_bin_count, 35)
        self.assertTrue(wrong_connection.endpoint_anchored)
        self.assertGreaterEqual(
            wrong_connection.coherent_ratio,
            parameters.min_horizontal_connection_coherent_ratio,
        )
        self.assertLess(
            wrong_connection.coherent_point_fraction,
            parameters.min_horizontal_connection_coherent_point_fraction,
        )

    def test_near_tie_density_can_select_opposite_side_real_arm(self) -> None:
        parameters = PoleSearchParameters()
        common = {
            "completeness_ratio": 1.0,
            "horizontal_connection_coverage_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "multi_return_fraction": 0.0,
            "score": 8.0,
        }
        thin_left_line = SimpleNamespace(
            **common,
            association_distance_m=4.323,
            horizontal_connection_point_count=1816,
            horizontal_connection_ridge_density_points_per_m=115.0,
            point_count=4632,
            support_side="LEFT_OF_TRAVEL",
        )
        actual_right_arm = SimpleNamespace(
            **common,
            association_distance_m=4.754,
            horizontal_connection_point_count=5611,
            horizontal_connection_ridge_density_points_per_m=484.0,
            point_count=7653,
            support_side="RIGHT_OF_TRAVEL",
        )

        self.assertIs(
            select_pole_candidate(
                (thin_left_line, actual_right_arm),
                parameters,
            ),
            actual_right_arm,
        )

    def test_density_cannot_override_a_support_more_than_075m_nearer(self) -> None:
        parameters = PoleSearchParameters()
        common = {
            "completeness_ratio": 1.0,
            "horizontal_connection_coverage_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "multi_return_fraction": 0.0,
            "score": 8.0,
            "point_count": 1000,
        }
        actual_near = SimpleNamespace(
            **common,
            association_distance_m=5.38,
            horizontal_connection_ridge_density_points_per_m=137.0,
            support_side="LEFT_OF_TRAVEL",
        )
        dense_far_clutter = SimpleNamespace(
            **common,
            association_distance_m=13.07,
            horizontal_connection_ridge_density_points_per_m=385.0,
            support_side="RIGHT_OF_TRAVEL",
        )

        self.assertIs(
            select_pole_candidate(
                (actual_near, dense_far_clutter),
                parameters,
            ),
            actual_near,
        )

    def test_density_compares_against_initial_side_when_fit_is_duplicated(self) -> None:
        parameters = PoleSearchParameters()
        common = {
            "completeness_ratio": 1.0,
            "horizontal_connection_coverage_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "multi_return_fraction": 0.0,
            "score": 8.0,
        }
        initial_left = SimpleNamespace(
            **common,
            association_distance_m=4.20,
            horizontal_connection_point_count=1000,
            horizontal_connection_ridge_density_points_per_m=100.0,
            point_count=1000,
            support_side="LEFT_OF_TRAVEL",
        )
        actual_right = SimpleNamespace(
            **common,
            association_distance_m=4.80,
            horizontal_connection_point_count=3000,
            horizontal_connection_ridge_density_points_per_m=300.0,
            point_count=3000,
            support_side="RIGHT_OF_TRAVEL",
        )
        duplicate_right = SimpleNamespace(
            **common,
            association_distance_m=4.70,
            horizontal_connection_point_count=2900,
            horizontal_connection_ridge_density_points_per_m=290.0,
            point_count=2900,
            support_side="RIGHT_OF_TRAVEL",
        )

        self.assertIs(
            select_pole_candidate(
                (initial_left, actual_right, duplicate_right),
                parameters,
            ),
            actual_right,
        )

    def test_density_does_not_flip_without_directional_side_metadata(self) -> None:
        parameters = PoleSearchParameters()
        common = {
            "completeness_ratio": 1.0,
            "horizontal_connection_coverage_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "multi_return_fraction": 0.0,
            "score": 8.0,
        }
        nearest = SimpleNamespace(
            **common,
            association_distance_m=4.20,
            horizontal_connection_point_count=1000,
            horizontal_connection_ridge_density_points_per_m=100.0,
            point_count=1000,
            support_side=None,
        )
        denser = SimpleNamespace(
            **common,
            association_distance_m=4.75,
            horizontal_connection_point_count=4000,
            horizontal_connection_ridge_density_points_per_m=300.0,
            point_count=4000,
            support_side=None,
        )

        self.assertIs(
            select_pole_candidate((nearest, denser), parameters),
            nearest,
        )
        nearest.support_side = "ALONG_TRAVEL"
        denser.support_side = "RIGHT_OF_TRAVEL"
        self.assertIs(
            select_pole_candidate((nearest, denser), parameters),
            nearest,
        )

    def test_remote_quality_uses_coherent_coverage_before_raw_coverage(
        self,
    ) -> None:
        common = {
            "association_distance_m": 5.0,
            "completeness_ratio": 1.0,
            "multi_return_fraction": 0.0,
        }
        disconnected_clutter = SimpleNamespace(
            **common,
            horizontal_connection_coverage_ratio=1.0,
            horizontal_connection_coherent_coverage_ratio=0.50,
        )
        coherent_arm = SimpleNamespace(
            **common,
            horizontal_connection_coverage_ratio=0.65,
            horizontal_connection_coherent_coverage_ratio=0.65,
        )

        self.assertEqual(
            pole_connection_coverage(disconnected_clutter),
            0.50,
        )
        self.assertLess(
            remote_pole_junction_cost(coherent_arm),
            remote_pole_junction_cost(disconnected_clutter),
        )

    def test_integer_xy_cell_grouping_matches_axis_unique(self) -> None:
        rng = np.random.default_rng(77)
        cells = rng.integers(-300, 500, size=(20_000, 2), dtype=np.int64)
        cells[::7] = cells[0]

        expected = np.unique(
            cells,
            axis=0,
            return_inverse=True,
            return_counts=True,
        )
        actual = _group_integer_xy_cells(cells)

        for actual_array, expected_array in zip(actual, expected):
            np.testing.assert_array_equal(actual_array, expected_array)

    def test_integer_xy_cell_grouping_handles_int64_extremes(self) -> None:
        cells = np.asarray(
            [
                [np.iinfo(np.int64).min, 0],
                [np.iinfo(np.int64).max, 0],
                [np.iinfo(np.int64).min, 0],
            ],
            dtype=np.int64,
        )

        expected = np.unique(
            cells,
            axis=0,
            return_inverse=True,
            return_counts=True,
        )
        actual = _group_integer_xy_cells(cells)

        for actual_array, expected_array in zip(actual, expected):
            np.testing.assert_array_equal(actual_array, expected_array)

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

    def test_remote_rank_prefers_first_valid_junction_over_far_full_coverage(
        self,
    ) -> None:
        parameters = PoleSearchParameters()
        near_support = SimpleNamespace(
            completeness_ratio=1.0,
            association_distance_m=5.38,
            horizontal_connection_coverage_ratio=0.75,
            radial_rmse_m=0.092,
            multi_return_fraction=0.0,
            score=8.0,
            point_count=1800,
        )
        far_structure = SimpleNamespace(
            completeness_ratio=1.0,
            association_distance_m=13.07,
            horizontal_connection_coverage_ratio=1.0,
            radial_rmse_m=0.085,
            multi_return_fraction=0.0,
            score=11.7,
            point_count=15000,
        )

        self.assertLess(
            pole_candidate_rank_key(near_support, parameters),
            pole_candidate_rank_key(far_structure, parameters),
        )

    def test_remote_rank_lets_quality_resolve_submetre_distance_difference(
        self,
    ) -> None:
        parameters = PoleSearchParameters()
        slightly_nearer_noise = SimpleNamespace(
            completeness_ratio=0.75,
            association_distance_m=5.38,
            horizontal_connection_coverage_ratio=0.50,
            radial_rmse_m=0.13,
            multi_return_fraction=0.20,
            score=5.0,
            point_count=300,
        )
        cleaner_support = SimpleNamespace(
            completeness_ratio=1.0,
            association_distance_m=5.39,
            horizontal_connection_coverage_ratio=1.0,
            radial_rmse_m=0.06,
            multi_return_fraction=0.0,
            score=7.0,
            point_count=900,
        )

        self.assertLess(
            pole_candidate_rank_key(cleaner_support, parameters),
            pole_candidate_rank_key(slightly_nearer_noise, parameters),
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

        rejected_hypotheses: list[dict[str, object]] = []
        with mock.patch(
            "mms_shp_detection.pole.estimate_local_ground",
            wraps=estimate_local_ground,
        ) as ground_estimate:
            result = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
                rejected_support_hypotheses=rejected_hypotheses,
            )

        self.assertIsNone(result)
        self.assertEqual(ground_estimate.call_count, 0)
        self.assertTrue(rejected_hypotheses)
        self.assertAlmostEqual(
            float(rejected_hypotheses[0]["axis_x"]),
            2.5,
            delta=0.08,
        )
        self.assertIn(
            rejected_hypotheses[0]["rejection_reason"],
            {"raw_coverage", "coherent_arm"},
        )

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
            travel_forward_xy=np.asarray([0.0, 1.0]),
            travel_right_xy=np.asarray([1.0, 0.0]),
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
        self.assertEqual(candidate.support_side, "RIGHT_OF_TRAVEL")
        self.assertAlmostEqual(
            candidate.travel_longitudinal_offset_m or 0.0,
            0.0,
            delta=0.01,
        )
        self.assertGreater(candidate.travel_lateral_offset_m or 0.0, 2.4)
        self.assertGreater(
            candidate.horizontal_connection_ridge_density_points_per_m or 0.0,
            0.0,
        )

    def test_remote_arm_requires_both_ratio_and_point_fraction(self) -> None:
        pole = _pole_surface(5.3, 0.0, 0.08, 4.9, seed=157)
        ground = _ground_surface(5.3, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )
        common = {
            "occupied_bin_count": 18,
            "expected_bin_count": 22,
            "coverage_ratio": 18.0 / 22.0,
            "point_count": 500,
            "ridge_point_count": 300,
            "ridge_density_points_per_m": 60.0,
            "coherent_bin_count": 12,
            "coherent_coverage_ratio": 12.0 / 22.0,
            "coherent_point_fraction": 0.373,
            "endpoint_anchored": True,
        }
        valid_connection = SimpleNamespace(
            **common,
            coherent_ratio=12.0 / 18.0,
        )
        low_ratio_connection = SimpleNamespace(
            **{
                **common,
                "coherent_bin_count": 11,
                "coherent_coverage_ratio": 11.0 / 22.0,
                "coherent_point_fraction": 0.67,
            },
            coherent_ratio=11.0 / 18.0,
        )

        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=valid_connection,
        ):
            accepted = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )
        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=low_ratio_connection,
        ):
            rejected = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )

        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)

    def test_short_complete_remote_arm_relaxes_only_point_fraction(self) -> None:
        pole = _pole_surface(2.75, 0.0, 0.08, 4.9, seed=158)
        ground = _ground_surface(2.75, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )
        common = {
            "expected_bin_count": 11,
            "point_count": 1200,
            "ridge_point_count": 80,
            "ridge_density_points_per_m": 32.0,
            "coherent_point_fraction": 0.1136,
            "endpoint_anchored": True,
        }
        complete_connection = SimpleNamespace(
            **common,
            occupied_bin_count=11,
            coverage_ratio=1.0,
            coherent_bin_count=11,
            coherent_coverage_ratio=1.0,
            coherent_ratio=1.0,
        )
        incomplete_290_like_connection = SimpleNamespace(
            **{**common, "expected_bin_count": 6},
            occupied_bin_count=5,
            coverage_ratio=5.0 / 6.0,
            coherent_bin_count=4,
            coherent_coverage_ratio=4.0 / 6.0,
            coherent_ratio=4.0 / 5.0,
        )
        thin_wire_connection = SimpleNamespace(
            **{**complete_connection.__dict__, "ridge_density_points_per_m": 8.0}
        )

        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=complete_connection,
        ):
            accepted = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )
        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=incomplete_290_like_connection,
        ):
            rejected = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )
        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=thin_wire_connection,
        ):
            rejected_thin_wire = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )

        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)
        self.assertIsNone(rejected_thin_wire)

    def test_complete_arm_beyond_short_limit_keeps_global_point_fraction(
        self,
    ) -> None:
        pole = _pole_surface(5.3, 0.0, 0.08, 4.9, seed=159)
        ground = _ground_surface(5.3, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )
        connection = SimpleNamespace(
            occupied_bin_count=22,
            expected_bin_count=22,
            coverage_ratio=1.0,
            point_count=1200,
            ridge_point_count=80,
            ridge_density_points_per_m=16.0,
            coherent_bin_count=22,
            coherent_coverage_ratio=1.0,
            coherent_ratio=1.0,
            coherent_point_fraction=0.1136,
            endpoint_anchored=True,
        )

        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=connection,
        ):
            result = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                classifications,
            )

        self.assertIsNone(result)

    def test_signal_profile_accepts_a_connected_arm_rising_above_detection(self) -> None:
        pole = _pole_surface(2.5, 0.0, 0.08, 6.2, seed=159)
        arm = _rising_arm(0.0, 2.5, 0.0, 5.0, 6.2)
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
        sign_xyz = np.asarray([0.0, 0.0, 5.0])

        strict = find_pole_bases(
            neighborhood,
            corridor,
            sign_xyz,
            PoleSearchParameters(),
            classifications,
        )
        signal = find_pole_bases(
            neighborhood,
            corridor,
            sign_xyz,
            PoleSearchParameters(
                horizontal_connection_above_tolerance_m=1.5,
            ),
            classifications,
        )

        self.assertIsNone(strict)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(
            signal.candidates[0].horizontal_connection_coverage_ratio or 0.0,
            0.9,
        )

    def test_short_long_range_structure_is_not_accepted_as_signal_support(
        self,
    ) -> None:
        pole = _pole_surface(9.5, 0.0, 1.9, 4.9, seed=160)
        arm = _horizontal_arm(0.0, 9.5, 0.0, 5.0)
        ground = _ground_surface(9.5, 0.0)
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
            PoleSearchParameters(
                search_radius_m=10.0,
                max_axis_sign_distance_m=10.0,
                long_remote_distance_m=8.0,
                long_remote_min_vertical_span_m=3.5,
            ),
            classifications,
        )

        self.assertIsNone(result)

    def test_long_range_gate_uses_coherent_instead_of_raw_coverage(self) -> None:
        pole = _pole_surface(9.5, 0.0, 0.08, 4.9, seed=168)
        ground = _ground_surface(9.5, 0.0)
        neighborhood = np.vstack((pole, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(pole.shape[0], 84, dtype=np.int16),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )
        connection = SimpleNamespace(
            occupied_bin_count=34,
            expected_bin_count=38,
            coverage_ratio=34.0 / 38.0,
            point_count=500,
            ridge_point_count=300,
            ridge_density_points_per_m=35.0,
            coherent_bin_count=29,
            coherent_coverage_ratio=29.0 / 38.0,
            coherent_ratio=29.0 / 34.0,
            coherent_point_fraction=0.50,
            endpoint_anchored=True,
        )

        with mock.patch(
            "mms_shp_detection.pole._horizontal_connection_coverage",
            return_value=connection,
        ):
            result = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(
                    search_radius_m=10.0,
                    max_axis_sign_distance_m=10.0,
                ),
                classifications,
            )

        self.assertIsNone(result)

    def test_small_endpoint_tilt_is_plumbed_through_robust_band_centres(
        self,
    ) -> None:
        pole = _pole_surface(0.0, 0.0, 0.2, 6.0, seed=169)
        height_fraction = (pole[:, 2] - 0.2) / 5.8
        pole[:, 0] += 0.18 * height_fraction
        ground = _ground_surface(0.09, 0.0)
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
            np.asarray([0.18, 0.0, 6.2]),
            PoleSearchParameters(axis_plumb_max_tilt_deg=4.0),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertTrue(candidate.axis_plumb_adjusted)
        self.assertLess(candidate.axis_endpoint_tilt_deg or 99.0, 4.0)
        np.testing.assert_allclose(candidate.axis_direction, [0.0, 0.0, 1.0])
        self.assertAlmostEqual(result.representative_xyz[0], 0.09, delta=0.04)

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

        with mock.patch(
            "mms_shp_detection.pole.estimate_local_ground",
            wraps=estimate_local_ground,
        ) as ground_estimate:
            result = find_pole_bases(
                neighborhood,
                corridor,
                np.asarray([0.0, 0.0, 5.0]),
                PoleSearchParameters(),
                None,
            )

        self.assertIsNone(result)
        self.assertEqual(ground_estimate.call_count, 0)

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
            {
                **common,
                "image_name": "a.jpg",
                "pole_x": 10.0,
                "pole_y": 20.0,
                "pole_z": 1.0,
                "pole_quality": 0.9,
                "pole_status": "AUTO",
                "pole_search_mode": "strict",
                "pole_fallback_attempted": False,
                "pole_fallback_used": False,
            },
            {
                **common,
                "image_name": "b.jpg",
                "pole_x": 10.2,
                "pole_y": 20.1,
                "pole_z": 1.1,
                "pole_quality": 0.8,
                "pole_status": "AUTO",
                "pole_occluded": True,
                "pole_search_mode": "physical_fallback_remote_expanded",
                "pole_fallback_attempted": True,
                "pole_fallback_used": True,
            },
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
        self.assertEqual(first["pole_search_mode"], "MIXED")
        self.assertTrue(first["pole_fallback_attempted"])
        self.assertEqual(first["pole_fallback_attempted_count"], 1)
        self.assertTrue(first["pole_fallback_used"])
        self.assertEqual(first["pole_fallback_used_count"], 1)

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
