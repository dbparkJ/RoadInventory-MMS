from __future__ import annotations

import unittest

import numpy as np

from mms_shp_detection.alignment import estimate_rgb_pixel_shift, select_alignment_tasks


class AlignmentPixelShiftTests(unittest.TestCase):
    def test_recovers_known_wrapped_rgb_shift(self) -> None:
        rng = np.random.default_rng(42)
        image = rng.integers(0, 256, size=(80, 120, 3), dtype=np.uint8)
        x = rng.integers(8, 112, size=600)
        y = rng.integers(8, 72, size=600)
        pixels = np.column_stack((x, y)).astype(np.float64)
        colors = image[y - 2, (x + 3) % image.shape[1]]

        result = estimate_rgb_pixel_shift(
            image,
            pixels,
            colors,
            search_radius_px=5,
            trim_fraction=0.8,
        )

        self.assertEqual(result["dx_px"], 3)
        self.assertEqual(result["dy_px"], -2)
        self.assertEqual(result["point_count"], 600)
        self.assertAlmostEqual(result["score"], 0.0)
        self.assertGreater(result["baseline_score"], result["score"])

    def test_rejects_invalid_trim_fraction(self) -> None:
        with self.assertRaisesRegex(ValueError, "trim_fraction"):
            estimate_rgb_pixel_shift(
                np.zeros((10, 10, 3), dtype=np.uint8),
                np.asarray([[5.0, 5.0]]),
                np.zeros((1, 3), dtype=np.uint8),
                search_radius_px=1,
                trim_fraction=0.0,
            )


class AlignmentTaskSelectionTests(unittest.TestCase):
    def test_balances_samples_across_records(self) -> None:
        tasks = [
            {"record_name": record, "image_name": f"{record}_{index}.jpg"}
            for record in ("A", "B")
            for index in range(10)
        ]
        selected = select_alignment_tasks(tasks, 6)
        self.assertEqual(len(selected), 6)
        self.assertEqual(
            {record: sum(item["record_name"] == record for item in selected) for record in ("A", "B")},
            {"A": 3, "B": 3},
        )


if __name__ == "__main__":
    unittest.main()
