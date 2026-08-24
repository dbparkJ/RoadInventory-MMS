from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp import review_tasks as review_tasks_module
from mms_shp_detection.webapp.datasets import detect_dataset_crs
from mms_shp_detection.webapp.store import WebStore

NOW = "2026-08-03T00:00:00+00:00"


def seed_ready_dataset(
    store: WebStore,
    *,
    dataset_id: str,
    root_id: str = "root",
    relative_path: str = "delivery",
) -> None:
    store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name=relative_path,
        root_id=root_id,
        relative_path=relative_path,
        crs="EPSG:4326",
        now=NOW,
    )
    store.finish_dataset_scan(
        dataset_id,
        frames=[
            {
                "id": "frame-1",
                "ordinal": 0,
                "track_id": "track-1",
                "task": {"image_path": "source.jpg"},
                "longitude": 127.0,
                "latitude": 37.0,
                "altitude": 10.0,
                "heading": 90.0,
            }
        ],
        tracks=[{"id": "track-1", "name": "Track 1", "frame_count": 1}],
        bbox=[127.0, 37.0, 127.0, 37.0],
        warnings=[],
        now=NOW,
    )


def seed_run(store: WebStore, *, dataset_id: str, run_id: str) -> None:
    store.create_run(
        {
            "id": run_id,
            "dataset_id": dataset_id,
            "request": {},
            "resolved": {},
            "work_relative": f"runs/{run_id}",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


class WebAppDatasetRegistryTests(unittest.TestCase):
    def test_unregister_keeps_terminal_run_fk_and_can_be_registered_again(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            seed_ready_dataset(store, dataset_id="dataset-a")
            seed_run(store, dataset_id="dataset-a", run_id="run-complete")
            store.update_run(
                "run-complete",
                NOW,
                status="completed",
                finished_at=NOW,
            )

            result = store.unregister_dataset("dataset-a", now=NOW)

            self.assertEqual(result, {"status": "unregistered"})
            self.assertIsNone(store.get_dataset("dataset-a"))
            tombstone = store.get_dataset("dataset-a", include_unregistered=True)
            self.assertIsNotNone(tombstone)
            self.assertEqual(tombstone["status"], "removed")  # type: ignore[index]
            self.assertEqual(store.list_datasets(), [])
            self.assertIsNone(store.get_frame("dataset-a", "frame-1"))
            self.assertEqual(store.get_run("run-complete")["dataset_id"], "dataset-a")  # type: ignore[index]

            existing = store.find_dataset("root", "delivery", "EPSG:4326")
            self.assertEqual(existing["id"], "dataset-a")  # type: ignore[index]
            store.upsert_scanning_dataset(
                dataset_id="dataset-a",
                name="delivery",
                root_id="root",
                relative_path="delivery",
                crs="EPSG:4326",
                now=NOW,
            )
            self.assertEqual(store.get_dataset("dataset-a")["status"], "scanning")  # type: ignore[index]

    def test_unregister_is_blocked_by_queued_or_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            seed_ready_dataset(store, dataset_id="dataset-a")
            seed_run(store, dataset_id="dataset-a", run_id="run-queued")

            result = store.unregister_dataset("dataset-a", now=NOW)

            self.assertEqual(result["status"], "active_run")
            self.assertEqual(result["run_id"], "run-queued")
            self.assertIsNotNone(store.get_dataset("dataset-a"))
            self.assertIsNotNone(store.get_frame("dataset-a", "frame-1"))

    def test_delete_api_only_unregisters_and_preserves_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            delivery = root / "delivery"
            delivery.mkdir()
            source = delivery / "source.jpg"
            source.write_bytes(b"original")
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                seed_run(
                    app.state.store,
                    dataset_id="dataset-a",
                    run_id="run-complete",
                )
                app.state.store.update_run(
                    "run-complete",
                    NOW,
                    status="completed",
                    finished_at=NOW,
                )
                response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["removed"])
                self.assertFalse(response.json()["source_deleted"])
                self.assertEqual(source.read_bytes(), b"original")
                self.assertEqual(client.get("/api/datasets/dataset-a").status_code, 404)
                bootstrap = client.get("/api/bootstrap").json()
                dataset_ids = {item["id"] for item in bootstrap["datasets"]}
                self.assertNotIn("dataset-a", dataset_ids)
                completed = next(
                    item for item in bootstrap["recent_runs"] if item["id"] == "run-complete"
                )
                self.assertEqual(completed["dataset_name"], "delivery")

    def test_delete_api_rejects_dataset_with_queued_run(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                seed_run(app.state.store, dataset_id="dataset-a", run_id="run-queued")

                response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 409)
                self.assertIn("run-queued", response.json()["detail"])
                self.assertEqual(client.get("/api/datasets/dataset-a").status_code, 200)

    def test_delete_api_preserves_frames_and_nonterminal_review_work(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"target_layer_ids": [], "status": "active"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]

                response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 409, response.text)
                self.assertIsNotNone(
                    app.state.store.get_frame("dataset-a", "frame-1")
                )
                self.assertIsNotNone(
                    app.state.store.get_review_session(session_id)
                )
                self.assertEqual(
                    client.get("/api/datasets/dataset-a").status_code,
                    200,
                )

    def test_delete_api_discards_only_a_truly_empty_draft_session(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"status": "draft"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]

                response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(app.state.store.get_review_session(session_id))
                self.assertIsNone(app.state.store.get_frame("dataset-a", "frame-1"))

    def test_delete_api_fails_closed_when_outbox_scan_is_truncated(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                with mock.patch(
                    "mms_shp_detection.webapp.task_resolution_outbox."
                    "reconcile_dataset_task_resolutions",
                    return_value={
                        "pending": 0,
                        "error": 0,
                        "reconciled": 0,
                        "attempted": 0,
                        "truncated": 1,
                    },
                ):
                    response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["detail"]["task_resolution_scan_truncated"],
                    1,
                )
                self.assertIsNotNone(
                    app.state.store.get_frame("dataset-a", "frame-1")
                )

    def test_delete_api_preserves_a_draft_with_a_qa_snapshot(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                created = client.post(
                    "/api/datasets/dataset-a/review-sessions",
                    json={"status": "draft"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                qa_run = client.post(f"/api/review-sessions/{session_id}/qa/run")
                self.assertEqual(qa_run.status_code, 200, qa_run.text)

                response = client.delete("/api/datasets/dataset-a")

                self.assertEqual(response.status_code, 409, response.text)
                self.assertIsNotNone(app.state.store.get_review_session(session_id))
                self.assertIsNotNone(
                    app.state.store.get_frame("dataset-a", "frame-1")
                )

    def test_session_creation_fences_concurrent_dataset_delete(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            entered = threading.Event()
            release = threading.Event()
            original_create = review_tasks_module._create_review_session_locked

            def blocked_create(*args, **kwargs):
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("dataset fence test timed out")
                return original_create(*args, **kwargs)

            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                seed_ready_dataset(
                    app.state.store,
                    dataset_id="dataset-a",
                    root_id=root_id,
                )
                with (
                    mock.patch.object(
                        review_tasks_module,
                        "_create_review_session_locked",
                        side_effect=blocked_create,
                    ),
                    ThreadPoolExecutor(max_workers=2) as executor,
                ):
                    create_future = executor.submit(
                        client.post,
                        "/api/datasets/dataset-a/review-sessions",
                        json={"status": "active"},
                    )
                    self.assertTrue(entered.wait(timeout=5))
                    delete_future = executor.submit(
                        client.delete, "/api/datasets/dataset-a"
                    )
                    release.set()
                    created = create_future.result(timeout=5)
                    deleted = delete_future.result(timeout=5)

                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(deleted.status_code, 409, deleted.text)
                session_id = created.json()["session"]["id"]
                self.assertIsNotNone(app.state.store.get_review_session(session_id))
                self.assertIsNotNone(
                    app.state.store.get_frame("dataset-a", "frame-1")
                )

    def test_malformed_leica_utm_wkt_beats_unrelated_wgs84_prj(self) -> None:
        try:
            import laspy  # noqa: F401
            from pyproj import CRS
        except ImportError as exc:  # pragma: no cover - full web requirements include both
            self.skipTest(f"point/CRS dependencies missing: {exc}")

        malformed_wkt = (
            'COMPD_CS["UTM52N_vendor EGM2008 height",'
            'PROJCS["UTM52N_vendor,GEOGCS["GCS_WGS_1984",'
            'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]],'
            'PROJECTION["Transverse_Mercator"],'
            'PARAMETER["False_Easting",500000],'
            'PARAMETER["False_Northing",0],'
            'PARAMETER["Central_Meridian",129],'
            'PARAMETER["Scale_Factor",0.9996],'
            'PARAMETER["Latitude_Of_Origin",0]]'
        )
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            (root / "trajectory.prj").write_text(
                CRS.from_epsg(4326).to_wkt(), encoding="utf-8"
            )
            (root / "points.las").write_bytes(b"header is mocked")
            header = SimpleNamespace(
                parse_crs=mock.Mock(side_effect=ValueError("invalid vendor WKT")),
                vlrs=[SimpleNamespace(string=malformed_wkt)],
                evlrs=[],
            )
            reader = mock.MagicMock()
            reader.__enter__.return_value = SimpleNamespace(header=header)
            reader.__exit__.return_value = False
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config=SimpleNamespace(
                        pipeline_config_path=root / "missing.yaml",
                        project_root=root,
                    )
                )
            )

            with mock.patch("laspy.open", return_value=reader):
                detected = detect_dataset_crs(app, root)

            self.assertEqual(detected, "EPSG:32652")

    def test_invalid_prj_is_skipped_before_valid_las_crs(self) -> None:
        try:
            import laspy  # noqa: F401
            from pyproj import CRS
        except ImportError as exc:  # pragma: no cover - full web requirements include both
            self.skipTest(f"point/CRS dependencies missing: {exc}")

        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            (root / "broken.prj").write_text("not a valid WKT", encoding="utf-8")
            (root / "points.las").write_bytes(b"header is mocked")
            header = SimpleNamespace(
                parse_crs=mock.Mock(return_value=CRS.from_epsg(32652)),
                vlrs=[],
                evlrs=[],
            )
            reader = mock.MagicMock()
            reader.__enter__.return_value = SimpleNamespace(header=header)
            reader.__exit__.return_value = False
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config=SimpleNamespace(
                        pipeline_config_path=root / "missing.yaml",
                        project_root=root,
                    )
                )
            )

            with mock.patch("laspy.open", return_value=reader):
                detected = detect_dataset_crs(app, root)

            self.assertEqual(detected, "EPSG:32652")


if __name__ == "__main__":
    unittest.main()
