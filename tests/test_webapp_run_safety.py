from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from mms_shp_detection.config import PipelineConfig, config_sha256
from mms_shp_detection.domain.models import JobStatus
from mms_shp_detection.infrastructure.manifest_writer import (
    PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES,
    RunManifestStore,
)
from mms_shp_detection.webapp import WebAppConfig, create_app
from mms_shp_detection.webapp.app import WorkerProcessLock
from mms_shp_detection.webapp.runs import (
    _process_failure_message,
    _progress_from_log,
    _result_summary,
    get_results,
    get_run,
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


def seed_run(
    store: WebStore,
    run_id: str,
    *,
    require_execution_contract: bool = False,
) -> None:
    store.create_run(
        {
            "id": run_id,
            "dataset_id": "dataset-a",
            "request": {},
            "resolved": (
                {"run_execution_contract_version": 1}
                if require_execution_contract
                else {}
            ),
            "work_relative": run_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


def seed_manifest(app, run_id: str, input_root: Path) -> RunManifestStore:
    work = app.state.config.state_dir / "runs" / run_id
    output = work / "output"
    output.mkdir(parents=True)
    (work / "config.yaml").write_text("config_version: 1\n", encoding="utf-8")
    values = {"config_version": 1}
    manifest = RunManifestStore(output / "run_manifest.json")
    manifest.create(
        job_id=run_id,
        config=PipelineConfig(values=values, config_hash=config_sha256(values)),
        input_root=input_root,
    )
    return manifest


def write_bundle(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES:
        path.with_suffix(suffix).write_bytes(f"fixture:{suffix}".encode("ascii"))


class WebAppRunSafetyTests(unittest.TestCase):
    def test_run_visibility_column_migrates_an_existing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            database = Path(state_text) / "registry.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        dataset_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        resolved_json TEXT NOT NULL,
                        work_relative TEXT NOT NULL,
                        pid INTEGER,
                        return_code INTEGER,
                        error TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO runs(
                        id,dataset_id,status,request_json,resolved_json,
                        work_relative,created_at,updated_at
                    ) VALUES('legacy-run','dataset-a','completed','{}','{}',
                             'legacy-run',?,?)
                    """,
                    (NOW, NOW),
                )
                connection.commit()
            finally:
                connection.close()

            store = WebStore(database)
            connection = sqlite3.connect(database)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(runs)").fetchall()
                }
            finally:
                connection.close()
            self.assertIn("dismissed", columns)
            self.assertIn("name", columns)
            self.assertFalse(store.get_run("legacy-run")["dismissed"])  # type: ignore[index]
            self.assertIsNone(store.get_run("legacy-run")["name"])  # type: ignore[index]

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
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
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
                single_payload = single.json()
                self.assertEqual(single_payload["status"], "completed")
                self.assertIn("result_url", single_payload)
                self.assertEqual(single_payload["results"]["file_count"], 2)
                explicit = client.get("/api/runs/run-complete/results")
                self.assertEqual(explicit.status_code, 200)
                self.assertEqual(explicit.json()["file_count"], 2)

    def test_terminal_runs_can_be_dismissed_without_deleting_artifacts(self) -> None:
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
            seed_dataset(app.state.store)
            allowed = {
                "run-completed": "completed",
                "run-failed": "failed",
                "run-interrupted": "interrupted",
            }
            markers: dict[str, Path] = {}
            for run_id, run_status in allowed.items():
                seed_run(app.state.store, run_id)
                marker = state / "runs" / run_id / "output" / "result.txt"
                marker.parent.mkdir(parents=True)
                marker.write_text(f"artifact:{run_id}", encoding="utf-8")
                markers[run_id] = marker
                app.state.store.update_run(
                    run_id,
                    NOW,
                    status=run_status,
                    finished_at=NOW,
                )
            for run_id, run_status in {
                "run-queued": "queued",
                "run-cancelled": "cancelled",
            }.items():
                seed_run(app.state.store, run_id)
                if run_status != "queued":
                    app.state.store.update_run(
                        run_id,
                        NOW,
                        status=run_status,
                        finished_at=NOW,
                    )

            with TestClient(app) as client:
                for run_id in allowed:
                    response = client.delete(f"/api/runs/{run_id}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()["dismissed"])
                    self.assertTrue(response.json()["artifacts_preserved"])
                    self.assertTrue(markers[run_id].is_file())
                    self.assertTrue(app.state.store.get_run(run_id)["dismissed"])  # type: ignore[index]
                    # Dismissal is idempotent and the direct audit URL survives.
                    self.assertEqual(client.delete(f"/api/runs/{run_id}").status_code, 200)
                    self.assertEqual(client.get(f"/api/runs/{run_id}").status_code, 200)
                    self.assertEqual(
                        client.get(f"/api/runs/{run_id}/results").status_code,
                        200,
                    )

                visible = {item["id"] for item in client.get("/api/runs").json()["items"]}
                boot_visible = {
                    item["id"] for item in client.get("/api/bootstrap").json()["recent_runs"]
                }
                self.assertTrue(set(allowed).isdisjoint(visible))
                self.assertTrue(set(allowed).isdisjoint(boot_visible))
                self.assertEqual(client.delete("/api/runs/run-queued").status_code, 409)
                self.assertEqual(client.delete("/api/runs/run-cancelled").status_code, 409)
                self.assertEqual(client.delete("/api/runs/missing").status_code, 404)

    def test_latest_completed_dataset_run_ignores_queue_visibility(
        self,
    ) -> None:
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
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-durable-result")
            app.state.store.update_run(
                "run-durable-result",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )

            with TestClient(app) as client:
                dismissed = client.delete("/api/runs/run-durable-result")
                self.assertEqual(dismissed.status_code, 200, dismissed.text)
                self.assertNotIn(
                    "run-durable-result",
                    {item["id"] for item in client.get("/api/runs").json()["items"]},
                )

                with mock.patch.object(
                    app.state.store,
                    "list_completed_runs_for_dataset",
                    wraps=app.state.store.list_completed_runs_for_dataset,
                ) as list_completed:
                    response = client.get(
                        "/api/datasets/dataset-a/runs/latest-completed"
                    )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["run"]["id"], "run-durable-result")
                self.assertEqual(response.json()["run"]["status"], "completed")
                list_completed.assert_called_once_with("dataset-a", limit=1)

    def test_latest_completed_dataset_run_ignores_recent_queue_page_limit(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            old_created_at = "2026-07-01T00:00:00+00:00"
            app.state.store.create_run(
                {
                    "id": "run-outside-recent-page",
                    "dataset_id": "dataset-a",
                    "request": {},
                    "resolved": {},
                    "work_relative": "run-outside-recent-page",
                    "created_at": old_created_at,
                    "updated_at": old_created_at,
                }
            )
            app.state.store.update_run(
                "run-outside-recent-page",
                old_created_at,
                status="completed",
                return_code=0,
                finished_at=old_created_at,
            )
            for index in range(51):
                created_at = f"2026-08-01T00:{index:02d}:00+00:00"
                app.state.store.create_run(
                    {
                        "id": f"run-recent-{index:02d}",
                        "dataset_id": "dataset-a",
                        "request": {},
                        "resolved": {},
                        "work_relative": f"run-recent-{index:02d}",
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                )

            with TestClient(app) as client:
                recent_ids = {
                    item["id"] for item in client.get("/api/runs").json()["items"]
                }
                self.assertNotIn("run-outside-recent-page", recent_ids)
                response = client.get(
                    "/api/datasets/dataset-a/runs/latest-completed"
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json()["run"]["id"],
                    "run-outside-recent-page",
                )

    def test_latest_completed_dataset_run_uses_completion_time_not_creation_time(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            runs = (
                (
                    "run-created-first-finished-last",
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T03:00:00+00:00",
                ),
                (
                    "run-created-last-finished-first",
                    "2026-08-01T01:00:00+00:00",
                    "2026-08-01T02:00:00+00:00",
                ),
            )
            for run_id, created_at, finished_at in runs:
                app.state.store.create_run(
                    {
                        "id": run_id,
                        "dataset_id": "dataset-a",
                        "request": {},
                        "resolved": {},
                        "work_relative": run_id,
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                )
                app.state.store.update_run(
                    run_id,
                    finished_at,
                    status="completed",
                    return_code=0,
                    finished_at=finished_at,
                )

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/runs/latest-completed"
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["run"]["id"],
                "run-created-first-finished-last",
            )

    def test_latest_completed_dataset_run_has_null_and_missing_dataset_states(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)

            with TestClient(app) as client:
                empty = client.get("/api/datasets/dataset-a/runs/latest-completed")
                self.assertEqual(empty.status_code, 200, empty.text)
                self.assertIsNone(empty.json()["run"])
                self.assertEqual(
                    client.get(
                        "/api/datasets/missing/runs/latest-completed"
                    ).status_code,
                    404,
                )

    def test_completed_dataset_runs_lists_every_durable_job_in_finish_order(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            for run_id, created_at, finished_at in (
                (
                    "run-finished-later",
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T03:00:00+00:00",
                ),
                (
                    "run-finished-earlier",
                    "2026-08-01T01:00:00+00:00",
                    "2026-08-01T02:00:00+00:00",
                ),
            ):
                app.state.store.create_run(
                    {
                        "id": run_id,
                        "dataset_id": "dataset-a",
                        "request": {
                            "dataset_id": "dataset-a",
                            "track_ids": ["track-a"],
                            "frame_range": [1, 7],
                        },
                        "resolved": {},
                        "work_relative": run_id,
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                )
                app.state.store.update_run(
                    run_id,
                    finished_at,
                    status="completed",
                    return_code=0,
                    finished_at=finished_at,
                )
                app.state.store.dismiss_terminal_run(run_id, finished_at)

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/runs/completed?limit=100"
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                [item["id"] for item in response.json()["items"]],
                ["run-finished-later", "run-finished-earlier"],
            )
            self.assertEqual(
                response.json()["items"][0]["request"]["frame_range"],
                [1, 7],
            )
            paged = client.get(
                "/api/datasets/dataset-a/runs/completed?limit=1&offset=0"
            )
            self.assertEqual(paged.status_code, 200, paged.text)
            self.assertEqual(paged.json()["total"], 2)
            self.assertEqual(paged.json()["next_offset"], 1)
            snapshot_at = paged.json()["snapshot_at"]
            self.assertIsInstance(snapshot_at, str)
            self.assertEqual(
                [item["id"] for item in paged.json()["items"]],
                ["run-finished-later"],
            )
            app.state.store.create_run(
                {
                    "id": "run-completed-after-snapshot",
                    "dataset_id": "dataset-a",
                    "request": {
                        "dataset_id": "dataset-a",
                        "track_ids": ["track-a"],
                        "frame_range": None,
                    },
                    "resolved": {},
                    "work_relative": "run-completed-after-snapshot",
                    "created_at": "2999-01-01T00:00:00+00:00",
                    "updated_at": "2999-01-01T00:00:00+00:00",
                }
            )
            app.state.store.update_run(
                "run-completed-after-snapshot",
                "2999-01-01T00:01:00+00:00",
                status="completed",
                return_code=0,
                finished_at="2999-01-01T00:01:00+00:00",
            )
            last_page = client.get(
                "/api/datasets/dataset-a/runs/completed",
                params={"limit": 1, "offset": 1, "snapshot_at": snapshot_at},
            )
            self.assertEqual(last_page.status_code, 200, last_page.text)
            self.assertEqual(last_page.json()["total"], 2)
            self.assertIsNone(last_page.json()["next_offset"])
            self.assertEqual(
                [item["id"] for item in last_page.json()["items"]],
                ["run-finished-earlier"],
            )

    def test_run_archives_are_complete_filtered_and_path_redacted(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as external_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            config = WebAppConfig(
                project_root=Path(__file__).resolve().parents[1],
                state_dir=state,
                allowed_roots=[root],
                enable_run_worker=False,
                # Archive membership must not inherit result-list pagination.
                max_result_files=1,
            )
            app = create_app(config)
            seed_dataset(app.state.store)
            seed_run(
                app.state.store,
                "run-archive",
                require_execution_contract=True,
            )
            manifest = seed_manifest(app, "run-archive", root)
            output = state / "runs" / "run-archive" / "output"
            published = output / "shp" / "detected_signs.shp"
            stale = output / "shp" / "stale_previous.shp"
            write_bundle(published)
            write_bundle(stale)

            detected_one = output / "model_a" / "image_crops" / "track" / "one.jpg"
            detected_two = output / "model_b" / "image_crops" / "track" / "two.png"
            forward = output / "forward_views" / "track" / "frame.jpg"
            point_preview = output / "model_a" / "point_previews" / "track" / "preview.png"
            structured = output / "model_a" / "txt" / "frame.txt"
            log = output / "model_a" / "logs" / "secret.txt"
            unsupported = output / "model_a" / "payload.bin"
            for path, payload in (
                (detected_one, b"jpeg-one"),
                (detected_two, b"png-two"),
                (forward, b"jpeg-forward"),
                (point_preview, b"png-preview"),
                (log, b"private-log"),
                (unsupported, b"unsupported"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            structured.parent.mkdir(parents=True, exist_ok=True)
            structured.write_text(
                json.dumps(
                    {
                        "dataset": str(root / "private" / "frame.jpg"),
                        "model": r"Z:\\private-models\\secret.pt",
                        "file_uri": "file:///etc/passwd",
                    }
                ),
                encoding="utf-8",
            )

            external = Path(external_text) / "external.jpg"
            external.write_bytes(b"must-not-leak")
            linked = output / "model_a" / "image_crops" / "track" / "linked.jpg"
            linked_created = False
            try:
                linked.symlink_to(external)
                linked_created = True
            except OSError:
                pass

            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            app.state.store.update_run(
                "run-archive",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )
            seed_run(app.state.store, "run-active")

            with TestClient(app) as client:
                results = client.get("/api/runs/run-archive/results")
                self.assertEqual(results.status_code, 200, results.text)
                archives = results.json()["archives"]
                self.assertEqual(
                    archives["all"]["url"],
                    "/api/runs/run-archive/archive?scope=all",
                )
                self.assertEqual(
                    archives["detected_images"]["url"],
                    "/api/runs/run-archive/archive?scope=detected-images",
                )

                all_response = client.get(archives["all"]["url"])
                self.assertEqual(all_response.status_code, 200, all_response.text)
                self.assertEqual(all_response.headers["content-type"], "application/zip")
                with zipfile.ZipFile(io.BytesIO(all_response.content)) as archive:
                    all_names = set(archive.namelist())
                    self.assertIn("model_a/image_crops/track/one.jpg", all_names)
                    self.assertIn("model_b/image_crops/track/two.png", all_names)
                    self.assertIn("forward_views/track/frame.jpg", all_names)
                    self.assertIn("model_a/point_previews/track/preview.png", all_names)
                    self.assertIn("model_a/txt/frame.txt", all_names)
                    self.assertIn("shp/detected_signs.shp", all_names)
                    self.assertFalse(any("stale_previous" in name for name in all_names))
                    self.assertFalse(any("/logs/" in f"/{name}" for name in all_names))
                    self.assertNotIn("model_a/payload.bin", all_names)
                    if linked_created:
                        self.assertNotIn(
                            "model_a/image_crops/track/linked.jpg",
                            all_names,
                        )
                    redacted = archive.read("model_a/txt/frame.txt").decode("utf-8")
                    self.assertNotIn(str(root), redacted)
                    self.assertNotIn("private-models", redacted)
                    self.assertNotIn("file:///etc/passwd", redacted)
                    self.assertIn("<server", redacted)

                images_response = client.get(archives["detected_images"]["url"])
                self.assertEqual(images_response.status_code, 200, images_response.text)
                with zipfile.ZipFile(io.BytesIO(images_response.content)) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {
                            "model_a/image_crops/track/one.jpg",
                            "model_b/image_crops/track/two.png",
                        },
                    )

                self.assertEqual(
                    client.get("/api/runs/run-active/archive?scope=all").status_code,
                    409,
                )
                self.assertEqual(
                    client.get("/api/runs/missing/archive?scope=all").status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        "/api/runs/run-archive/archive?scope=unsupported"
                    ).status_code,
                    422,
                )

    def test_shapefile_and_manifest_discovery_bypasses_artifact_page_limit(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            config = WebAppConfig(
                project_root=Path(__file__).resolve().parents[1],
                state_dir=state,
                allowed_roots=[root],
                enable_run_worker=False,
                max_result_files=2,
                max_result_priority_entries=1,
            )
            app = create_app(config)
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-priority")
            output = state / "runs" / "run-priority" / "output"
            shp_dir = output / "shp"
            shp_dir.mkdir(parents=True)
            ordinary = []
            for name in ("first.txt", "second.json"):
                path = output / name
                path.write_text(name, encoding="utf-8")
                ordinary.append(path)
            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                (shp_dir / f"detected_signs{suffix}").write_bytes(
                    suffix.encode("ascii")
                )
            model_shp = output / "model_a" / "shp"
            model_shp.mkdir(parents=True)
            for suffix in (".shp", ".shx", ".dbf"):
                (model_shp / f"pole_bottoms{suffix}").write_bytes(
                    suffix.encode("ascii")
                )
            (output / "run_manifest.json").write_text(
                '{"status":"completed","feature_counts":{"detections":7}}',
                encoding="utf-8",
            )
            (output / "models_manifest.json").write_text(
                '{"models":[{"model_key":"C:"},{"model_key":"model_a"}]}',
                encoding="utf-8",
            )
            app.state.store.update_run(
                "run-priority",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )
            run = app.state.store.get_run("run-priority")

            # Simulate the ordinary artifact walker consuming its entire page
            # before it ever visits output/shp or the manifest.
            with mock.patch(
                "mms_shp_detection.webapp.runs._bounded_result_files",
                return_value=(ordinary, True),
            ):
                summary = _result_summary(app, run)  # type: ignore[arg-type]
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["feature_counts"], {"detections": 7})
            self.assertEqual(len(summary["shapefiles"]), 2)
            shapes = {shape["name"]: shape for shape in summary["shapefiles"]}
            shape = shapes["detected_signs"]
            self.assertEqual(shape["path"], "shp/detected_signs.shp")
            self.assertEqual(len(shape["files"]), 5)
            self.assertIn("/shapefile?path=", shape["download_url"])
            self.assertEqual(
                shapes["pole_bottoms"]["path"], "model_a/shp/pole_bottoms.shp"
            )

            # Result tree scans run in FastAPI's worker thread pool, not the
            # event loop that serves panorama/map requests.
            self.assertFalse(inspect.iscoroutinefunction(get_run))
            self.assertFalse(inspect.iscoroutinefunction(get_results))

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

    def test_cas_transition_keeps_terminal_run_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            seed_dataset(store)
            seed_run(store, "run-terminal")
            store.update_run("run-terminal", NOW, status="completed", finished_at=NOW)

            changed = store.transition_run(
                "run-terminal",
                NOW,
                from_statuses=("running", "cancelling"),
                to_status="failed",
                error="late worker failure",
            )

            self.assertFalse(changed)
            self.assertEqual(store.get_run("run-terminal")["status"], "completed")  # type: ignore[index]

    def test_public_manifest_projection_and_redacted_artifact(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-manifest")
            manifest = seed_manifest(app, "run-manifest", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.set_progress(
                percent=42.5, current_stage="detect_project_and_estimate"
            )
            manifest.update_counts(images=12, detections_2d=7)
            app.state.store.update_run("run-manifest", NOW, status="starting")
            run = app.state.store.get_run("run-manifest")

            public = public_run(app, run, include_log=False)  # type: ignore[arg-type]
            self.assertEqual(public["status"], "preparing")
            self.assertEqual(public["canonical_status"], "running")
            self.assertEqual(public["job_id"], "run-manifest")
            self.assertEqual(public["attempt"], 1)
            self.assertEqual(public["progress"], 42.5)
            self.assertEqual(public["current_stage"], "detect_project_and_estimate")
            self.assertEqual(public["counts"]["images"], 12)

            logs = (
                app.state.config.state_dir / "runs" / "run-manifest" / "output" / "logs"
            )
            logs.mkdir()
            (logs / "run.log").write_text(f"input={root}\n", encoding="utf-8")
            result_txt = logs.parent / "txt" / "frame.txt"
            result_txt.parent.mkdir()
            result_txt.write_text(
                '{"image_path":"Z:\\\\private-delivery\\\\frame.jpg",'
                '"message":"failed at Z:\\\\external-models\\\\secret.pt",'
                '"posix_message":"failed at /mnt/private/secret.pt",'
                '"root_posix_message":"failed at /secret.pt",'
                '"forward_unc":"failed at //server/share/secret.pt",'
                '"file_uri":"failed at file:///etc/passwd",'
                '"url":"https://example.com/api/v1"}',
                encoding="utf-8",
            )
            plain_txt = logs.parent / "txt" / "notes.txt"
            plain_txt.write_text(
                "failed at /private/model.pt; docs=https://example.com/api/v1",
                encoding="utf-8",
            )
            with TestClient(app) as client:
                artifact = client.get(
                    "/api/runs/run-manifest/artifacts",
                    params={"path": "run_manifest.json"},
                )
                self.assertEqual(artifact.status_code, 200, artifact.text)
                self.assertNotIn(str(root), artifact.text)
                self.assertIn("<server>", artifact.text)
                denied_log = client.get(
                    "/api/runs/run-manifest/artifacts",
                    params={"path": "logs/run.log"},
                )
                self.assertEqual(denied_log.status_code, 404)
                structured = client.get(
                    "/api/runs/run-manifest/artifacts",
                    params={"path": "txt/frame.txt"},
                )
                self.assertEqual(structured.status_code, 200, structured.text)
                self.assertNotIn("private-delivery", structured.text)
                self.assertNotIn("external-models", structured.text)
                self.assertNotIn("/mnt/private", structured.text)
                self.assertNotIn("/secret.pt", structured.text)
                self.assertNotIn("//server/share", structured.text)
                self.assertNotIn("file:///etc/passwd", structured.text)
                self.assertIn("file:<server-path>", structured.json()["file_uri"])
                self.assertIn("<server", structured.text)
                self.assertEqual(structured.json()["url"], "https://example.com/api/v1")
                plain = client.get(
                    "/api/runs/run-manifest/artifacts",
                    params={"path": "txt/notes.txt"},
                )
                self.assertEqual(plain.status_code, 200, plain.text)
                self.assertNotIn("/private/model.pt", plain.text)
                self.assertIn("https://example.com/api/v1", plain.text)

    def test_zero_exit_requires_succeeded_manifest_and_propagates_job_id(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-invalid-success")
            manifest = seed_manifest(app, "run-invalid-success", root)
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            class EmptyStdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            class SuccessfulProcess:
                pid = 4321
                returncode = None
                stdout = EmptyStdout()

                async def wait(self) -> int:
                    self.returncode = 0
                    return 0

            async def exercise() -> dict[str, object]:
                with mock.patch(
                    "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=SuccessfulProcess()),
                ) as spawn:
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]
                    return spawn.await_args.kwargs

            spawn_kwargs = asyncio.run(exercise())
            self.assertEqual(
                spawn_kwargs["env"]["MMS_PIPELINE_JOB_ID"],
                "run-invalid-success",  # type: ignore[index]
            )
            stored = app.state.store.get_run("run-invalid-success")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            self.assertIn("succeeded run manifest", stored["error"])  # type: ignore[index]
            updated_manifest = manifest.read()
            self.assertEqual(updated_manifest["status"], "failed")
            self.assertEqual(
                updated_manifest["errors"][-1]["code"], "RUN_MANIFEST_NOT_SUCCEEDED"
            )

    def test_zero_exit_with_succeeded_manifest_completes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-valid-success")
            manifest = seed_manifest(app, "run-valid-success", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-valid-success"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            class EmptyStdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            class SuccessfulProcess:
                pid = 4322
                returncode = None
                stdout = EmptyStdout()

                async def wait(self) -> int:
                    manifest.transition(JobStatus.VALIDATING)
                    manifest.transition(JobStatus.RUNNING)
                    manifest.transition(JobStatus.SUCCEEDED)
                    self.returncode = 0
                    return 0

            async def exercise() -> None:
                with mock.patch(
                    "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=SuccessfulProcess()),
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]

            asyncio.run(exercise())
            stored = app.state.store.get_run("run-valid-success")
            self.assertEqual(stored["status"], "completed")  # type: ignore[index]
            self.assertEqual(stored["return_code"], 0)  # type: ignore[index]
            stale_path = shp_path.with_name("stale_previous_run.shp")
            write_bundle(stale_path)
            stale_models_manifest = shp_path.parents[1] / "models_manifest.json"
            stale_models_manifest.write_text(
                '{"models":[{"model_key":"stale"}]}',
                encoding="utf-8",
            )
            summary = _result_summary(app, stored)  # type: ignore[arg-type]
            self.assertEqual(
                [item["path"] for item in summary["shapefiles"]],  # type: ignore[index]
                ["shp/detected_signs.shp"],
            )
            self.assertNotIn(
                "models_manifest.json",
                {item["path"] for item in summary["files"]},  # type: ignore[index]
            )
            with TestClient(app) as client:
                stale_download = client.get(
                    "/api/runs/run-valid-success/shapefile",
                    params={"path": "shp/stale_previous_run.shp"},
                )
                self.assertEqual(stale_download.status_code, 404)
                stale_artifact = client.get(
                    "/api/runs/run-valid-success/artifacts",
                    params={"path": "shp/stale_previous_run.shp"},
                )
                self.assertEqual(stale_artifact.status_code, 404)
                stale_models_artifact = client.get(
                    "/api/runs/run-valid-success/artifacts",
                    params={"path": "models_manifest.json"},
                )
                self.assertEqual(stale_models_artifact.status_code, 404)

    def test_zero_exit_rejects_succeeded_manifest_without_outputs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-missing-outputs")
            manifest = seed_manifest(app, "run-missing-outputs", root)
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            class EmptyStdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            class SuccessfulProcess:
                pid = 4323
                returncode = None
                stdout = EmptyStdout()

                async def wait(self) -> int:
                    manifest.transition(JobStatus.VALIDATING)
                    manifest.transition(JobStatus.RUNNING)
                    manifest.transition(JobStatus.SUCCEEDED)
                    self.returncode = 0
                    return 0

            async def exercise() -> None:
                with mock.patch(
                    "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=SuccessfulProcess()),
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]

            asyncio.run(exercise())
            stored = app.state.store.get_run("run-missing-outputs")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            public = public_run(app, stored)  # type: ignore[arg-type]
            self.assertNotIn("canonical_status", public)
            self.assertEqual(public["error_info"]["code"], "RUN_OUTPUT_INVALID")

    def test_completed_contract_run_hides_results_after_output_damage(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(
                app.state.store,
                "run-damaged-output",
                require_execution_contract=True,
            )
            manifest = seed_manifest(app, "run-damaged-output", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-damaged-output"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            app.state.store.update_run(
                "run-damaged-output",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )
            shp_path.with_suffix(".dbf").unlink()

            stored = app.state.store.get_run("run-damaged-output")
            public = public_run(
                app,
                stored,  # type: ignore[arg-type]
                include_results=True,
            )

            self.assertEqual(public["status"], "failed")
            self.assertNotIn("result_url", public)
            self.assertEqual(public["error_info"]["code"], "RUN_OUTPUT_INVALID")
            self.assertEqual(public["results"]["shapefiles"], [])
            self.assertEqual(public["results"]["status"], "failed")
            with TestClient(app) as client:
                download = client.get(
                    "/api/runs/run-damaged-output/shapefile",
                    params={"path": "shp/detected_signs.shp"},
                )
                self.assertEqual(download.status_code, 404)

    def test_invalid_output_contract_reason_redacts_manifest_server_paths(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(
                app.state.store,
                "run-private-contract-path",
                require_execution_contract=True,
            )
            manifest = seed_manifest(app, "run-private-contract-path", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            document = manifest.read()
            document["outputs"] = {
                "root": ".",
                "shapefiles": [r"Z:\private-delivery\secret.shp"],
                "models_manifest": None,
            }
            manifest.path.write_text(
                json.dumps(document),
                encoding="utf-8",
            )
            app.state.store.update_run(
                "run-private-contract-path",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )

            stored = app.state.store.get_run("run-private-contract-path")
            public = public_run(app, stored)  # type: ignore[arg-type]
            rendered = json.dumps(public["error_info"])

            self.assertEqual(public["status"], "failed")
            self.assertNotIn("private-delivery", rendered)
            self.assertIn("<server-path>", rendered)

    def test_completed_contract_run_requires_its_manifest(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(
                app.state.store,
                "run-missing-manifest",
                require_execution_contract=True,
            )
            work = app.state.config.state_dir / "runs" / "run-missing-manifest"
            (work / "output").mkdir(parents=True)
            app.state.store.update_run(
                "run-missing-manifest",
                NOW,
                status="completed",
                return_code=0,
                finished_at=NOW,
            )

            stored = app.state.store.get_run("run-missing-manifest")
            public = public_run(app, stored)  # type: ignore[arg-type]

            self.assertEqual(public["status"], "failed")
            self.assertNotIn("result_url", public)
            self.assertEqual(public["error_info"]["code"], "RUN_MANIFEST_INVALID")

    def test_queued_cancel_synchronizes_manifest(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-cancel-manifest")
            manifest = seed_manifest(app, "run-cancel-manifest", root)

            cancelled = asyncio.run(app.state.run_manager.cancel("run-cancel-manifest"))

            self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(cancelled["cancel_requested"])
            self.assertEqual(manifest.read()["status"], "cancelled")

    def test_launcher_error_synchronizes_failed_manifest(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-launch-error")
            manifest = seed_manifest(app, "run-launch-error", root)
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            async def exercise() -> None:
                with mock.patch(
                    "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(side_effect=OSError("launcher unavailable")),
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]

            with TestClient(app):
                asyncio.run(exercise())
            stored = app.state.store.get_run("run-launch-error")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            failed_manifest = manifest.read()
            self.assertEqual(failed_manifest["status"], "failed")
            self.assertEqual(failed_manifest["errors"][-1]["code"], "RUN_LAUNCH_FAILED")

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
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
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

    def test_restart_recovery_closes_a_running_manifest(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-restarted")
            manifest = seed_manifest(app, "run-restarted", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            app.state.store.update_run("run-restarted", NOW, status="running")

            recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 1)
            self.assertEqual(
                app.state.store.get_run("run-restarted")["status"],  # type: ignore[index]
                "failed",
            )
            recovered_manifest = manifest.read()
            self.assertEqual(recovered_manifest["status"], "failed")
            self.assertEqual(
                recovered_manifest["errors"][-1]["code"],
                "WORKER_RESTARTED",
            )

    def test_restart_recovery_trusts_a_succeeded_manifest(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-finished-before-restart")
            manifest = seed_manifest(app, "run-finished-before-restart", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-finished-before-restart"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            app.state.store.update_run(
                "run-finished-before-restart",
                NOW,
                status="running",
            )

            recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 1)
            self.assertEqual(
                app.state.store.get_run("run-finished-before-restart")["status"],  # type: ignore[index]
                "completed",
            )

    def test_restart_recovery_rejects_succeeded_manifest_without_outputs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-invalid-restart")
            manifest = seed_manifest(app, "run-invalid-restart", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            app.state.store.update_run(
                "run-invalid-restart",
                NOW,
                status="running",
            )

            recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 1)
            stored = app.state.store.get_run("run-invalid-restart")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            self.assertIn("incomplete", stored["error"])  # type: ignore[index]

    def test_restart_recovery_rereads_manifest_when_terminal_sync_loses_race(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(
                app.state.store,
                "run-recovery-race",
                require_execution_contract=True,
            )
            manifest = seed_manifest(app, "run-recovery-race", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-recovery-race"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            app.state.store.update_run(
                "run-recovery-race",
                NOW,
                status="running",
            )

            def child_commits_success(*_args, **_kwargs) -> bool:
                manifest.transition(JobStatus.SUCCEEDED)
                return False

            with mock.patch(
                "mms_shp_detection.webapp.runs._sync_manifest_terminal",
                side_effect=child_commits_success,
            ):
                recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 1)
            self.assertEqual(
                app.state.store.get_run("run-recovery-race")["status"],  # type: ignore[index]
                "completed",
            )
            self.assertEqual(manifest.read()["status"], "succeeded")

    def test_restart_recovery_is_idempotent_for_an_interrupted_run(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-already-interrupted")
            manifest = seed_manifest(app, "run-already-interrupted", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            app.state.store.update_run(
                "run-already-interrupted",
                NOW,
                status="interrupted",
            )

            app.state.run_manager.recover_after_restart(NOW)
            first_manifest = manifest.read()
            app.state.run_manager.recover_after_restart(NOW)

            stored = app.state.store.get_run("run-already-interrupted")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            self.assertEqual(manifest.read(), first_manifest)
            self.assertEqual(first_manifest["errors"][-1]["code"], "WORKER_RESTARTED")

    def test_restart_reconciles_contract_terminal_rows_after_cross_store_crash(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            for run_id, database_status in (
                ("run-db-failed-first", "failed"),
                ("run-db-cancelled-first", "cancelled"),
            ):
                seed_run(
                    app.state.store,
                    run_id,
                    require_execution_contract=True,
                )
                manifest = seed_manifest(app, run_id, root)
                manifest.transition(JobStatus.VALIDATING)
                manifest.transition(JobStatus.RUNNING)
                app.state.store.update_run(
                    run_id,
                    NOW,
                    status=database_status,
                    finished_at=NOW,
                )

            app.state.run_manager.recover_after_restart(NOW)

            failed_manifest = RunManifestStore(
                app.state.config.state_dir
                / "runs"
                / "run-db-failed-first"
                / "output"
                / "run_manifest.json"
            ).read()
            cancelled_manifest = RunManifestStore(
                app.state.config.state_dir
                / "runs"
                / "run-db-cancelled-first"
                / "output"
                / "run_manifest.json"
            ).read()
            self.assertEqual(failed_manifest["status"], "failed")
            self.assertEqual(failed_manifest["errors"][-1]["code"], "WORKER_RESTARTED")
            self.assertEqual(cancelled_manifest["status"], "cancelled")

    def test_restart_finishes_cancel_requested_active_rows(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            manifests: dict[str, RunManifestStore] = {}
            for run_id, database_status in (
                ("run-cancelled-while-preparing", "preparing"),
                ("run-cancelled-while-running", "running"),
            ):
                seed_run(
                    app.state.store,
                    run_id,
                    require_execution_contract=True,
                )
                manifest = seed_manifest(app, run_id, root)
                if database_status == "running":
                    manifest.transition(JobStatus.VALIDATING)
                    manifest.transition(JobStatus.RUNNING)
                manifests[run_id] = manifest
                app.state.store.update_run(
                    run_id,
                    NOW,
                    status=database_status,
                    cancel_requested=1,
                )
            seed_run(app.state.store, "run-next-after-recovery")

            recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 2)
            for run_id, manifest in manifests.items():
                stored = app.state.store.get_run(run_id)
                self.assertEqual(stored["status"], "cancelled")  # type: ignore[index]
                self.assertEqual(manifest.read()["status"], "cancelled")
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertEqual(claimed["id"], "run-next-after-recovery")  # type: ignore[index]

    def test_restart_does_not_requeue_a_possibly_spawned_starting_run(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-starting-at-restart")
            manifest = seed_manifest(app, "run-starting-at-restart", root)
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            app.state.store.update_run(
                "run-starting-at-restart",
                NOW,
                status="starting",
            )

            recovered = app.state.run_manager.recover_after_restart(NOW)

            self.assertEqual(recovered, 1)
            stored = app.state.store.get_run("run-starting-at-restart")
            self.assertEqual(stored["status"], "failed")  # type: ignore[index]
            self.assertEqual(manifest.read()["status"], "failed")

    def test_durable_success_wins_a_late_cancellation_request(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-late-cancel")
            manifest = seed_manifest(app, "run-late-cancel", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-late-cancel"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            class EmptyStdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            class SuccessfulProcess:
                pid = 4330
                returncode = None
                stdout = EmptyStdout()

                async def wait(self) -> int:
                    manifest.transition(JobStatus.VALIDATING)
                    manifest.transition(JobStatus.RUNNING)
                    app.state.store.update_run(
                        "run-late-cancel",
                        NOW,
                        status="cancelling",
                        cancel_requested=1,
                    )
                    manifest.transition(JobStatus.SUCCEEDED)
                    self.returncode = 0
                    return 0

            async def exercise() -> None:
                with mock.patch(
                    "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=SuccessfulProcess()),
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]

            asyncio.run(exercise())

            stored = app.state.store.get_run("run-late-cancel")
            self.assertEqual(stored["status"], "completed")  # type: ignore[index]
            self.assertEqual(manifest.read()["status"], "succeeded")

    def test_shutdown_preserves_an_already_committed_success(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-success-at-stop")
            manifest = seed_manifest(app, "run-success-at-stop", root)
            shp_path = (
                app.state.config.state_dir
                / "runs"
                / "run-success-at-stop"
                / "output"
                / "shp"
                / "detected_signs.shp"
            )
            write_bundle(shp_path)
            manifest.set_outputs(
                {
                    "root": ".",
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                }
            )
            manifest.transition(JobStatus.VALIDATING)
            manifest.transition(JobStatus.RUNNING)
            manifest.transition(JobStatus.SUCCEEDED)
            app.state.store.update_run(
                "run-success-at-stop",
                NOW,
                status="running",
            )
            process = SimpleNamespace(returncode=None)
            app.state.run_manager._active_run_id = "run-success-at-stop"
            app.state.run_manager._active_process = process

            with mock.patch.object(
                app.state.run_manager,
                "_terminate",
                new=mock.AsyncMock(),
            ) as terminate:
                asyncio.run(app.state.run_manager.stop())
                terminate.assert_awaited_once_with(process)

            stored = app.state.store.get_run("run-success-at-stop")
            self.assertEqual(stored["status"], "completed")  # type: ignore[index]
            self.assertEqual(manifest.read()["status"], "succeeded")

    def test_cancel_retries_when_queued_run_becomes_running(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-cancel-cas")
            original_transition = app.state.store.transition_run
            raced = False

            def race_once(run_id: str, now: str, **kwargs) -> bool:
                nonlocal raced
                if kwargs.get("to_status") == "cancelled" and not raced:
                    raced = True
                    app.state.store.update_run(
                        run_id,
                        now,
                        status="running",
                    )
                    return False
                return original_transition(run_id, now, **kwargs)

            with mock.patch.object(
                app.state.store,
                "transition_run",
                side_effect=race_once,
            ):
                cancelled = asyncio.run(app.state.run_manager.cancel("run-cancel-cas"))

            self.assertTrue(raced)
            self.assertEqual(cancelled["status"], "cancelling")
            self.assertEqual(cancelled["cancel_requested"], 1)

    def test_process_failure_wins_a_concurrent_cancelling_transition(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-failure-cancel-race")
            manifest = seed_manifest(app, "run-failure-cancel-race", root)
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)

            class EmptyStdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            class FailedProcess:
                pid = 4881
                returncode = None
                stdout = EmptyStdout()

                async def wait(self) -> int:
                    manifest.transition(JobStatus.VALIDATING)
                    manifest.transition(JobStatus.RUNNING)
                    self.returncode = 2
                    return 2

            original_transition = app.state.store.transition_run
            raced = False

            def request_cancel_before_failure(run_id: str, now: str, **kwargs) -> bool:
                nonlocal raced
                if kwargs.get("to_status") == "failed" and not raced:
                    raced = True
                    app.state.store.update_run(
                        run_id,
                        now,
                        status="cancelling",
                        cancel_requested=1,
                    )
                return original_transition(run_id, now, **kwargs)

            async def exercise() -> None:
                with (
                    mock.patch(
                        "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=FailedProcess()),
                    ),
                    mock.patch.object(
                        app.state.store,
                        "transition_run",
                        side_effect=request_cancel_before_failure,
                    ),
                ):
                    await app.state.run_manager._execute(claimed)  # type: ignore[arg-type]

            asyncio.run(exercise())

            self.assertTrue(raced)
            self.assertEqual(
                app.state.store.get_run("run-failure-cancel-race")["status"],  # type: ignore[index]
                "failed",
            )
            self.assertEqual(manifest.read()["status"], "failed")

    def test_shutdown_waits_for_spawn_and_terminates_the_published_child(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            seed_dataset(app.state.store)
            seed_run(app.state.store, "run-shutdown-spawn")
            manifest = seed_manifest(app, "run-shutdown-spawn", root)
            claimed = app.state.store.claim_next_queued_run(NOW)
            self.assertIsNotNone(claimed)
            process = SimpleNamespace(pid=4882, returncode=None, stdout=None)

            async def exercise() -> mock.AsyncMock:
                spawn_started = asyncio.Event()
                release_spawn = asyncio.Event()

                async def delayed_spawn(*_args, **_kwargs):
                    spawn_started.set()
                    await release_spawn.wait()
                    return process

                async def terminate_child(candidate) -> None:
                    self.assertIs(candidate, process)
                    candidate.returncode = -15

                with (
                    mock.patch(
                        "mms_shp_detection.webapp.runs.asyncio.create_subprocess_exec",
                        side_effect=delayed_spawn,
                    ),
                    mock.patch.object(
                        app.state.run_manager,
                        "_terminate",
                        new=mock.AsyncMock(side_effect=terminate_child),
                    ) as terminate,
                ):
                    execute_task = asyncio.create_task(
                        app.state.run_manager._execute(claimed)  # type: ignore[arg-type]
                    )
                    await spawn_started.wait()
                    stop_task = asyncio.create_task(app.state.run_manager.stop())
                    await asyncio.sleep(0)
                    release_spawn.set()
                    await asyncio.gather(execute_task, stop_task)
                    return terminate

            terminate = asyncio.run(exercise())

            self.assertEqual(terminate.await_count, 1)
            self.assertEqual(
                app.state.store.get_run("run-shutdown-spawn")["status"],  # type: ignore[index]
                "interrupted",
            )
            self.assertEqual(manifest.read()["status"], "failed")

    def test_cancellation_during_spawn_cannot_publish_running_status(self) -> None:
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
