from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from mms_shp_detection.manual_object_tools import (
    MANUAL_OBJECT_TEMPLATES,
    infer_panorama_bbox_point,
)
from mms_shp_detection.webapp.manual_objects import (
    ProposalCommitRequest,
    _commit_proposal_to_overlay,
)
from mms_shp_detection.webapp.overlays import _feature_db, _initialize_feature_store

ORIGIN = np.zeros(3, dtype=np.float64)
FORWARD = np.asarray([0.0, 1.0, 0.0], dtype=np.float64)
RIGHT = np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
UP = np.asarray([0.0, 0.0, 1.0], dtype=np.float64)


def _surface(depth: float, *, rear: bool = False, count: int = 36) -> np.ndarray:
    x = np.linspace(-0.18, 0.18, count)
    z = np.linspace(-0.12, 0.12, count)
    y = np.full(count, -depth if rear else depth)
    return np.column_stack((x, y, z))


class ManualObjectToolTests(unittest.TestCase):
    def infer(
        self,
        points: np.ndarray,
        *,
        u_intervals: tuple[tuple[float, float], ...] = ((0.47, 0.53),),
        max_range_m: float = 100.0,
    ):
        return infer_panorama_bbox_point(
            points,
            ORIGIN,
            FORWARD,
            RIGHT,
            UP,
            u_intervals=u_intervals,
            v_min=0.45,
            v_max=0.55,
            image_width=7040,
            image_height=3520,
            max_range_m=max_range_m,
        )

    def test_single_and_multiple_depth_clusters(self) -> None:
        single = self.infer(_surface(10.0))
        self.assertEqual(single.status, "auto")
        self.assertIsNotNone(single.position)
        assert single.position is not None
        self.assertAlmostEqual(float(single.position[1]), 10.0, delta=0.05)

        multiple = self.infer(np.vstack((_surface(10.0), _surface(20.0))))
        self.assertEqual(multiple.status, "review")
        self.assertIn("MULTIPLE_DEPTH_CLUSTERS", multiple.reason_codes)
        self.assertIsNotNone(multiple.position)
        assert multiple.position is not None
        self.assertAlmostEqual(float(multiple.position[1]), 10.0, delta=0.05)

    def test_empty_support_and_maximum_range_fail_without_geometry(self) -> None:
        empty = self.infer(np.empty((0, 3), dtype=np.float64))
        self.assertEqual(empty.status, "failed")
        self.assertEqual(empty.reason_codes, ("NO_SUPPORTING_POINTS",))

        out_of_range = self.infer(_surface(30.0), max_range_m=15.0)
        self.assertEqual(out_of_range.status, "failed")
        self.assertEqual(out_of_range.reason_codes, ("MAX_RANGE_EXCEEDED",))

    def test_proposal_uses_supported_uv_instead_of_loose_bbox_center(self) -> None:
        points = _surface(10.0)
        points[:, 0] += 2.0
        result = self.infer(points, u_intervals=((0.47, 0.56),))

        self.assertIn(result.status, ("auto", "review"))
        self.assertIsNotNone(result.position)
        assert result.position is not None
        self.assertAlmostEqual(float(result.position[0]), 2.0, delta=0.15)
        self.assertAlmostEqual(float(result.position[1]), 10.0, delta=0.15)

    def test_seam_bbox_and_initial_templates(self) -> None:
        seam = self.infer(
            _surface(12.0, rear=True),
            u_intervals=((0.97, 1.0), (0.0, 0.03)),
        )
        self.assertIn(seam.status, ("auto", "review"))
        self.assertIsNotNone(seam.position)
        assert seam.position is not None
        self.assertAlmostEqual(float(seam.position[1]), -12.0, delta=0.05)

        self.assertEqual(
            set(MANUAL_OBJECT_TEMPLATES),
            {"TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"},
        )
        self.assertEqual(
            MANUAL_OBJECT_TEMPLATES["SIGN_SUPPORT_POLE"].tool_id,
            "manual_pole_base_v1",
        )

    def test_commit_is_idempotent_and_preserves_internal_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            layer_dir = Path(directory)
            fields = [
                {"name": "ID", "type": "N", "size": 9, "decimal": 0},
                {"name": "CLASS_NM", "type": "C", "size": 40, "decimal": 0},
            ]
            _initialize_feature_store(layer_dir / "features.sqlite3", iter(()), fields)
            stored = {
                "proposal": {
                    "proposal_id": "prp_1",
                    "tool_id": "panorama_bbox_point_v1",
                    "status": "review",
                    "coordinate_space": "dataset",
                    "geometry": {"type": "Point", "coordinates": [10.0, 20.0, 3.0]},
                    "property_patch": {"CLASS_NM": "WRONG_USER_VALUE"},
                    "quality": {"score": 0.78, "support_point_count": 20},
                    "reason_codes": ["DEPTH_CLUSTER_WEAK"],
                    "evidence": {
                        "frame_id": "frm_1",
                        "observation_id": "mob_1",
                        "seed_position": [10.0, 20.0, 3.0],
                    },
                },
                "dataset_id": "ds_1",
                "frame_id": "frm_1",
                "target_layer_id": "ov_1",
                "template_id": "TRAFFIC_SIGN",
                "observation_id": "mob_1",
            }
            manifest = {
                "dataset_id": "ds_1",
                "geometry_type": "Point",
                "fields": fields,
                "source_encoding": "utf-8",
            }
            request = ProposalCommitRequest(
                expected_revision=1,
                idempotency_key="commit-key-1",
                created_by="operator-local",
            )
            committed = _commit_proposal_to_overlay(
                layer_dir,
                manifest,
                stored,
                request,
                maximum_features=100,
            )
            self.assertEqual(
                committed["feature"]["properties"]["CLASS_NM"], "TRAFFIC_SIGN"
            )
            self.assertFalse(committed["idempotent_replay"])

            replayed = _commit_proposal_to_overlay(
                layer_dir,
                manifest,
                stored,
                request,
                maximum_features=100,
            )
            self.assertTrue(replayed["idempotent_replay"])
            self.assertEqual(replayed["feature"]["id"], committed["feature"]["id"])

            with _feature_db(layer_dir) as connection:
                provenance = connection.execute(
                    "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                    (committed["feature"]["id"],),
                ).fetchone()
                feature_count = connection.execute(
                    "SELECT COUNT(*) FROM features WHERE deleted=0"
                ).fetchone()[0]
            self.assertIsNotNone(provenance)
            self.assertEqual(feature_count, 1)

            with self.assertRaises(FileExistsError):
                _commit_proposal_to_overlay(
                    layer_dir,
                    manifest,
                    {
                        **stored,
                        "proposal": {**stored["proposal"], "proposal_id": "prp_2"},
                    },
                    ProposalCommitRequest(
                        expected_revision=2,
                        idempotency_key="commit-key-2",
                        created_by="operator-local",
                    ),
                    maximum_features=100,
                )


if __name__ == "__main__":
    unittest.main()
