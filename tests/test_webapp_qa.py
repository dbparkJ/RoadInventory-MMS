from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.routing import Mount

from mms_shp_detection.manual_object_tools import MANUAL_OBJECT_TEMPLATES
from mms_shp_detection.qa_rules import QaRuleContext, evaluate, nearby_duplicate_ids
from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp.overlays import (
    _feature_db,
    _initialize_feature_store,
    _overlay_root,
)
from mms_shp_detection.webapp.qa import router as qa_router

NOW = "2026-08-24T00:00:00+00:00"
LAYER_ID = "ov_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMPTY_LAYER_ID = "ov_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _seed_dataset(app: object, dataset_id: str = "dataset-a") -> None:
    app.state.store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name="Dataset A",
        root_id="root-a",
        relative_path="",
        crs="EPSG:32652",
        now=NOW,
    )
    app.state.store.finish_dataset_scan(
        dataset_id,
        frames=[
            {
                "id": "frame-a",
                "ordinal": 0,
                "track_id": "track-a",
                "task": {
                    "image_name": "frame-a.jpg",
                    "origin": [300_000.0, 4_100_000.0, 10.0],
                    "direction": [1.0, 0.0, 0.0],
                    "up": [0.0, 0.0, 1.0],
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


def _feature_row(
    feature_id: str,
    ordinal: int,
    coordinates: list[float] | None,
    properties: dict[str, object],
) -> tuple[object, ...]:
    geometry = (
        None
        if coordinates is None
        else json.dumps(
            {"type": "Point", "coordinates": coordinates}, separators=(",", ":")
        )
    )
    return (
        feature_id,
        ordinal,
        geometry,
        json.dumps(properties, separators=(",", ":")),
        None if coordinates is None else coordinates[0],
        None if coordinates is None else coordinates[1],
        None if coordinates is None or len(coordinates) < 3 else coordinates[2],
        NOW,
    )


def _write_qa_layer(app: object, dataset_id: str = "dataset-a") -> Path:
    layer_dir = _overlay_root(app, dataset_id) / LAYER_ID
    layer_dir.mkdir()
    fields = [
        {
            "name": "CLASS_NM",
            "type": "C",
            "size": 40,
            "decimal": 0,
            "required": True,
            "domain": ["TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"],
        },
        {"name": "SUPPORT_ID", "type": "C", "size": 40, "decimal": 0},
        {
            "name": "STATE",
            "type": "C",
            "size": 10,
            "decimal": 0,
            "domain": ["OK"],
        },
        {"name": "QA_STATUS", "type": "C", "size": 20, "decimal": 0},
    ]
    rows = iter(
        [
            _feature_row(
                "f_bad",
                0,
                None,
                {
                    "CLASS_NM": None,
                    "SUPPORT_ID": "",
                    "STATE": "BAD",
                    "QA_STATUS": "REVIEW",
                },
            ),
            _feature_row(
                "f_duplicate_1",
                1,
                [300_010.0, 4_100_000.0, 10.0],
                {
                    "CLASS_NM": "TRAFFIC_SIGN",
                    "SUPPORT_ID": "",
                    "STATE": "OK",
                    "QA_STATUS": "CONFIRMED",
                },
            ),
            _feature_row(
                "f_duplicate_2",
                2,
                [300_010.2, 4_100_000.0, 10.1],
                {
                    "CLASS_NM": "TRAFFIC_SIGN",
                    "SUPPORT_ID": "pole-a",
                    "STATE": "OK",
                    "QA_STATUS": "CONFIRMED",
                },
            ),
            _feature_row(
                "f_outlier",
                3,
                [400_000.0, 4_200_000.0, 1_000.0],
                {
                    "CLASS_NM": "SIGN_SUPPORT_POLE",
                    "SUPPORT_ID": "pole-outlier",
                    "STATE": "OK",
                    "QA_STATUS": "CONFIRMED",
                },
            ),
        ]
    )
    _initialize_feature_store(layer_dir / "features.sqlite3", rows, fields)
    manifest = {
        "schema_version": 1,
        "id": LAYER_ID,
        "dataset_id": dataset_id,
        "name": "QA target",
        "registered": True,
        "geometry_type": "Point",
        "fields": fields,
        "source_kind": "upload",
        "source_crs": "EPSG:32652",
        "dataset_crs": "EPSG:32652",
        "source_encoding": "utf-8",
    }
    (layer_dir / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )
    connection = sqlite3.connect(layer_dir / "features.sqlite3")
    try:
        connection.execute(
            """
            CREATE TABLE feature_provenance (
                feature_id TEXT PRIMARY KEY,
                provenance_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        provenance = {
            "f_bad": {
                "origin": "MANUAL",
                "source_frame_ids": [],
                "manual_observation_ids": ["obs-bad"],
                "creation_tool": "panorama_bbox_point_v1",
                "review_status": "todo",
                "proposal_status": "review",
            },
            "f_duplicate_1": {
                "origin": "MANUAL",
                "source_frame_ids": ["frame-a"],
                "manual_observation_ids": ["obs-1"],
                "creation_tool": "panorama_bbox_point_v1",
                "review_status": "confirmed",
            },
            "f_outlier": {
                "origin": "AI",
                "source_frame_ids": ["frame-a"],
                "creation_tool": "manual_pole_base_v1",
                "review_status": "confirmed",
            },
        }
        connection.executemany(
            "INSERT INTO feature_provenance(feature_id,provenance_json,updated_at) VALUES(?,?,?)",
            [
                (feature_id, json.dumps(value, separators=(",", ":")), NOW)
                for feature_id, value in provenance.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return layer_dir


def _write_empty_qa_layer(app: object, dataset_id: str = "dataset-a") -> Path:
    layer_dir = _overlay_root(app, dataset_id) / EMPTY_LAYER_ID
    layer_dir.mkdir()
    _initialize_feature_store(layer_dir / "features.sqlite3", iter(()), [])
    (layer_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": EMPTY_LAYER_ID,
                "dataset_id": dataset_id,
                "name": "Empty QA target",
                "registered": True,
                "geometry_type": "Point",
                "fields": [],
                "source_kind": "upload",
                "source_crs": "EPSG:32652",
                "dataset_crs": "EPSG:32652",
                "source_encoding": "utf-8",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return layer_dir


class QaRuleTests(unittest.TestCase):
    def test_table_driven_rules_and_duplicate_grid(self) -> None:
        feature = {
            "id": "f-a",
            "geometry": {"type": "Point", "coordinates": [500.0, 500.0, 500.0]},
            "properties": {
                "CLASS_NM": "TRAFFIC_SIGN",
                "SUPPORT_ID": "",
                "STATE": "BAD",
                "QA_STATUS": "REVIEW",
            },
        }
        context = QaRuleContext(
            layer_id="ov-a",
            fields=({"name": "STATE", "required": True, "domain": ["OK"]},),
            known_frame_ids=frozenset({"frame-a"}),
            dataset_bounds_xy=(0.0, 0.0, 100.0, 100.0),
            z_bounds=(0.0, 100.0),
            duplicate_ids=("f-b",),
        )
        findings = evaluate(
            feature,
            MANUAL_OBJECT_TEMPLATES["TRAFFIC_SIGN"],
            context,
            provenance={
                "origin": "MANUAL",
                "source_frame_ids": ["missing-frame"],
                "review_status": "todo",
                "proposal_status": "review",
            },
        )
        expected = {
            "DOMAIN_VALUE",
            "OUTSIDE_DATASET_BOUNDS",
            "Z_OUTLIER",
            "DUPLICATE_NEARBY",
            "MISSING_SOURCE_FRAME",
            "UNREVIEWED_MANUAL_FEATURE",
            "SUPPORT_RELATION_REQUIRED",
            "REVIEW_PROPOSAL_UNRESOLVED",
        }
        self.assertTrue(expected.issubset({item.rule_id for item in findings}))

        duplicates = nearby_duplicate_ids(
            [
                ("a", (0.0, 0.0, 1.0), "sign", 0.75),
                ("b", (0.2, 0.0, 1.1), "sign", 0.75),
                ("c", (0.2, 0.0, 1.1), "pole", 0.50),
            ]
        )
        self.assertEqual(duplicates, {"a": ("b",), "b": ("a",)})


class WebAppQaTests(unittest.TestCase):
    def test_wildcard_scope_qa_tracks_task_target_layer_revision(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            layer_dir = _write_empty_qa_layer(app)
            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"target_layer_ids": [], "status": "active"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                task = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={
                        "task_type": "MANUAL_SCAN",
                        "target_layer_id": EMPTY_LAYER_ID,
                    },
                )
                self.assertEqual(task.status_code, 201, task.text)
                task_id = task.json()["task"]["id"]

                run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(run.status_code, 200, run.text)
                self.assertEqual(run.json()["items"], [])
                self.assertEqual(run.json()["layer_revisions"], {EMPTY_LAYER_ID: 1})

                claimed = client.patch(
                    f"/api/review-tasks/{task_id}",
                    json={"status": "in_progress", "claimed_by": "operator-local"},
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                resolved = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "skipped"},
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)

                connection = sqlite3.connect(layer_dir / "features.sqlite3")
                try:
                    connection.execute(
                        "UPDATE metadata SET value='2' WHERE key='revision'"
                    )
                    connection.commit()
                finally:
                    connection.close()
                stale = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["detail"]["blockers"]["stale_qa_target_layers"],
                    1,
                )

                rerun = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(rerun.status_code, 200, rerun.text)
                self.assertEqual(
                    rerun.json()["layer_revisions"], {EMPTY_LAYER_ID: 2}
                )
                completed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)

    def test_empty_target_scope_records_fresh_snapshot_and_can_complete(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"target_layer_ids": [], "status": "active"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]

                run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(run.status_code, 200, run.text)
                self.assertEqual(run.json()["items"], [])
                self.assertEqual(run.json()["total"], 0)
                self.assertEqual(run.json()["layer_revisions"], {})
                self.assertEqual(
                    run.json()["counts"], {"error": 0, "warning": 0, "info": 0}
                )

                stored = client.get(
                    f"/api/review-sessions/{session_id}"
                ).json()["session"]
                self.assertEqual(stored["qa_layer_revisions"], {})
                self.assertIsNotNone(stored["qa_ran_at"])
                report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(report.status_code, 200, report.text)
                self.assertTrue(report.json()["completion_gate"]["eligible"])
                self.assertEqual(
                    report.json()["completion_gate"]["blockers"],
                    {
                        "open_tasks": 0,
                        "open_error_qa_issues": 0,
                        "qa_not_run": 0,
                        "stale_qa_target_layers": 0,
                        "pending_task_resolutions": 0,
                        "task_resolution_errors": 0,
                        "task_resolution_scan_truncated": 0,
                    },
                )
                completed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)

    def test_run_filter_navigation_and_override_contract(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=state,
                start_runner=False,
            )
            if not any(
                getattr(route, "path", "") == "/api/review-sessions/{session_id}/qa/run"
                for route in app.routes
            ):
                previous_length = len(app.router.routes)
                app.include_router(qa_router)
                added_routes = app.router.routes[previous_length:]
                del app.router.routes[previous_length:]
                mount_index = next(
                    (
                        index
                        for index, route in enumerate(app.router.routes)
                        if isinstance(route, Mount)
                    ),
                    len(app.router.routes),
                )
                app.router.routes[mount_index:mount_index] = added_routes
            _seed_dataset(app)
            _write_qa_layer(app)
            app.state.catalogs["dataset-a"] = {
                "files": [
                    {
                        "min": [299_000.0, 4_099_000.0, 0.0],
                        "max": [301_000.0, 4_101_000.0, 30.0],
                    }
                ]
            }
            app.state.manual_object_proposals = {
                "prp_unresolved": {
                    "dataset_id": "dataset-a",
                    "frame_id": "frame-a",
                    "target_layer_id": LAYER_ID,
                    "observation_id": "obs-uncommitted",
                    "proposal": {
                        "proposal_id": "prp_unresolved",
                        "status": "review",
                    },
                }
            }

            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={
                        "target_layer_ids": [LAYER_ID],
                        "status": "active",
                        "created_by": "qa-test",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]

                run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(run.status_code, 200, run.text)
                payload = run.json()
                rule_ids = {item["rule_id"] for item in payload["items"]}
                self.assertEqual(
                    rule_ids,
                    {
                        "REQUIRED_FIELD",
                        "DOMAIN_VALUE",
                        "GEOMETRY_REQUIRED",
                        "OUTSIDE_DATASET_BOUNDS",
                        "Z_OUTLIER",
                        "DUPLICATE_NEARBY",
                        "MISSING_SOURCE_FRAME",
                        "UNREVIEWED_MANUAL_FEATURE",
                        "SUPPORT_RELATION_REQUIRED",
                        "REVIEW_PROPOSAL_UNRESOLVED",
                    },
                )
                self.assertEqual(payload["total"], len(payload["items"]))
                self.assertGreater(payload["counts"]["error"], 0)
                self.assertGreater(payload["counts"]["warning"], 0)
                self.assertEqual(payload["layer_revisions"], {LAYER_ID: 1})
                stored_session = client.get(
                    f"/api/review-sessions/{session_id}"
                ).json()["session"]
                self.assertEqual(stored_session["qa_layer_revisions"], {LAYER_ID: 1})
                self.assertEqual(
                    stored_session["qa_ran_at"].replace("Z", "+00:00"),
                    payload["ran_at"],
                )
                self.assertNotIn(state_text, run.text)

                duplicate = next(
                    item
                    for item in payload["items"]
                    if item["rule_id"] == "DUPLICATE_NEARBY"
                    and item["feature_id"] == "f_duplicate_1"
                )
                self.assertEqual(duplicate["frame_id"], "frame-a")
                self.assertEqual(
                    duplicate["location_hint"], [300_010.0, 4_100_000.0, 10.0]
                )
                proposal_issue = next(
                    item for item in payload["items"] if item["feature_id"] is None
                )
                self.assertEqual(proposal_issue["frame_id"], "frame-a")

                filtered = client.get(
                    f"/api/review-sessions/{session_id}/qa/issues",
                    params={
                        "severity": "warning",
                        "rule_id": "DUPLICATE_NEARBY",
                        "layer_id": LAYER_ID,
                        "limit": 1,
                    },
                )
                self.assertEqual(filtered.status_code, 200, filtered.text)
                self.assertEqual(filtered.json()["total"], 2)
                self.assertEqual(len(filtered.json()["items"]), 1)
                self.assertEqual(filtered.json()["next_offset"], 1)
                self.assertTrue(
                    all(
                        item["severity"] == "warning"
                        and item["rule_id"] == "DUPLICATE_NEARBY"
                        for item in filtered.json()["items"]
                    )
                )
                filtered_tail = client.get(
                    f"/api/review-sessions/{session_id}/qa/issues",
                    params={
                        "severity": "warning",
                        "rule_id": "DUPLICATE_NEARBY",
                        "layer_id": LAYER_ID,
                        "limit": 1,
                        "offset": 1,
                    },
                )
                self.assertEqual(filtered_tail.status_code, 200, filtered_tail.text)
                self.assertEqual(filtered_tail.json()["total"], 2)
                self.assertEqual(len(filtered_tail.json()["items"]), 1)
                self.assertIsNone(filtered_tail.json()["next_offset"])

                no_reason = client.patch(
                    f"/api/qa/issues/{duplicate['id']}",
                    json={"status": "dismissed"},
                )
                self.assertEqual(no_reason.status_code, 422, no_reason.text)
                dismissed = client.patch(
                    f"/api/qa/issues/{duplicate['id']}",
                    json={
                        "status": "dismissed",
                        "override_reason": "Confirmed separate sign faces",
                    },
                )
                self.assertEqual(dismissed.status_code, 200, dismissed.text)
                self.assertEqual(dismissed.json()["issue"]["status"], "dismissed")

                error = next(
                    item
                    for item in payload["items"]
                    if item["severity"] == "error"
                    and item["rule_id"] == "REQUIRED_FIELD"
                    and item["feature_id"] == "f_bad"
                )
                blocked = client.patch(
                    f"/api/qa/issues/{error['id']}",
                    json={"status": "dismissed", "override_reason": "Ignore error"},
                )
                self.assertEqual(blocked.status_code, 409, blocked.text)
                resolved = client.patch(
                    f"/api/qa/issues/{error['id']}",
                    json={"status": "resolved"},
                )
                self.assertEqual(resolved.status_code, 409, resolved.text)

                # A legacy/manual registry mutation cannot suppress an error
                # that the next deterministic QA run still reproduces.
                with app.state.store.connection(write=True) as connection:
                    connection.execute(
                        "UPDATE qa_issues SET status='resolved' WHERE id=?",
                        (error["id"],),
                    )

                rerun = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(rerun.status_code, 200, rerun.text)
                rerun_by_id = {item["id"]: item for item in rerun.json()["items"]}
                self.assertEqual(rerun_by_id[duplicate["id"]]["status"], "dismissed")
                self.assertEqual(
                    rerun_by_id[duplicate["id"]]["override_reason"],
                    "Confirmed separate sign faces",
                )
                self.assertEqual(rerun_by_id[error["id"]]["status"], "open")

                layer_dir = _overlay_root(app, "dataset-a") / LAYER_ID
                with _feature_db(layer_dir, write=True) as connection:
                    properties = json.loads(
                        str(
                            connection.execute(
                                "SELECT properties_json FROM features WHERE id='f_bad'"
                            ).fetchone()[0]
                        )
                    )
                    properties["CLASS_NM"] = "TRAFFIC_SIGN"
                    connection.execute(
                        "UPDATE features SET properties_json=? WHERE id='f_bad'",
                        (json.dumps(properties, separators=(",", ":")),),
                    )
                    connection.execute(
                        "UPDATE metadata SET value='2' WHERE key='revision'"
                    )
                stale_report = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertEqual(
                    stale_report.json()["completion_gate"]["blockers"][
                        "stale_qa_target_layers"
                    ],
                    1,
                )
                corrected = client.post(
                    f"/api/review-sessions/{session_id}/qa/run"
                )
                self.assertEqual(corrected.status_code, 200, corrected.text)
                self.assertEqual(corrected.json()["layer_revisions"], {LAYER_ID: 2})
                self.assertNotIn(
                    error["id"], {item["id"] for item in corrected.json()["items"]}
                )
                removed_proposal = client.delete(
                    "/api/manual-object-proposals/prp_unresolved"
                )
                self.assertEqual(
                    removed_proposal.status_code, 200, removed_proposal.text
                )
                without_proposal = client.post(
                    f"/api/review-sessions/{session_id}/qa/run"
                )
                self.assertEqual(
                    without_proposal.status_code, 200, without_proposal.text
                )
                self.assertNotIn(
                    proposal_issue["id"],
                    {item["id"] for item in without_proposal.json()["items"]},
                )

                dismissed_page = client.get(
                    f"/api/review-sessions/{session_id}/qa/issues",
                    params={"status": "dismissed", "limit": 1},
                )
                self.assertEqual(dismissed_page.status_code, 200, dismissed_page.text)
                self.assertEqual(dismissed_page.json()["total"], 1)
                self.assertIsNone(dismissed_page.json()["next_offset"])

                missing = client.patch(
                    "/api/qa/issues/qai_missing",
                    json={"status": "resolved"},
                )
                self.assertEqual(missing.status_code, 404, missing.text)

                with app.state.store.connection(write=True) as connection:
                    connection.execute(
                        "UPDATE review_sessions SET status='completed' WHERE id=?",
                        (session_id,),
                    )
                self.assertEqual(
                    client.post(
                        f"/api/review-sessions/{session_id}/qa/run"
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.patch(
                        f"/api/qa/issues/{duplicate['id']}",
                        json={"status": "open"},
                    ).status_code,
                    409,
                )
                with app.state.store.connection(write=True) as connection:
                    connection.execute(
                        "UPDATE review_sessions SET status='archived' WHERE id=?",
                        (session_id,),
                    )
                self.assertEqual(
                    client.post(
                        f"/api/review-sessions/{session_id}/qa/run"
                    ).status_code,
                    409,
                )


if __name__ == "__main__":
    unittest.main()
