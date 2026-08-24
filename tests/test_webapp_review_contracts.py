from __future__ import annotations

import unittest

from pydantic import ValidationError

from mms_shp_detection.webapp.review_contracts import (
    FeatureProvenance,
    GeometryProposal,
    ManualObservation,
    QaIssue,
    ReviewSession,
    ReviewTask,
)

NOW = "2026-08-24T12:00:00Z"


class ReviewContractTests(unittest.TestCase):
    def test_session_task_and_internal_metadata_contracts(self) -> None:
        session = ReviewSession.model_validate(
            {
                "id": "rvw_1",
                "dataset_id": "ds_1",
                "source_run_ids": ["run_1"],
                "target_layer_ids": ["ov_1"],
                "track_ids": ["Track01"],
                "frame_range": [0, 1200],
                "class_filters": ["TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"],
                "status": "active",
                "created_by": "operator-local",
                "created_at": NOW,
                "updated_at": NOW,
                "last_task_id": "rvt_1",
            }
        )
        self.assertEqual(session.frame_range, (0, 1200))

        task = ReviewTask.model_validate(
            {
                "id": "rvt_1",
                "session_id": session.id,
                "dataset_id": session.dataset_id,
                "task_type": "PROJECTION_FAILED",
                "status": "todo",
                "priority": 72,
                "frame_id": "frm_1",
                "track_id": "Track01",
                "source_run_id": "run_1",
                "source_detection_id": "det_1",
                "target_layer_id": "ov_1",
                "class_hint": "TRAFFIC_SIGN",
                "reason_codes": ["NO_SUPPORTING_POINTS"],
                "location_hint": [123.0, 456.0, 10.0],
                "claimed_by": None,
                "resolved_feature_ids": [],
                "resolution": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        self.assertEqual(task.task_type.value, "PROJECTION_FAILED")

        provenance = FeatureProvenance.model_validate(
            {
                "layer_id": "ov_1",
                "feature_id": "f_1",
                "origin": "MANUAL",
                "source_run_id": "run_1",
                "source_frame_ids": ["frm_1"],
                "source_detection_ids": [],
                "manual_observation_ids": ["mob_1"],
                "creation_tool": "panorama_bbox_point_v1",
                "proposal_quality": 0.78,
                "review_status": "confirmed",
                "created_by": "operator-local",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        issue = QaIssue.model_validate(
            {
                "id": "qai_1",
                "session_id": session.id,
                "layer_id": provenance.layer_id,
                "feature_id": provenance.feature_id,
                "rule_id": "DUPLICATE_NEARBY",
                "severity": "warning",
                "message": "Nearby object",
                "related_feature_ids": ["f_2"],
                "status": "open",
            }
        )
        self.assertEqual(issue.feature_id, provenance.feature_id)

        with self.assertRaises(ValidationError):
            ReviewTask.model_validate(
                {
                    **task.model_dump(mode="json"),
                    "status": "silently_done",
                }
            )

    def test_panorama_observation_preserves_seam_crossing_bbox(self) -> None:
        observation = ManualObservation.model_validate(
            {
                "observation_id": "mob_1",
                "dataset_id": "ds_1",
                "frame_id": "frm_1",
                "view_type": "panorama",
                "class_name": "TRAFFIC_SIGN",
                "geometry_2d": {
                    "type": "equirectangular_bbox",
                    "u_intervals": [[0.94, 1.0], [0.0, 0.03]],
                    "v_min": 0.22,
                    "v_max": 0.41,
                    "image_width": 7040,
                    "image_height": 3520,
                },
                "created_by": "operator-local",
            }
        )
        self.assertEqual(len(observation.geometry_2d.u_intervals), 2)

        payload = observation.model_dump(mode="json")
        payload["geometry_2d"]["u_intervals"] = [[0.1, 0.6], [0.5, 0.8]]
        with self.assertRaises(ValidationError):
            ManualObservation.model_validate(payload)

    def test_proposal_requires_non_failed_geometry_and_forbids_unknown_fields(self) -> None:
        payload = {
            "proposal_id": "prp_1",
            "tool_id": "panorama_bbox_point_v1",
            "status": "review",
            "coordinate_space": "dataset",
            "geometry": {"type": "Point", "coordinates": [123.0, 456.0, 7.8]},
            "property_patch": {"CLASS_NM": "TRAFFIC_SIGN"},
            "quality": {
                "score": 0.78,
                "support_point_count": 43,
                "depth_spread_m": 0.18,
                "reprojection_error_px": 4.2,
            },
            "reason_codes": ["DEPTH_CLUSTER_WEAK"],
            "evidence": {
                "frame_id": "frm_1",
                "observation_id": "mob_1",
                "seed_position": [123.1, 456.0, 8.2],
            },
        }
        proposal = GeometryProposal.model_validate(payload)
        self.assertEqual(proposal.geometry.coordinates[2], 7.8)

        with self.assertRaises(ValidationError):
            GeometryProposal.model_validate({**payload, "geometry": None})
        with self.assertRaises(ValidationError):
            GeometryProposal.model_validate({**payload, "server_path": "D:/secret"})


if __name__ == "__main__":
    unittest.main()
