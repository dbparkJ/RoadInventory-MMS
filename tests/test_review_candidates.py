from __future__ import annotations

import unittest

from mms_shp_detection.review_candidates import (
    CandidateSourceSettings,
    generate_review_candidates,
)


def _artifact(
    ordinal: int,
    x: float,
    *,
    confidence: float = 0.9,
    accepted: bool = True,
) -> dict:
    return {
        "source_run_id": "run-a",
        "run_fingerprint": "run-fingerprint-a",
        "model_fingerprint": "model-version-a",
        "frame_id": f"frame-{ordinal}",
        "frame_ordinal": ordinal,
        "track_id": "track-a",
        "record_name": "record-a",
        "image_name": f"frame-{ordinal}.jpg",
        "detection_id": f"det-{ordinal}",
        "detection_index": 1,
        "class_name": "TRAFFIC_SIGN",
        "detection": {
            "detection_index": 1,
            "class_name": "TRAFFIC_SIGN",
            "confidence": confidence,
            "accepted_for_shp": accepted,
            "x": x if accepted else None,
            "y": 0.0 if accepted else None,
            "z": 2.0 if accepted else None,
            "candidate_x": x,
            "candidate_y": 0.0,
            "candidate_z": 2.0,
        },
    }


class ReviewCandidateTests(unittest.TestCase):
    def test_sources_are_switchable_and_fingerprints_are_stable(self) -> None:
        artifact = _artifact(1, 10.0, confidence=0.2, accepted=False)
        artifact["detection"].update(
            {
                "geometry_status": "REVIEW",
                "geometry_reason": "weak support",
                "pole": {
                    "status": "REVIEW",
                    "quality": 0.4,
                    "x": 10.0,
                    "y": 0.0,
                    "z": 0.0,
                    "occlusion_status": "OCCLUDED",
                },
            }
        )
        settings = CandidateSourceSettings(
            unreviewed_interval=False,
            spacing_anomaly=False,
            low_confidence_threshold=0.5,
        )
        first = generate_review_candidates(
            session_id="rvw-a",
            dataset_id="dataset-a",
            artifacts=[artifact],
            frames=[],
            settings=settings,
            target_layer_id="layer-a",
        )
        second = generate_review_candidates(
            session_id="rvw-a",
            dataset_id="dataset-a",
            artifacts=[artifact],
            frames=[],
            settings=settings,
            target_layer_id="layer-a",
        )
        self.assertEqual(
            {item["task_type"] for item in first},
            {
                "LOW_CONFIDENCE",
                "PROJECTION_FAILED",
                "GEOMETRY_REVIEW",
                "POLE_BASE_REVIEW",
            },
        )
        self.assertEqual(
            [item["source_fingerprint"] for item in first],
            [item["source_fingerprint"] for item in second],
        )
        self.assertTrue(all(item["priority_evidence"]["reason"] for item in first))
        disabled = generate_review_candidates(
            session_id="rvw-a",
            dataset_id="dataset-a",
            artifacts=[artifact],
            frames=[],
            settings=CandidateSourceSettings(
                low_confidence=False,
                projection_failed=False,
                geometry_review=False,
                pole_base_review=False,
                unreviewed_interval=False,
                spacing_anomaly=False,
            ),
        )
        self.assertEqual(disabled, [])

    def test_spacing_anomaly_requires_stable_median_and_is_deterministic(self) -> None:
        artifacts = [
            _artifact(index, x)
            for index, x in enumerate((0.0, 5.0, 10.0, 15.0, 20.0, 100.0))
        ]
        settings = CandidateSourceSettings(
            low_confidence=False,
            projection_failed=False,
            geometry_review=False,
            pole_base_review=False,
            unreviewed_interval=False,
            spacing_anomaly=True,
        )
        tasks = generate_review_candidates(
            session_id="rvw-spacing",
            dataset_id="dataset-a",
            artifacts=artifacts,
            frames=[],
            settings=settings,
        )
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["task_type"], "SPACING_ANOMALY")
        self.assertEqual(task["location_hint"], [60.0, 0.0, 2.0])
        self.assertEqual(task["priority_evidence"]["median_gap_m"], 5.0)
        self.assertEqual(task["priority_evidence"]["threshold_m"], 25.0)
        too_few = generate_review_candidates(
            session_id="rvw-spacing",
            dataset_id="dataset-a",
            artifacts=artifacts[:4],
            frames=[],
            settings=settings,
        )
        self.assertEqual(too_few, [])


if __name__ == "__main__":
    unittest.main()
