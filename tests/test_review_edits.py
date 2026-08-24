from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mms_shp_detection.webapp.overlays import (
    ReviewEditMetadata,
    _feature_db,
    _initialize_feature_store,
    _write_feature_review_metadata,
)
from mms_shp_detection.webapp.review_edits import (
    HistoryMutationRequest,
    _mutate_history,
)
from mms_shp_detection.webapp.task_resolution_outbox import (
    enqueue_task_resolution_intent,
)


def _feature(feature_id: str, x: float) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "Point", "coordinates": [x, 2.0, 3.0]},
        "properties": {"NAME": f"point-{x}"},
    }


class ReviewEditHistoryTests(unittest.TestCase):
    def _layer(self, root: Path) -> Path:
        layer = root / "ov_test"
        layer.mkdir()
        initial = _feature("f_000000001", 1.0)
        _initialize_feature_store(
            layer / "features.sqlite3",
            iter(
                [
                    (
                        "f_000000001",
                        0,
                        json.dumps(initial["geometry"]),
                        json.dumps(initial["properties"]),
                        1.0,
                        2.0,
                        3.0,
                        "2026-01-01T00:00:00Z",
                    )
                ]
            ),
            [{"name": "NAME", "type": "C", "size": 40, "decimal": 0}],
        )
        return layer

    def test_outbox_source_key_cannot_be_reused_across_review_scope(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            layer = self._layer(Path(root_text))
            with _feature_db(layer, write=True) as connection:
                enqueue_task_resolution_intent(
                    connection,
                    source_key="shared-source-key",
                    task_id="rvt_scope",
                    feature_id="f_000000001",
                    transition_kind="resolve",
                    resolution="corrected",
                    expected_status=None,
                    allow_claim=False,
                    actor="operator-local",
                    now="2026-08-24T00:00:00+00:00",
                    session_id="rvw_a",
                    dataset_id="dataset-a",
                    layer_id="ov_a",
                )
                with self.assertRaisesRegex(ValueError, "another intent"):
                    enqueue_task_resolution_intent(
                        connection,
                        source_key="shared-source-key",
                        task_id="rvt_scope",
                        feature_id="f_000000001",
                        transition_kind="resolve",
                        resolution="corrected",
                        expected_status=None,
                        allow_claim=False,
                        actor="operator-local",
                        now="2026-08-24T00:00:01+00:00",
                        session_id="rvw_b",
                        dataset_id="dataset-b",
                        layer_id="ov_b",
                    )

    def _record_update(self, layer: Path) -> None:
        before = _feature("f_000000001", 1.0)
        after = _feature("f_000000001", 5.0)
        connection = sqlite3.connect(layer / "features.sqlite3")
        try:
            connection.execute(
                """
                UPDATE features SET geometry_json=?,properties_json=?,point_x=?,updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(after["geometry"]),
                    json.dumps(after["properties"]),
                    5.0,
                    "2026-01-01T00:01:00Z",
                    "f_000000001",
                ),
            )
            connection.execute("UPDATE metadata SET value='2' WHERE key='revision'")
            connection.execute(
                """
                INSERT INTO audit(revision,action,feature_id,before_json,after_json,created_at)
                VALUES(2,'update','f_000000001',?,?,?)
                """,
                (
                    json.dumps(before),
                    json.dumps(after),
                    "2026-01-01T00:01:00Z",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_update_undo_redo_is_revision_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            layer = self._layer(Path(temp_dir))
            self._record_update(layer)
            undo_request = HistoryMutationRequest(
                expected_revision=2,
                idempotency_key="undo-key-0001",
            )
            undone = _mutate_history(layer, undo_request, "undo")
            self.assertEqual(undone["revision"], 3)
            self.assertEqual(undone["feature"]["geometry"]["coordinates"][0], 1.0)

            replay = _mutate_history(layer, undo_request, "undo")
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["revision"], 3)

            redone = _mutate_history(
                layer,
                HistoryMutationRequest(
                    expected_revision=3,
                    idempotency_key="redo-key-0001",
                ),
                "redo",
            )
            self.assertEqual(redone["revision"], 4)
            self.assertEqual(redone["feature"]["geometry"]["coordinates"][0], 5.0)

            with self.assertRaisesRegex(RuntimeError, "revision:4"):
                _mutate_history(
                    layer,
                    HistoryMutationRequest(
                        expected_revision=2,
                        idempotency_key="undo-key-stale",
                    ),
                    "undo",
                )

    def test_new_edit_invalidates_redo_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            layer = self._layer(Path(temp_dir))
            self._record_update(layer)
            _mutate_history(
                layer,
                HistoryMutationRequest(
                    expected_revision=2,
                    idempotency_key="undo-key-0002",
                ),
                "undo",
            )
            connection = sqlite3.connect(layer / "features.sqlite3")
            try:
                before = _feature("f_000000001", 1.0)
                after = _feature("f_000000001", 7.0)
                connection.execute(
                    """
                    UPDATE features SET geometry_json=?,properties_json=?,point_x=?
                    WHERE id='f_000000001'
                    """,
                    (
                        json.dumps(after["geometry"]),
                        json.dumps(after["properties"]),
                        7.0,
                    ),
                )
                connection.execute("UPDATE metadata SET value='4' WHERE key='revision'")
                connection.execute(
                    """
                    INSERT INTO audit(
                        revision,action,feature_id,before_json,after_json,created_at
                    ) VALUES(4,'update','f_000000001',?,?,?)
                    """,
                    (json.dumps(before), json.dumps(after), "2026-01-01T00:02:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "redo_invalidated"):
                _mutate_history(
                    layer,
                    HistoryMutationRequest(
                        expected_revision=4,
                        idempotency_key="redo-key-0002",
                    ),
                    "redo",
                )

    def test_pole_adapter_metadata_stays_out_of_dbf_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            layer = self._layer(Path(temp_dir))
            feature = _feature("f_000000001", 1.0)
            with _feature_db(layer, write=True) as connection:
                _write_feature_review_metadata(
                    connection,
                    dataset_id="dataset-test",
                    layer_id="ov_test",
                    feature_id="f_000000001",
                    metadata=ReviewEditMetadata(
                        source_frame_ids=["frm_1"],
                        creation_tool="manual_pole_base_v1",
                        proposal_quality=0.81,
                        task_id="rvt_1",
                    ),
                    action="create",
                    revision=2,
                    before=None,
                    after=feature,
                    now="2026-01-01T00:01:00Z",
                )
            with _feature_db(layer) as connection:
                row = connection.execute(
                    "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                    ("f_000000001",),
                ).fetchone()
                transaction = connection.execute(
                    "SELECT task_id,revision FROM edit_transactions"
                ).fetchone()
                properties = json.loads(
                    connection.execute(
                        "SELECT properties_json FROM features WHERE id=?",
                        ("f_000000001",),
                    ).fetchone()[0]
                )
            self.assertEqual(properties, {"NAME": "point-1.0"})
            provenance = json.loads(row[0])
            self.assertEqual(provenance["creation_tool"], "manual_pole_base_v1")
            self.assertEqual(provenance["source_frame_ids"], ["frm_1"])
            self.assertEqual(tuple(transaction), ("rvt_1", 2))


if __name__ == "__main__":
    unittest.main()
