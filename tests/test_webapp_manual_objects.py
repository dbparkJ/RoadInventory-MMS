from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from mms_shp_detection.manual_object_tools import PanoramaBboxPointResult
from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp import manual_objects as manual_objects_module
from mms_shp_detection.webapp import overlays as overlays_module
from mms_shp_detection.webapp import review_edits as review_edits_module
from mms_shp_detection.webapp import task_resolution_outbox as outbox_module
from mms_shp_detection.webapp.overlays import _initialize_feature_store, _overlay_root

NOW = "2026-08-24T00:00:00+00:00"
LAYER_A = "ov_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
LAYER_B = "ov_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _seed_dataset(app: object, dataset_id: str, *, suffix: str) -> None:
    app.state.store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name=f"Dataset {suffix.upper()}",
        root_id=f"root-{suffix}",
        relative_path="",
        crs="EPSG:32652",
        now=NOW,
    )
    base_x = 300_000.0 if suffix == "a" else 400_000.0
    frames = [
        {
            "id": f"frame-{suffix}",
            "ordinal": 0,
            "track_id": f"track-{suffix}",
            "task": {
                "image_name": f"frame-{suffix}.jpg",
                "origin": [base_x, 4_100_000.0, 10.0],
                "direction": [1.0, 0.0, 0.0],
                "up": [0.0, 0.0, 1.0],
            },
            "longitude": 126.75,
            "latitude": 37.03,
            "altitude": 10.0,
            "heading": 90.0,
        }
    ]
    if suffix == "a":
        frames.append(
            {
                "id": "frame-a2",
                "ordinal": 1,
                "track_id": "track-a",
                "task": {
                    "image_name": "frame-a2.jpg",
                    "origin": [base_x + 10.0, 4_100_000.0, 10.0],
                    "direction": [1.0, 0.0, 0.0],
                    "up": [0.0, 0.0, 1.0],
                },
                "longitude": 126.7501,
                "latitude": 37.03,
                "altitude": 10.0,
                "heading": 90.0,
            }
        )
    app.state.store.finish_dataset_scan(
        dataset_id,
        frames=frames,
        tracks=[
            {
                "id": f"track-{suffix}",
                "name": f"Track {suffix.upper()}",
                "frame_count": len(frames),
            }
        ],
        bbox=[126.75, 37.03, 126.76, 37.04],
        warnings=[],
        now=NOW,
    )


def _seed_run(app: object, dataset_id: str, run_id: str) -> None:
    app.state.store.create_run(
        {
            "id": run_id,
            "dataset_id": dataset_id,
            "name": run_id,
            "request": {},
            "resolved": {},
            "work_relative": f"runs/{run_id}",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    app.state.store.update_run(
        run_id,
        NOW,
        status="completed",
        return_code=0,
        finished_at=NOW,
    )


def _create_layer(
    app: object,
    dataset_id: str,
    layer_id: str,
    *,
    initial_point: tuple[float, float, float] | None = None,
    require_name: bool = False,
) -> Path:
    layer_dir = _overlay_root(app, dataset_id) / layer_id
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
        {
            "name": "NAME",
            "type": "C",
            "size": 80,
            "decimal": 0,
            "required": require_name,
        },
        {"name": "SUPPORT_ID", "type": "C", "size": 40, "decimal": 0},
    ]
    rows: list[tuple[object, ...]] = []
    if initial_point is not None:
        geometry = {"type": "Point", "coordinates": list(initial_point)}
        properties = {
            "CLASS_NM": "SIGN_SUPPORT_POLE",
            "NAME": "initial",
            "SUPPORT_ID": None,
        }
        rows.append(
            (
                "f_000000001",
                0,
                json.dumps(geometry, separators=(",", ":")),
                json.dumps(properties, separators=(",", ":")),
                *initial_point,
                NOW,
            )
        )
    _initialize_feature_store(layer_dir / "features.sqlite3", iter(rows), fields)
    manifest = {
        "schema_version": 1,
        "id": layer_id,
        "dataset_id": dataset_id,
        "name": f"Layer {layer_id[-1]}",
        "color": None,
        "metadata_revision": 1,
        "source_kind": "upload",
        "source_reference": None,
        "source_files": [],
        "source_crs": "EPSG:32652",
        "source_encoding": "utf-8",
        "dataset_crs": "EPSG:32652",
        "geometry_type": "Point",
        "shape_type": 11,
        "original_feature_count": len(rows),
        "fields": fields,
        "warnings": [],
        "registered": True,
        "created_at": NOW,
    }
    (layer_dir / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )
    return layer_dir


def _review_result(position: tuple[float, float, float]) -> PanoramaBboxPointResult:
    point = np.asarray(position, dtype=np.float64)
    return PanoramaBboxPointResult(
        status="review",
        position=point,
        score=0.72,
        support_point_count=18,
        depth_spread_m=0.35,
        reprojection_error_px=2.0,
        cluster_count=2,
        reason_codes=("MULTIPLE_DEPTH_CLUSTERS",),
        seed_position=point.copy(),
        support_points=np.empty((0, 3), dtype=np.float64),
    )


def _failed_result() -> PanoramaBboxPointResult:
    return PanoramaBboxPointResult(
        status="failed",
        position=None,
        score=0.0,
        support_point_count=0,
        depth_spread_m=None,
        reprojection_error_px=None,
        cluster_count=0,
        reason_codes=("NO_SUPPORTING_POINTS",),
        seed_position=None,
        support_points=np.empty((0, 3), dtype=np.float64),
    )


def _create_session_task(
    client: TestClient,
    dataset_id: str,
    layer_id: str,
    frame_id: str,
    *,
    claimed_by: str = "operator-local",
    source_run_id: str | None = None,
    source_detection_id: str | None = None,
) -> tuple[str, str]:
    session_response = client.post(
        f"/api/datasets/{dataset_id}/review-sessions",
        json={
            "target_layer_ids": [layer_id],
            "source_run_ids": [source_run_id] if source_run_id else [],
            "status": "active",
            "created_by": claimed_by,
        },
    )
    if session_response.status_code != 201:
        raise AssertionError(session_response.text)
    session_id = session_response.json()["session"]["id"]
    task_response = client.post(
        f"/api/review-sessions/{session_id}/tasks",
        json={
            "task_type": "MANUAL_SCAN",
            "priority": 50,
            "frame_id": frame_id,
            "target_layer_id": layer_id,
            "class_hint": "TRAFFIC_SIGN",
            "source_run_id": source_run_id,
            "source_detection_id": source_detection_id,
        },
    )
    if task_response.status_code != 201:
        raise AssertionError(task_response.text)
    task_id = task_response.json()["task"]["id"]
    claimed = client.patch(
        f"/api/review-tasks/{task_id}",
        json={"status": "in_progress", "claimed_by": claimed_by},
    )
    if claimed.status_code != 200:
        raise AssertionError(claimed.text)
    return session_id, task_id


def _observation_payload(layer_id: str) -> dict[str, object]:
    return {
        "target_layer_id": layer_id,
        "template_id": "TRAFFIC_SIGN",
        "geometry_2d": {
            "type": "equirectangular_bbox",
            "u_intervals": [[0.97, 1.0], [0.0, 0.03]],
            "v_min": 0.35,
            "v_max": 0.65,
            "image_width": 2_000,
            "image_height": 1_000,
        },
        "created_by": "operator-local",
    }


def _create_observation(client: TestClient, layer_id: str = LAYER_A) -> str:
    response = client.post(
        "/api/datasets/dataset-a/frames/frame-a/manual-observations",
        json=_observation_payload(layer_id),
    )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()["observation"]["observation_id"]


def _create_proposal(
    client: TestClient,
    observation_id: str,
    result: PanoramaBboxPointResult,
) -> dict[str, object]:
    with patch(
        "mms_shp_detection.webapp.manual_objects._infer_proposal_from_frame",
        return_value=result,
    ):
        response = client.post(
            "/api/datasets/dataset-a/frames/frame-a/manual-object-proposals",
            json={
                "target_layer_id": LAYER_A,
                "observation_id": observation_id,
                "template_id": "TRAFFIC_SIGN",
                "property_patch": {"NAME": "manual sign"},
            },
        )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()["proposal"]


class WebAppManualObjectTests(unittest.TestCase):
    def _app(self, root: Path, state: Path) -> object:
        app = create_app(
            allowed_roots=[root],
            state_dir=state,
            start_runner=False,
        )
        required_routes = {
            "/api/manual-object-templates",
            "/api/manual-object-proposals/{proposal_id}/commit",
            "/api/datasets/{dataset_id}/overlays/{layer_id}/undo",
            "/api/datasets/{dataset_id}/overlays/{layer_id}/redo",
        }
        available = {getattr(route, "path", "") for route in app.routes}
        for route in app.routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                available.update(
                    getattr(candidate, "path", "") for candidate in included.routes
                )
        self.assertTrue(
            required_routes.issubset(available),
            f"Shared create_app is missing P1 routes: {sorted(required_routes - available)}",
        )
        app.state.point_reader = Mock()
        app.state.catalogs["dataset-a"] = {"files": []}
        return app

    def test_unexpected_startup_outbox_failure_does_not_block_api(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            patch(
                "mms_shp_detection.webapp.app.reconcile_all_task_resolutions",
                side_effect=RuntimeError("injected unexpected recovery failure"),
            ),
        ):
            app = self._app(Path(root_text), Path(state_text))
            with TestClient(app) as client:
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200, response.text)

    def test_session_report_fails_closed_when_outbox_layer_scan_fails(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"target_layer_ids": [LAYER_A], "status": "active"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                with patch.object(
                    outbox_module,
                    "reconcile_layer_task_resolutions",
                    side_effect=OSError("injected layer scan failure"),
                ):
                    report = client.get(
                        f"/api/review-sessions/{session_id}/report"
                    )
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(
                    report.json()["completion_gate"]["blockers"][
                        "task_resolution_scan_truncated"
                    ],
                    1,
                )

    def test_feature_resolutions_require_task_owned_committed_linkage(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            _create_layer(
                app,
                "dataset-a",
                LAYER_A,
                initial_point=(300_000.0, 4_100_000.0, 10.0),
            )
            with TestClient(app) as client:
                _, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                unlinked = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={
                        "resolution": "corrected",
                        "resolved_feature_ids": ["f_000000001"],
                    },
                )
                self.assertEqual(unlinked.status_code, 422, unlinked.text)
                self.assertEqual(
                    client.get(f"/api/review-tasks/{task_id}").json()["task"][
                        "status"
                    ],
                    "in_progress",
                )
                confirmed = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "confirmed"},
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_templates_seam_observation_and_failed_or_review_proposals(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                templates = client.get("/api/manual-object-templates")
                self.assertEqual(templates.status_code, 200, templates.text)
                self.assertEqual(
                    {item["template_id"] for item in templates.json()["items"]},
                    {"TRAFFIC_SIGN", "SIGN_SUPPORT_POLE"},
                )
                observation_response = client.post(
                    "/api/datasets/dataset-a/frames/frame-a/manual-observations",
                    json=_observation_payload(LAYER_A),
                )
                self.assertEqual(
                    observation_response.status_code, 200, observation_response.text
                )
                observation = observation_response.json()["observation"]
                self.assertEqual(
                    observation["geometry_2d"]["u_intervals"],
                    [[0.97, 1.0], [0.0, 0.03]],
                )

                failed = _create_proposal(
                    client, observation["observation_id"], _failed_result()
                )
                self.assertEqual(failed["status"], "failed")
                blocked = client.post(
                    f"/api/manual-object-proposals/{failed['proposal_id']}/commit",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "failed-commit-key",
                    },
                )
                self.assertEqual(blocked.status_code, 422, blocked.text)

                review = _create_proposal(
                    client,
                    observation["observation_id"],
                    _review_result((300_010.0, 4_100_000.0, 12.0)),
                )
                self.assertEqual(review["status"], "review")
                loaded = client.get(
                    f"/api/manual-object-proposals/{review['proposal_id']}"
                )
                self.assertEqual(loaded.status_code, 200, loaded.text)
                self.assertEqual(
                    loaded.json()["proposal"]["proposal_id"], review["proposal_id"]
                )

                wrong_tool = client.post(
                    "/api/datasets/dataset-a/frames/frame-a/manual-observations",
                    json={
                        **_observation_payload(LAYER_A),
                        "template_id": "SIGN_SUPPORT_POLE",
                    },
                )
                self.assertEqual(wrong_tool.status_code, 422, wrong_tool.text)
                mismatch_observation = _create_observation(client)
                connection = sqlite3.connect(layer / "features.sqlite3")
                try:
                    connection.execute(
                        "UPDATE manual_observations SET class_name=? WHERE id=?",
                        ("SIGN_SUPPORT_POLE", mismatch_observation),
                    )
                    connection.commit()
                finally:
                    connection.close()
                mismatch = client.post(
                    "/api/datasets/dataset-a/frames/frame-a/manual-object-proposals",
                    json={
                        "target_layer_id": LAYER_A,
                        "observation_id": mismatch_observation,
                        "template_id": "TRAFFIC_SIGN",
                    },
                )
                self.assertEqual(mismatch.status_code, 422, mismatch.text)

    def test_commit_idempotency_duplicate_override_and_task_ownership(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            _seed_dataset(app, "dataset-b", suffix="b")
            _seed_run(app, "dataset-a", "run-a")
            layer_a = _create_layer(app, "dataset-a", LAYER_A)
            _create_layer(app, "dataset-b", LAYER_B)
            with TestClient(app) as client:
                session_a, task_a = _create_session_task(
                    client,
                    "dataset-a",
                    LAYER_A,
                    "frame-a",
                    source_run_id="run-a",
                    source_detection_id="det-a-17",
                )
                _, task_b = _create_session_task(
                    client, "dataset-b", LAYER_B, "frame-b"
                )
                observation_id = _create_observation(client)
                proposal = _create_proposal(
                    client,
                    observation_id,
                    _review_result((300_010.0, 4_100_000.0, 12.0)),
                )
                proposal_id = str(proposal["proposal_id"])

                cross_dataset = client.post(
                    f"/api/manual-object-proposals/{proposal_id}/commit",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "cross-dataset-key",
                        "task_id": task_b,
                    },
                )
                self.assertEqual(cross_dataset.status_code, 422, cross_dataset.text)

                wrong_operator = client.post(
                    f"/api/manual-object-proposals/{proposal_id}/commit",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "wrong-operator-key",
                        "task_id": task_a,
                        "created_by": "another-operator",
                    },
                )
                self.assertEqual(wrong_operator.status_code, 409, wrong_operator.text)

                commit_payload = {
                    "expected_revision": 1,
                    "idempotency_key": "manual-commit-key",
                    "task_id": task_a,
                    "created_by": "operator-local",
                    "properties": {"CLASS_NM": "WRONG", "NAME": "committed"},
                }
                paused = client.patch(
                    f"/api/review-sessions/{session_a}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused.status_code, 200, paused.text)
                paused_commit = client.post(
                    f"/api/manual-object-proposals/{proposal_id}/commit",
                    json=commit_payload,
                )
                self.assertEqual(paused_commit.status_code, 409, paused_commit.text)
                resumed = client.patch(
                    f"/api/review-sessions/{session_a}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)
                committed = client.post(
                    f"/api/manual-object-proposals/{proposal_id}/commit",
                    json=commit_payload,
                )
                self.assertEqual(committed.status_code, 200, committed.text)
                committed_payload = committed.json()
                self.assertFalse(committed_payload["idempotent_replay"])
                self.assertFalse(committed_payload["task_resolution_pending"])
                feature_id = committed_payload["feature"]["id"]
                self.assertEqual(
                    committed_payload["feature"]["properties"]["CLASS_NM"],
                    "TRAFFIC_SIGN",
                )

                replay = client.post(
                    f"/api/manual-object-proposals/{proposal_id}/commit",
                    json=commit_payload,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()["idempotent_replay"])
                self.assertEqual(replay.json()["feature"]["id"], feature_id)
                task = client.get(f"/api/review-tasks/{task_a}").json()["task"]
                self.assertEqual(task["status"], "manual_added")
                self.assertEqual(task["resolved_feature_ids"], [feature_id])

                exact_observation = _create_observation(client)
                exact_proposal = _create_proposal(
                    client,
                    exact_observation,
                    _review_result((300_010.0, 4_100_000.0, 12.0)),
                )
                collision = client.post(
                    f"/api/manual-object-proposals/{exact_proposal['proposal_id']}/commit",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "manual-commit-key",
                    },
                )
                self.assertEqual(collision.status_code, 422, collision.text)
                terminal_task_reuse = client.post(
                    f"/api/manual-object-proposals/{exact_proposal['proposal_id']}/commit",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "resolved-task-new-edit",
                        "task_id": task_a,
                    },
                )
                self.assertEqual(
                    terminal_task_reuse.status_code, 409, terminal_task_reuse.text
                )
                exact = client.post(
                    f"/api/manual-object-proposals/{exact_proposal['proposal_id']}/commit",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "exact-duplicate-key",
                    },
                )
                self.assertEqual(exact.status_code, 409, exact.text)
                self.assertEqual(
                    exact.json()["detail"]["reason_code"], "EXACT_DUPLICATE"
                )

                near_observation = _create_observation(client)
                near_proposal = _create_proposal(
                    client,
                    near_observation,
                    _review_result((300_010.30, 4_100_000.0, 12.0)),
                )
                near_url = f"/api/manual-object-proposals/{near_proposal['proposal_id']}/commit"
                warned = client.post(
                    near_url,
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "near-duplicate-key",
                    },
                )
                self.assertEqual(warned.status_code, 409, warned.text)
                no_reason = client.post(
                    near_url,
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "near-duplicate-key",
                        "allow_near_duplicate": True,
                    },
                )
                self.assertEqual(no_reason.status_code, 422, no_reason.text)
                overridden = client.post(
                    near_url,
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "near-duplicate-key",
                        "allow_near_duplicate": True,
                        "override_reason": "Confirmed separate sign face",
                    },
                )
                self.assertEqual(overridden.status_code, 200, overridden.text)
                self.assertEqual(overridden.json()["revision"], 3)
                self.assertEqual(len(overridden.json()["duplicate_warnings"]), 1)

                race_observation = _create_observation(client)
                race_proposal = _create_proposal(
                    client,
                    race_observation,
                    _review_result((300_012.0, 4_100_000.0, 12.0)),
                )
                race_url = (
                    f"/api/manual-object-proposals/{race_proposal['proposal_id']}"
                )
                started = threading.Event()
                release = threading.Event()
                original_commit = manual_objects_module._commit_proposal_to_overlay

                def blocking_commit(
                    *args: object, **kwargs: object
                ) -> dict[str, object]:
                    started.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release proposal commit")
                    return original_commit(*args, **kwargs)

                with (
                    patch.object(
                        manual_objects_module,
                        "_commit_proposal_to_overlay",
                        side_effect=blocking_commit,
                    ),
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    future = executor.submit(
                        client.post,
                        f"{race_url}/commit",
                        json={
                            "expected_revision": 3,
                            "idempotency_key": "commit-cancel-race",
                        },
                    )
                    self.assertTrue(started.wait(timeout=5))
                    cancelled = client.delete(race_url)
                    self.assertEqual(cancelled.status_code, 409, cancelled.text)
                    release.set()
                    race_commit = future.result(timeout=5)
                self.assertEqual(race_commit.status_code, 200, race_commit.text)
                removed_after_commit = client.delete(race_url)
                self.assertEqual(
                    removed_after_commit.status_code, 200, removed_after_commit.text
                )

            connection = sqlite3.connect(layer_a / "features.sqlite3")
            try:
                properties = json.loads(
                    connection.execute(
                        "SELECT properties_json FROM features WHERE id=?", (feature_id,)
                    ).fetchone()[0]
                )
                provenance = json.loads(
                    connection.execute(
                        "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                        (feature_id,),
                    ).fetchone()[0]
                )
                override_reason = connection.execute(
                    "SELECT override_reason FROM edit_transactions WHERE idempotency_key=?",
                    ("near-duplicate-key",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertNotIn("creation_tool", properties)
            self.assertNotIn("source_frame_ids", properties)
            self.assertNotIn("source_run_id", properties)
            self.assertEqual(provenance["source_frame_ids"], ["frame-a"])
            self.assertEqual(provenance["source_run_id"], "run-a")
            self.assertEqual(provenance["source_detection_ids"], ["det-a-17"])
            self.assertEqual(override_reason, "Confirmed separate sign face")

    def test_overlay_review_metadata_is_internal_owned_and_resolves_tasks(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            _seed_dataset(app, "dataset-b", suffix="b")
            _seed_run(app, "dataset-a", "run-overlay-a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                create_session, create_task = _create_session_task(
                    client,
                    "dataset-a",
                    LAYER_A,
                    "frame-a",
                    source_run_id="run-overlay-a",
                    source_detection_id="det-overlay-1",
                )
                create_body = {
                    "geometry": {
                        "type": "Point",
                        "coordinates": [300_020.0, 4_100_000.0, 9.5],
                    },
                    "coordinate_space": "dataset",
                    "expected_revision": 1,
                    "idempotency_key": "overlay-create-replay-key",
                    "properties": {
                        "CLASS_NM": "SIGN_SUPPORT_POLE",
                        "NAME": "P0 base",
                    },
                    "review_metadata": {
                        "source_frame_ids": ["frame-a"],
                        "manual_observation_ids": ["mob-p0-1"],
                        "creation_tool": "manual_pole_base_v1",
                        "proposal_quality": 0.84,
                        "created_by": "operator-local",
                        "task_id": create_task,
                    },
                }
                missing_key = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json={**create_body, "idempotency_key": None},
                )
                self.assertEqual(missing_key.status_code, 422, missing_key.text)
                paused_create_session = client.patch(
                    f"/api/review-sessions/{create_session}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused_create_session.status_code, 200)
                paused_create = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json=create_body,
                )
                self.assertEqual(paused_create.status_code, 409, paused_create.text)
                resumed_create_session = client.patch(
                    f"/api/review-sessions/{create_session}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed_create_session.status_code, 200)
                created = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json=create_body,
                )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertFalse(created.json()["task_resolution_pending"])
                feature_id = created.json()["feature"]["id"]
                task = client.get(f"/api/review-tasks/{create_task}").json()["task"]
                self.assertEqual(task["status"], "manual_added")
                create_replay = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json=create_body,
                )
                self.assertEqual(create_replay.status_code, 201, create_replay.text)
                self.assertTrue(create_replay.json()["idempotent_replay"])
                self.assertEqual(create_replay.json()["feature"]["id"], feature_id)
                self.assertEqual(create_replay.json()["revision"], 2)
                key_collision = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json={
                        **create_body,
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_020.5, 4_100_000.0, 9.5],
                        },
                    },
                )
                self.assertEqual(key_collision.status_code, 422, key_collision.text)

                invalid_frame = {
                    **create_body,
                    "expected_revision": 2,
                    "idempotency_key": "invalid-frame-key",
                    "review_metadata": {
                        **create_body["review_metadata"],
                        "source_frame_ids": ["frame-b"],
                        "task_id": None,
                    },
                }
                rejected = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json=invalid_frame,
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                duplicate_ids = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json={
                        **invalid_frame,
                        "review_metadata": {
                            **invalid_frame["review_metadata"],
                            "source_frame_ids": ["frame-a", "frame-a"],
                        },
                    },
                )
                self.assertEqual(duplicate_ids.status_code, 422, duplicate_ids.text)

                _, mismatched_task = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                mismatch = client.patch(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features/{feature_id}",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_021.0, 4_100_000.0, 9.5],
                        },
                        "expected_revision": 2,
                        "review_metadata": {
                            "source_frame_ids": ["frame-a2"],
                            "creation_tool": "manual_pole_base_v1",
                            "task_id": mismatched_task,
                        },
                    },
                )
                self.assertEqual(mismatch.status_code, 422, mismatch.text)

                update_session, update_task = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                update_body = {
                    "geometry": {
                        "type": "Point",
                        "coordinates": [300_022.0, 4_100_000.0, 9.0],
                    },
                    "expected_revision": 2,
                    "idempotency_key": "overlay-patch-replay-key",
                    "review_metadata": {
                        "source_frame_ids": ["frame-a"],
                        "creation_tool": "manual_pole_base_v1",
                        "proposal_quality": 0.91,
                        "task_id": update_task,
                    },
                }
                paused_update_session = client.patch(
                    f"/api/review-sessions/{update_session}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused_update_session.status_code, 200)
                paused_update = client.patch(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features/{feature_id}",
                    json=update_body,
                )
                self.assertEqual(paused_update.status_code, 409, paused_update.text)
                resumed_update_session = client.patch(
                    f"/api/review-sessions/{update_session}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed_update_session.status_code, 200)
                updated = client.patch(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features/{feature_id}",
                    json=update_body,
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertFalse(updated.json()["task_resolution_pending"])
                update_task_value = client.get(
                    f"/api/review-tasks/{update_task}"
                ).json()["task"]
                self.assertEqual(update_task_value["status"], "corrected")
                update_replay = client.patch(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features/{feature_id}",
                    json=update_body,
                )
                self.assertEqual(update_replay.status_code, 200, update_replay.text)
                self.assertTrue(update_replay.json()["idempotent_replay"])
                self.assertEqual(update_replay.json()["revision"], 3)

            connection = sqlite3.connect(layer / "features.sqlite3")
            try:
                properties = json.loads(
                    connection.execute(
                        "SELECT properties_json FROM features WHERE id=?", (feature_id,)
                    ).fetchone()[0]
                )
                provenance = json.loads(
                    connection.execute(
                        "SELECT provenance_json FROM feature_provenance WHERE feature_id=?",
                        (feature_id,),
                    ).fetchone()[0]
                )
                revision = int(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='revision'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            self.assertEqual(revision, 3)
            self.assertEqual(set(properties), {"CLASS_NM", "NAME", "SUPPORT_ID"})
            self.assertNotIn("creation_tool", properties)
            self.assertNotIn("source_run_id", properties)
            self.assertEqual(provenance["origin"], "CORRECTED")
            self.assertEqual(provenance["source_frame_ids"], ["frame-a"])
            self.assertEqual(provenance["source_run_id"], "run-overlay-a")
            self.assertEqual(provenance["source_detection_ids"], ["det-overlay-1"])

    def test_pending_task_resolution_replays_after_app_restart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            app = self._app(root, state)
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                session_id, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                observation_id = _create_observation(client)
                proposal = _create_proposal(
                    client,
                    observation_id,
                    _review_result((300_040.0, 4_100_000.0, 12.0)),
                )
                app.state.store.resolve_review_task = Mock(
                    side_effect=sqlite3.OperationalError("injected registry failure")
                )
                committed = client.post(
                    f"/api/manual-object-proposals/{proposal['proposal_id']}/commit",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "restart-outbox-key",
                        "task_id": task_id,
                        "created_by": "operator-local",
                    },
                )
                self.assertEqual(committed.status_code, 200, committed.text)
                self.assertTrue(committed.json()["task_resolution_pending"])
                feature_id = committed.json()["feature"]["id"]
                with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                    outbox = connection.execute(
                        """
                        SELECT status,session_id,dataset_id,layer_id,task_id,feature_id
                        FROM task_resolution_outbox
                        """
                    ).fetchone()
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM feature_provenance WHERE feature_id=?",
                            (feature_id,),
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM edit_transactions WHERE feature_id=?",
                            (feature_id,),
                        ).fetchone()[0],
                        1,
                    )
                self.assertEqual(
                    tuple(outbox),
                    (
                        "pending",
                        session_id,
                        "dataset-a",
                        LAYER_A,
                        task_id,
                        feature_id,
                    ),
                )

            restarted = self._app(root, state)
            with TestClient(restarted) as client:
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "manual_added")
                self.assertEqual(task["resolved_feature_ids"], [feature_id])
                report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(
                    report.json()["task_resolution_reconciliation"],
                    {"pending": 0, "errors": 0, "scan_truncated": False},
                )
            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM task_resolution_outbox"
                    ).fetchone()[0],
                    "reconciled",
                )
            with closing(sqlite3.connect(state / "registry.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM review_task_events "
                        "WHERE task_id=? AND event_type='resolved'",
                        (task_id,),
                    ).fetchone()[0],
                    1,
                )

    def test_conflicting_terminal_task_keeps_outbox_error_visible(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                session_id, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                proposal = _create_proposal(
                    client,
                    _create_observation(client),
                    _review_result((300_050.0, 4_100_000.0, 12.0)),
                )
                original_resolver = app.state.store.resolve_review_task
                app.state.store.resolve_review_task = Mock(
                    side_effect=sqlite3.OperationalError("injected registry failure")
                )
                committed = client.post(
                    f"/api/manual-object-proposals/{proposal['proposal_id']}/commit",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "conflicting-outbox-key",
                        "task_id": task_id,
                    },
                )
                self.assertEqual(committed.status_code, 200, committed.text)
                feature_id = committed.json()["feature"]["id"]
                app.state.store.resolve_review_task = original_resolver
                outcome, _ = original_resolver(
                    task_id,
                    resolution="false_positive",
                    resolved_feature_ids=[],
                    now=NOW,
                    actor="operator-local",
                )
                self.assertEqual(outcome, "updated")

                report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(
                    report.json()["completion_gate"]["blockers"][
                        "task_resolution_errors"
                    ],
                    1,
                )
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "false_positive")
                self.assertEqual(task["resolved_feature_ids"], [])
            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                row = connection.execute(
                    "SELECT status,last_error_code,feature_id "
                    "FROM task_resolution_outbox"
                ).fetchone()
            self.assertEqual(
                tuple(row),
                ("error", "TERMINAL_TASK_CONFLICT", feature_id),
            )

    def test_overlay_feature_outbox_replays_after_registry_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            app = self._app(root, state)
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                session_id, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                app.state.store.resolve_review_task = Mock(
                    side_effect=sqlite3.OperationalError("injected registry failure")
                )
                created = client.post(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_060.0, 4_100_000.0, 10.0],
                        },
                        "coordinate_space": "dataset",
                        "expected_revision": 1,
                        "idempotency_key": "overlay-outbox-restart-key",
                        "properties": {
                            "CLASS_NM": "SIGN_SUPPORT_POLE",
                            "NAME": "outbox pole",
                        },
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "manual_pole_base_v1",
                            "created_by": "operator-local",
                            "task_id": task_id,
                        },
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertTrue(created.json()["task_resolution_pending"])
                feature_id = created.json()["feature"]["id"]
                pending_report = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertEqual(pending_report.status_code, 200, pending_report.text)
                self.assertEqual(
                    pending_report.json()["completion_gate"]["blockers"][
                        "pending_task_resolutions"
                    ],
                    1,
                )
                unregister = client.delete(
                    f"/api/datasets/dataset-a/overlays/{LAYER_A}"
                )
                self.assertEqual(unregister.status_code, 409, unregister.text)
                self.assertTrue(layer.is_dir())
                paused = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused.status_code, 200, paused.text)
            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    tuple(
                        connection.execute(
                            "SELECT status,task_id,feature_id "
                            "FROM task_resolution_outbox"
                        ).fetchone()
                    ),
                    ("pending", task_id, feature_id),
                )

            restarted = self._app(root, state)
            with TestClient(restarted) as client:
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "in_progress")
                paused_report = client.get(
                    f"/api/review-sessions/{session_id}/report"
                )
                self.assertEqual(paused_report.status_code, 200, paused_report.text)
                self.assertEqual(
                    paused_report.json()["completion_gate"]["blockers"][
                        "pending_task_resolutions"
                    ],
                    1,
                )
                resumed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "manual_added")
                self.assertEqual(task["resolved_feature_ids"], [feature_id])
                report = client.get(f"/api/review-sessions/{session_id}/report")
                self.assertEqual(
                    report.json()["task_resolution_reconciliation"]["pending"],
                    0,
                )
            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM task_resolution_outbox"
                    ).fetchone()[0],
                    "reconciled",
                )

    def test_overlay_feature_and_outbox_write_roll_back_together(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            with TestClient(app) as client:
                _, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                with patch.object(
                    overlays_module,
                    "enqueue_task_resolution_intent",
                    side_effect=sqlite3.OperationalError("injected outbox failure"),
                ):
                    created = client.post(
                        f"/api/datasets/dataset-a/overlays/{LAYER_A}/features",
                        json={
                            "geometry": {
                                "type": "Point",
                                "coordinates": [300_070.0, 4_100_000.0, 10.0],
                            },
                            "coordinate_space": "dataset",
                            "expected_revision": 1,
                            "idempotency_key": "overlay-atomic-write-key",
                            "properties": {
                                "CLASS_NM": "SIGN_SUPPORT_POLE",
                                "NAME": "atomic pole",
                            },
                            "review_metadata": {
                                "source_frame_ids": ["frame-a"],
                                "creation_tool": "manual_pole_base_v1",
                                "created_by": "operator-local",
                                "task_id": task_id,
                            },
                        },
                    )
                self.assertEqual(created.status_code, 422, created.text)

            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM features WHERE deleted=0"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='revision'"
                    ).fetchone()[0],
                    "1",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit WHERE action='create'"
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='task_resolution_outbox'"
                    ).fetchone()
                )

    def test_linked_history_is_immutable_when_session_is_not_active(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            base_url = f"/api/datasets/dataset-a/overlays/{LAYER_A}"
            with TestClient(app) as client:
                session_id, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                created = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_080.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 1,
                        "idempotency_key": "history-immutable-create",
                        "properties": {"CLASS_NM": "TRAFFIC_SIGN"},
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "panorama_bbox_point_v1",
                            "task_id": task_id,
                        },
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                feature_id = created.json()["feature"]["id"]
                paused = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused.status_code, 200, paused.text)
                rejected = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "history-paused-undo",
                    },
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)

                resumed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)
                qa_run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(qa_run.status_code, 200, qa_run.text)
                completed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                rejected_completed = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "history-completed-undo",
                    },
                )
                self.assertEqual(
                    rejected_completed.status_code, 409, rejected_completed.text
                )

            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='revision'"
                    ).fetchone()[0],
                    "2",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT deleted FROM features WHERE id=?", (feature_id,)
                    ).fetchone()[0],
                    0,
                )

    def test_session_completion_waits_for_linked_history_fence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            _create_layer(app, "dataset-a", LAYER_A)
            base_url = f"/api/datasets/dataset-a/overlays/{LAYER_A}"
            with TestClient(app) as client:
                session_id, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                created = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_090.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 1,
                        "idempotency_key": "history-race-create",
                        "properties": {"CLASS_NM": "TRAFFIC_SIGN"},
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "panorama_bbox_point_v1",
                            "task_id": task_id,
                        },
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                qa_run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(qa_run.status_code, 200, qa_run.text)

                started = threading.Event()
                release = threading.Event()
                original_mutation = review_edits_module._mutate_history

                def blocking_mutation(
                    *args: object, **kwargs: object
                ) -> dict[str, object]:
                    started.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release history mutation")
                    return original_mutation(*args, **kwargs)

                with (
                    patch.object(
                        review_edits_module,
                        "_mutate_history",
                        side_effect=blocking_mutation,
                    ),
                    ThreadPoolExecutor(max_workers=2) as executor,
                ):
                    undo_future = executor.submit(
                        client.post,
                        f"{base_url}/undo",
                        json={
                            "expected_revision": 2,
                            "idempotency_key": "history-race-undo",
                        },
                    )
                    self.assertTrue(started.wait(timeout=5))
                    completion_future = executor.submit(
                        client.patch,
                        f"/api/review-sessions/{session_id}",
                        json={"status": "completed"},
                    )
                    self.assertFalse(completion_future.done())
                    release.set()
                    undone = undo_future.result(timeout=5)
                    completed = completion_future.result(timeout=5)
                self.assertEqual(undone.status_code, 200, undone.text)
                self.assertEqual(completed.status_code, 409, completed.text)
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "todo")

    def test_resolve_ack_crash_then_pending_undo_recovers_after_restart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            app = self._app(root, state)
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            base_url = f"/api/datasets/dataset-a/overlays/{LAYER_A}"
            with TestClient(app) as client:
                _, task_id = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                with patch.object(
                    outbox_module,
                    "_update_intent",
                    side_effect=sqlite3.OperationalError("injected ack crash"),
                ):
                    created = client.post(
                        f"{base_url}/features",
                        json={
                            "geometry": {
                                "type": "Point",
                                "coordinates": [300_095.0, 4_100_000.0, 10.0],
                            },
                            "expected_revision": 1,
                            "idempotency_key": "resolve-ack-crash-create",
                            "properties": {"CLASS_NM": "TRAFFIC_SIGN"},
                            "review_metadata": {
                                "source_frame_ids": ["frame-a"],
                                "creation_tool": "panorama_bbox_point_v1",
                                "task_id": task_id,
                            },
                        },
                    )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertTrue(created.json()["task_resolution_pending"])
                feature_id = created.json()["feature"]["id"]
                self.assertEqual(
                    client.get(f"/api/review-tasks/{task_id}").json()["task"][
                        "status"
                    ],
                    "manual_added",
                )

                original_update = app.state.store.update_review_task
                app.state.store.update_review_task = Mock(
                    side_effect=sqlite3.OperationalError("injected reopen failure")
                )
                undone = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 2,
                        "idempotency_key": "pending-reopen-after-crash",
                    },
                )
                self.assertEqual(undone.status_code, 200, undone.text)
                self.assertTrue(undone.json()["task_transition_pending"])
                app.state.store.update_review_task = original_update
                with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT deleted FROM features WHERE id=?", (feature_id,)
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM task_resolution_outbox "
                            "WHERE status='pending'"
                        ).fetchone()[0],
                        2,
                    )

            restarted = self._app(root, state)
            with TestClient(restarted) as client:
                task = client.get(f"/api/review-tasks/{task_id}").json()["task"]
                self.assertEqual(task["status"], "todo")
            with closing(sqlite3.connect(layer / "features.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_resolution_outbox "
                        "WHERE status='reconciled'"
                    ).fetchone()[0],
                    2,
                )

    def test_pole_template_overlay_validation_is_transactional(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(
                app,
                "dataset-a",
                LAYER_A,
                initial_point=(300_000.0, 4_100_000.0, 10.0),
                require_name=True,
            )
            base_url = f"/api/datasets/dataset-a/overlays/{LAYER_A}"
            validation = {"template_id": "SIGN_SUPPORT_POLE"}
            with TestClient(app) as client:
                initial_preflight = client.post(
                    "/api/datasets/dataset-a/manual-objects/duplicate-preflight",
                    json={
                        "target_layer_id": LAYER_A,
                        "template_id": "SIGN_SUPPORT_POLE",
                        "position": [300_000.0, 4_100_000.0, 10.0],
                    },
                )
                self.assertEqual(initial_preflight.status_code, 200)
                self.assertTrue(initial_preflight.json()["exact_duplicate"])
                self_only = client.post(
                    "/api/datasets/dataset-a/manual-objects/duplicate-preflight",
                    json={
                        "target_layer_id": LAYER_A,
                        "template_id": "SIGN_SUPPORT_POLE",
                        "position": [300_000.0, 4_100_000.0, 10.0],
                        "exclude_feature_id": "f_000000001",
                    },
                )
                self.assertEqual(self_only.status_code, 200)
                self.assertFalse(self_only.json()["exact_duplicate"])

                missing_required = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 1,
                        "manual_object_validation": validation,
                    },
                )
                self.assertEqual(missing_required.status_code, 422)
                self.assertIn("NAME is required", missing_required.text)

                clean_preflight = client.post(
                    "/api/datasets/dataset-a/manual-objects/duplicate-preflight",
                    json={
                        "target_layer_id": LAYER_A,
                        "template_id": "SIGN_SUPPORT_POLE",
                        "position": [300_010.0, 4_100_000.0, 10.0],
                    },
                )
                self.assertEqual(clean_preflight.status_code, 200)
                self.assertFalse(clean_preflight.json()["blocked"])
                self.assertEqual(clean_preflight.json()["warning_count"], 0)

                # A competing ordinary edit after the advisory preflight must
                # still be caught by the validation inside the write transaction.
                competitor = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 1,
                        "properties": {
                            "CLASS_NM": "SIGN_SUPPORT_POLE",
                            "NAME": "competing pole",
                        },
                    },
                )
                self.assertEqual(competitor.status_code, 201, competitor.text)
                raced = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 2,
                        "properties": {"NAME": "raced pole"},
                        "manual_object_validation": validation,
                    },
                )
                self.assertEqual(raced.status_code, 409, raced.text)
                self.assertEqual(
                    raced.json()["detail"]["reason_code"], "DUPLICATE_EXACT"
                )

                near = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.3, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 2,
                        "properties": {"NAME": "near pole"},
                        "manual_object_validation": validation,
                    },
                )
                self.assertEqual(near.status_code, 409, near.text)
                self.assertEqual(
                    near.json()["detail"]["reason_code"], "DUPLICATE_NEARBY"
                )
                accepted_near = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.3, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 2,
                        "properties": {
                            "CLASS_NM": "TRAFFIC_SIGN",
                            "NAME": "confirmed separate pole",
                        },
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "manual_pole_base_v1",
                        },
                        "manual_object_validation": {
                            **validation,
                            "allow_near_duplicate": True,
                            "override_reason": "separate support pole",
                        },
                    },
                )
                self.assertEqual(accepted_near.status_code, 201, accepted_near.text)
                self.assertEqual(accepted_near.json()["revision"], 3)
                self.assertEqual(
                    accepted_near.json()["feature"]["properties"]["CLASS_NM"],
                    "SIGN_SUPPORT_POLE",
                )
                self.assertEqual(len(accepted_near.json()["duplicate_warnings"]), 1)

                self_move = client.patch(
                    f"{base_url}/features/f_000000001",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_000.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 3,
                        "manual_object_validation": validation,
                    },
                )
                self.assertEqual(self_move.status_code, 200, self_move.text)
                blocked_move = client.patch(
                    f"{base_url}/features/f_000000001",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_010.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 4,
                        "manual_object_validation": validation,
                    },
                )
                self.assertEqual(blocked_move.status_code, 409, blocked_move.text)
                self.assertEqual(
                    blocked_move.json()["detail"]["reason_code"],
                    "DUPLICATE_EXACT",
                )

            connection = sqlite3.connect(layer / "features.sqlite3")
            try:
                revision = int(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='revision'"
                    ).fetchone()[0]
                )
                override_reason_row = connection.execute(
                    "SELECT override_reason FROM edit_transactions "
                    "WHERE override_reason IS NOT NULL"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(revision, 4)
            self.assertIsNotNone(override_reason_row)
            self.assertEqual(override_reason_row[0], "separate support pole")

    def test_history_create_update_delete_stale_and_redo_invalidation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = self._app(Path(root_text), Path(state_text))
            _seed_dataset(app, "dataset-a", suffix="a")
            layer = _create_layer(app, "dataset-a", LAYER_A)
            base_url = f"/api/datasets/dataset-a/overlays/{LAYER_A}"
            with TestClient(app) as client:
                create_session, create_task = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                created = client.post(
                    f"{base_url}/features",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_030.0, 4_100_000.0, 10.0],
                        },
                        "expected_revision": 1,
                        "idempotency_key": "history-create-feature-key",
                        "properties": {"CLASS_NM": "TRAFFIC_SIGN", "NAME": "v1"},
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "panorama_bbox_point_v1",
                            "task_id": create_task,
                        },
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                feature_id = created.json()["feature"]["id"]

                undo_create_body = {
                    "expected_revision": 2,
                    "idempotency_key": "undo-create-key",
                    "actor": "operator-local",
                }
                other_operator = client.post(
                    f"{base_url}/undo",
                    json={
                        **undo_create_body,
                        "idempotency_key": "foreign-undo-key",
                        "actor": "another-operator",
                    },
                )
                self.assertEqual(other_operator.status_code, 409, other_operator.text)
                undo_create = client.post(f"{base_url}/undo", json=undo_create_body)
                self.assertEqual(undo_create.status_code, 200, undo_create.text)
                self.assertTrue(undo_create.json()["deleted"])
                self.assertFalse(undo_create.json()["task_transition_pending"])
                self.assertEqual(
                    client.get(f"/api/review-tasks/{create_task}").json()["task"][
                        "status"
                    ],
                    "todo",
                )
                foreign_replay = client.post(
                    f"{base_url}/undo",
                    json={**undo_create_body, "actor": "another-operator"},
                )
                self.assertEqual(foreign_replay.status_code, 409, foreign_replay.text)
                replay = client.post(f"{base_url}/undo", json=undo_create_body)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()["idempotent_replay"])
                self.assertFalse(replay.json()["task_transition_pending"])

                redo_create_body = {
                    "expected_revision": 3,
                    "idempotency_key": "redo-create-key",
                    "actor": "operator-local",
                }
                redo_create = client.post(f"{base_url}/redo", json=redo_create_body)
                self.assertEqual(redo_create.status_code, 200, redo_create.text)
                self.assertFalse(redo_create.json()["task_transition_pending"])
                self.assertEqual(
                    client.get(f"/api/review-tasks/{create_task}").json()["task"][
                        "status"
                    ],
                    "manual_added",
                )
                redo_replay = client.post(f"{base_url}/redo", json=redo_create_body)
                self.assertEqual(redo_replay.status_code, 200, redo_replay.text)
                self.assertTrue(redo_replay.json()["idempotent_replay"])
                self.assertFalse(redo_replay.json()["task_transition_pending"])
                with closing(
                    sqlite3.connect(layer / "features.sqlite3")
                ) as connection:
                    outbox_states = connection.execute(
                        """
                        SELECT transition_kind,status,session_id
                        FROM task_resolution_outbox
                        WHERE task_id=? ORDER BY created_at,id
                        """,
                        (create_task,),
                    ).fetchall()
                self.assertEqual(
                    outbox_states,
                    [
                        ("resolve", "reconciled", create_session),
                        ("reopen", "reconciled", create_session),
                        ("resolve", "reconciled", create_session),
                    ],
                )

                _, update_task = _create_session_task(
                    client, "dataset-a", LAYER_A, "frame-a"
                )
                updated = client.patch(
                    f"{base_url}/features/{feature_id}",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_035.0, 4_100_000.0, 11.0],
                        },
                        "expected_revision": 4,
                        "idempotency_key": "history-update-feature-key",
                        "review_metadata": {
                            "source_frame_ids": ["frame-a"],
                            "creation_tool": "panorama_bbox_point_v1",
                            "task_id": update_task,
                        },
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                undo_update = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 5,
                        "idempotency_key": "undo-update-key",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(undo_update.status_code, 200, undo_update.text)
                self.assertEqual(
                    undo_update.json()["feature"]["geometry"]["coordinates"][0],
                    300_030.0,
                )
                redo_update = client.post(
                    f"{base_url}/redo",
                    json={
                        "expected_revision": 6,
                        "idempotency_key": "redo-update-key",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(redo_update.status_code, 200, redo_update.text)
                self.assertEqual(
                    client.get(f"/api/review-tasks/{update_task}").json()["task"][
                        "status"
                    ],
                    "corrected",
                )

                deleted = client.delete(
                    f"{base_url}/features/{feature_id}",
                    params={"expected_revision": 7},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                stale = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 1,
                        "idempotency_key": "undo-stale-key",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                undo_delete = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 8,
                        "idempotency_key": "undo-delete-key",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(undo_delete.status_code, 200, undo_delete.text)
                self.assertFalse(undo_delete.json()["deleted"])
                redo_delete = client.post(
                    f"{base_url}/redo",
                    json={
                        "expected_revision": 9,
                        "idempotency_key": "redo-delete-key",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(redo_delete.status_code, 200, redo_delete.text)
                self.assertTrue(redo_delete.json()["deleted"])

                restored = client.post(
                    f"{base_url}/undo",
                    json={
                        "expected_revision": 10,
                        "idempotency_key": "undo-delete-again",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(restored.status_code, 200, restored.text)
                new_edit = client.patch(
                    f"{base_url}/features/{feature_id}",
                    json={
                        "properties": {"NAME": "new branch"},
                        "expected_revision": 11,
                    },
                )
                self.assertEqual(new_edit.status_code, 200, new_edit.text)
                invalidated = client.post(
                    f"{base_url}/redo",
                    json={
                        "expected_revision": 12,
                        "idempotency_key": "redo-invalidated",
                        "actor": "operator-local",
                    },
                )
                self.assertEqual(invalidated.status_code, 409, invalidated.text)

                history = client.get(f"{base_url}/edit-history")
                self.assertEqual(history.status_code, 200, history.text)
                actions = {item["action"] for item in history.json()["items"]}
                self.assertTrue({"create", "update", "delete"}.issubset(actions))


class ManualObjectOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_bbox_request_keeps_combined_semaphore_owned(self) -> None:
        semaphore = asyncio.Semaphore(1)
        started = asyncio.Event()
        release = asyncio.Event()
        owner_tasks: set[asyncio.Task[object]] = set()

        async def work() -> str:
            started.set()
            await release.wait()
            return "done"

        async def request_owner() -> str:
            async with semaphore:
                return (
                    await manual_objects_module._finish_inference_after_request_cancel(
                        work(),
                        owner_tasks=owner_tasks,
                        logger=Mock(),
                        context="manual bbox test",
                    )
                )

        request_task = asyncio.create_task(request_owner())
        await started.wait()
        request_task.cancel()
        await asyncio.sleep(0)
        queued = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)
        self.assertFalse(queued.done())
        self.assertEqual(len(owner_tasks), 1)

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request_task
        await queued
        semaphore.release()
        self.assertFalse(owner_tasks)


if __name__ == "__main__":
    unittest.main()
