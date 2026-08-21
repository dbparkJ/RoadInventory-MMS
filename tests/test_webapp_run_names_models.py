from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import shapefile
import yaml
from fastapi.testclient import TestClient

from mms_shp_detection.config import _Yaml12SafeLoader
from mms_shp_detection.webapp import WebAppConfig, create_app
from mms_shp_detection.webapp.runs import RunRequest, _build_job_config
from mms_shp_detection.webapp.store import WebStore

NOW = "2026-08-20T00:00:00+00:00"
FINISHED = "2026-08-20T00:05:00+00:00"


def seed_ready_dataset(
    app,
    root: Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    root_id = app.state.storage_roots[0].id
    app.state.store.upsert_scanning_dataset(
        dataset_id="dataset-a",
        name="Dataset A",
        root_id=root_id,
        relative_path="",
        crs="EPSG:4326",
        now=NOW,
    )
    track = {
        "id": "track-a",
        "name": "Track A",
        "record_name": "Record_A",
        "job_name": "Job_A",
        "frame_count": 1,
    }
    frame = {
        "id": "frame-a",
        "ordinal": 0,
        "track_id": "track-a",
        "task": {"image_name": "frame-a.jpg", "image_stem": "frame-a"},
    }
    app.state.store.finish_dataset_scan(
        "dataset-a",
        frames=[frame],
        tracks=[track],
        bbox=None,
        warnings=[],
        now=NOW,
    )
    dataset = {
        "id": "dataset-a",
        "root_id": root_id,
        "relative_path": "",
        "tracks": [track],
    }
    return dataset, [frame]


def seed_completed_run(app, *, name: str = "Initial run") -> dict[str, object]:
    app.state.store.create_run(
        {
            "id": "run-completed",
            "dataset_id": "dataset-a",
            "name": name,
            "request": {
                "dataset_id": "dataset-a",
                "layer_name": "표지판 검출 레이어",
            },
            "resolved": {},
            "work_relative": "run-completed",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    app.state.store.transition_run(
        "run-completed",
        FINISHED,
        from_statuses=("queued",),
        to_status="completed",
        finished_at=FINISHED,
    )
    return app.state.store.get_run("run-completed")  # type: ignore[return-value]


class WebAppRunNamesAndModelsTests(unittest.TestCase):
    def test_completed_run_name_is_persistent_and_uses_optimistic_locking(self) -> None:
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
            seed_ready_dataset(app, root)
            run = seed_completed_run(app)

            with TestClient(app) as client:
                renamed = client.patch(
                    "/api/runs/run-completed",
                    json={
                        "name": "  야간 표지판 검출  ",
                        "expected_updated_at": run["updated_at"],
                    },
                )
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual(renamed.json()["name"], "야간 표지판 검출")

                stale = client.patch(
                    "/api/runs/run-completed",
                    json={"name": "stale overwrite", "expected_updated_at": FINISHED},
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                listed = client.get("/api/datasets/dataset-a/runs/completed")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(listed.json()["items"][0]["name"], "야간 표지판 검출")

                invalid = client.patch(
                    "/api/runs/run-completed",
                    json={"name": "   "},
                )
                self.assertEqual(invalid.status_code, 422, invalid.text)

            reopened = WebStore(app.state.store.path)
            self.assertEqual(
                reopened.get_run("run-completed")["name"],  # type: ignore[index]
                "야간 표지판 검출",
            )

    def test_model_catalog_and_selected_subset_reach_the_job_yaml(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            model_dir = state / "models"
            model_dir.mkdir()
            (model_dir / "Zeta.pt").write_bytes(b"z")
            (model_dir / "alpha.PT").write_bytes(b"a")
            base_config = state / "base.yaml"
            base_config.write_text(
                "\n".join(
                    (
                        "config_version: 1",
                        "paths:",
                        f"  model_dir: {model_dir.as_posix()}",
                        "  model_path: null",
                        "  model_names: [alpha.PT]",
                        "model_filters:",
                        "  Zeta.pt: {object_type: traffic_sign}",
                        "  alpha.PT: {object_type: traffic_signal}",
                    )
                ),
                encoding="utf-8",
            )
            app = create_app(
                WebAppConfig(
                    project_root=Path(__file__).resolve().parents[1],
                    state_dir=state / "web",
                    allowed_roots=[root],
                    pipeline_config_path=base_config,
                    enable_run_worker=False,
                )
            )
            dataset, frames = seed_ready_dataset(app, root)

            with TestClient(app) as client:
                catalog = client.get("/api/detection-models")
                self.assertEqual(catalog.status_code, 200, catalog.text)
                self.assertEqual(
                    [item["name"] for item in catalog.json()["items"]],
                    ["alpha.PT", "Zeta.pt"],
                )
                empty = client.post(
                    "/api/runs",
                    json={"dataset_id": "dataset-a", "model_names": []},
                )
                self.assertEqual(empty.status_code, 422, empty.text)

            config_path, resolved = _build_job_config(
                app,
                run_id="run-selected-model",
                dataset=dataset,
                frames=frames,
                payload=RunRequest(
                    dataset_id="dataset-a",
                    track_ids=["track-a"],
                    model_names=["zeta.PT"],
                    layer_name="선택 모델 결과",
                ),
                core_parameters={},
            )
            document = yaml.load(
                config_path.read_text(encoding="utf-8"),
                Loader=_Yaml12SafeLoader,
            )
            self.assertEqual(document["paths"]["model_names"], ["Zeta.pt"])
            self.assertEqual(resolved["model_names"], ["Zeta.pt"])
            self.assertEqual(resolved["layer_name"], "선택 모델 결과")

            all_config_path, all_resolved = _build_job_config(
                app,
                run_id="run-all-models",
                dataset=dataset,
                frames=frames,
                payload=RunRequest(
                    dataset_id="dataset-a",
                    track_ids=["track-a"],
                ),
                core_parameters={},
            )
            all_document = yaml.load(
                all_config_path.read_text(encoding="utf-8"),
                Loader=_Yaml12SafeLoader,
            )
            self.assertIsNone(all_document["paths"]["model_names"])
            self.assertIsNone(all_resolved["model_names"])

            with self.assertRaisesRegex(ValueError, "no longer available"):
                _build_job_config(
                    app,
                    run_id="run-missing-model",
                    dataset=dataset,
                    frames=frames,
                    payload=RunRequest(
                        dataset_id="dataset-a",
                        track_ids=["track-a"],
                        model_names=["removed.pt"],
                    ),
                    core_parameters={},
                )
            self.assertFalse((state / "web" / "runs" / "run-missing-model").exists())

    def test_create_run_rejects_only_selected_model_output_collisions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            model_dir = state / "models"
            model_dir.mkdir()
            (model_dir / "road sign.pt").write_bytes(b"space")
            (model_dir / "road_sign.pt").write_bytes(b"underscore")
            base_config = state / "base.yaml"
            base_config.write_text(
                "\n".join(
                    (
                        "config_version: 1",
                        "paths:",
                        f"  model_dir: {model_dir.as_posix()}",
                        "  model_path: null",
                    )
                ),
                encoding="utf-8",
            )
            app = create_app(
                WebAppConfig(
                    project_root=Path(__file__).resolve().parents[1],
                    state_dir=state / "web",
                    allowed_roots=[root],
                    pipeline_config_path=base_config,
                    enable_run_worker=False,
                )
            )
            seed_ready_dataset(app, root)

            with TestClient(app) as client:
                catalog = client.get("/api/detection-models")
                self.assertEqual(catalog.status_code, 200, catalog.text)
                self.assertEqual(
                    [item["name"] for item in catalog.json()["items"]],
                    ["road sign.pt", "road_sign.pt"],
                )
                one_model = client.post(
                    "/api/runs",
                    json={
                        "dataset_id": "dataset-a",
                        "model_names": ["road sign.pt"],
                    },
                )
                self.assertEqual(one_model.status_code, 201, one_model.text)
                collision = client.post(
                    "/api/runs",
                    json={
                        "dataset_id": "dataset-a",
                        "model_names": ["road sign.pt", "road_sign.pt"],
                    },
                )
                self.assertEqual(collision.status_code, 422, collision.text)
                self.assertIn("collide", collision.json()["detail"])
                self.assertEqual(len(client.get("/api/runs").json()["items"]), 1)

    def test_layer_name_is_exposed_and_is_the_server_import_default(self) -> None:
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
            seed_ready_dataset(app, root)
            seed_completed_run(app)
            shp_root = state / "runs" / "run-completed" / "output" / "model-a" / "shp"
            shp_root.mkdir(parents=True)
            collision_root = shp_root.parent / "other"
            collision_root.mkdir()
            shp_paths = (
                shp_root / "pole_bottoms.shp",
                shp_root / "signs.shp",
                collision_root / "signs.shp",
            )
            for shp_path in shp_paths:
                writer = shapefile.Writer(str(shp_path), shapeType=shapefile.POINT)
                writer.field("ID", "N", size=10, decimal=0)
                writer.point(127.0, 37.0)
                writer.record(1)
                writer.close()

            with TestClient(app) as client:
                results = client.get("/api/runs/run-completed/results")
                self.assertEqual(results.status_code, 200, results.text)
                shapefiles = results.json()["shapefiles"]
                display_by_path = {
                    item["path"]: item["display_name"] for item in shapefiles
                }
                self.assertEqual(
                    display_by_path["model-a/other/signs.shp"],
                    "표지판 검출 레이어 · model-a · signs",
                )
                self.assertEqual(
                    display_by_path["model-a/shp/pole_bottoms.shp"],
                    "표지판 검출 레이어 · model-a · pole_bottoms",
                )
                self.assertEqual(
                    display_by_path["model-a/shp/signs.shp"],
                    "표지판 검출 레이어 · model-a · signs · 3",
                )
                self.assertEqual(len(set(display_by_path.values())), 3)
                imported = client.post(
                    "/api/runs/run-completed/shapefile/import",
                    json={"path": "model-a/shp/signs.shp", "crs": "EPSG:4326"},
                )
                self.assertEqual(imported.status_code, 201, imported.text)
                self.assertEqual(
                    imported.json()["layer"]["name"],
                    display_by_path["model-a/shp/signs.shp"],
                )


if __name__ == "__main__":
    unittest.main()
