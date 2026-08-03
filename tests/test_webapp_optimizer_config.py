from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from mms_shp_detection.config import _Yaml12SafeLoader
from mms_shp_detection.webapp import WebAppConfig, create_app
from mms_shp_detection.webapp.optimizer import resolve_run_parameters
from mms_shp_detection.webapp.runs import RunRequest, _build_job_config


class WebAppOptimizerConfigTests(unittest.TestCase):
    def test_automatic_profile_preserves_algorithm_thresholds(self) -> None:
        ui, core, profile = resolve_run_parameters(
            mode="automatic", parameters=None, preset="balanced"
        )
        self.assertEqual(profile, "balanced")
        self.assertEqual(ui["confidence"], 0.8)
        self.assertEqual(ui["min_points"], 100)
        self.assertNotIn("conf", core)
        self.assertNotIn("min_point_count", core)
        self.assertNotIn("max_range_m", core)
        self.assertIn("num_workers", core)

    def test_optimize_rejects_out_of_range_manual_values(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                response = client.post(
                    "/api/optimize",
                    json={
                        "mode": "manual",
                        "parameters": {
                            "voxel_size": 0.1,
                            "confidence": 4.0,
                            "cluster_distance": 0.35,
                            "min_points": 100,
                            "search_radius": 15,
                            "ground_tolerance": 0.35,
                        },
                    },
                )
                self.assertEqual(response.status_code, 422)

    def test_job_config_preserves_yaml12_off_and_uses_exact_records(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            base_config = state / "base.yaml"
            base_config.write_text(
                """
config_version: 1
paths:
  data_root: .
  output_dir: output
  pointcloud_cache_path: cache.json
runtime:
  pole_classification_mode: off
  start_index: 0
  limit_images: 0
input:
  include_record_names: [Old_Record]
  include_job_names: [Old_Job]
  include_track_names: [Old_Track]
  frame_id_from: old_001
  frame_id_to: old_999
models:
  imgsz: 1280
model_filters:
  traffic.pt:
    object_type: traffic_sign
    conf: 0.8
    min_point_count: 100
""".strip(),
                encoding="utf-8",
            )
            config = WebAppConfig(
                project_root=Path(__file__).resolve().parents[1],
                state_dir=state / "web",
                allowed_roots=[root],
                pipeline_config_path=base_config,
                enable_run_worker=False,
            )
            app = create_app(config)
            selected_id = "track-a"
            dataset = {
                "id": "dataset-a",
                "root_id": app.state.storage_roots[0].id,
                "relative_path": "",
                "tracks": [
                    {
                        "id": selected_id,
                        "name": "TRACK01",
                        "job_name": "Job_A",
                        "record_name": "Job_A_TRACK01",
                    },
                    {
                        "id": "track-b",
                        "name": "TRACK01",
                        "job_name": "Job_B",
                        "record_name": "Job_B_TRACK01",
                    },
                ],
            }
            frames = [
                {
                    "id": "a1",
                    "ordinal": 0,
                    "track_id": selected_id,
                    "task": {"image_stem": "frame1"},
                },
                {
                    "id": "b1",
                    "ordinal": 1,
                    "track_id": "track-b",
                    "task": {"image_stem": "frame1"},
                },
                {
                    "id": "a2",
                    "ordinal": 2,
                    "track_id": selected_id,
                    "task": {"image_stem": "frame2"},
                },
            ]
            payload = RunRequest(
                dataset_id="dataset-a",
                track_ids=[selected_id],
                frame_range=None,
                mode="automatic",
                auto={"preset": "balanced"},
            )
            path, resolved = _build_job_config(
                app,
                run_id="run_test_config",
                dataset=dataset,
                frames=frames,
                payload=payload,
                core_parameters={"imgsz": 960},
            )
            document = yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=_Yaml12SafeLoader,
            )
            self.assertEqual(document["runtime"]["pole_classification_mode"], "off")
            self.assertEqual(document["model_filters"]["traffic.pt"]["conf"], 0.8)
            self.assertEqual(
                document["input"]["include_record_names"], ["Job_A_TRACK01"]
            )
            self.assertIsNone(document["input"]["include_job_names"])
            self.assertIsNone(document["input"]["include_track_names"])
            self.assertIsNone(document["input"]["frame_id_from"])
            self.assertIsNone(document["input"]["frame_id_to"])
            self.assertFalse(document["web_run"]["disable_console_progress"])
            self.assertEqual(resolved["start_index"], 0)
            self.assertEqual(resolved["limit_images"], 2)

            ranged_path, ranged = _build_job_config(
                app,
                run_id="run_test_ranged_config",
                dataset=dataset,
                frames=frames,
                payload=RunRequest(
                    dataset_id="dataset-a",
                    track_ids=[selected_id],
                    frame_range=(2, 2),
                    mode="automatic",
                    auto={"preset": "balanced"},
                ),
                core_parameters={"imgsz": 960},
            )
            ranged_document = yaml.load(
                ranged_path.read_text(encoding="utf-8"),
                Loader=_Yaml12SafeLoader,
            )
            self.assertEqual(ranged_document["runtime"]["start_index"], 1)
            self.assertEqual(ranged_document["runtime"]["limit_images"], 1)
            self.assertEqual(ranged["frame_range"], [2, 2])


if __name__ == "__main__":
    unittest.main()
