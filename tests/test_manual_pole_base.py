from __future__ import annotations

import math
import unittest

import numpy as np

from mms_shp_detection.manual_pole_base import infer_pole_base_from_seed


def _shaft(
    base: tuple[float, float, float],
    *,
    height_m: float = 6.0,
    bottom_gap_m: float = 0.04,
    tilt_xy: tuple[float, float] = (0.0, 0.0),
) -> np.ndarray:
    base_array = np.asarray(base, dtype=np.float64)
    z_values = np.arange(
        base_array[2] + bottom_gap_m,
        base_array[2] + height_m,
        0.08,
    )
    angles = np.tile(
        np.linspace(0.0, 2.0 * math.pi, 8, endpoint=False),
        z_values.size,
    )
    repeated_z = np.repeat(z_values, 8)
    relative_z = repeated_z - base_array[2]
    return np.column_stack(
        (
            base_array[0] + (relative_z * tilt_xy[0]) + (0.03 * np.cos(angles)),
            base_array[1] + (relative_z * tilt_xy[1]) + (0.03 * np.sin(angles)),
            repeated_z,
        )
    )


def _partially_observed_thick_shaft(
    base: tuple[float, float, float],
    *,
    radius_m: float = 0.15,
) -> np.ndarray:
    """A mobile-LiDAR-like 120 degree visible arc from a thick round pole."""

    base_array = np.asarray(base, dtype=np.float64)
    z_values = np.arange(base_array[2] + 0.04, base_array[2] + 6.0, 0.08)
    visible_angles = np.linspace(-math.pi / 3.0, math.pi / 3.0, 13)
    repeated_z = np.repeat(z_values, visible_angles.size)
    angles = np.tile(visible_angles, z_values.size)
    return np.column_stack(
        (
            base_array[0] + (radius_m * np.cos(angles)),
            base_array[1] + (radius_m * np.sin(angles)),
            repeated_z,
        )
    )


def _ground(
    base: tuple[float, float, float],
    *,
    slope_xy: tuple[float, float] = (0.0, 0.0),
    curb: bool = False,
) -> np.ndarray:
    base_array = np.asarray(base, dtype=np.float64)
    x_grid, y_grid = np.meshgrid(
        np.arange(base_array[0] - 1.5, base_array[0] + 1.51, 0.12),
        np.arange(base_array[1] - 1.5, base_array[1] + 1.51, 0.12),
    )
    z_grid = (
        base_array[2]
        + (slope_xy[0] * (x_grid - base_array[0]))
        + (slope_xy[1] * (y_grid - base_array[1]))
    )
    if curb:
        z_grid = np.where(x_grid < base_array[0] + 0.45, base_array[2], base_array[2] - 0.70)
    return np.column_stack((x_grid.ravel(), y_grid.ravel(), z_grid.ravel()))


def _vertical_panel(
    x: float,
    *,
    y_min: float = -1.0,
    y_max: float = 1.0,
    height_m: float = 6.0,
) -> np.ndarray:
    """Dense wall/guardrail-like returns with pole-like vertical continuity."""

    y_values = np.arange(y_min, y_max + 0.001, 0.025)
    z_values = np.arange(0.04, height_m, 0.08)
    y_grid, z_grid = np.meshgrid(y_values, z_values)
    return np.column_stack(
        (
            np.full(y_grid.size, x, dtype=np.float64),
            y_grid.ravel(),
            z_grid.ravel(),
        )
    )


def _scene(
    shafts: list[np.ndarray],
    ground: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    groups = [*shafts, *([] if ground is None else [ground])]
    points = np.vstack(groups)
    classifications = np.concatenate(
        [
            *(np.zeros(item.shape[0], dtype=np.int16) for item in shafts),
            *(
                []
                if ground is None
                else [np.full(ground.shape[0], 2, dtype=np.int16)]
            ),
        ]
    )
    return points, classifications


class ManualPoleBaseTests(unittest.TestCase):
    def test_flat_vertical_shaft_is_stable_for_low_middle_and_high_seeds(self) -> None:
        base = (2.0, 3.0, 10.0)
        shaft = _shaft(base)
        points, classes = _scene([shaft], _ground(base))
        payloads: list[dict[str, object]] = []
        for fraction in (0.20, 0.50, 0.95):
            seed = shaft[min(shaft.shape[0] - 1, int(shaft.shape[0] * fraction))]
            result = infer_pole_base_from_seed(
                points,
                seed,
                classifications=classes,
            )
            self.assertEqual(result.status, "auto")
            self.assertIsNotNone(result.base_position)
            assert result.base_position is not None
            self.assertLessEqual(
                float(np.linalg.norm(result.base_position - np.asarray(base))),
                0.05,
            )
            payloads.append(result.to_dict())
        self.assertEqual(payloads[0]["base_position"], payloads[1]["base_position"])
        self.assertEqual(payloads[1]["base_position"], payloads[2]["base_position"])

    def test_sloped_ground_tilt_and_occluded_bottom_meet_accuracy_gates(self) -> None:
        base = (0.0, 0.0, 5.0)
        cases = (
            {
                "shaft": _shaft(
                    base,
                    tilt_xy=(math.tan(math.radians(7.0)), 0.02),
                ),
                "ground": _ground(base, slope_xy=(0.02, -0.01)),
                "maximum_error": 0.08,
                "status": "auto",
                "reason": None,
            },
            {
                "shaft": _shaft(base, bottom_gap_m=0.84),
                "ground": _ground(base),
                "maximum_error": 0.10,
                "status": "review",
                "reason": "BOTTOM_EXTRAPOLATED",
            },
        )
        for case in cases:
            shaft = case["shaft"]
            assert isinstance(shaft, np.ndarray)
            ground = case["ground"]
            assert isinstance(ground, np.ndarray)
            points, classes = _scene([shaft], ground)
            result = infer_pole_base_from_seed(
                points,
                shaft[int(shaft.shape[0] * 0.90)],
                classifications=classes,
            )
            self.assertEqual(result.status, case["status"])
            self.assertIsNotNone(result.base_position)
            assert result.base_position is not None
            self.assertLessEqual(
                float(np.linalg.norm(result.base_position - np.asarray(base))),
                float(case["maximum_error"]),
            )
            if case["reason"] is not None:
                self.assertIn(case["reason"], result.reason_codes)

    def test_fifteen_degree_top_click_expands_over_full_two_metre_crop(self) -> None:
        base = (0.0, 0.0, 5.0)
        shaft = _shaft(
            base,
            tilt_xy=(math.tan(math.radians(15.0)), 0.0),
        )
        points, classes = _scene([shaft], _ground(base))
        seed = shaft[-8]

        # Match the endpoint's exact 2 m XY crop. The lower shaft is within the
        # request crop but well outside the 0.75 m seed-local discovery radius.
        crop_mask = np.linalg.norm(points[:, :2] - seed[None, :2], axis=1) <= 2.0
        result = infer_pole_base_from_seed(
            points[crop_mask],
            seed,
            classifications=classes[crop_mask],
        )

        self.assertIn(result.status, ("auto", "review"))
        self.assertIsNotNone(result.base_position)
        self.assertIsNotNone(result.axis)
        assert result.base_position is not None
        assert result.axis is not None
        self.assertLessEqual(
            float(np.linalg.norm(result.base_position - np.asarray(base))),
            0.08,
        )
        self.assertGreater(result.axis.vertical_span_m, 5.7)
        self.assertIn("BASE_OUTSIDE_LOCAL_WINDOW", result.reason_codes)

    def test_anchored_surface_avoids_lower_road_and_seed_selects_neighbor_axis(self) -> None:
        curb_base = (0.0, 0.0, 1.0)
        curb_shaft = _shaft(curb_base)
        curb_points, curb_classes = _scene(
            [curb_shaft],
            _ground(curb_base, curb=True),
        )
        curb_result = infer_pole_base_from_seed(
            curb_points,
            curb_shaft[curb_shaft.shape[0] // 2],
            classifications=curb_classes,
        )
        self.assertIsNotNone(curb_result.base_position)
        assert curb_result.base_position is not None
        self.assertAlmostEqual(float(curb_result.base_position[2]), 1.0, delta=0.05)

        left = _shaft((-0.30, 0.0, 0.0))
        right = _shaft((0.30, 0.0, 0.0))
        points, classes = _scene([left, right], _ground((0.0, 0.0, 0.0)))
        for shaft, expected_x in ((left, -0.30), (right, 0.30)):
            result = infer_pole_base_from_seed(
                points,
                shaft[shaft.shape[0] // 2],
                classifications=classes,
            )
            self.assertIsNotNone(result.base_position)
            assert result.base_position is not None
            self.assertAlmostEqual(float(result.base_position[0]), expected_x, delta=0.05)

    def test_six_tenths_boundary_seed_is_marked_ambiguous(self) -> None:
        left = _shaft((-0.30, 0.0, 0.0))
        right = _shaft((0.30, 0.0, 0.0))
        points, classes = _scene([left, right], _ground((0.0, 0.0, 0.0)))
        boundary_seed = np.asarray([0.0, 0.0, 3.0], dtype=np.float64)
        points = np.vstack((points, boundary_seed))
        classes = np.concatenate((classes, np.asarray([0], dtype=np.int16)))

        result = infer_pole_base_from_seed(
            points,
            boundary_seed,
            classifications=classes,
        )

        self.assertEqual(result.status, "review")
        self.assertTrue(result.quality.ambiguous)
        self.assertIn("AMBIGUOUS_AXES", result.reason_codes)
        self.assertEqual(result.quality.candidate_count, 2)

    def test_dense_vertical_wall_is_rejected_but_cylindrical_shaft_survives(self) -> None:
        base = (0.0, 0.0, 0.0)
        shaft = _shaft(base)
        panel = _vertical_panel(0.65)
        points, classes = _scene([shaft, panel], _ground(base))

        shaft_result = infer_pole_base_from_seed(
            points,
            shaft[shaft.shape[0] // 2],
            classifications=classes,
        )
        self.assertIsNotNone(shaft_result.base_position)
        assert shaft_result.base_position is not None
        self.assertLessEqual(
            float(np.linalg.norm(shaft_result.base_position - np.asarray(base))),
            0.05,
        )

        wall_seed = panel[np.argmin(np.linalg.norm(panel - [0.65, 0.80, 3.0], axis=1))]
        wall_result = infer_pole_base_from_seed(
            points,
            wall_seed,
            classifications=classes,
        )
        self.assertEqual(wall_result.status, "failed")
        self.assertIsNone(wall_result.base_position)
        self.assertIn("NO_VERTICAL_AXIS", wall_result.reason_codes)

    def test_wide_partial_cylindrical_arc_is_not_rejected_as_a_wall(self) -> None:
        base = (0.0, 0.0, 0.0)
        shaft = _partially_observed_thick_shaft(base)
        points, classes = _scene([shaft], _ground(base))
        seed = shaft[shaft.shape[0] // 2]

        result = infer_pole_base_from_seed(
            points,
            seed,
            classifications=classes,
        )

        self.assertIn(result.status, ("auto", "review"))
        self.assertIsNotNone(result.base_position)
        self.assertIsNotNone(result.axis)
        assert result.axis is not None
        self.assertGreater(result.axis.vertical_span_m, 5.7)

    def test_missing_ground_and_non_source_seed_fail_without_a_base(self) -> None:
        shaft = _shaft((0.0, 0.0, 0.0))
        points, classes = _scene([shaft], None)
        no_ground = infer_pole_base_from_seed(
            points,
            shaft[shaft.shape[0] // 2],
            classifications=classes,
        )
        self.assertEqual(no_ground.status, "failed")
        self.assertIsNone(no_ground.base_position)
        self.assertIn("NO_GROUND_SUPPORT", no_ground.reason_codes)

        missing_seed = infer_pole_base_from_seed(
            points,
            np.asarray([10.0, 10.0, 10.0]),
            classifications=classes,
        )
        self.assertEqual(missing_seed.status, "failed")
        self.assertIsNone(missing_seed.base_position)
        self.assertIn("SEED_NOT_ON_SOURCE_POINT", missing_seed.reason_codes)

    def test_repeated_calls_have_identical_public_payloads(self) -> None:
        base = (0.0, 0.0, 0.0)
        shaft = _shaft(base)
        points, classes = _scene([shaft], _ground(base))
        seed = shaft[shaft.shape[0] // 2]
        payloads = [
            infer_pole_base_from_seed(
                points,
                seed,
                classifications=classes,
            ).to_dict(debug=True)
            for _ in range(3)
        ]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1], payloads[2])
        self.assertLessEqual(
            len((payloads[0]["debug"] or {})["support_points"]),  # type: ignore[index]
            256,
        )


if __name__ == "__main__":
    unittest.main()
