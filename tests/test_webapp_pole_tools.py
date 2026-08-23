from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app, pole_tools

NOW = "2026-08-23T00:00:00+00:00"


def _seed_dataset(
    app,
    *,
    dataset_id: str = "dataset-poles",
    frame_id: str = "frame-poles",
    crs: str = "EPSG:32652",
) -> None:
    root = app.state.storage_roots[0]
    app.state.store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name="Pole delivery",
        root_id=root.id,
        relative_path="delivery",
        crs=crs,
        now=NOW,
    )
    app.state.store.finish_dataset_scan(
        dataset_id,
        frames=[
            {
                "id": frame_id,
                "ordinal": 0,
                "track_id": "track-a",
                "task": {
                    "origin": [300_000.0, 4_100_000.0, 10.0],
                    "direction": [1.0, 0.0, 0.0],
                    "up": [0.0, 0.0, 1.0],
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                },
                "longitude": 126.75,
                "latitude": 37.03,
                "altitude": 10.0,
                "heading": 90.0,
            }
        ],
        tracks=[{"id": "track-a", "name": "TRACK01", "frame_count": 1}],
        bbox=[126.75, 37.03, 126.75, 37.03],
        warnings=[],
        now=NOW,
    )


def _catalog(blocks, *, path: str = "cloud.las"):
    return {
        "files": [
            {
                "path": path,
                "source_type": "las",
                "job_name": "Job_A",
                "track_name": "TRACK01",
                "file_min": [299_990.0, 4_099_990.0, -10.0],
                "file_max": [300_010.0, 4_100_010.0, 20.0],
                "blocks": blocks,
            }
        ]
    }


class _Reader:
    def __init__(self, records_by_name=None):
        self.records_by_name = records_by_name or {}
        self.calls = []

    def read_block_records(self, point_file, block):
        self.calls.append((point_file, block))
        return self.records_by_name[str(block["name"])]

    def close(self):
        return None


class WebAppPoleToolTests(unittest.TestCase):
    def test_metric_crs_and_frame_window_validation(self) -> None:
        pole_tools._validate_metric_dataset_crs("EPSG:32652")
        for crs in ("EPSG:4326", "EPSG:2277", "not-a-crs"):
            with (
                self.subTest(crs=crs),
                self.assertRaisesRegex(ValueError, "METRIC_CRS_REQUIRED"),
            ):
                pole_tools._validate_metric_dataset_crs(crs)

        task = {"origin": [100.0, 200.0, 10.0]}
        np.testing.assert_allclose(
            pole_tools._validate_seed_against_frame([120.0, 200.0, 15.0], task),
            [100.0, 200.0, 10.0],
        )
        for seed in ([131.0, 200.0, 10.0], [100.0, 200.0, 41.0]):
            with (
                self.subTest(seed=seed),
                self.assertRaisesRegex(ValueError, "SEED_OUTSIDE_FRAME_WINDOW"),
            ):
                pole_tools._validate_seed_against_frame(seed, task)

    def test_block_filter_uses_cylinder_and_vertical_window(self) -> None:
        seed = [0.0, 0.0, 10.0]
        cases = [
            ({"min": [-0.1, -0.1, 0.0], "max": [0.1, 0.1, 11.0]}, True),
            ({"min": [1.9, -0.1, 8.0], "max": [2.1, 0.1, 9.0]}, True),
            ({"min": [1.5, 1.5, 8.0], "max": [1.6, 1.6, 9.0]}, False),
            ({"min": [-0.1, -0.1, 14.1], "max": [0.1, 0.1, 15.0]}, False),
            ({"min": [None, 0.0, 0.0], "max": [1.0, 1.0, 1.0]}, False),
        ]
        for block, expected in cases:
            with self.subTest(block=block):
                self.assertEqual(
                    pole_tools._block_intersects_local_window(block, seed), expected
                )

    def test_collection_reads_only_intersecting_raw_blocks_and_exactly_crops(
        self,
    ) -> None:
        local_block = {
            "name": "local",
            "source_type": "las",
            "min": [299_999.0, 4_099_999.0, 0.0],
            "max": [300_003.0, 4_100_003.0, 14.0],
        }
        far_block = {
            "name": "far",
            "source_type": "las",
            "min": [300_100.0, 4_100_100.0, 0.0],
            "max": [300_110.0, 4_100_110.0, 14.0],
        }
        xyz = np.asarray(
            [
                [300_000.0, 4_100_000.0, 10.0],
                [300_001.0, 4_100_001.0, -2.0],
                [300_001.5, 4_100_001.5, 10.0],
                [300_000.0, 4_100_000.0, 14.1],
            ],
            dtype=np.float64,
        )
        reader = _Reader(
            {
                "local": {
                    "xyz": xyz,
                    "classification": np.asarray([7, 2, 5, 9], dtype=np.int16),
                }
            }
        )
        records = pole_tools._collect_local_point_records(
            {
                "origin": [300_000.0, 4_100_000.0, 10.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _catalog([far_block, local_block]),
            reader,
            [300_000.0, 4_100_000.0, 10.0],
        )
        self.assertEqual([call[1]["name"] for call in reader.calls], ["local"])
        np.testing.assert_allclose(
            records["xyz"],
            [
                [300_000.0, 4_100_000.0, 10.0],
                [300_001.0, 4_100_001.0, -2.0],
            ],
        )
        np.testing.assert_array_equal(records["classification"], [7, 2])

    def test_collection_enforces_candidate_and_point_hard_limits(self) -> None:
        blocks = [
            {
                "name": f"block-{index}",
                "source_type": "las",
                "min": [-1.0, -1.0, -1.0],
                "max": [1.0, 1.0, 1.0],
            }
            for index in range(2)
        ]
        with self.assertRaisesRegex(RuntimeError, "too many") as caught:
            pole_tools._collect_local_point_records(
                {"origin": [0.0, 0.0, 0.0]},
                _catalog(blocks),
                _Reader(),
                [0.0, 0.0, 0.0],
                max_candidate_blocks=1,
            )
        self.assertEqual(caught.exception.reason_code, "TOO_MANY_CANDIDATE_BLOCKS")

        reader = _Reader(
            {
                "block-0": {
                    "xyz": np.zeros((3, 3), dtype=np.float64),
                    "classification": np.zeros(3, dtype=np.int16),
                }
            }
        )
        with self.assertRaisesRegex(RuntimeError, "too many") as caught:
            pole_tools._collect_local_point_records(
                {"origin": [0.0, 0.0, 0.0]},
                _catalog([blocks[0]]),
                reader,
                [0.0, 0.0, 0.0],
                max_local_points=2,
            )
        self.assertEqual(caught.exception.reason_code, "LOCAL_POINT_LIMIT_EXCEEDED")

    def test_catalog_data_root_is_enforced_by_safe_resolver(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as outside_text,
        ):
            root = Path(root_text)
            outside = Path(outside_text) / "outside.las"
            outside.write_bytes(b"source")
            catalog = _catalog(
                [
                    {
                        "name": "block",
                        "source_type": "las",
                        "min": [-1.0, -1.0, -1.0],
                        "max": [1.0, 1.0, 1.0],
                    }
                ],
                path=str(outside),
            )
            catalog["data_root"] = str(root)
            with self.assertRaisesRegex(ValueError, "escaped"):
                pole_tools._collect_local_point_records(
                    {"origin": [0.0, 0.0, 0.0]},
                    catalog,
                    _Reader(),
                    [0.0, 0.0, 0.0],
                )

    def test_result_serialization_accepts_dataclasses_and_bounds_debug(self) -> None:
        @dataclass
        class Result:
            status: str
            base_position: np.ndarray
            reason_codes: tuple[str, ...]
            warnings: tuple[str, ...]
            debug: dict[str, object]

        payload = pole_tools._public_manual_pole_base_result(
            Result(
                status="auto",
                base_position=np.asarray([1.0, 2.0, 3.0]),
                reason_codes=(),
                warnings=(),
                debug={"points": np.arange(900).reshape(300, 3)},
            ),
            seed_xyz=[1.0, 2.0, 4.0],
            debug=True,
        )
        self.assertEqual(payload["base_position"], [1.0, 2.0, 3.0])
        self.assertEqual(payload["seed_position"], [1.0, 2.0, 4.0])
        self.assertEqual(len(payload["debug"]["points"]), pole_tools.MAX_DEBUG_POINTS)

        from mms_shp_detection.manual_pole_base import (
            ManualPoleBaseQuality,
            ManualPoleBaseResult,
        )

        manual_result = ManualPoleBaseResult(
            status="failed",
            seed_position=np.asarray([1.0, 2.0, 4.0]),
            snapped_seed_position=None,
            base_position=None,
            axis=None,
            ground=None,
            quality=ManualPoleBaseQuality(
                score=0.0,
                candidate_count=0,
                ambiguous=False,
                bottom_gap_m=None,
                components={},
            ),
            reason_codes=("NO_GROUND_SUPPORT",),
            debug_points=np.arange(900, dtype=np.float64).reshape(300, 3),
        )
        actual_contract = pole_tools._public_manual_pole_base_result(
            manual_result, debug=True
        )
        self.assertEqual(
            len(actual_contract["debug"]["support_points"]),
            pole_tools.MAX_DEBUG_POINTS,
        )
        failed_contract = pole_tools._failed_public_result(
            [1.0, 2.0, 4.0], "NO_LOCAL_POINTS"
        )
        self.assertEqual(
            set(failed_contract["quality"]["components"]),
            {"seed", "axis", "span", "continuity", "ground", "bottom_gap"},
        )

    def test_endpoint_validation_catalog_202_capability_and_read_only_result(
        self,
    ) -> None:
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
            _seed_dataset(app)
            endpoint = "/api/datasets/dataset-poles/frames/frame-poles/pole-base/infer"
            request_payload = {
                "coordinate_space": "dataset",
                "seed_position": [300_000.0, 4_100_000.0, 10.0],
                "profile": "balanced",
            }
            with (
                patch.object(pole_tools, "schedule_catalog") as scheduled,
                TestClient(app) as client,
            ):
                self.assertTrue(
                    client.get("/api/bootstrap").json()["capabilities"][
                        "pole_base_inference"
                    ]
                )
                missing_frame = client.post(
                    endpoint.replace("frame-poles", "missing"), json=request_payload
                )
                self.assertEqual(missing_frame.status_code, 404, missing_frame.text)
                invalid_seed = client.post(
                    endpoint,
                    json={
                        **request_payload,
                        "seed_position": [300_100.0, 4_100_000.0, 10.0],
                    },
                )
                self.assertEqual(invalid_seed.status_code, 422, invalid_seed.text)
                indexing = client.post(endpoint, json=request_payload)
                self.assertEqual(indexing.status_code, 202, indexing.text)
                self.assertEqual(indexing.headers["retry-after"], "2")
                scheduled.assert_called_once()

                block = {
                    "name": "raw-block",
                    "source_type": "las",
                    "min": [299_999.0, 4_099_999.0, 9.0],
                    "max": [300_001.0, 4_100_001.0, 11.0],
                }
                raw_reader = _Reader(
                    {
                        "raw-block": {
                            "xyz": np.asarray(
                                [[300_000.0, 4_100_000.0, 10.0]],
                                dtype=np.float64,
                            ),
                            "classification": np.asarray([0], dtype=np.int16),
                        }
                    }
                )
                app.state.point_reader.close()
                app.state.point_reader = raw_reader
                app.state.catalogs["dataset-poles"] = _catalog([block])
                before = app.state.store.get_dataset("dataset-poles")["updated_at"]
                response = client.post(endpoint, json=request_payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "failed")
                self.assertEqual(response.json()["reason_codes"], ["NO_VERTICAL_AXIS"])
                self.assertEqual(
                    [call[1]["name"] for call in raw_reader.calls], ["raw-block"]
                )
                self.assertEqual(
                    app.state.store.get_dataset("dataset-poles")["updated_at"], before
                )

    def test_endpoint_rejects_non_metric_dataset_and_missing_reader(self) -> None:
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
            _seed_dataset(app, crs="EPSG:4326")
            endpoint = "/api/datasets/dataset-poles/frames/frame-poles/pole-base/infer"
            payload = {
                "coordinate_space": "dataset",
                "seed_position": [300_000.0, 4_100_000.0, 10.0],
                "profile": "balanced",
            }
            with TestClient(app) as client:
                metric = client.post(endpoint, json=payload)
                self.assertEqual(metric.status_code, 422, metric.text)
                self.assertEqual(
                    metric.json()["detail"]["reason_code"], "METRIC_CRS_REQUIRED"
                )

            with app.state.store.connection(write=True) as connection:
                connection.execute(
                    "UPDATE datasets SET crs=? WHERE id=?",
                    ("EPSG:32652", "dataset-poles"),
                )
            app.state.point_reader = None
            with TestClient(app) as client:
                unavailable = client.post(endpoint, json=payload)
                self.assertEqual(unavailable.status_code, 503, unavailable.text)


class PoleToolOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_keeps_its_semaphore_until_owner_finishes(
        self,
    ) -> None:
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
                return await pole_tools._finish_inference_after_request_cancel(
                    work(),
                    owner_tasks=owner_tasks,
                    logger=Mock(),
                    context="test inference",
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
