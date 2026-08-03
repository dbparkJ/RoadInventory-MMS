from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

try:
    from mms_shp_detection.webapp.app import _panorama_alignment_defaults
    from mms_shp_detection.webapp.media import (
        MMSO_HEADER,
        MMSO_RECORD_BYTES,
        MMSP_HEADER,
        MMSP_RECORD_BYTES,
        _build_mmso,
        _build_mmsp,
        _enforce_file_cache_quota,
        _finish_preview_after_request_cancel,
        _task_point_fingerprint,
    )
except ImportError as exc:  # pragma: no cover - minimal environments
    MMSP_IMPORT_ERROR = exc
else:
    MMSP_IMPORT_ERROR = None


class _Reader:
    def read_block_points(self, _point_file, block):
        index = int(block["name"].removeprefix("block"))
        x = np.linspace(index, index + 0.9, 100)
        xyz = np.column_stack((x, np.zeros(100), np.zeros(100))).astype(np.float64)
        rgb = np.full((100, 3), index * 20, dtype=np.uint8)
        return xyz, rgb, np.zeros(100, dtype=np.uint16)


class _StaticReader:
    def __init__(self, xyz, rgb):
        self.xyz = np.asarray(xyz, dtype=np.float64)
        self.rgb = np.asarray(rgb, dtype=np.uint8)

    def read_block_points(self, _point_file, _block):
        return (
            self.xyz.copy(),
            self.rgb.copy(),
            np.zeros(self.xyz.shape[0], dtype=np.uint16),
        )

    def close(self):
        return None


def _static_catalog(xyz):
    points = np.asarray(xyz, dtype=np.float64)
    minimum = points.min(axis=0).tolist()
    maximum = points.max(axis=0).tolist()
    return {
        "files": [
            {
                "path": "cloud.las",
                "source_type": "las",
                "job_name": "Job_A",
                "track_name": "TRACK01",
                "file_min": minimum,
                "file_max": maximum,
                "blocks": [
                    {
                        "name": "block0",
                        "source_type": "las",
                        "start": 0,
                        "count": int(points.shape[0]),
                        "min": minimum,
                        "max": maximum,
                    }
                ],
            }
        ]
    }


@unittest.skipIf(MMSP_IMPORT_ERROR is not None, f"point dependencies missing: {MMSP_IMPORT_ERROR}")
class WebAppMediaTests(unittest.TestCase):
    def test_panorama_point_cache_quota_removes_oldest_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            paths = [directory / f"{name}.mmso" for name in ("old", "middle", "keep")]
            timestamp = time.time_ns()
            for index, path in enumerate(paths, start=1):
                path.write_bytes(b"1234")
                modified = timestamp - (4 - index) * 2_000_000_000
                os.utime(path, ns=(modified, modified))

            _enforce_file_cache_quota(
                directory,
                suffix=".mmso",
                maximum_bytes=8,
                keep=paths[-1],
            )

            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())
            self.assertTrue(paths[2].exists())

    def test_cancelled_request_drains_and_releases_preview_owner(self) -> None:
        async def exercise() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            finished = asyncio.Event()
            owner_tasks: set[asyncio.Task] = set()

            async def work() -> int:
                started.set()
                await release.wait()
                finished.set()
                return 7

            owner = asyncio.create_task(
                _finish_preview_after_request_cancel(
                    work(),
                    owner_tasks=owner_tasks,
                    logger=mock.Mock(),
                    context="test preview",
                )
            )
            await started.wait()
            self.assertEqual(len(owner_tasks), 1)
            owner.cancel()
            await asyncio.sleep(0)
            self.assertFalse(owner.done())
            self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            self.assertTrue(finished.is_set())
            await asyncio.sleep(0)
            self.assertFalse(owner_tasks)

        asyncio.run(exercise())

    def test_mmsp_header_and_budget_are_compact_and_multi_block(self) -> None:
        files = []
        for index in range(4):
            files.append(
                {
                    "path": f"cloud{index}.las",
                    "source_type": "las",
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                    "file_min": [index, -1, -1],
                    "file_max": [index + 1, 1, 1],
                    "blocks": [
                        {
                            "name": f"block{index}",
                            "source_type": "las",
                            "start": 0,
                            "count": 100,
                            "min": [index, -1, -1],
                            "max": [index + 1, 1, 1],
                        }
                    ],
                }
            )
        payload = _build_mmsp(
            {
                "origin": [0.0, 0.0, 0.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            {"files": files},
            _Reader(),
            budget=20,
            radius=10.0,
        )
        self.assertEqual(MMSP_HEADER.size, 40)
        magic, version, flags, count, *_rest = MMSP_HEADER.unpack_from(payload)
        self.assertEqual(magic, b"MMSP")
        self.assertEqual(version, 1)
        self.assertEqual(flags & 1, 1)
        self.assertEqual(count, 20)
        self.assertEqual(len(payload), 40 + count * MMSP_RECORD_BYTES)
        colors = np.frombuffer(payload, dtype=np.uint8, offset=40).reshape(count, 15)[:, 12:15]
        # Quotas ensure all four nearby blocks contribute instead of block 0
        # consuming the complete response budget.
        self.assertEqual(len(np.unique(colors[:, 0])), 4)

    def test_mmso_projects_world_points_to_normalized_panorama_uv(self) -> None:
        xyz = np.asarray(
            [
                [0.0, 10.0, 0.0],  # forward / panorama centre
                [10.0, 0.0, 0.0],  # camera right / quarter turn
                [0.0, 10.0, 10.0],  # above the horizon
            ],
            dtype=np.float64,
        )
        rgb = np.asarray(
            [[255, 0, 0], [0, 255, 0], [0, 0, 255]],
            dtype=np.uint8,
        )
        payload = _build_mmso(
            {
                "origin": [0.0, 0.0, 0.0],
                "direction": [0.0, 1.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, rgb),
            budget=10,
            radius=30.0,
            cell_size_px=1,
            yaw_offset_deg=0.0,
            pitch_offset_deg=0.0,
        )

        magic, version, flags, count, *_bounds = MMSO_HEADER.unpack_from(payload)
        self.assertEqual(magic, b"MMSO")
        self.assertEqual(version, 1)
        self.assertEqual(flags & 3, 3)
        self.assertEqual(count, 3)
        self.assertEqual(len(payload), MMSO_HEADER.size + count * MMSO_RECORD_BYTES)
        records = np.frombuffer(
            payload,
            dtype=np.dtype([("uvd", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSO_HEADER.size,
        )
        by_color = {tuple(row["rgb"]): row["uvd"] for row in records}
        np.testing.assert_allclose(by_color[(255, 0, 0)][:2], [0.5, 0.5], atol=1e-6)
        np.testing.assert_allclose(by_color[(0, 255, 0)][:2], [0.75, 0.5], atol=1e-6)
        self.assertLess(float(by_color[(0, 0, 255)][1]), 0.5)

    def test_mmso_offsets_follow_operator_image_space_signs(self) -> None:
        xyz = np.asarray([[0.0, 10.0, 0.0]], dtype=np.float64)
        payload = _build_mmso(
            {
                "origin": [0.0, 0.0, 0.0],
                "direction": [0.0, 1.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, [[10, 20, 30]]),
            budget=10,
            radius=30.0,
            cell_size_px=1,
            yaw_offset_deg=36.0,
            pitch_offset_deg=18.0,
        )
        record = np.frombuffer(
            payload,
            dtype=np.dtype([("uvd", "<f4", (3,)), ("rgb", "u1", (3,))]),
            count=1,
            offset=MMSO_HEADER.size,
        )[0]
        self.assertGreater(float(record["uvd"][0]), 0.5)
        self.assertGreater(float(record["uvd"][1]), 0.5)

    def test_mmso_screen_cell_reducer_keeps_nearest_point(self) -> None:
        xyz = np.asarray([[0.0, 5.0, 0.0], [0.0, 10.0, 0.0]], dtype=np.float64)
        payload = _build_mmso(
            {
                "origin": [0.0, 0.0, 0.0],
                "direction": [0.0, 1.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, [[1, 2, 3], [4, 5, 6]]),
            budget=10,
            radius=30.0,
            cell_size_px=3,
            yaw_offset_deg=0.0,
            pitch_offset_deg=0.0,
        )
        _magic, _version, _flags, count, *_bounds = MMSO_HEADER.unpack_from(payload)
        self.assertEqual(count, 1)
        record = np.frombuffer(
            payload,
            dtype=np.dtype([("uvd", "<f4", (3,)), ("rgb", "u1", (3,))]),
            count=1,
            offset=MMSO_HEADER.size,
        )[0]
        self.assertAlmostEqual(float(record["uvd"][2]), 5.0)
        np.testing.assert_array_equal(record["rgb"], [1, 2, 3])

    def test_task_point_fingerprint_changes_when_pose_is_corrected(self) -> None:
        first = {
            "origin": [1.0, 2.0, 3.0],
            "direction": [0.0, 1.0, 0.0],
            "up": [0.0, 0.0, 1.0],
            "job_name": "Job_A",
            "track_name": "TRACK01",
        }
        corrected = {**first, "origin": [1.1, 2.0, 3.0]}
        self.assertNotEqual(
            _task_point_fingerprint(first),
            _task_point_fingerprint(corrected),
        )

    def test_web_preview_uses_pipeline_panorama_alignment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.yaml"
            config_path.write_text(
                "panorama_alignment:\n"
                "  panorama_yaw_offset_deg: 0.1534\n"
                "  panorama_pitch_offset_deg: -0.25\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _panorama_alignment_defaults(config_path),
                (0.1534, -0.25),
            )

    def test_panorama_point_endpoint_streams_mmso_for_ready_frame(self) -> None:
        from mms_shp_detection.webapp import create_app

        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            (root / "delivery").mkdir()
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            now = "2026-01-01T00:00:00+00:00"
            dataset_id = "d_overlay"
            frame_id = "f_overlay"
            root_id = app.state.storage_roots[0].id
            task = {
                "origin": [0.0, 0.0, 0.0],
                "direction": [0.0, 1.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            }
            app.state.store.upsert_scanning_dataset(
                dataset_id=dataset_id,
                name="overlay",
                root_id=root_id,
                relative_path="delivery",
                crs="EPSG:4326",
                now=now,
            )
            app.state.store.finish_dataset_scan(
                dataset_id,
                frames=[
                    {
                        "id": frame_id,
                        "ordinal": 0,
                        "track_id": "track",
                        "task": task,
                        "longitude": 0.0,
                        "latitude": 0.0,
                        "altitude": 0.0,
                        "heading": 0.0,
                    }
                ],
                tracks=[{"id": "track", "name": "TRACK01", "frame_count": 1}],
                bbox=[0.0, 0.0, 0.0, 0.0],
                warnings=[],
                now=now,
            )
            xyz = np.asarray([[0.0, 10.0, 0.0]], dtype=np.float64)
            app.state.catalogs[dataset_id] = _static_catalog(xyz)
            app.state.point_reader.close()
            app.state.point_reader = _StaticReader(xyz, [[7, 8, 9]])

            with TestClient(app) as client:
                response = client.get(
                    f"/api/datasets/{dataset_id}/panorama-points/{frame_id}",
                    params={"budget": 1000, "yaw_offset_deg": 0.0},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(
                    response.headers["content-type"].startswith("application/vnd.mmso")
                )
                magic, version, flags, count, *_bounds = MMSO_HEADER.unpack_from(
                    response.content
                )
                self.assertEqual((magic, version, flags, count), (b"MMSO", 1, 3, 1))
                cached = client.get(
                    f"/api/datasets/{dataset_id}/panorama-points/{frame_id}",
                    params={"budget": 1000, "yaw_offset_deg": 0.0},
                    headers={"If-None-Match": response.headers["etag"]},
                )
                self.assertEqual(cached.status_code, 304)


if __name__ == "__main__":
    unittest.main()
