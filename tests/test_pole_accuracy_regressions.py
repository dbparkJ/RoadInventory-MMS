from __future__ import annotations

import math
import unittest

import numpy as np

from mms_shp_detection.pole import (
    PoleSearchParameters,
    cluster_pole_observations,
    find_pole_bases,
)


def _pole_surface(
    x: float,
    y: float,
    z_min: float,
    z_max: float,
    *,
    seed: int,
    z_steps: int = 36,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    for z in np.linspace(z_min, z_max, z_steps):
        for angle in np.linspace(0.0, 2.0 * np.pi, 6, endpoint=False):
            rows.append(
                [
                    x + (0.055 * np.cos(angle)) + rng.normal(0.0, 0.004),
                    y + (0.055 * np.sin(angle)) + rng.normal(0.0, 0.004),
                    z + rng.normal(0.0, 0.006),
                ]
            )
    return np.asarray(rows, dtype=np.float64)


def _flat_ground() -> np.ndarray:
    return np.asarray(
        [
            [x, y, 0.0]
            for x in np.linspace(-1.4, 1.4, 15)
            for y in np.linspace(-1.4, 1.4, 15)
            if np.hypot(x, y) >= 0.28
        ],
        dtype=np.float64,
    )


class PoleAccuracyRegressionTests(unittest.TestCase):
    def test_dominant_lower_clutter_does_not_pull_vertical_pole_base(self) -> None:
        pole = _pole_surface(0.0, 0.0, 0.2, 6.0, seed=1)
        rng = np.random.default_rng(2)
        clutter_rows: list[list[float]] = []
        for z in np.linspace(0.2, 2.0, 18):
            offset_y = 0.18 + (0.02 * (2.0 - z) / 1.8)
            for _ in range(18):
                clutter_rows.append(
                    [
                        rng.normal(0.0, 0.02),
                        offset_y + rng.normal(0.0, 0.015),
                        z + rng.normal(0.0, 0.005),
                    ]
                )
        lower_clutter = np.asarray(clutter_rows, dtype=np.float64)
        ground = _flat_ground()
        neighborhood = np.vstack((pole, lower_clutter, ground))
        corridor = np.zeros(neighborhood.shape[0], dtype=bool)
        corridor[: pole.shape[0] + lower_clutter.shape[0]] = True
        classifications = np.concatenate(
            (
                np.full(
                    pole.shape[0] + lower_clutter.shape[0],
                    84,
                    dtype=np.int16,
                ),
                np.full(ground.shape[0], 2, dtype=np.int16),
            )
        )

        result = find_pole_bases(
            neighborhood,
            corridor,
            np.asarray([0.0, 0.0, 6.2]),
            PoleSearchParameters(
                axis_cluster_radius_m=0.32,
                axis_inlier_radius_m=0.22,
            ),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        base_xy_error = float(np.linalg.norm(result.representative_xyz[:2]))
        tilt_deg = math.degrees(
            math.atan2(
                float(np.linalg.norm(candidate.axis_direction[:2])),
                abs(float(candidate.axis_direction[2])),
            )
        )
        self.assertLess(base_xy_error, 0.08)
        self.assertLess(tilt_deg, 1.0)

    def test_below_ground_shaft_is_reported_as_ground_conflict(self) -> None:
        pole = _pole_surface(
            0.0,
            0.0,
            -0.2,
            5.0,
            seed=3,
            z_steps=40,
        )
        ground = _flat_ground()
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
            np.asarray([0.0, 0.0, 5.2]),
            PoleSearchParameters(),
            classifications,
        )

        self.assertIsNotNone(result)
        assert result is not None
        candidate = result.candidates[0]
        self.assertIsNotNone(candidate.bottom_gap_m)
        self.assertLess(float(candidate.bottom_gap_m), -0.10)
        self.assertEqual(candidate.occlusion_status, "GROUND_CONFLICT")
        self.assertEqual(result.occlusion_status, "GROUND_CONFLICT")
        self.assertNotEqual(candidate.status, "AUTO")
        self.assertNotEqual(result.status, "AUTO")

    def test_multiframe_consensus_rejects_high_quality_base_outlier(self) -> None:
        common = {
            "record_name": "Job_A_Track01",
            "class_id": 1,
            "confidence": 0.9,
            "pole_status": "AUTO",
            "pole_type": "SINGLE",
            "pole_occlusion_status": "VISIBLE",
        }
        observations = [
            {
                **common,
                "image_name": "frame_a.jpg",
                "pole_x": 0.0,
                "pole_y": 0.0,
                "pole_z": 0.0,
                "pole_quality": 0.72,
            },
            {
                **common,
                "image_name": "frame_b.jpg",
                "pole_x": 0.04,
                "pole_y": -0.02,
                "pole_z": 0.03,
                "pole_quality": 0.70,
            },
            {
                **common,
                "image_name": "frame_outlier.jpg",
                "pole_x": 0.32,
                "pole_y": 0.25,
                "pole_z": -0.20,
                "pole_quality": 0.99,
            },
        ]

        merged = cluster_pole_observations(observations, radius_m=0.75)

        self.assertEqual(len(merged), 3)
        self.assertEqual(len({item["support_id"] for item in merged}), 1)
        close_group_center = np.asarray([0.02, -0.01, 0.015])
        representative = np.asarray(
            [merged[0]["pole_x"], merged[0]["pole_y"], merged[0]["pole_z"]]
        )
        self.assertLess(
            float(np.linalg.norm(representative - close_group_center)),
            0.05,
        )
        self.assertTrue(
            all(item["consensus_outlier_count"] == 1 for item in merged)
        )
        self.assertTrue(all(item["pole_status"] == "REVIEW" for item in merged))


if __name__ == "__main__":
    unittest.main()
