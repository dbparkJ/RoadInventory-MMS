from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

from mms_shp_detection.webapp import WebAppConfig, create_app, review_reports
from mms_shp_detection.webapp.manual_objects import _ensure_manual_object_tables
from mms_shp_detection.webapp.overlays import _feature_db, _overlay_root

NOW = "2026-08-24T12:00:00+00:00"
FORMULA_ACTOR = '=HYPERLINK("mailto:alice@example.com")'


def _bundle(directory: Path) -> list[Path]:
    primary = directory / "review-poles.shp"
    writer = shapefile.Writer(
        str(primary), shapeType=shapefile.POINTZ, encoding="utf-8"
    )
    writer.field("CLASS", "C", size=40)
    writer.pointz(300_010.0, 4_100_010.0, 10.0)
    writer.record("TRAFFIC_SIGN")
    writer.pointz(300_020.0, 4_100_020.0, 10.0)
    writer.record("TRAFFIC_SIGN")
    writer.close()
    primary.with_suffix(".prj").write_text(
        CRS.from_epsg(32652).to_wkt(version="WKT1_ESRI"), encoding="utf-8"
    )
    primary.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    return sorted(directory.glob("review-poles.*"))


def _seed(app, state: Path) -> None:
    app.state.store.upsert_scanning_dataset(
        dataset_id="dataset-a",
        name="Dataset A",
        root_id="root-a",
        relative_path="private/dataset-a",
        crs="EPSG:32652",
        now=NOW,
    )
    app.state.store.finish_dataset_scan(
        "dataset-a",
        frames=[
            {
                "id": "frame-a",
                "ordinal": 0,
                "track_id": "track-a",
                "task": {
                    "record_name": "record-a",
                    "image_name": "frame-a.jpg",
                    "origin": [300_000.0, 4_100_000.0, 10.0],
                    "private_path": "D:/not-public/frame-a.jpg",
                },
                "longitude": 126.75,
                "latitude": 37.03,
                "altitude": 10.0,
                "heading": 90.0,
            }
        ],
        tracks=[{"id": "track-a", "name": "Track A", "frame_count": 1}],
        bbox=[126.75, 37.03, 126.75, 37.03],
        warnings=[],
        now=NOW,
    )
    app.state.store.create_run(
        {
            "id": "run-a",
            "dataset_id": "dataset-a",
            "name": "Run A",
            "request": {"private_model_path": "D:/models/private.pt"},
            "resolved": {},
            "work_relative": "run-a",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    result = state / "runs" / "run-a" / "output" / "model-a" / "txt" / "record-a"
    result.mkdir(parents=True)
    (result / "frame-a.txt").write_text(
        json.dumps(
            {
                "schema_version": 17,
                "run_fingerprint": "a" * 64,
                "model_name": "model-a.pt",
                "model_sha256": "b" * 64,
                "record_name": "record-a",
                "image_name": "frame-a.jpg",
                "detections": [
                    {
                        "detection_index": 1,
                        "class_name": "TRAFFIC_SIGN",
                        "confidence": 0.2,
                        "accepted_for_shp": False,
                        "exclude_reason": "no_points",
                        "candidate_x": 300_010.0,
                        "candidate_y": 4_100_010.0,
                        "candidate_z": 12.0,
                        "geometry_status": "REVIEW",
                        "geometry_reason": "weak_support",
                        "pole": {
                            "status": "REVIEW",
                            "quality": 0.4,
                            "x": 300_010.0,
                            "y": 4_100_010.0,
                            "z": 10.0,
                            "occlusion_status": "OCCLUDED",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (state / "runs" / "run-a" / "output" / "models_manifest.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_key": "model-a",
                        "status": "completed",
                        "run_fingerprint": "c" * 64,
                        "private_path": "D:/models/private.pt",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    app.state.store.update_run(
        "run-a",
        NOW,
        status="completed",
        return_code=0,
        finished_at=NOW,
    )


class WebAppReviewCandidateReportTests(unittest.TestCase):
    def test_active_learning_capability_is_server_enforced(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                WebAppConfig(
                    project_root=Path(__file__).resolve().parents[1],
                    allowed_roots=[Path(root_text)],
                    state_dir=Path(state_text),
                    static_dir=Path(state_text) / "missing-static",
                    enable_run_worker=False,
                    enable_active_learning_export=False,
                    review_export_stale_seconds=1,
                )
            )
            downloads = Path(state_text) / "downloads"
            stale = downloads / "active-learning-stale-abcdefgh"
            fresh = downloads / "review-fresh-abcdefgh"
            unrelated = downloads / "other-stale-abcdefgh"
            for directory in (stale, fresh, unrelated):
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "partial.zip").write_bytes(b"partial")
            os.utime(stale, (0, 0))
            os.utime(unrelated, (0, 0))
            with patch.object(Path, "is_symlink", return_value=True):
                self.assertEqual(review_reports.cleanup_stale_review_exports(app), 0)
            self.assertTrue(stale.exists())
            with TestClient(app) as client:
                self.assertFalse(stale.exists())
                self.assertTrue(fresh.exists())
                self.assertTrue(unrelated.exists())
                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
                self.assertFalse(
                    bootstrap.json()["capabilities"]["active_learning_export"]
                )
                denied = client.get(
                    "/api/review-sessions/not-present/active-learning-export"
                )
                self.assertEqual(denied.status_code, 403, denied.text)
            protected_csv = review_reports._report_csv(
                {"leading_formula": " \t\x01=2+3", "control": "a\x00b\nc"}
            )
            protected_rows = dict(csv.reader(io.StringIO(protected_csv)))
            self.assertEqual(protected_rows["leading_formula"], "' =2+3")
            self.assertEqual(protected_rows["control"], "abc")

    def test_completed_interval_spans_cover_every_frame_and_do_not_regenerate(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            app.state.store.upsert_scanning_dataset(
                dataset_id="dataset-interval",
                name="Interval dataset",
                root_id="root-interval",
                relative_path="private/dataset-interval",
                crs="EPSG:32652",
                now=NOW,
            )
            app.state.store.finish_dataset_scan(
                "dataset-interval",
                frames=[
                    {
                        "id": f"frame-{index:03d}",
                        "ordinal": index,
                        "track_id": "track-interval",
                        "task": {
                            "record_name": "record-interval",
                            "image_name": f"frame-{index:03d}.jpg",
                            "origin": [300_000.0 + index, 4_100_000.0, 10.0],
                        },
                        "longitude": 126.75,
                        "latitude": 37.03,
                        "altitude": 10.0,
                        "heading": 90.0,
                    }
                    for index in range(100)
                ],
                tracks=[
                    {
                        "id": "track-interval",
                        "name": "Interval track",
                        "frame_count": 100,
                    }
                ],
                bbox=[126.75, 37.03, 126.75, 37.03],
                warnings=[],
                now=NOW,
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-interval/review-sessions",
                    json={
                        "track_ids": ["track-interval"],
                        "frame_range": [0, 99],
                        "status": "active",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                generation_body = {
                    "sources": {
                        "low_confidence": False,
                        "projection_failed": False,
                        "geometry_review": False,
                        "pole_base_review": False,
                        "unreviewed_interval": True,
                        "spacing_anomaly": False,
                    },
                    "unreviewed_interval_frames": 50,
                }
                generated = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json=generation_body,
                )
                self.assertEqual(generated.status_code, 200, generated.text)
                self.assertEqual(generated.json()["created"], 2)
                self.assertEqual(
                    [
                        (task["frame_start"], task["frame_end"])
                        for task in generated.json()["items"]
                    ],
                    [(0, 49), (50, 99)],
                )
                for task in generated.json()["items"]:
                    task_id = task["id"]
                    claimed = client.patch(
                        f"/api/review-tasks/{task_id}",
                        json={
                            "status": "in_progress",
                            "claimed_by": "operator-a",
                        },
                    )
                    self.assertEqual(claimed.status_code, 200, claimed.text)
                    resolved = client.post(
                        f"/api/review-tasks/{task_id}/resolve",
                        json={"resolution": "skipped"},
                    )
                    self.assertEqual(resolved.status_code, 200, resolved.text)

                report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(report.json()["coverage"]["reviewed_frame_count"], 100)
                self.assertEqual(report.json()["coverage"]["frame_coverage_ratio"], 1.0)
                self.assertEqual(report.json()["coverage"]["reviewed_distance_m"], 99.0)
                regenerated = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json=generation_body,
                )
                self.assertEqual(regenerated.status_code, 200, regenerated.text)
                self.assertEqual(regenerated.json()["created"], 0)
                self.assertEqual(regenerated.json()["existing"], 0)

    def test_generation_is_idempotent_and_report_gate_matches_completion(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=state,
                start_runner=False,
            )
            _seed(app, state)
            with TestClient(app) as client:
                bundle = _bundle(Path(bundle_text))
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    data={"name": "Review poles"},
                    files=[
                        (
                            "files",
                            (path.name, path.read_bytes(), "application/octet-stream"),
                        )
                        for path in bundle
                    ],
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={
                        "source_run_ids": ["run-a"],
                        "target_layer_ids": [layer_id],
                        "track_ids": ["track-a"],
                        "frame_range": [0, 0],
                        "class_filters": ["TRAFFIC_SIGN"],
                        "status": "active",
                        "created_by": FORMULA_ACTOR,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                generation_body = {
                    "sources": {
                        "low_confidence": True,
                        "projection_failed": True,
                        "geometry_review": True,
                        "pole_base_review": True,
                        "unreviewed_interval": False,
                        "spacing_anomaly": False,
                    },
                    "low_confidence_threshold": 0.5,
                }
                first = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json=generation_body,
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json()["created"], 4)
                self.assertEqual(first.json()["existing"], 0)
                self.assertFalse(first.json()["discovery"]["truncated"])
                self.assertNotIn(str(state), first.text)
                self.assertNotIn("private.pt", first.text)
                second = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json=generation_body,
                )
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(second.json()["created"], 0)
                self.assertEqual(second.json()["existing"], 4)
                self.assertEqual(
                    [item["id"] for item in first.json()["items"]],
                    [item["id"] for item in second.json()["items"]],
                )
                queue = client.get(f"/api/review-sessions/{session_id}/tasks")
                self.assertEqual(queue.status_code, 200, queue.text)
                self.assertEqual(queue.json()["total"], 4)
                self.assertEqual(queue.json()["status_counts"], {"todo": 4})
                blocked_report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertFalse(blocked_report.json()["completion_gate"]["eligible"])
                blocked_completion = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(blocked_completion.status_code, 409)

                first_task = queue.json()["items"][0]
                first_task_id = first_task["id"]
                self.assertEqual(
                    client.patch(
                        f"/api/review-tasks/{first_task_id}",
                        json={
                            "status": "in_progress",
                            "claimed_by": "operator-a",
                        },
                    ).status_code,
                    200,
                )
                missing_feature = client.post(
                    f"/api/review-tasks/{first_task_id}/resolve",
                    json={
                        "resolution": "confirmed",
                        "resolved_feature_ids": ["f_missing"],
                    },
                )
                self.assertEqual(missing_feature.status_code, 422)
                valid_feature = client.post(
                    f"/api/review-tasks/{first_task_id}/resolve",
                    json={
                        "resolution": "confirmed",
                        "resolved_feature_ids": ["f_000000001"],
                    },
                )
                self.assertEqual(valid_feature.status_code, 200, valid_feature.text)
                partially_reviewed = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertEqual(
                    partially_reviewed.json()["coverage"]["reviewed_frame_count"],
                    0,
                )
                remaining_tasks = queue.json()["items"][1:]
                for index, task in enumerate(remaining_tasks):
                    task_id = task["id"]
                    self.assertEqual(
                        client.patch(
                            f"/api/review-tasks/{task_id}",
                            json={
                                "status": "in_progress",
                                "claimed_by": "operator-a",
                            },
                        ).status_code,
                        200,
                    )
                    if index == 1:
                        linked_edit = client.patch(
                            f"/api/datasets/dataset-a/overlays/{layer_id}/features/"
                            "f_000000001",
                            json={
                                "properties": {"CLASS": "TRAFFIC_SIGN"},
                                "expected_revision": 1,
                                "idempotency_key": "report-linked-correction",
                                "review_metadata": {
                                    "source_frame_ids": ["frame-a"],
                                    "creation_tool": "review_edit_v1",
                                    "created_by": "operator-a",
                                    "task_id": task_id,
                                },
                            },
                        )
                        self.assertEqual(
                            linked_edit.status_code, 200, linked_edit.text
                        )
                        self.assertEqual(
                            client.get(f"/api/review-tasks/{task_id}").json()[
                                "task"
                            ]["status"],
                            "corrected",
                        )
                        continue
                    resolution_body = (
                        {"resolution": "false_positive"}
                        if index == 0
                        else {
                            "resolution": "corrected",
                            "resolved_feature_ids": ["f_000000001"],
                        }
                        if index == 1
                        else {"resolution": "skipped"}
                    )
                    self.assertEqual(
                        client.post(
                            f"/api/review-tasks/{task_id}/resolve",
                            json=resolution_body,
                        ).status_code,
                        200,
                    )
                qa_missing = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertEqual(
                    qa_missing.json()["completion_gate"]["blockers"]["qa_not_run"],
                    1,
                )
                self.assertEqual(
                    client.patch(
                        f"/api/review-sessions/{session_id}",
                        json={"status": "completed"},
                    ).status_code,
                    409,
                )

                qa_snapshot = client.post(
                    f"/api/review-sessions/{session_id}/qa/run"
                )
                self.assertEqual(qa_snapshot.status_code, 200, qa_snapshot.text)
                layer_revision = qa_snapshot.json()["layer_revisions"][layer_id]
                app.state.store.replace_review_qa_issues(
                    session_id,
                    [
                        {
                            "id": "qai_error",
                            "session_id": session_id,
                            "layer_id": layer_id,
                            "feature_id": None,
                            "rule_id": "GEOMETRY_INVALID",
                            "severity": "error",
                            "message": "Open error blocks completion.",
                            "related_feature_ids": [],
                            "status": "open",
                            "created_at": NOW,
                            "updated_at": NOW,
                            "override_reason": None,
                        }
                    ],
                    layer_revisions={layer_id: layer_revision},
                    ran_at=NOW,
                )
                qa_blocked = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(
                    qa_blocked.json()["completion_gate"]["blockers"][
                        "open_error_qa_issues"
                    ],
                    1,
                )
                self.assertEqual(
                    client.patch(
                        f"/api/review-sessions/{session_id}",
                        json={"status": "completed"},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    app.state.store.update_review_qa_issue(
                        "qai_error", "dismissed", "Operator override", NOW
                    )[0],
                    "error_immutable",
                )
                still_blocked = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertFalse(
                    still_blocked.json()["completion_gate"]["eligible"]
                )
                qa_cleared = client.post(
                    f"/api/review-sessions/{session_id}/qa/run"
                )
                self.assertEqual(qa_cleared.status_code, 200, qa_cleared.text)
                self.assertNotIn(
                    "qai_error",
                    {item["id"] for item in qa_cleared.json()["items"]},
                )
                ready = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertTrue(ready.json()["completion_gate"]["eligible"])
                self.assertEqual(ready.json()["coverage"]["reviewed_frame_count"], 1)
                self.assertEqual(
                    ready.json()["tasks"]["by_source"]["LOW_CONFIDENCE"], 1
                )
                self.assertNotIn(str(state), ready.text)

                layer_dir = _overlay_root(app, "dataset-a") / layer_id
                with _feature_db(layer_dir, write=True) as connection:
                    next_revision = layer_revision + 1
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='revision'",
                        (str(next_revision),),
                    )
                stale = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(
                    stale.json()["completion_gate"]["blockers"][
                        "stale_qa_target_layers"
                    ],
                    1,
                )
                self.assertEqual(
                    client.patch(
                        f"/api/review-sessions/{session_id}",
                        json={"status": "completed"},
                    ).status_code,
                    409,
                )
                refreshed = client.post(
                    f"/api/review-sessions/{session_id}/qa/run"
                )
                self.assertEqual(refreshed.status_code, 200, refreshed.text)
                self.assertEqual(
                    refreshed.json()["layer_revisions"],
                    {layer_id: next_revision},
                )
                refreshed_report = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertTrue(
                    refreshed_report.json()["completion_gate"]["eligible"]
                )
                app.state.store.replace_review_qa_issues(
                    session_id,
                    [
                        {
                            "id": "qai_warning",
                            "session_id": session_id,
                            "layer_id": layer_id,
                            "feature_id": None,
                            "rule_id": "ATTRIBUTE_WARNING",
                            "severity": "warning",
                            "message": "Operator review recommended.",
                            "related_feature_ids": [],
                            "status": "open",
                            "created_at": NOW,
                            "updated_at": NOW,
                            "override_reason": None,
                        }
                    ],
                    layer_revisions={layer_id: next_revision},
                    ran_at=NOW,
                )
                snapshot_session = app.state.store.get_review_session(session_id)
                qa_before = review_reports._capture_registry_snapshot(
                    app, snapshot_session
                )
                self.assertEqual(
                    app.state.store.update_review_qa_issue(
                        "qai_warning", "dismissed", "Reviewed", NOW
                    )[0],
                    "updated",
                )
                qa_after = review_reports._capture_registry_snapshot(
                    app, snapshot_session
                )
                self.assertEqual(
                    qa_before["session"]["updated_at"],
                    qa_after["session"]["updated_at"],
                )
                self.assertNotEqual(qa_before["fingerprint"], qa_after["fingerprint"])

                original_csv_renderer = review_reports._report_csv

                def mutate_during_csv(report):
                    rendered = original_csv_renderer(report)
                    with app.state.store.connection(write=True) as connection:
                        connection.execute(
                            "UPDATE review_tasks SET priority=priority+0.25 WHERE id=?",
                            (first_task_id,),
                        )
                    return rendered

                with patch.object(
                    review_reports, "_report_csv", side_effect=mutate_during_csv
                ):
                    changed_report = client.get(
                        f"/api/review-sessions/{session_id}/report",
                        params={"format": "csv"},
                    )
                self.assertEqual(changed_report.status_code, 409, changed_report.text)
                csv_report = client.get(
                    f"/api/review-sessions/{session_id}/report",
                    params={"format": "csv"},
                )
                self.assertIn("completion_gate.eligible,true", csv_report.text)
                csv_rows = dict(csv.reader(io.StringIO(csv_report.text)))
                self.assertEqual(csv_rows["session.created_by"], "'" + FORMULA_ACTOR)
                markdown = client.get(
                    f"/api/review-sessions/{session_id}/report",
                    params={"format": "markdown"},
                )
                self.assertIn("# Review report", markdown.text)
                completed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                self.assertEqual(
                    client.post(
                        f"/api/review-tasks/{first_task_id}/reopen"
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.patch(
                        f"/api/review-tasks/{first_task_id}",
                        json={"priority": 99},
                    ).status_code,
                    409,
                )
                completed_resolve = client.post(
                    f"/api/review-tasks/{first_task_id}/resolve",
                    json={"resolution": "confirmed"},
                )
                self.assertEqual(completed_resolve.status_code, 409)
                self.assertIn("read-only", completed_resolve.text)
                archived = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "archived"},
                )
                self.assertEqual(archived.status_code, 200, archived.text)
                archived_reopen = client.post(
                    f"/api/review-tasks/{first_task_id}/reopen"
                )
                self.assertEqual(archived_reopen.status_code, 409)
                self.assertIn("read-only", archived_reopen.text)

                layer_dir = _overlay_root(app, "dataset-a") / layer_id
                current_task_id = first_task_id
                with _feature_db(layer_dir, write=True) as connection:
                    _ensure_manual_object_tables(connection)
                    connection.executemany(
                        """
                        INSERT INTO manual_observations(
                            id,dataset_id,layer_id,frame_id,view_type,class_name,
                            geometry_json,created_by,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        [
                            (
                                "obs_current",
                                "dataset-a",
                                layer_id,
                                "frame-a",
                                "panorama",
                                "TRAFFIC_SIGN",
                                json.dumps(
                                    {
                                        "type": "equirectangular_bbox",
                                        "u_intervals": [[0.1, 0.2]],
                                        "v_min": 0.2,
                                        "v_max": 0.4,
                                        "image_width": 4000,
                                        "image_height": 2000,
                                    }
                                ),
                                "operator-a",
                                NOW,
                            ),
                            (
                                "obs_other_session",
                                "dataset-a",
                                layer_id,
                                "frame-a",
                                "panorama",
                                "TRAFFIC_SIGN",
                                json.dumps(
                                    {
                                        "type": "equirectangular_bbox",
                                        "u_intervals": [[0.7, 0.8]],
                                        "v_min": 0.2,
                                        "v_max": 0.4,
                                        "image_width": 4000,
                                        "image_height": 2000,
                                    }
                                ),
                                "operator-b",
                                NOW,
                            ),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO feature_provenance(
                            feature_id,provenance_json,updated_at
                        ) VALUES(?,?,?)
                        """,
                        [
                            (
                                "f_000000001",
                                json.dumps(
                                    {
                                        "feature_id": "f_000000001",
                                        "origin": "MANUAL",
                                        "manual_observation_ids": ["obs_current"],
                                        "review_status": "confirmed",
                                    }
                                ),
                                NOW,
                            ),
                            (
                                "f_000000002",
                                json.dumps(
                                    {
                                        "feature_id": "f_000000002",
                                        "origin": "MANUAL",
                                        "manual_observation_ids": ["obs_other_session"],
                                        "review_status": "confirmed",
                                    }
                                ),
                                NOW,
                            ),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO edit_transactions(
                            id,idempotency_key,action,feature_id,task_id,revision,
                            before_json,after_json,status,created_by,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        [
                            (
                                "edit_current",
                                "idem-current",
                                "manual_create",
                                "f_000000001",
                                current_task_id,
                                1,
                                None,
                                "{}",
                                "committed",
                                "operator-a",
                                NOW,
                            ),
                            (
                                "edit_other",
                                "idem-other-session",
                                "manual_create",
                                "f_000000002",
                                "rvt_other_session",
                                1,
                                None,
                                "{}",
                                "committed",
                                "operator-b",
                                NOW,
                            ),
                        ],
                    )

                with app.state.store.connection(write=True) as connection:
                    connection.execute(
                        """
                        UPDATE review_sessions
                        SET source_run_ids_json='[]',target_layer_ids_json='[]'
                        WHERE id=?
                        """,
                        (session_id,),
                    )

                exported = client.get(f"/api/review-sessions/{session_id}/export")
                self.assertEqual(exported.status_code, 200, exported.text)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    self.assertTrue(
                        {
                            "report/report.json",
                            "report/report.csv",
                            "report/report.md",
                        }.issubset(archive.namelist())
                    )
                    provenance_name = next(
                        name
                        for name in archive.namelist()
                        if name.endswith("/provenance.json")
                    )
                    provenance = json.loads(archive.read(provenance_name))
                    self.assertEqual(provenance["layer_revision"], next_revision)
                    self.assertEqual(
                        [item["feature_id"] for item in provenance["items"]],
                        ["f_000000001"],
                    )
                    self.assertTrue(
                        any(name.endswith(".shp") for name in archive.namelist())
                    )
                    self.assertNotIn(
                        str(state), archive.read("report/report.json").decode()
                    )
                    delivery_manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(
                        delivery_manifest["layer_revisions"], {layer_id: next_revision}
                    )
                    self.assertRegex(
                        delivery_manifest["export_fingerprint"], r"^[0-9a-f]{64}$"
                    )
                    exported_report = json.loads(archive.read("report/report.json"))
                    self.assertEqual(
                        exported_report["effective_scope"]["source_run_ids"],
                        ["run-a"],
                    )
                    self.assertEqual(
                        exported_report["effective_scope"]["target_layer_ids"],
                        [layer_id],
                    )
                learning = client.get(
                    f"/api/review-sessions/{session_id}/active-learning-export"
                )
                self.assertEqual(learning.status_code, 200, learning.text)
                with zipfile.ZipFile(io.BytesIO(learning.content)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertFalse(manifest["automation"]["training_started"])
                    self.assertFalse(manifest["automation"]["deployment_started"])
                    self.assertEqual(
                        manifest["effective_scope"]["source_run_ids"], ["run-a"]
                    )
                    self.assertEqual(
                        manifest["effective_scope"]["target_layer_ids"], [layer_id]
                    )
                    self.assertEqual(
                        manifest["layer_revisions"], {layer_id: next_revision}
                    )
                    self.assertRegex(
                        manifest["export_fingerprint"], r"^[0-9a-f]{64}$"
                    )
                    self.assertEqual(len(manifest["model_versions"]), 1)
                    self.assertEqual(
                        manifest["model_versions"][0]["version"], "b" * 64
                    )
                    self.assertEqual(
                        manifest["model_versions"][0]["provenance_status"],
                        "available",
                    )
                    self.assertEqual(
                        manifest["fingerprints"]["runs"][0]["fingerprint"],
                        "a" * 64,
                    )
                    for filename in ("manual_bboxes.jsonl", "review_labels.jsonl"):
                        self.assertEqual(
                            manifest["files"][filename]["sha256"],
                            hashlib.sha256(archive.read(filename)).hexdigest(),
                        )
                    label_rows = [
                        json.loads(line)
                        for line in archive.read("review_labels.jsonl")
                        .decode()
                        .splitlines()
                    ]
                    self.assertEqual(
                        {row["label_action"] for row in label_rows},
                        {"false_positive", "corrected"},
                    )
                    corrected_row = next(
                        row for row in label_rows if row["label_action"] == "corrected"
                    )
                    self.assertEqual(corrected_row["corrected_class"], "TRAFFIC_SIGN")
                    self.assertTrue(
                        corrected_row["source_image_ref"].startswith("image_")
                    )
                    self.assertEqual(
                        corrected_row["source_image"]["dataset_id"], "dataset-a"
                    )
                    self.assertEqual(
                        corrected_row["source_image"]["frame_id"], "frame-a"
                    )
                    self.assertNotIn("path", corrected_row["source_image"])
                    self.assertTrue(corrected_row["model_refs"])
                    manual_rows = [
                        json.loads(line)
                        for line in archive.read("manual_bboxes.jsonl")
                        .decode()
                        .splitlines()
                    ]
                    self.assertEqual(
                        [row["observation_id"] for row in manual_rows],
                        ["obs_current"],
                    )
                    self.assertNotIn("created_by", manual_rows[0])
                    self.assertNotIn("created_at", manual_rows[0])
                    archive_text = "\n".join(
                        archive.read(name).decode("utf-8") for name in archive.namelist()
                    )
                    self.assertNotIn("alice@example.com", archive_text)
                    self.assertNotIn(str(state), archive_text)
                    self.assertNotIn("private.pt", archive_text)

                original_builder = review_reports._build_active_learning_export

                def mutate_after_build(*args, **kwargs):
                    result = original_builder(*args, **kwargs)
                    with app.state.store.connection(write=True) as connection:
                        connection.execute(
                            "UPDATE review_tasks SET priority=priority+1 WHERE id=?",
                            (first_task_id,),
                        )
                    return result

                with patch.object(
                    review_reports,
                    "_build_active_learning_export",
                    side_effect=mutate_after_build,
                ):
                    changed = client.get(
                        f"/api/review-sessions/{session_id}/active-learning-export"
                    )
                self.assertEqual(changed.status_code, 409, changed.text)

                app.state.config.max_review_export_tasks = 3
                bounded = client.get(
                    f"/api/review-sessions/{session_id}/active-learning-export"
                )
                self.assertEqual(bounded.status_code, 413, bounded.text)


if __name__ == "__main__":
    unittest.main()
