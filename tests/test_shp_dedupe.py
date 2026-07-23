from __future__ import annotations

import copy
import unittest
from typing import Any

from mms_shp_detection.shp_writer import deduplicate_sign_and_pole_observations


def _sign(
    detection_id: str,
    image_name: str,
    x: float,
    *,
    y: float = 0.0,
    z: float = 10.0,
    class_id: int = 59,
    support_id: str = "P1",
    point_count: int = 100,
    confidence: float = 0.9,
    record_name: str = "track-01",
) -> dict[str, Any]:
    return {
        "record_name": record_name,
        "detection_id": detection_id,
        "detection_index": int(detection_id.removeprefix("D") or 0),
        "class_id": class_id,
        "class_name": f"class-{class_id}",
        "support_id": support_id,
        "x": x,
        "y": y,
        "z": z,
        "image_name": image_name,
        "point_count": point_count,
        "confidence": confidence,
    }


def _pole(sign: dict[str, Any], *, point_count: int = 50) -> dict[str, Any]:
    return {
        "record_name": sign["record_name"],
        "detection_id": sign["detection_id"],
        "class_id": sign["class_id"],
        "class_name": sign["class_name"],
        "support_id": sign.get("support_id") or "P1",
        "image_name": sign["image_name"],
        "confidence": sign["confidence"],
        "pole_x": 1.0,
        "pole_y": 2.0,
        "pole_z": 3.0,
        "pole_point_count": point_count,
        "pole_quality": 0.8,
    }


class ShapefileObservationDeduplicationTests(unittest.TestCase):
    def test_supported_repeat_keeps_one_canonical_sign_and_relation(self) -> None:
        first = _sign("D1", "frame-001.jpg", 0.0, point_count=60, confidence=0.99)
        second = _sign("D2", "frame-002.jpg", 0.10, point_count=120, confidence=0.90)

        signs, poles = deduplicate_sign_and_pole_observations(
            [first, second],
            [_pole(first), _pole(second)],
        )

        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0]["detection_id"], "D2")
        self.assertEqual(signs[0]["observation_count"], 2)
        self.assertEqual(signs[0]["source_detection_ids"], ["D1", "D2"])
        self.assertEqual(len(poles), 1)
        self.assertEqual(poles[0]["detection_id"], "D2")
        self.assertEqual(poles[0]["sign_observation_count"], 2)

    def test_supported_pole_observation_wins_over_stronger_poleless_frame(self) -> None:
        supported = _sign(
            "D1",
            "frame-001.jpg",
            0.0,
            support_id="P-visible",
            point_count=5,
            confidence=0.50,
        )
        poleless = _sign(
            "D2",
            "frame-002.jpg",
            0.10,
            z=10.10,
            support_id="",
            point_count=500,
            confidence=0.99,
        )

        signs, poles = deduplicate_sign_and_pole_observations(
            [poleless, supported],
            [_pole(supported)],
        )

        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0]["detection_id"], "D1")
        self.assertEqual(signs[0]["support_id"], "P-visible")
        self.assertEqual([item["detection_id"] for item in poles], ["D1"])

    def test_medoid_precedes_point_count_and_confidence(self) -> None:
        left = _sign("D1", "frame-001.jpg", 0.00, point_count=1000, confidence=0.99)
        middle = _sign("D2", "frame-002.jpg", 0.10, point_count=10, confidence=0.50)
        right = _sign("D3", "frame-003.jpg", 0.20, point_count=900, confidence=0.98)

        signs, _ = deduplicate_sign_and_pole_observations([left, middle, right], [])

        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0]["detection_id"], "D2")

    def test_same_image_twins_are_preserved(self) -> None:
        first = _sign("D1", "same-frame.jpg", 0.0)
        second = _sign("D2", "same-frame.jpg", 0.01)

        signs, poles = deduplicate_sign_and_pole_observations(
            [first, second],
            [_pole(first), _pole(second)],
        )

        self.assertEqual({item["detection_id"] for item in signs}, {"D1", "D2"})
        self.assertEqual({item["detection_id"] for item in poles}, {"D1", "D2"})

    def test_class_support_and_vertical_separation_are_preserved(self) -> None:
        cases = (
            (
                _sign("D1", "a.jpg", 0.0, class_id=59),
                _sign("D2", "b.jpg", 0.0, class_id=60),
            ),
            (
                _sign("D1", "a.jpg", 0.0, support_id="P1"),
                _sign("D2", "b.jpg", 0.0, support_id="P2"),
            ),
            (
                _sign("D1", "a.jpg", 0.0, z=10.0),
                _sign("D2", "b.jpg", 0.0, z=10.251),
            ),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                signs, _ = deduplicate_sign_and_pole_observations([first, second], [])
                self.assertEqual(len(signs), 2)

    def test_unsupported_observations_use_tighter_fallback_bounds(self) -> None:
        close = _sign("D1", "a.jpg", 0.0, z=10.0, support_id="")
        within = _sign("D2", "b.jpg", 0.14, z=10.19, support_id="")
        too_far_xy = _sign("D3", "c.jpg", 0.16, z=10.0, support_id="")
        too_far_z = _sign("D4", "d.jpg", 0.0, z=10.21, support_id="")

        merged, _ = deduplicate_sign_and_pole_observations([close, within], [])
        separated_xy, _ = deduplicate_sign_and_pole_observations([close, too_far_xy], [])
        separated_z, _ = deduplicate_sign_and_pole_observations([close, too_far_z], [])

        self.assertEqual(len(merged), 1)
        self.assertEqual(len(separated_xy), 2)
        self.assertEqual(len(separated_z), 2)

    def test_complete_link_does_not_chain_and_is_input_order_independent(self) -> None:
        records = [
            _sign("D1", "a.jpg", 0.0),
            _sign("D2", "b.jpg", 0.2),
            _sign("D3", "c.jpg", 0.4),
        ]

        forward, _ = deduplicate_sign_and_pole_observations(records, [])
        reverse, _ = deduplicate_sign_and_pole_observations(list(reversed(records)), [])
        forward_clusters = {tuple(item["source_detection_ids"]) for item in forward}
        reverse_clusters = {tuple(item["source_detection_ids"]) for item in reverse}

        self.assertEqual(forward_clusters, {("D1", "D2"), ("D3",)})
        self.assertEqual(reverse_clusters, forward_clusters)

    def test_distinct_signs_on_one_support_keep_distinct_pole_relations(self) -> None:
        first = _sign("D1", "a.jpg", 0.0, class_id=59, support_id="P1")
        second = _sign("D2", "b.jpg", 0.0, class_id=60, support_id="P1")
        first_pole = _pole(first)
        second_pole = _pole(second)

        signs, poles = deduplicate_sign_and_pole_observations(
            [first, second],
            [first_pole, second_pole],
        )

        self.assertEqual(len(signs), 2)
        self.assertEqual(len(poles), 2)
        self.assertEqual({item["detection_id"] for item in poles}, {"D1", "D2"})
        self.assertEqual(
            {(item["pole_x"], item["pole_y"], item["pole_z"]) for item in poles},
            {(1.0, 2.0, 3.0)},
        )

    def test_inputs_are_not_mutated(self) -> None:
        first = _sign("D1", "a.jpg", 0.0)
        second = _sign("D2", "b.jpg", 0.1)
        signs = [first, second]
        poles = [_pole(first), _pole(second)]
        original_signs = copy.deepcopy(signs)
        original_poles = copy.deepcopy(poles)

        deduplicate_sign_and_pole_observations(signs, poles)

        self.assertEqual(signs, original_signs)
        self.assertEqual(poles, original_poles)


if __name__ == "__main__":
    unittest.main()
