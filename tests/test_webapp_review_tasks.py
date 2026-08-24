from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp.overlays import _overlay_root
from mms_shp_detection.webapp.store import WebStore

NOW = "2026-08-24T12:00:00+00:00"


def _seed_dataset(app, dataset_id: str, suffix: str) -> tuple[str, str, str]:
    frame_id = f"frame-{suffix}"
    track_id = f"track-{suffix}"
    run_id = f"run-{suffix}"
    app.state.store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name=f"Dataset {suffix}",
        root_id=f"root-{suffix}",
        relative_path=f"private/{suffix}",
        crs="EPSG:32652",
        now=NOW,
    )
    app.state.store.finish_dataset_scan(
        dataset_id,
        frames=[
            {
                "id": frame_id,
                "ordinal": 0,
                "track_id": track_id,
                "task": {
                    "image_name": f"{frame_id}.jpg",
                    "origin": [300_000.0, 4_100_000.0, 10.0],
                    "private_server_path": f"D:/deliveries/{suffix}",
                },
                "longitude": 126.75,
                "latitude": 37.03,
                "altitude": 10.0,
                "heading": 90.0,
            }
        ],
        tracks=[{"id": track_id, "name": track_id, "frame_count": 1}],
        bbox=[126.75, 37.03, 126.75, 37.03],
        warnings=[],
        now=NOW,
    )
    app.state.store.create_run(
        {
            "id": run_id,
            "dataset_id": dataset_id,
            "name": f"Run {suffix}",
            "request": {},
            "resolved": {},
            "work_relative": f"private/work/{suffix}",
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
    layer_id = f"ov_{suffix[0] * 32}"
    layer_dir = _overlay_root(app, dataset_id) / layer_id
    layer_dir.mkdir()
    (layer_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": layer_id,
                "dataset_id": dataset_id,
                "registered": True,
                "private_server_path": f"D:/overlays/{suffix}",
            }
        ),
        encoding="utf-8",
    )
    return frame_id, run_id, layer_id


class WebAppReviewTaskTests(unittest.TestCase):
    def test_completion_status_reuses_gate_for_active_paused_and_completed_work(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            frame_id, _, _ = _seed_dataset(app, "dataset-gate", "gate")
            with TestClient(app) as client:
                missing = client.get(
                    "/api/review-sessions/rvw_missing/completion-status"
                )
                self.assertEqual(missing.status_code, 404, missing.text)

                created = client.post(
                    "/api/datasets/dataset-gate/review-sessions",
                    json={"status": "active"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]

                active = client.get(
                    f"/api/review-sessions/{session_id}/completion-status"
                )
                self.assertEqual(active.status_code, 200, active.text)
                self.assertEqual(active.headers["cache-control"], "no-store")
                self.assertEqual(active.json()["session_status"], "active")
                self.assertFalse(active.json()["requirements_met"])
                self.assertFalse(active.json()["can_complete"])
                self.assertEqual(active.json()["blockers"]["qa_not_run"], 1)
                self.assertIn("checked_at", active.json())

                task_created = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_id},
                )
                self.assertEqual(task_created.status_code, 201, task_created.text)
                task_id = task_created.json()["task"]["id"]
                open_gate = client.get(
                    f"/api/review-sessions/{session_id}/completion-status"
                )
                self.assertEqual(open_gate.json()["blockers"]["open_tasks"], 1)
                self.assertFalse(open_gate.json()["requirements_met"])
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

                paused = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused.status_code, 200, paused.text)
                paused_gate = client.get(
                    f"/api/review-sessions/{session_id}/completion-status"
                )
                self.assertEqual(paused_gate.status_code, 200, paused_gate.text)
                self.assertEqual(paused_gate.json()["blockers"]["qa_not_run"], 1)

                resumed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)
                qa = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(qa.status_code, 200, qa.text)
                ready_gate = client.get(
                    f"/api/review-sessions/{session_id}/completion-status"
                )
                self.assertTrue(ready_gate.json()["requirements_met"])
                self.assertTrue(ready_gate.json()["can_complete"])
                completed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "completed"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                completed_gate = client.get(
                    f"/api/review-sessions/{session_id}/completion-status"
                )
                self.assertEqual(completed_gate.status_code, 200, completed_gate.text)
                self.assertEqual(completed_gate.json()["session_status"], "completed")
                self.assertTrue(completed_gate.json()["requirements_met"])
                self.assertFalse(completed_gate.json()["can_complete"])
                self.assertFalse(any(completed_gate.json()["blockers"].values()))

    def test_filtered_queue_cursor_does_not_skip_after_membership_change(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            frame_id, _, _ = _seed_dataset(app, "dataset-a", "a")
            with TestClient(app) as client:
                session_response = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"status": "active"},
                )
                self.assertEqual(session_response.status_code, 201)
                session_id = session_response.json()["session"]["id"]
                first_task_response = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_id},
                )
                self.assertEqual(first_task_response.status_code, 201)
                template = app.state.store.get_review_task(
                    first_task_response.json()["task"]["id"]
                )
                assert template is not None
                clones = []
                for index in range(1, 205):
                    queue_priority = (
                        100.0
                        if index < 200
                        else 72.8
                        if index == 200
                        else 72.2
                    )
                    clone = {
                        **template,
                        "id": f"rvt_cursor_{index:06d}",
                        "source_fingerprint": None,
                        "priority": queue_priority,
                        "queue_priority": queue_priority,
                    }
                    clones.append(clone)
                app.state.store.create_review_tasks(clones)

                first_page = client.get(
                    f"/api/review-sessions/{session_id}/tasks",
                    params={"status": "todo", "limit": 200},
                )
                self.assertEqual(first_page.status_code, 200, first_page.text)
                first_payload = first_page.json()
                self.assertEqual(len(first_payload["items"]), 200)
                self.assertIsNotNone(first_payload["next_cursor"])
                self.assertEqual(first_payload["items"][-1]["priority"], 72.8)
                original_ids = [item["id"] for item in first_payload["items"]]

                resolved_id = original_ids[0]
                claimed = client.patch(
                    f"/api/review-tasks/{resolved_id}",
                    json={"status": "in_progress", "claimed_by": "operator-local"},
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                resolved = client.post(
                    f"/api/review-tasks/{resolved_id}/resolve",
                    json={"resolution": "skipped"},
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)

                tail = client.get(
                    f"/api/review-sessions/{session_id}/tasks",
                    params={
                        "status": "todo",
                        "limit": 200,
                        "cursor": first_payload["next_cursor"],
                    },
                )
                self.assertEqual(tail.status_code, 200, tail.text)
                self.assertEqual(tail.json()["items"][0]["priority"], 72.2)
                reached = original_ids + [item["id"] for item in tail.json()["items"]]
                self.assertEqual(len(reached), 205)
                self.assertEqual(len(set(reached)), 205)
                self.assertIsNone(tail.json()["next_cursor"])
                for invalid_priority in (True, float("nan"), float("inf")):
                    invalid_cursor = base64.urlsafe_b64encode(
                        json.dumps(
                            [invalid_priority, NOW, "rvt_cursor_invalid"]
                        ).encode("utf-8")
                    ).decode("ascii").rstrip("=")
                    rejected = client.get(
                        f"/api/review-sessions/{session_id}/tasks",
                        params={"status": "todo", "cursor": invalid_cursor},
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_session_queue_relations_transitions_pagination_and_restart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            frame_a, run_a, layer_a = _seed_dataset(app, "dataset-a", "a")
            frame_b, run_b, layer_b = _seed_dataset(app, "dataset-b", "b")
            pending_run_a = "run-pending-a"
            app.state.store.create_run(
                {
                    "id": pending_run_a,
                    "dataset_id": "dataset-a",
                    "name": "Pending run A",
                    "request": {},
                    "resolved": {},
                    "work_relative": "private/work/pending-a",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )

            with TestClient(app) as client:
                wrong_run = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"source_run_ids": [run_b], "status": "active"},
                )
                self.assertEqual(wrong_run.status_code, 422, wrong_run.text)
                pending_run = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"source_run_ids": [pending_run_a], "status": "active"},
                )
                self.assertEqual(pending_run.status_code, 422, pending_run.text)
                self.assertIn("must be completed", pending_run.text)
                wrong_layer = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"target_layer_ids": [layer_b], "status": "active"},
                )
                self.assertEqual(wrong_layer.status_code, 422, wrong_layer.text)

                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={
                        "source_run_ids": [run_a],
                        "target_layer_ids": [layer_a],
                        "track_ids": ["track-a"],
                        "frame_range": [0, 0],
                        "class_filters": ["TRAFFIC_SIGN"],
                        "status": "active",
                        "created_by": "operator-local",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                session = created.json()["session"]
                session_id = session["id"]
                self.assertTrue(session_id.startswith("rvw_"))
                self.assertNotIn(str(state), created.text)
                self.assertNotIn("private/work", created.text)

                second_session = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"status": "draft"},
                )
                self.assertEqual(second_session.status_code, 201, second_session.text)
                draft_session_id = second_session.json()["session"]["id"]
                draft_task = client.post(
                    f"/api/review-sessions/{draft_session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_a},
                )
                self.assertEqual(draft_task.status_code, 409, draft_task.text)
                editable_draft_scope = client.patch(
                    f"/api/review-sessions/{draft_session_id}",
                    json={"class_filters": ["TRAFFIC_SIGN"]},
                )
                self.assertEqual(editable_draft_scope.status_code, 200)
                pending_draft_scope = client.patch(
                    f"/api/review-sessions/{draft_session_id}",
                    json={"source_run_ids": [pending_run_a]},
                )
                self.assertEqual(
                    pending_draft_scope.status_code,
                    422,
                    pending_draft_scope.text,
                )
                activated_draft = client.patch(
                    f"/api/review-sessions/{draft_session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(activated_draft.status_code, 200)
                draft_task = client.post(
                    f"/api/review-sessions/{draft_session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_a},
                )
                self.assertEqual(draft_task.status_code, 201, draft_task.text)
                locked_draft_scope = client.patch(
                    f"/api/review-sessions/{draft_session_id}",
                    json={"class_filters": []},
                )
                self.assertEqual(locked_draft_scope.status_code, 409)
                first_page = client.get(
                    "/api/datasets/dataset-a/review-sessions",
                    params={"offset": 0, "limit": 1},
                )
                self.assertEqual(first_page.status_code, 200, first_page.text)
                self.assertEqual(first_page.json()["total"], 2)
                self.assertEqual(first_page.json()["next_offset"], 1)
                second_page = client.get(
                    "/api/datasets/dataset-a/review-sessions",
                    params={"offset": 1, "limit": 1},
                )
                self.assertIsNone(second_page.json()["next_offset"])
                legacy_session_id = "rvw_legacy_pending_run"
                app.state.store.create_review_session(
                    {
                        "id": legacy_session_id,
                        "dataset_id": "dataset-a",
                        "source_run_ids": [pending_run_a],
                        "target_layer_ids": [],
                        "track_ids": [],
                        "frame_range": None,
                        "class_filters": [],
                        "status": "active",
                        "created_by": "operator-local",
                        "created_at": NOW,
                        "updated_at": NOW,
                        "last_task_id": None,
                    }
                )
                legacy_status_patch = client.patch(
                    f"/api/review-sessions/{legacy_session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(
                    legacy_status_patch.status_code,
                    200,
                    legacy_status_patch.text,
                )
                invalid_session_scope = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"target_layer_ids": [layer_b]},
                )
                self.assertEqual(
                    invalid_session_scope.status_code,
                    409,
                    invalid_session_scope.text,
                )
                paused = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused.status_code, 200, paused.text)
                paused_create = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_a},
                )
                self.assertEqual(paused_create.status_code, 409, paused_create.text)
                paused_generate = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json={"task_type": "MANUAL_FLAG", "frame_id": frame_a},
                )
                self.assertEqual(paused_generate.status_code, 409, paused_generate.text)
                resumed = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)

                wrong_frame = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "frame_id": frame_b},
                )
                self.assertEqual(wrong_frame.status_code, 422, wrong_frame.text)
                wrong_task_run = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={"task_type": "MANUAL_SCAN", "source_run_id": run_b},
                )
                self.assertEqual(wrong_task_run.status_code, 422, wrong_task_run.text)
                pending_task_run = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={
                        "task_type": "MANUAL_SCAN",
                        "source_run_id": pending_run_a,
                    },
                )
                self.assertEqual(
                    pending_task_run.status_code,
                    422,
                    pending_task_run.text,
                )
                self.assertIn("must be completed", pending_task_run.text)
                orphan_detection = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={
                        "task_type": "MANUAL_SCAN",
                        "source_detection_id": "det-orphan",
                    },
                )
                self.assertEqual(
                    orphan_detection.status_code,
                    422,
                    orphan_detection.text,
                )
                self.assertIn("requires source_run_id", orphan_detection.text)

                task_response = client.post(
                    f"/api/review-sessions/{session_id}/tasks",
                    json={
                        "task_type": "MANUAL_SCAN",
                        "priority": 80,
                        "frame_id": frame_a,
                        "source_run_id": run_a,
                        "target_layer_id": layer_a,
                        "class_hint": "TRAFFIC_SIGN",
                        "location_hint": [300_000.0, 4_100_000.0, 10.0],
                    },
                )
                self.assertEqual(task_response.status_code, 201, task_response.text)
                task = task_response.json()["task"]
                task_id = task["id"]
                self.assertEqual(task["track_id"], "track-a")
                self.assertNotIn(str(state), task_response.text)

                generated_one = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json={
                        "task_type": "MANUAL_FLAG",
                        "priority": 20,
                        "frame_id": frame_a,
                    },
                )
                self.assertEqual(generated_one.status_code, 200, generated_one.text)
                self.assertEqual(generated_one.json()["created"], 1)
                generated_batch = client.post(
                    f"/api/review-sessions/{session_id}/tasks/generate",
                    json={
                        "tasks": [
                            {
                                "task_type": "UNREVIEWED_INTERVAL",
                                "priority": 10,
                                "track_id": "track-a",
                            }
                        ]
                    },
                )
                self.assertEqual(generated_batch.status_code, 200, generated_batch.text)
                self.assertEqual(generated_batch.json()["created"], 1)

                queue_page = client.get(
                    f"/api/review-sessions/{session_id}/tasks",
                    params={"offset": 0, "limit": 2},
                )
                self.assertEqual(queue_page.status_code, 200, queue_page.text)
                self.assertEqual(queue_page.json()["total"], 3)
                self.assertEqual(queue_page.json()["next_offset"], 2)
                self.assertEqual(queue_page.json()["items"][0]["id"], task_id)
                queue_tail = client.get(
                    f"/api/review-sessions/{session_id}/tasks",
                    params={"offset": 2, "limit": 2},
                )
                self.assertIsNone(queue_tail.json()["next_offset"])

                invalid_direct_resolution = client.patch(
                    f"/api/review-tasks/{task_id}",
                    json={"status": "confirmed"},
                )
                self.assertEqual(
                    invalid_direct_resolution.status_code,
                    409,
                    invalid_direct_resolution.text,
                )
                claimed = client.patch(
                    f"/api/review-tasks/{task_id}",
                    json={"status": "in_progress", "claimed_by": "operator-local"},
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                self.assertEqual(claimed.json()["task"]["status"], "in_progress")
                restored_session = client.get(f"/api/review-sessions/{session_id}")
                self.assertEqual(
                    restored_session.json()["session"]["last_task_id"], task_id
                )
                empty_manual_added = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "manual_added"},
                )
                self.assertEqual(
                    empty_manual_added.status_code,
                    422,
                    empty_manual_added.text,
                )
                direct_corrected = client.patch(
                    f"/api/review-tasks/{task_id}",
                    json={"status": "corrected"},
                )
                self.assertEqual(direct_corrected.status_code, 422)

                paused_in_progress = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused_in_progress.status_code, 200)
                paused_patch = client.patch(
                    f"/api/review-tasks/{task_id}", json={"priority": 81}
                )
                self.assertEqual(paused_patch.status_code, 409, paused_patch.text)
                paused_resolve = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "skipped"},
                )
                self.assertEqual(paused_resolve.status_code, 409, paused_resolve.text)
                raced_outcome, raced_task = app.state.store.resolve_review_task(
                    task_id,
                    resolution="skipped",
                    resolved_feature_ids=[],
                    now=NOW,
                    actor="operator-local",
                )
                self.assertEqual(raced_outcome, "inactive")
                assert raced_task is not None
                self.assertEqual(raced_task["status"], "in_progress")
                resumed_in_progress = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed_in_progress.status_code, 200)

                resolved = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "skipped"},
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                self.assertEqual(resolved.json()["task"]["status"], "skipped")
                paused_terminal = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "paused"},
                )
                self.assertEqual(paused_terminal.status_code, 200)
                paused_reopen = client.post(f"/api/review-tasks/{task_id}/reopen")
                self.assertEqual(paused_reopen.status_code, 409, paused_reopen.text)
                resumed_terminal = client.patch(
                    f"/api/review-sessions/{session_id}",
                    json={"status": "active"},
                )
                self.assertEqual(resumed_terminal.status_code, 200)
                repeated_resolve = client.post(
                    f"/api/review-tasks/{task_id}/resolve",
                    json={"resolution": "confirmed"},
                )
                self.assertEqual(
                    repeated_resolve.status_code, 409, repeated_resolve.text
                )
                terminal_patch = client.patch(
                    f"/api/review-tasks/{task_id}", json={"priority": 100}
                )
                self.assertEqual(terminal_patch.status_code, 409, terminal_patch.text)

                reopened = client.post(f"/api/review-tasks/{task_id}/reopen")
                self.assertEqual(reopened.status_code, 200, reopened.text)
                self.assertEqual(reopened.json()["task"]["status"], "todo")
                self.assertIsNone(reopened.json()["task"]["resolution"])
                invalid_reopen = client.post(f"/api/review-tasks/{task_id}/reopen")
                self.assertEqual(invalid_reopen.status_code, 409, invalid_reopen.text)

            restarted_app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with TestClient(restarted_app) as restarted_client:
                restored = restarted_client.get(f"/api/review-sessions/{session_id}")
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual(restored.json()["session"]["last_task_id"], task_id)
                restored_task = restarted_client.get(f"/api/review-tasks/{task_id}")
                self.assertEqual(restored_task.status_code, 200, restored_task.text)
                self.assertEqual(restored_task.json()["task"]["status"], "todo")
                self.assertNotIn(str(state), restored.text + restored_task.text)

            with closing(sqlite3.connect(state / "registry.sqlite3")) as connection:
                event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM review_task_events WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                )
                self.assertEqual(event_count, 4)
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "review_sessions",
                        "review_tasks",
                        "review_task_events",
                        "qa_issues",
                    }.issubset(tables)
                )

    def test_review_queue_offset_stays_stable_after_more_than_one_page_changes(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            registry = Path(state_text) / "registry.sqlite3"
            store = WebStore(registry)
            store.upsert_scanning_dataset(
                dataset_id="dataset-queue",
                name="Queue",
                root_id="root-queue",
                relative_path="queue",
                crs="EPSG:32652",
                now=NOW,
            )
            store.create_review_session(
                {
                    "id": "rvw_queue",
                    "dataset_id": "dataset-queue",
                    "source_run_ids": [],
                    "target_layer_ids": ["ov_qa"],
                    "track_ids": [],
                    "frame_range": None,
                    "class_filters": [],
                    "status": "active",
                    "created_by": "operator-local",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "last_task_id": None,
                }
            )
            store.create_review_tasks(
                [
                    {
                        "id": f"rvt_queue_{index:03d}",
                        "session_id": "rvw_queue",
                        "dataset_id": "dataset-queue",
                        "task_type": "MANUAL_SCAN",
                        "status": "todo",
                        "priority": 50,
                        "reason_codes": [],
                        "priority_evidence": {},
                        "resolved_feature_ids": [],
                        "resolution": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for index in range(205)
                ]
            )

            first_page, total = store.list_review_tasks(
                "rvw_queue", offset=0, limit=200
            )
            self.assertEqual(total, 205)
            self.assertEqual(len(first_page), 200)
            self.assertNotIn("queue_priority", first_page[0])
            for task in first_page:
                outcome, _ = store.update_review_task(
                    task["id"],
                    expected_status="todo",
                    now=NOW,
                    fields={
                        "status": "in_progress",
                        "priority": 0,
                        "claimed_by": "operator-local",
                    },
                    event_type="patched",
                    actor="operator-local",
                )
                self.assertEqual(outcome, "updated")
                outcome, _ = store.resolve_review_task(
                    task["id"],
                    resolution="confirmed",
                    resolved_feature_ids=[],
                    now=NOW,
                    actor="operator-local",
                )
                self.assertEqual(outcome, "updated")

            tail, tail_total = store.list_review_tasks(
                "rvw_queue", offset=200, limit=200
            )
            self.assertEqual(tail_total, 205)
            self.assertEqual(
                [task["id"] for task in tail],
                [f"rvt_queue_{index:03d}" for index in range(200, 205)],
            )

            restarted = WebStore(registry)
            persisted_tail, _ = restarted.list_review_tasks(
                "rvw_queue", offset=200, limit=200
            )
            self.assertEqual(
                [task["id"] for task in persisted_tail],
                [f"rvt_queue_{index:03d}" for index in range(200, 205)],
            )

    def test_review_queue_priority_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            registry = Path(state_text) / "registry.sqlite3"
            with closing(sqlite3.connect(registry)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE review_tasks (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        dataset_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority REAL NOT NULL,
                        frame_id TEXT,
                        track_id TEXT,
                        source_run_id TEXT,
                        source_detection_id TEXT,
                        target_layer_id TEXT,
                        class_hint TEXT,
                        reason_codes_json TEXT NOT NULL DEFAULT '[]',
                        location_hint_json TEXT,
                        source_fingerprint TEXT,
                        priority_evidence_json TEXT NOT NULL DEFAULT '{}',
                        claimed_by TEXT,
                        resolved_feature_ids_json TEXT NOT NULL DEFAULT '[]',
                        resolution TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX review_tasks_session_queue
                    ON review_tasks(session_id,status,priority DESC,created_at,id);
                    INSERT INTO review_tasks(
                        id,session_id,dataset_id,task_type,status,priority,track_id,
                        priority_evidence_json,created_at,updated_at
                    ) VALUES(
                        'rvt_legacy','rvw_legacy','dataset-legacy',
                        'UNREVIEWED_INTERVAL','todo',73,'track-legacy',
                        '{"details":{"start_ordinal":4,"end_ordinal":9}}',
                        '2026-08-24T00:00:00+00:00',
                        '2026-08-24T00:00:00+00:00'
                    );
                    """
                )

            WebStore(registry)
            WebStore(registry)
            with closing(sqlite3.connect(registry)) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(review_tasks)")
                }
                self.assertIn("queue_priority", columns)
                self.assertEqual(
                    connection.execute(
                        "SELECT queue_priority FROM review_tasks WHERE id='rvt_legacy'"
                    ).fetchone()[0],
                    73,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT frame_start,frame_end FROM review_tasks "
                        "WHERE id='rvt_legacy'"
                    ).fetchone(),
                    (4, 9),
                )
                index_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='index' AND name='review_tasks_session_queue'"
                    ).fetchone()[0]
                )
                self.assertIn("queue_priority DESC", index_sql)

    def test_qa_store_snapshot_methods_are_idempotent_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            store.upsert_scanning_dataset(
                dataset_id="dataset-qa",
                name="QA",
                root_id="root-qa",
                relative_path="qa",
                crs="EPSG:32652",
                now=NOW,
            )
            store.create_review_session(
                {
                    "id": "rvw_qa",
                    "dataset_id": "dataset-qa",
                    "source_run_ids": [],
                    "target_layer_ids": ["ov_qa"],
                    "track_ids": [],
                    "frame_range": None,
                    "class_filters": [],
                    "status": "active",
                    "created_by": "operator-local",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "last_task_id": None,
                }
            )
            issues = store.replace_review_qa_issues(
                "rvw_qa",
                [
                    {
                        "id": "qai_1",
                        "session_id": "rvw_qa",
                        "layer_id": "ov_qa",
                        "feature_id": "f_1",
                        "rule_id": "REQUIRED_FIELD",
                        "severity": "warning",
                        "message": "Required field is missing.",
                        "related_feature_ids": [],
                        "status": "open",
                        "created_at": NOW,
                        "updated_at": NOW,
                        "override_reason": None,
                    }
                ],
                layer_revisions={"ov_qa": 7},
                ran_at=NOW,
            )
            self.assertEqual([item["id"] for item in issues], ["qai_1"])
            listed, total = store.list_review_qa_issues(
                "rvw_qa", offset=0, limit=10, status="open"
            )
            self.assertEqual(total, 1)
            self.assertEqual(listed[0]["related_feature_ids"], [])
            outcome, updated = store.update_review_qa_issue(
                "qai_1",
                "dismissed",
                "Checked in source imagery.",
                "2026-08-24T12:01:00+00:00",
            )
            self.assertEqual(outcome, "updated")
            assert updated is not None
            self.assertEqual(updated["status"], "dismissed")

            restarted = WebStore(Path(state_text) / "registry.sqlite3")
            restored_session = restarted.get_review_session("rvw_qa")
            assert restored_session is not None
            self.assertEqual(restored_session["qa_layer_revisions"], {"ov_qa": 7})
            self.assertEqual(restored_session["qa_ran_at"], NOW)
            self.assertEqual(
                restarted.review_session_completion_blockers(
                    "rvw_qa", current_layer_revisions={"ov_qa": 7}
                ),
                {
                    "open_tasks": 0,
                    "open_error_qa_issues": 0,
                    "qa_not_run": 0,
                    "stale_qa_target_layers": 0,
                },
            )
            persisted, persisted_total = restarted.list_review_qa_issues(
                "rvw_qa", offset=0, limit=10
            )
            self.assertEqual(persisted_total, 1)
            self.assertEqual(
                persisted[0]["override_reason"], "Checked in source imagery."
            )


if __name__ == "__main__":
    unittest.main()
