from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app

NOW = "2026-08-03T00:00:00+00:00"


def _seed_dataset(app) -> None:
    app.state.store.upsert_scanning_dataset(
        dataset_id="dataset-a",
        name="Dataset A",
        root_id="root-a",
        relative_path="",
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
                    "image_stem": "frame-a",
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


def _completed_run(
    app,
    state: Path,
    *,
    run_id: str,
    created_at: str,
    payloads: dict[str, dict],
    dismissed: bool = False,
) -> None:
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
    output = state / "runs" / run_id / "output"
    output.mkdir(parents=True)
    models = []
    for model_key, payload in payloads.items():
        result = output / model_key / "txt" / "record-a" / "frame-a.txt"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps(payload), encoding="utf-8")
        models.append({"model_key": model_key, "status": "completed"})
    (output / "models_manifest.json").write_text(
        json.dumps({"models": models}), encoding="utf-8"
    )
    app.state.store.update_run(
        run_id,
        created_at,
        status="completed",
        return_code=0,
        finished_at=created_at,
    )
    if dismissed:
        with app.state.store.connection(write=True) as connection:
            connection.execute("UPDATE runs SET dismissed=1 WHERE id=?", (run_id,))


def _payload(model_name: str, detections: list[dict], *, schema_version: int = 17):
    return {
        "schema_version": schema_version,
        "model_name": model_name,
        "record_name": "record-a",
        "image_name": "frame-a.jpg",
        "panorama": {"image_width": 4000, "image_height": 2000},
        "detections": detections,
    }


class WebAppDetectionTests(unittest.TestCase):
    def test_zero_based_same_frame_indices_remain_distinct_and_stable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            common = {
                "image_name": "frame-a.jpg",
                "confidence": 0.9,
                "panorama_width": 4000,
                "panorama_height": 2000,
                "accepted_for_shp": True,
            }
            _completed_run(
                app,
                state,
                run_id="run-zero-based",
                created_at=NOW,
                payloads={
                    "model-a": _payload(
                        "traffic-light.pt",
                        [
                            {
                                **common,
                                "detection_index": 0,
                                "class_id": 1,
                                "class_name": "signal-left",
                                "bbox_xyxy": [1800, 800, 1900, 900],
                                "x": 300_010.0,
                                "y": 4_100_010.0,
                                "z": 14.0,
                            },
                            {
                                **common,
                                "detection_index": 1,
                                "class_id": 2,
                                "class_name": "signal-right",
                                "bbox_xyxy": [2000, 800, 2100, 900],
                                "x": 300_012.0,
                                "y": 4_100_010.0,
                                "z": 14.0,
                            },
                        ],
                    ),
                },
            )

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )

            self.assertEqual(response.status_code, 200, response.text)
            items = response.json()["items"]
            self.assertEqual([item["properties"]["det_index"] for item in items], [0, 1])
            self.assertEqual(len({item["observation_id"] for item in items}), 2)

    def test_frame_boxes_do_not_require_an_imported_or_visible_shp_layer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            common = {
                "detection_index": 1,
                "image_name": "frame-a.jpg",
                "confidence": 0.9,
                "panorama_width": 4000,
                "panorama_height": 2000,
            }
            _completed_run(
                app,
                state,
                run_id="run-hidden",
                created_at=NOW,
                dismissed=True,
                payloads={
                    "model-a": _payload(
                        "traffic-light.pt",
                        [
                            {
                                **common,
                                "class_id": 1,
                                "class_name": "signal",
                                "bbox_xyxy": [1900, 900, 2000, 1000],
                                "accepted_for_shp": True,
                                "x": 300_010.5,
                                "y": 4_100_012.25,
                                "z": 14.0,
                            }
                        ],
                    ),
                    "model-b": _payload(
                        "traffic-sign.pt",
                        [
                            {
                                **common,
                                "class_id": 2,
                                "class_name": "sign",
                                # An unwrapped seam box is a valid panorama box.
                                "bbox_xyxy": [3980, 800, 4020, 900],
                                "accepted_for_shp": False,
                                "candidate_x": 300_020.0,
                                "candidate_y": 4_100_020.0,
                                "candidate_z": 15.0,
                            }
                        ],
                    ),
                },
            )

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["coordinate_space"], "panorama_equirectangular_pixels")
            self.assertEqual(body["model_count"], 2)
            self.assertEqual(
                body["models"],
                [
                    {
                        "model_id": body["items"][0]["model_id"],
                        "source_id": body["items"][0]["source_id"],
                        "source_name": "traffic-light.pt",
                        "count": 1,
                    },
                    {
                        "model_id": body["items"][1]["model_id"],
                        "source_id": body["items"][1]["source_id"],
                        "source_name": "traffic-sign.pt",
                        "count": 1,
                    },
                ],
            )
            self.assertEqual(body["count"], 2)
            self.assertEqual(
                [item["source_name"] for item in body["items"]],
                ["traffic-light.pt", "traffic-sign.pt"],
            )
            self.assertNotEqual(
                body["items"][0]["source_id"], body["items"][1]["source_id"]
            )
            self.assertNotEqual(
                body["items"][0]["model_id"], body["items"][1]["model_id"]
            )
            self.assertEqual(
                body["items"][0]["dataset_position"],
                [300_010.5, 4_100_012.25, 14.0],
            )
            self.assertNotIn("dataset_position", body["items"][1])
            self.assertEqual(body["items"][1]["properties"]["bbox_r"], 4020.0)
            self.assertFalse((state / "overlays").exists())

    def test_explicit_forward_view_box_is_inverse_mapped_with_complete_metadata(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            payload = _payload(
                "legacy-forward.pt",
                [
                    {
                        "detection_index": 1,
                        "image_name": "frame-a.jpg",
                        "class_id": 4,
                        "class_name": "signal",
                        "confidence": 0.8,
                        "bbox_xyxy": [450, 450, 550, 550],
                        "bbox_coordinate_space": "forward_rectilinear_pixels",
                        "panorama_width": 4000,
                        "panorama_height": 2000,
                    }
                ],
                schema_version=16,
            )
            payload["panorama_detection"] = {
                "mode": "forward",
                "forward_view_width_px": 1000,
                "forward_view_height_px": 1000,
                "forward_view_hfov_deg": 90,
                "forward_view_vfov_deg": 90,
            }
            payload["panorama_alignment"] = {
                "yaw_offset_deg": 0,
                "pitch_offset_deg": 0,
            }
            _completed_run(
                app,
                state,
                run_id="run-forward",
                created_at=NOW,
                payloads={"model-forward": payload},
            )

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )
            self.assertEqual(response.status_code, 200, response.text)
            properties = response.json()["items"][0]["properties"]
            self.assertAlmostEqual(properties["bbox_l"], 1936.55, delta=0.05)
            self.assertAlmostEqual(properties["bbox_t"], 936.55, delta=0.05)
            self.assertAlmostEqual(properties["bbox_r"], 2063.45, delta=0.05)
            self.assertAlmostEqual(properties["bbox_b"], 1063.45, delta=0.05)
            self.assertEqual(properties["bbox_mapping"], "forward_rectilinear_inverse_v1")

    def test_newest_exact_frame_payload_prevents_stale_run_boxes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            detection = {
                "detection_index": 1,
                "image_name": "frame-a.jpg",
                "class_name": "old",
                "bbox_xyxy": [10, 10, 20, 20],
                "panorama_width": 4000,
                "panorama_height": 2000,
            }
            _completed_run(
                app,
                state,
                run_id="run-old",
                created_at="2026-08-01T00:00:00+00:00",
                payloads={"model-a": _payload("old.pt", [detection])},
            )
            _completed_run(
                app,
                state,
                run_id="run-new",
                created_at="2026-08-02T00:00:00+00:00",
                payloads={"model-a": _payload("new.pt", [])},
            )

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["model_count"], 1)
            self.assertEqual(response.json()["items"], [])
            self.assertEqual(response.json()["models"][0]["source_name"], "new.pt")
            self.assertEqual(response.json()["models"][0]["count"], 0)

    def test_request_wide_json_budget_stops_model_parsing_and_marks_response(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            detection = {
                "detection_index": 1,
                "image_name": "frame-a.jpg",
                "class_name": "sign",
                "bbox_xyxy": [100, 100, 200, 200],
                "panorama_width": 4000,
                "panorama_height": 2000,
            }
            _completed_run(
                app,
                state,
                run_id="run-budget",
                created_at=NOW,
                payloads={
                    "model-a": _payload("model-a.pt", [detection]),
                    "model-b": _payload("model-b.pt", [detection]),
                },
            )
            first_result = (
                state
                / "runs"
                / "run-budget"
                / "output"
                / "model-a"
                / "txt"
                / "record-a"
                / "frame-a.txt"
            )
            model_manifest = (
                state / "runs" / "run-budget" / "output" / "models_manifest.json"
            )

            # The first exact payload fits; parsing the second would cross the
            # request-wide cap and must stop before loading it into memory.
            with (
                patch(
                    "mms_shp_detection.webapp.detections.MAX_DETECTION_REQUEST_BYTES",
                    model_manifest.stat().st_size + first_result.stat().st_size,
                ),
                TestClient(app) as client,
            ):
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["model_count"], 1)
            self.assertEqual(body["count"], 1)
            self.assertEqual(body["items"][0]["source_name"], "model-a.pt")
            self.assertTrue(body["truncated"])

    def test_newest_over_model_cap_fails_closed_instead_of_using_stale_run(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            detection = {
                "detection_index": 1,
                "image_name": "frame-a.jpg",
                "class_name": "sign",
                "bbox_xyxy": [100, 100, 200, 200],
                "panorama_width": 4000,
                "panorama_height": 2000,
            }
            _completed_run(
                app,
                state,
                run_id="run-old",
                created_at="2026-08-01T00:00:00+00:00",
                payloads={"model-a": _payload("stale.pt", [detection])},
            )
            _completed_run(
                app,
                state,
                run_id="run-new",
                created_at="2026-08-02T00:00:00+00:00",
                payloads={
                    "model-a": _payload("model-a.pt", [detection]),
                    "model-b": _payload("model-b.pt", [detection]),
                },
            )

            with (
                patch(
                    "mms_shp_detection.webapp.detections.MAX_MODEL_COUNT",
                    1,
                ),
                TestClient(app) as client,
            ):
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["model_count"], 0)
            self.assertEqual(body["items"], [])
            self.assertTrue(body["truncated"])

    def test_malformed_newest_exact_artifact_does_not_fall_back_to_stale_run(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)], state_dir=state, start_runner=False
            )
            _seed_dataset(app)
            detection = {
                "detection_index": 1,
                "image_name": "frame-a.jpg",
                "class_name": "sign",
                "bbox_xyxy": [100, 100, 200, 200],
                "panorama_width": 4000,
                "panorama_height": 2000,
            }
            _completed_run(
                app,
                state,
                run_id="run-old",
                created_at="2026-08-01T00:00:00+00:00",
                payloads={"model-a": _payload("stale.pt", [detection])},
            )
            _completed_run(
                app,
                state,
                run_id="run-new",
                created_at="2026-08-02T00:00:00+00:00",
                payloads={"model-a": _payload("new.pt", [detection])},
            )
            newest_result = (
                state
                / "runs"
                / "run-new"
                / "output"
                / "model-a"
                / "txt"
                / "record-a"
                / "frame-a.txt"
            )
            newest_result.write_text("{malformed", encoding="utf-8")

            with TestClient(app) as client:
                response = client.get(
                    "/api/datasets/dataset-a/frames/frame-a/detections"
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["model_count"], 0)
            self.assertEqual(body["items"], [])
            self.assertTrue(body["truncated"])


if __name__ == "__main__":
    unittest.main()
