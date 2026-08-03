from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import WebAppConfig, create_app
from mms_shp_detection.webapp.app import WorkerProcessLock
from mms_shp_detection.webapp.runs import (
    _process_failure_message,
    _progress_from_log,
    _result_summary,
    public_run,
)
from mms_shp_detection.webapp.store import WebStore


NOW = "2026-07-31T00:00:00+00:00"


def seed_dataset(store: WebStore, dataset_id: str = "dataset-a") -> None:
    store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name="Dataset A",
        root_id="root-a",
        relative_path="",
        crs="EPSG:4326",
        now=NOW,
    )
    store.finish_dataset_scan(
        dataset_id,
        frames=[],
        tracks=[],
        bbox=None,
        warnings=[],
        now=NOW,
    )


def seed_run(store: WebStore, run_id: str) -> None:
    store.create_run(
        {
            "id": run_id,
            "dataset_id": "dataset-a",
            "request": {},
            "resolved": {},
            "work_relative": run_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


class WebAppRunSafetyTests(unittest.TestCase):
    def test_tqdm_output_drives_web_progress(self) -> None:
        log = "MMS multi-model:  12%|# | 24/200 [00:10<01:03]\r"
        self.assertEqual(_progress_from_log("running", log), 12.0)

    def test_windows_console_abort_is_reported_with_actionable_reason(self) -> None:
        app = SimpleNamespace()
        run = {"id": "run-aborted"}
        with mock.patch(
            "mms_shp_detection.webapp.runs._runtime_log_text",
            return_value="forrtl: error (200): program aborting due to window-CLOSE event",
        ):
            message = _process_failure_message(app, run, 2)
        self.assertIn("Windows host console closed", message)
        self.assertIn("exit code 2", message)

    def test_active_and_collection_payloads_do_not_scan_result_tree(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-active")
            (state / "runs" / "run-active" / "output").mkdir(parents=True)
            run = app.state.store.get_run("run-active")

            with mock.patch(
                "mms_shp_detection.webapp.runs._result_summary",
                side_effect=AssertionError("result tree was scanned"),
            ) as summary:
                public_run(app, run, include_log=False)  # type: ignore[arg-type]
                with TestClient(app) as client:
                    self.assertEqual(client.get("/api/bootstrap").status_code, 200)
                    self.assertEqual(client.get("/api/runs").status_code, 200)
                    self.assertEqual(
                        client.get("/api/runs/run-active").status_code,
                        200,
                    )
                    app.state.store.update_run(
                        "run-active",
                        NOW,
                        status="completed",
                        finished_at=NOW,
                    )
                    self.assertEqual(client.get("/api/bootstrap").status_code, 200)
                    self.assertEqual(client.get("/api/runs").status_code, 200)
                summary.assert_not_called()

    def test_terminal_single_get_and_explicit_results_include_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            config = WebAppConfig(
                project_root=Path(__file__).resolve().parents[1],
                state_dir=state,
                allowed_roots=[root],
                enable_run_worker=False,
                max_result_files=2,
            )
            app = create_app(config)
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-complete")
            output = state / "runs" / "run-complete" / "output"
            output.mkdir(parents=True)
            for name in ("one.txt", "two.json", "three.csv"):
                (output / name).write_text(name, encoding="utf-8")
            app.state.store.update_run(
                "run-complete",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )

            run = app.state.store.get_run("run-complete")
            with mock.patch.object(
                Path,
                "rglob",
                side_effect=AssertionError("unbounded rglob used"),
            ):
                summary = _result_summary(app, run)  # type: ignore[arg-type]
            self.assertEqual(summary["file_count"], 2)  # type: ignore[index]
            self.assertTrue(summary["truncated"])  # type: ignore[index]

            with TestClient(app) as client:
                single = client.get("/api/runs/run-complete")
                self.assertEqual(single.status_code, 200)
                self.assertEqual(single.json()["results"]["file_count"], 2)
                explicit = client.get("/api/runs/run-complete/results")
                self.assertEqual(explicit.status_code, 200)
                self.assertEqual(explicit.json()["file_count"], 2)

    def test_claim_refuses_second_run_while_any_run_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            seed_dataset(store)
            seed_run(store, "run-one")
            seed_run(store, "run-two")

            first = store.claim_next_queued_run(NOW)
            self.assertIsNotNone(first)
            self.assertIsNone(store.claim_next_queued_run(NOW))

            store.update_run(first["id"], NOW, status="completed")  # type: ignore[index]
            second = store.claim_next_queued_run(NOW)
            self.assertIsNotNone(second)
            self.assertNotEqual(first["id"], second["id"])  # type: ignore[index]

    def test_worker_process_lock_rejects_a_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            lock_path = Path(state_text) / "worker.lock"
            first = WorkerProcessLock(lock_path)
            second = WorkerProcessLock(lock_path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "exactly one ASGI worker"):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_recovery_runs_only_after_the_worker_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            with mock.patch.object(
                WebStore,
                "recover_after_restart",
                return_value=0,
            ) as recover:
                app = create_app(
                    allowed_roots=[Path(root_text)],
                    state_dir=Path(state_text),
                    start_runner=True,
                )
                recover.assert_not_called()
                with TestClient(app):
                    recover.assert_called_once()

    def test_cancellation_during_spawn_cannot_publish_running_status(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=state,
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-race")
            work = state / "runs" / "run-race"
            work.mkdir(parents=True)
            (work / "config.yaml").write_text("config_version: 1\n", encoding="utf-8")
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            spawned = SimpleNamespace(pid=4321, returncode=None)

            async def spawn_then_cancel(*_args, **_kwargs):
                app.state.store.update_run(
                    "run-race",
                    NOW,
                    status="cancelled",
                    cancel_requested=1,
                    finished_at=NOW,
                )
                return spawned

            async def exercise() -> None:
                with (
                    mock.patch(
                        "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                        side_effect=spawn_then_cancel,
                    ) as spawn,
                    mock.patch.object(
                        app.state.run_manager,
                        "_terminate",
                        new=mock.AsyncMock(),
                    ) as terminate,
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]
                    terminate.assert_awaited_once_with(spawned)
                    spawn_kwargs = spawn.await_args.kwargs
                    if os.name == "nt":
                        self.assertEqual(
                            spawn_kwargs["creationflags"],
                            subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        self.assertTrue(spawn_kwargs["start_new_session"])

            import asyncio

            asyncio.run(exercise())
            stored = app.state.store.get_run("run-race")
            self.assertEqual(stored["status"], "cancelled")  # type: ignore[index]
            self.assertIsNone(stored["pid"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
