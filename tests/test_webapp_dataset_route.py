from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp.store import WebStore


class WebAppDatasetRouteTests(unittest.TestCase):
    def test_route_sampler_is_budgeted_and_keeps_track_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as state_text:
            store = WebStore(Path(state_text) / "registry.sqlite3")
            now = "2026-01-01T00:00:00+00:00"
            store.upsert_scanning_dataset(
                dataset_id="d_route",
                name="route",
                root_id="root",
                relative_path="delivery",
                crs="EPSG:4326",
                now=now,
            )
            frames = [
                {
                    "id": f"a-{index}",
                    "ordinal": index,
                    "track_id": "a",
                    "task": {"large": "x" * 10_000},
                    "longitude": 127.0 + index * 0.001,
                    "latitude": 37.0,
                    "altitude": None,
                    "heading": None,
                }
                for index in range(6)
            ]
            frames.extend(
                {
                    "id": f"b-{index}",
                    "ordinal": 6 + index,
                    "track_id": "b",
                    "task": {"large": "y" * 10_000},
                    "longitude": 128.0 + index * 0.001,
                    "latitude": 38.0,
                    "altitude": None,
                    "heading": None,
                }
                for index in range(2)
            )
            store.finish_dataset_scan(
                "d_route",
                frames=frames,
                tracks=[],
                bbox=None,
                warnings=[],
                now=now,
            )

            sampled = store.sample_route_frames(
                "d_route", track_ids=("a", "b"), max_points=5
            )

            self.assertEqual(len(sampled), 5)
            self.assertEqual(
                [item["id"] for item in sampled],
                ["a-0", "a-2", "a-5", "b-0", "b-1"],
            )
            self.assertTrue(all("task_json" not in item for item in sampled))

    def test_background_scan_groups_tracks_and_builds_wgs84_route(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            dataset_root = root / "delivery"
            dataset_root.mkdir()
            first_image = dataset_root / "first.jpg"
            second_image = dataset_root / "second.jpg"
            first_image.write_bytes(b"image")
            second_image.write_bytes(b"image")
            pose = dataset_root / "poses.csv"
            pose.write_text("", encoding="utf-8")
            tasks = [
                {
                    "image_path": str(first_image.resolve()),
                    "image_name": first_image.name,
                    "image_stem": first_image.stem,
                    "timestamp_iso": "2026-01-01T00:00:00",
                    "route_id": "route",
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                    "record_name": "Job_A_TRACK01",
                    "pose_csv_path": str(pose.resolve()),
                    "pose_row_number": 1,
                    "pose_format": "legacy",
                    "origin": [127.0, 37.0, 10.0],
                    "direction": [1.0, 0.0, 0.0],
                },
                {
                    "image_path": str(second_image.resolve()),
                    "image_name": second_image.name,
                    "image_stem": second_image.stem,
                    "timestamp_iso": "2026-01-01T00:00:01",
                    "route_id": "route",
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                    "record_name": "Job_A_TRACK01",
                    "pose_csv_path": str(pose.resolve()),
                    "pose_row_number": 2,
                    "pose_format": "legacy",
                    "origin": [127.0001, 37.0001, 10.2],
                    "direction": [1.0, 0.0, 0.0],
                },
            ]
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with patch(
                "mms_shp_detection.webapp.datasets.scan_image_tasks",
                return_value=tasks,
            ), TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                response = client.post(
                    "/api/datasets/scan",
                    json={
                        "root_id": root_id,
                        "relative_path": "delivery",
                        "crs": "EPSG:4326",
                    },
                )
                self.assertEqual(response.status_code, 202)
                dataset_id = response.json()["id"]
                detail = response.json()
                deadline = time.time() + 5
                while detail["status"] == "indexing" and time.time() < deadline:
                    time.sleep(0.02)
                    detail = client.get(f"/api/datasets/{dataset_id}").json()
                self.assertEqual(detail["status"], "ready", detail)
                self.assertEqual(detail["frame_count"], 2)
                self.assertEqual(len(detail["tracks"]), 1)
                self.assertGreater(detail["tracks"][0]["distance_m"], 0)

                # Route generation uses a bounded scalar-column cursor rather
                # than the legacy helper that materializes every task_json.
                app.state.config.max_route_points = 1
                with patch.object(
                    app.state.store,
                    "all_frames",
                    side_effect=AssertionError("route loaded every frame task"),
                ):
                    route = client.get(f"/api/datasets/{dataset_id}/route")
                self.assertEqual(route.status_code, 200)
                self.assertEqual(route.json()["type"], "FeatureCollection")
                self.assertEqual(len(route.json()["points"]), 1)
                self.assertAlmostEqual(route.json()["points"][0]["lon"], 127.0)
                self.assertEqual(route.json()["points"][0]["index"], 0)

                frames = client.get(
                    f"/api/datasets/{dataset_id}/frames",
                    params={"offset": 0, "limit": 1},
                ).json()
                self.assertEqual(frames["total"], 2)
                self.assertEqual(frames["next_offset"], 1)
                self.assertEqual(frames["items"][0]["coordinate"]["lat"], 37.0)
                self.assertNotIn(str(root), route.text)
                self.assertNotIn(str(root), client.get(f"/api/datasets/{dataset_id}").text)


if __name__ == "__main__":
    unittest.main()
