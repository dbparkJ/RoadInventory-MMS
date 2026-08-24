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
        POINT_PREVIEW_CACHE_LIMIT_BYTES,
        POINT_PREVIEW_DEFAULT_BUDGET,
        POINT_PREVIEW_DENSE_RADIUS_M,
        POINT_PREVIEW_FOCUS_BUDGET_FRACTION,
        POINT_PREVIEW_FOCUS_CORRIDOR_RADIUS_M,
        POINT_PREVIEW_FOCUS_RADIUS_M,
        POINT_PREVIEW_MAX_BUDGET,
        POINT_PREVIEW_MAX_FOCUS_CORRIDOR_RADIUS_M,
        POINT_PREVIEW_MAX_FOCUS_COUNT,
        POINT_PREVIEW_MAX_PAYLOAD_BYTES,
        POINT_PREVIEW_MAX_RADIUS_M,
        POINT_PREVIEW_MIN_BUDGET,
        VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES,
        VWORLD_ADDRESS_MAX_INFLIGHT,
        _address_inflight_is_full,
        _bounded_preview_candidates,
        _build_mmso,
        _build_mmsp,
        _enforce_file_cache_quota,
        _finish_preview_after_request_cancel,
        _focus_roi_mask,
        _prune_address_failure_cache,
        _remember_address_failure,
        _task_point_fingerprint,
        _vworld_reverse_geocode,
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
    def __init__(self, xyz, rgb, intensity=None, classification=None):
        self.xyz = np.asarray(xyz, dtype=np.float64)
        self.rgb = np.asarray(rgb, dtype=np.uint8)
        self.intensity = np.asarray(
            np.zeros(self.xyz.shape[0], dtype=np.uint16)
            if intensity is None
            else intensity,
            dtype=np.uint16,
        )
        self.classification = np.asarray(
            np.full(self.xyz.shape[0], -1, dtype=np.int16)
            if classification is None
            else classification,
            dtype=np.int16,
        )

    def read_block_points(self, _point_file, _block):
        return (
            self.xyz.copy(),
            self.rgb.copy(),
            self.intensity.copy(),
        )

    def read_block_records(self, _point_file, _block):
        return {
            "xyz": self.xyz.copy(),
            "rgb": self.rgb.copy(),
            "intensity": self.intensity.copy(),
            "classification": self.classification.copy(),
        }

    def close(self):
        return None


class _PerBlockReader:
    def __init__(self, blocks):
        self.blocks = blocks

    def read_block_points(self, _point_file, block):
        xyz, rgb = self.blocks[block["name"]]
        return (
            np.asarray(xyz, dtype=np.float64).copy(),
            np.asarray(rgb, dtype=np.uint8).copy(),
            np.zeros(len(xyz), dtype=np.uint16),
        )


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
    def test_focus_blocks_are_promoted_before_the_preview_block_cap(self) -> None:
        candidates = [
            (
                float(index),
                {"path": f"cloud-{index}.las"},
                {
                    "name": f"block-{index}",
                    "min": [0.0, 0.0, 0.0],
                    "max": [0.5, 0.5, 1.0],
                },
            )
            for index in range(33)
        ]
        focused = (
            33.0,
            {"path": "selected.las"},
            {
                "name": "selected-block",
                "min": [19.5, -0.5, 0.0],
                "max": [20.5, 0.5, 5.0],
            },
        )
        candidates.append(focused)

        bounded = _bounded_preview_candidates(
            candidates,
            focus_centers=(np.asarray([20.0, 0.0]),),
        )

        self.assertEqual(len(bounded), 32)
        self.assertIs(bounded[0], focused)
        self.assertIn(focused, bounded)

    def test_multi_focus_corridors_radiate_from_selected_anchor(self) -> None:
        xyz = np.asarray(
            [
                [2.0, 0.0, 0.0],
                [0.0, 2.0, 0.0],
                [2.0, 2.0, 0.0],
            ],
            dtype=np.float64,
        )
        mask = _focus_roi_mask(
            xyz,
            (
                np.asarray([0.0, 0.0]),
                np.asarray([4.0, 0.0]),
                np.asarray([0.0, 4.0]),
            ),
            focus_radius_squared=0.25**2,
            corridor_radius_squared=0.5**2,
        )

        self.assertEqual(mask.tolist(), [True, True, False])

    def test_address_inflight_backlog_is_bounded_and_prunes_completed_tasks(self) -> None:
        class _Task:
            def __init__(self, complete: bool = False) -> None:
                self.complete = complete

            def done(self) -> bool:
                return self.complete

        inflight = {
            f"pending-{index}": _Task()
            for index in range(VWORLD_ADDRESS_MAX_INFLIGHT)
        }
        inflight["completed"] = _Task(complete=True)
        self.assertTrue(_address_inflight_is_full(inflight))  # type: ignore[arg-type]
        self.assertNotIn("completed", inflight)
        inflight.pop("pending-0")
        self.assertFalse(_address_inflight_is_full(inflight))  # type: ignore[arg-type]

    def test_address_failure_cache_is_expiring_and_bounded(self) -> None:
        failures = {
            "expired": 9.0,
            **{
                f"active-{index}": 20.0 + index
                for index in range(VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES + 10)
            },
        }
        _prune_address_failure_cache(failures, 10.0)
        self.assertNotIn("expired", failures)
        self.assertEqual(len(failures), VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES)
        self.assertNotIn("active-0", failures)
        self.assertIn(
            f"active-{VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES + 9}", failures
        )
        _remember_address_failure(failures, "newest", 11.0)
        self.assertEqual(len(failures), VWORLD_ADDRESS_FAILURE_CACHE_MAX_ENTRIES)
        self.assertIn("newest", failures)

    def test_vworld_reverse_geocode_prefers_road_then_falls_back_to_parcel(self) -> None:
        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit: int) -> bytes:
                return self.payload

        responses = [
            _Response(b'{"response":{"status":"NOT_FOUND"}}'),
            _Response(
                '{"response":{"status":"OK","result":{"type":"parcel",'
                '"text":"서울특별시 송파구 잠실동 10-1","zipcode":"05500"}}}'
                .encode()
            ),
        ]
        with mock.patch(
            "mms_shp_detection.webapp.media.urlopen", side_effect=responses
        ) as request:
            result = _vworld_reverse_geocode(127.0731, 37.5128, "development-key")
        self.assertEqual(result["address_type"], "parcel")
        self.assertEqual(result["zipcode"], "05500")
        self.assertIn("type=ROAD", request.call_args_list[0].args[0].full_url)
        self.assertIn("type=PARCEL", request.call_args_list[1].args[0].full_url)

    def test_point_preview_budget_and_cache_bounds_cover_one_million_payloads(self) -> None:
        self.assertEqual(POINT_PREVIEW_DEFAULT_BUDGET, 250_000)
        self.assertEqual(POINT_PREVIEW_MIN_BUDGET, 250_000)
        self.assertEqual(POINT_PREVIEW_MAX_BUDGET, 1_000_000)
        self.assertEqual(
            POINT_PREVIEW_MAX_PAYLOAD_BYTES,
            MMSP_HEADER.size + 1_000_000 * MMSP_RECORD_BYTES,
        )
        self.assertGreater(
            POINT_PREVIEW_CACHE_LIMIT_BYTES,
            POINT_PREVIEW_MAX_PAYLOAD_BYTES,
        )

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

    def test_point_preview_cache_quota_removes_oldest_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            paths = [directory / f"{name}.mmsp" for name in ("old", "middle", "keep")]
            timestamp = time.time_ns()
            for index, path in enumerate(paths, start=1):
                path.write_bytes(b"1234")
                modified = timestamp - (4 - index) * 2_000_000_000
                os.utime(path, ns=(modified, modified))

            _enforce_file_cache_quota(
                directory,
                suffix=".mmsp",
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

    def test_mmsp_uses_dense_15m_and_sparse_15_to_25m_bands(self) -> None:
        inner = np.column_stack(
            (np.linspace(1.0, 14.0, 1_000), np.zeros(1_000), np.zeros(1_000))
        )
        outer = np.column_stack(
            (np.linspace(16.0, 24.0, 1_000), np.zeros(1_000), np.zeros(1_000))
        )
        beyond = np.column_stack(
            (np.linspace(26.0, 35.0, 100), np.zeros(100), np.zeros(100))
        )
        xyz = np.concatenate((inner, outer, beyond))
        rgb = np.full((xyz.shape[0], 3), 120, dtype=np.uint8)
        payload = _build_mmsp(
            {
                "origin": [0.0, 0.0, 0.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, rgb),
            budget=100,
        )
        _magic, _version, _flags, count, *_bounds = MMSP_HEADER.unpack_from(payload)
        records = np.frombuffer(
            payload,
            dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSP_HEADER.size,
        )
        distances = np.linalg.norm(records["xyz"], axis=1)
        self.assertEqual(count, 100)
        self.assertEqual(
            int(np.count_nonzero(distances <= POINT_PREVIEW_DENSE_RADIUS_M)), 75
        )
        self.assertEqual(
            int(
                np.count_nonzero(
                    (distances > POINT_PREVIEW_DENSE_RADIUS_M)
                    & (distances <= POINT_PREVIEW_MAX_RADIUS_M)
                )
            ),
            25,
        )
        self.assertLessEqual(float(distances.max()), POINT_PREVIEW_MAX_RADIUS_M)

    def test_mmsp_redistributes_unused_dense_band_budget_to_sparse_band(self) -> None:
        inner = np.column_stack(
            (np.linspace(1.0, 10.0, 10), np.zeros(10), np.zeros(10))
        )
        outer = np.column_stack(
            (np.linspace(16.0, 24.0, 200), np.zeros(200), np.zeros(200))
        )
        xyz = np.concatenate((inner, outer))
        payload = _build_mmsp(
            {
                "origin": [0.0, 0.0, 0.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, np.full((xyz.shape[0], 3), 80, dtype=np.uint8)),
            budget=100,
        )
        _magic, _version, _flags, count, *_bounds = MMSP_HEADER.unpack_from(payload)
        records = np.frombuffer(
            payload,
            dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSP_HEADER.size,
        )
        distances = np.linalg.norm(records["xyz"], axis=1)
        self.assertEqual(count, 100)
        self.assertEqual(int(np.count_nonzero(distances <= 15.0)), 10)
        self.assertEqual(int(np.count_nonzero(distances > 15.0)), 90)

    def test_mmsp_focus_prioritizes_xyz_roi_deterministically(self) -> None:
        focus = np.column_stack(
            (
                np.linspace(9.0, 11.0, 1_000),
                np.zeros(1_000),
                np.full(1_000, 2.0),
            )
        )
        background = np.column_stack(
            (
                np.linspace(-24.0, 0.0, 1_000),
                np.full(1_000, 0.5),
                np.zeros(1_000),
            )
        )
        xyz = np.concatenate((focus, background))
        rgb = np.full((xyz.shape[0], 3), 90, dtype=np.uint8)
        arguments = (
            {
                "origin": [0.0, 0.0, 0.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, rgb),
        )
        first = _build_mmsp(
            *arguments,
            budget=100,
            focus_center=np.asarray([10.0, 0.0, 2.0]),
        )
        second = _build_mmsp(
            *arguments,
            budget=100,
            focus_center=np.asarray([10.0, 0.0, 2.0]),
        )

        self.assertEqual(first, second)
        _magic, _version, _flags, count, *_bounds = MMSP_HEADER.unpack_from(first)
        records = np.frombuffer(
            first,
            dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSP_HEADER.size,
        )
        focus_distances = np.linalg.norm(
            records["xyz"] - np.asarray([10.0, 0.0, 2.0]), axis=1
        )
        self.assertEqual(count, 100)
        self.assertEqual(
            int(np.count_nonzero(focus_distances <= POINT_PREVIEW_FOCUS_RADIUS_M)),
            round(100 * POINT_PREVIEW_FOCUS_BUDGET_FRACTION),
        )

    def test_mmsp_multi_focus_prioritizes_union_and_ordered_corridor(self) -> None:
        first_focus = np.column_stack(
            (
                np.linspace(5.5, 6.5, 400),
                np.zeros(400),
                np.linspace(0.0, 4.0, 400),
            )
        )
        corridor = np.column_stack(
            (
                np.linspace(10.0, 14.0, 400),
                np.full(400, 0.5),
                np.linspace(0.0, 4.0, 400),
            )
        )
        second_focus = np.column_stack(
            (
                np.linspace(17.5, 18.5, 400),
                np.zeros(400),
                np.linspace(0.0, 4.0, 400),
            )
        )
        background = np.column_stack(
            (
                np.linspace(-20.0, -5.0, 1_200),
                np.full(1_200, 4.0),
                np.zeros(1_200),
            )
        )
        xyz = np.concatenate((first_focus, corridor, second_focus, background))
        rgb = np.concatenate(
            tuple(
                np.tile(np.asarray(color, dtype=np.uint8), (points.shape[0], 1))
                for points, color in (
                    (first_focus, [240, 20, 20]),
                    (corridor, [20, 240, 20]),
                    (second_focus, [20, 20, 240]),
                    (background, [90, 90, 90]),
                )
            )
        )
        arguments = (
            {
                "origin": [0.0, 0.0, 0.0],
                "job_name": "Job_A",
                "track_name": "TRACK01",
            },
            _static_catalog(xyz),
            _StaticReader(xyz, rgb),
        )
        centers = (np.asarray([6.0, 0.0]), np.asarray([18.0, 0.0]))
        first = _build_mmsp(
            *arguments,
            budget=100,
            focus_centers=centers,
            focus_corridor_radius=POINT_PREVIEW_FOCUS_CORRIDOR_RADIUS_M,
        )
        second = _build_mmsp(
            *arguments,
            budget=100,
            focus_centers=centers,
            focus_corridor_radius=POINT_PREVIEW_FOCUS_CORRIDOR_RADIUS_M,
        )

        self.assertEqual(first, second)
        records = np.frombuffer(
            first,
            dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSP_HEADER.size,
        )
        first_distance = np.linalg.norm(
            records["xyz"][:, :2] - centers[0][None, :], axis=1
        )
        second_distance = np.linalg.norm(
            records["xyz"][:, :2] - centers[1][None, :], axis=1
        )
        relation_distance = np.abs(records["xyz"][:, 1])
        on_relation = (
            (records["xyz"][:, 0] >= centers[0][0])
            & (records["xyz"][:, 0] <= centers[1][0])
            & (relation_distance <= POINT_PREVIEW_FOCUS_CORRIDOR_RADIUS_M)
        )
        in_union = (
            (first_distance <= POINT_PREVIEW_FOCUS_RADIUS_M)
            | (second_distance <= POINT_PREVIEW_FOCUS_RADIUS_M)
            | on_relation
        )
        self.assertEqual(
            int(np.count_nonzero(in_union)),
            round(100 * POINT_PREVIEW_FOCUS_BUDGET_FRACTION),
        )
        self.assertGreater(int(np.count_nonzero(first_distance <= 1.0)), 0)
        self.assertGreater(int(np.count_nonzero(second_distance <= 1.0)), 0)
        self.assertGreater(
            int(
                np.count_nonzero(
                    (records["xyz"][:, 0] >= 11.0)
                    & (records["xyz"][:, 0] <= 13.0)
                    & on_relation
                )
            ),
            0,
        )
        sampled_colors = {tuple(color) for color in records["rgb"].tolist()}
        self.assertTrue(
            {(240, 20, 20), (20, 240, 20), (20, 20, 240)} <= sampled_colors
        )

    def test_mmsp_xy_focus_keeps_vertical_pole_dense_and_background_blocks_visible(
        self,
    ) -> None:
        files = []
        blocks = {}
        colors = ([240, 20, 20], [20, 240, 20], [20, 20, 240], [240, 240, 20])
        for index, color in enumerate(colors):
            if index == 0:
                pole = np.column_stack(
                    (
                        np.full(200, 10.0),
                        np.zeros(200),
                        np.linspace(0.0, 20.0, 200),
                    )
                )
                background = np.column_stack(
                    (
                        np.full(100, -3.0),
                        np.linspace(-2.0, 2.0, 100),
                        np.zeros(100),
                    )
                )
                xyz = np.concatenate((pole, background))
            else:
                xyz = np.column_stack(
                    (
                        np.full(100, -5.0 * index),
                        np.linspace(-2.0, 2.0, 100),
                        np.zeros(100),
                    )
                )
            rgb = np.tile(np.asarray(color, dtype=np.uint8), (xyz.shape[0], 1))
            name = f"block{index}"
            blocks[name] = (xyz, rgb)
            files.append(
                {
                    "path": f"cloud{index}.las",
                    "source_type": "las",
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                    "file_min": xyz.min(axis=0).tolist(),
                    "file_max": xyz.max(axis=0).tolist(),
                    "blocks": [
                        {
                            "name": name,
                            "source_type": "las",
                            "start": 0,
                            "count": int(xyz.shape[0]),
                            "min": xyz.min(axis=0).tolist(),
                            "max": xyz.max(axis=0).tolist(),
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
            _PerBlockReader(blocks),
            budget=100,
            focus_center=np.asarray([10.0, 0.0]),
        )
        records = np.frombuffer(
            payload,
            dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
            offset=MMSP_HEADER.size,
        )
        xy_distances = np.linalg.norm(
            records["xyz"][:, :2] - np.asarray([10.0, 0.0]), axis=1
        )
        self.assertEqual(
            int(np.count_nonzero(xy_distances <= POINT_PREVIEW_FOCUS_RADIUS_M)),
            round(100 * POINT_PREVIEW_FOCUS_BUDGET_FRACTION),
        )
        self.assertGreater(float(records["xyz"][:, 2].max()), 15.0)
        self.assertEqual(len(np.unique(records["rgb"], axis=0)), len(colors))

    def test_mmsp_without_focus_preserves_legacy_sampling_bytes(self) -> None:
        xyz = np.column_stack(
            (np.linspace(1.0, 24.0, 500), np.zeros(500), np.zeros(500))
        )
        reader = _StaticReader(xyz, np.full((500, 3), 70, dtype=np.uint8))
        task = {
            "origin": [0.0, 0.0, 0.0],
            "job_name": "Job_A",
            "track_name": "TRACK01",
        }
        catalog = _static_catalog(xyz)
        legacy = _build_mmsp(task, catalog, reader, budget=100)
        explicit_none = _build_mmsp(
            task,
            catalog,
            reader,
            budget=100,
            focus_center=None,
        )
        self.assertEqual(explicit_none, legacy)

    def test_mmsp_focus_preserves_all_color_modes(self) -> None:
        focus = np.column_stack(
            (np.linspace(7.5, 12.5, 200), np.zeros(200), np.linspace(0.0, 4.0, 200))
        )
        background = np.column_stack(
            (np.linspace(-20.0, -5.0, 200), np.ones(200), np.zeros(200))
        )
        xyz = np.concatenate((focus, background))
        rgb = np.tile(np.asarray([[12, 34, 56]], dtype=np.uint8), (400, 1))
        intensity = np.linspace(0, 4_000, 400, dtype=np.uint16)
        classification = np.resize(np.asarray([2, 5, -1], dtype=np.int16), 400)
        reader = _StaticReader(xyz, rgb, intensity, classification)
        catalog = _static_catalog(xyz)
        task = {
            "origin": [0.0, 0.0, 0.0],
            "job_name": "Job_A",
            "track_name": "TRACK01",
        }

        rendered = {}
        for mode in ("rgb", "intensity", "classification", "height"):
            payload = _build_mmsp(
                task,
                catalog,
                reader,
                budget=100,
                focus_center=np.asarray([10.0, 0.0]),
                color_mode=mode,
            )
            _magic, _version, _flags, count, *_bounds = MMSP_HEADER.unpack_from(payload)
            self.assertEqual(count, 100)
            rendered[mode] = np.frombuffer(
                payload,
                dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
                offset=MMSP_HEADER.size,
            )["rgb"]

        self.assertTrue(np.all(rendered["rgb"] == [12, 34, 56]))
        self.assertGreater(len(np.unique(rendered["intensity"], axis=0)), 4)
        self.assertGreater(len(np.unique(rendered["classification"], axis=0)), 2)
        self.assertGreater(len(np.unique(rendered["height"], axis=0)), 4)

    def test_mmsp_color_derivatives_use_source_attributes_and_keep_v1_layout(self) -> None:
        xyz = np.column_stack(
            (np.linspace(1.0, 10.0, 20), np.zeros(20), np.linspace(0.0, 5.0, 20))
        )
        rgb = np.tile(np.asarray([[12, 34, 56]], dtype=np.uint8), (20, 1))
        intensity = np.linspace(0, 4_000, 20, dtype=np.uint16)
        classification = np.resize(np.asarray([2, 5, -1], dtype=np.int16), 20)
        reader = _StaticReader(xyz, rgb, intensity, classification)
        catalog = _static_catalog(xyz)

        rendered: dict[str, np.ndarray] = {}
        for mode in ("rgb", "intensity", "classification", "height"):
            payload = _build_mmsp(
                {
                    "origin": [0.0, 0.0, 0.0],
                    "job_name": "Job_A",
                    "track_name": "TRACK01",
                },
                catalog,
                reader,
                budget=20,
                color_mode=mode,
            )
            magic, version, _flags, count, *_bounds = MMSP_HEADER.unpack_from(payload)
            self.assertEqual((magic, version, count), (b"MMSP", 1, 20))
            self.assertEqual(len(payload), MMSP_HEADER.size + 20 * MMSP_RECORD_BYTES)
            rendered[mode] = np.frombuffer(
                payload,
                dtype=np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))]),
                offset=MMSP_HEADER.size,
            )["rgb"]

        np.testing.assert_array_equal(rendered["rgb"], rgb)
        self.assertTrue(np.all(rendered["intensity"][:, 0] == rendered["intensity"][:, 1]))
        self.assertGreater(int(rendered["intensity"][-1, 0]), int(rendered["intensity"][0, 0]))
        self.assertGreater(len(np.unique(rendered["classification"], axis=0)), 2)
        self.assertGreater(len(np.unique(rendered["height"], axis=0)), 4)

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

    def test_panorama_derivative_is_reused_from_server_disk_after_restart(self) -> None:
        from PIL import Image

        from mms_shp_detection.webapp import create_app
        from mms_shp_detection.webapp import media as media_module

        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            delivery = root / "delivery"
            delivery.mkdir()
            source = delivery / "frame-a.jpg"
            Image.new("RGB", (1024, 512), color=(24, 48, 72)).save(
                source,
                format="JPEG",
            )
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            now = "2026-01-01T00:00:00+00:00"
            dataset_id = "d_panorama_cache"
            frame_id = "f_panorama_cache"
            app.state.store.upsert_scanning_dataset(
                dataset_id=dataset_id,
                name="panorama cache",
                root_id=app.state.storage_roots[0].id,
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
                        "task": {
                            "image_path": str(source),
                            "image_name": source.name,
                        },
                        "longitude": 127.0,
                        "latitude": 37.0,
                        "altitude": 0.0,
                        "heading": 0.0,
                    }
                ],
                tracks=[{"id": "track", "name": "TRACK01", "frame_count": 1}],
                bbox=[127.0, 37.0, 127.0, 37.0],
                warnings=[],
                now=now,
            )
            url = f"/api/datasets/{dataset_id}/panoramas/{frame_id}?width=512"
            with (
                mock.patch.object(
                    media_module,
                    "_resize_panorama",
                    wraps=media_module._resize_panorama,
                ) as resize,
                TestClient(app) as client,
            ):
                first = client.get(url)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertIn(first.headers["content-type"], {"image/webp", "image/jpeg"})
                self.assertIn("immutable", first.headers["cache-control"])
                cached = client.get(url, headers={"If-None-Match": first.headers["etag"]})
                self.assertEqual(cached.status_code, 304)
                self.assertEqual(resize.call_count, 1)

            derivatives = list(
                (state / "media" / "panoramas" / dataset_id).glob("*")
            )
            self.assertEqual(len(derivatives), 1)
            self.assertTrue(derivatives[0].is_file())

            restarted = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with (
                mock.patch.object(
                    media_module,
                    "_resize_panorama",
                    side_effect=AssertionError("persistent cache must be reused"),
                ),
                TestClient(restarted) as client,
            ):
                after_restart = client.get(url)
            self.assertEqual(after_restart.status_code, 200, after_restart.text)
            self.assertEqual(after_restart.headers["etag"], first.headers["etag"])

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
                point_response = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={"budget": POINT_PREVIEW_MAX_BUDGET},
                )
                self.assertEqual(point_response.status_code, 200, point_response.text)
                self.assertEqual(point_response.headers["x-mmsp-color-mode"], "rgb")
                self.assertTrue(
                    point_response.headers["content-type"].startswith(
                        "application/vnd.mmsp"
                    )
                )
                point_header = MMSP_HEADER.unpack_from(point_response.content)
                self.assertEqual(point_header[:4], (b"MMSP", 1, 1, 1))
                over_budget = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={"budget": POINT_PREVIEW_MAX_BUDGET + 1},
                )
                self.assertEqual(over_budget.status_code, 422, over_budget.text)
                under_budget = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={"budget": POINT_PREVIEW_MIN_BUDGET - 1},
                )
                self.assertEqual(under_budget.status_code, 422, under_budget.text)
                invalid_color = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MIN_BUDGET,
                        "color_mode": "synthetic",
                    },
                )
                self.assertEqual(invalid_color.status_code, 422, invalid_color.text)

                focused = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MAX_BUDGET,
                        "focus_x": 0.0,
                        "focus_y": 10.0,
                    },
                )
                self.assertEqual(focused.status_code, 200, focused.text)
                self.assertNotEqual(focused.headers["etag"], point_response.headers["etag"])
                focused_cached = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MAX_BUDGET,
                        "focus_x": 0.0,
                        "focus_y": 10.0,
                    },
                    headers={"If-None-Match": focused.headers["etag"]},
                )
                self.assertEqual(focused_cached.status_code, 304)
                focused_xyz = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MAX_BUDGET,
                        "focus_x": 0.0,
                        "focus_y": 10.0,
                        "focus_z": 0.0,
                    },
                )
                self.assertEqual(focused_xyz.status_code, 200, focused_xyz.text)
                self.assertNotEqual(focused_xyz.headers["etag"], focused.headers["etag"])
                legacy_with_unused_corridor = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MAX_BUDGET,
                        "focus_x": 0.0,
                        "focus_y": 10.0,
                        "focus_corridor_radius": 2.0,
                    },
                )
                self.assertEqual(
                    legacy_with_unused_corridor.headers["etag"],
                    focused.headers["etag"],
                )
                no_focus_with_unused_corridor = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params={
                        "budget": POINT_PREVIEW_MAX_BUDGET,
                        "focus_corridor_radius": 2.0,
                    },
                )
                self.assertEqual(
                    no_focus_with_unused_corridor.headers["etag"],
                    point_response.headers["etag"],
                )

                multi_focus_params = [
                    ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                    ("focus", "0,10"),
                    ("focus", "0,18"),
                    ("focus_corridor_radius", "1.5"),
                ]
                multi_focused = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=multi_focus_params,
                )
                self.assertEqual(multi_focused.status_code, 200, multi_focused.text)
                self.assertNotEqual(
                    multi_focused.headers["etag"], focused.headers["etag"]
                )
                multi_focused_cached = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=multi_focus_params,
                    headers={"If-None-Match": multi_focused.headers["etag"]},
                )
                self.assertEqual(multi_focused_cached.status_code, 304)
                reversed_focus = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=[
                        ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                        ("focus", "0,18"),
                        ("focus", "0,10"),
                        ("focus_corridor_radius", "1.5"),
                    ],
                )
                self.assertEqual(reversed_focus.status_code, 200, reversed_focus.text)
                self.assertNotEqual(
                    reversed_focus.headers["etag"], multi_focused.headers["etag"]
                )
                changed_focus = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=[
                        ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                        ("focus", "0,10"),
                        ("focus", "0,17"),
                        ("focus_corridor_radius", "1.5"),
                    ],
                )
                self.assertEqual(changed_focus.status_code, 200, changed_focus.text)
                self.assertNotEqual(
                    changed_focus.headers["etag"], multi_focused.headers["etag"]
                )
                changed_corridor = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=[
                        ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                        ("focus", "0,10"),
                        ("focus", "0,18"),
                        ("focus_corridor_radius", "2"),
                    ],
                )
                self.assertEqual(changed_corridor.status_code, 200, changed_corridor.text)
                self.assertNotEqual(
                    changed_corridor.headers["etag"], multi_focused.headers["etag"]
                )
                for invalid_focus in (
                    {"focus_x": 0.0},
                    {"focus_z": 0.0},
                    {"focus_x": "nan", "focus_y": 0.0},
                    {"focus_x": "inf", "focus_y": 0.0},
                    {"focus_x": 26.0, "focus_y": 0.0},
                    {"focus_x": 1_000_000_001.0, "focus_y": 0.0},
                ):
                    invalid_focus_response = client.get(
                        f"/api/datasets/{dataset_id}/points/{frame_id}",
                        params={
                            "budget": POINT_PREVIEW_MAX_BUDGET,
                            **invalid_focus,
                        },
                    )
                    self.assertEqual(
                        invalid_focus_response.status_code,
                        422,
                        invalid_focus_response.text,
                    )
                for invalid_repeated_focus in (
                    "0",
                    "0,10,0,1",
                    "word,10",
                    "nan,10",
                    "inf,10",
                    "0,26",
                    "1000000001,0",
                ):
                    invalid_focus_response = client.get(
                        f"/api/datasets/{dataset_id}/points/{frame_id}",
                        params={
                            "budget": POINT_PREVIEW_MAX_BUDGET,
                            "focus": invalid_repeated_focus,
                        },
                    )
                    self.assertEqual(
                        invalid_focus_response.status_code,
                        422,
                        invalid_focus_response.text,
                    )
                mixed_dimensions = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=[
                        ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                        ("focus", "0,10"),
                        ("focus", "0,18,0"),
                    ],
                )
                self.assertEqual(mixed_dimensions.status_code, 422)
                too_many_focuses = client.get(
                    f"/api/datasets/{dataset_id}/points/{frame_id}",
                    params=[
                        ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                        *[
                            ("focus", f"0,{10 + index}")
                            for index in range(POINT_PREVIEW_MAX_FOCUS_COUNT + 1)
                        ],
                    ],
                )
                self.assertEqual(too_many_focuses.status_code, 422)
                for invalid_corridor_radius in (
                    -0.1,
                    POINT_PREVIEW_MAX_FOCUS_CORRIDOR_RADIUS_M + 0.1,
                    "nan",
                    "inf",
                ):
                    invalid_corridor = client.get(
                        f"/api/datasets/{dataset_id}/points/{frame_id}",
                        params=[
                            ("budget", str(POINT_PREVIEW_MAX_BUDGET)),
                            ("focus", "0,10"),
                            ("focus", "0,18"),
                            ("focus_corridor_radius", str(invalid_corridor_radius)),
                        ],
                    )
                    self.assertEqual(
                        invalid_corridor.status_code,
                        422,
                        invalid_corridor.text,
                    )

                projection = client.get(
                    f"/api/datasets/{dataset_id}/frames/{frame_id}/panorama-projection",
                    params={"yaw_offset_deg": 0.0, "pitch_offset_deg": 0.0},
                )
                self.assertEqual(projection.status_code, 200, projection.text)
                self.assertEqual(projection.json()["origin"], [0.0, 0.0, 0.0])
                self.assertEqual(projection.json()["forward"], [0.0, 1.0, 0.0])
                self.assertEqual(projection.json()["right"], [1.0, 0.0, 0.0])
                self.assertEqual(projection.json()["up"], [0.0, 0.0, 1.0])
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

    def test_frame_address_prefers_metadata_without_persisting_vworld_result(self) -> None:
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
            dataset_id = "d_address"
            root_id = app.state.storage_roots[0].id
            app.state.store.upsert_scanning_dataset(
                dataset_id=dataset_id,
                name="address",
                root_id=root_id,
                relative_path="delivery",
                crs="EPSG:4326",
                now=now,
            )
            app.state.store.finish_dataset_scan(
                dataset_id,
                frames=[
                    {
                        "id": "metadata",
                        "ordinal": 0,
                        "track_id": "track",
                        "task": {"road_address": "서울특별시 송파구 올림픽로 1"},
                        "longitude": 127.1,
                        "latitude": 37.5,
                        "altitude": 0.0,
                        "heading": 0.0,
                    },
                    {
                        "id": "vworld",
                        "ordinal": 1,
                        "track_id": "track",
                        "task": {},
                        "longitude": 127.2,
                        "latitude": 37.6,
                        "altitude": 0.0,
                        "heading": 0.0,
                    },
                    {
                        "id": "missing",
                        "ordinal": 2,
                        "track_id": "track",
                        "task": {},
                        "longitude": None,
                        "latitude": None,
                        "altitude": None,
                        "heading": None,
                    },
                ],
                tracks=[{"id": "track", "name": "TRACK01", "frame_count": 3}],
                bbox=[127.1, 37.5, 127.2, 37.6],
                warnings=[],
                now=now,
            )

            with (
                mock.patch(
                    "mms_shp_detection.webapp.media._vworld_reverse_geocode",
                    return_value={
                        "address": "서울특별시 송파구 잠실동 10-1",
                        "address_type": "parcel",
                        "zipcode": "05500",
                    },
                ) as reverse_geocode,
                TestClient(app) as client,
            ):
                metadata = client.get(
                    f"/api/datasets/{dataset_id}/frames/metadata/address"
                )
                self.assertEqual(metadata.status_code, 200, metadata.text)
                self.assertEqual(metadata.json()["source"], "delivery_metadata")
                self.assertEqual(
                    metadata.json()["address"], "서울특별시 송파구 올림픽로 1"
                )
                reverse_geocode.assert_not_called()

                resolved = client.get(
                    f"/api/datasets/{dataset_id}/frames/vworld/address"
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                self.assertEqual(resolved.json()["source"], "vworld")
                self.assertEqual(resolved.json()["address_type"], "parcel")

                repeated = client.get(
                    f"/api/datasets/{dataset_id}/frames/vworld/address"
                )
                self.assertEqual(repeated.status_code, 200, repeated.text)
                self.assertEqual(reverse_geocode.call_count, 2)
                with app.state.store.connection() as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name='frame_addresses'"
                        ).fetchone()
                    )

                missing = client.get(
                    f"/api/datasets/{dataset_id}/frames/missing/address"
                )
                self.assertEqual(missing.status_code, 422, missing.text)

    def test_frame_address_external_failure_returns_coordinate_fallback(self) -> None:
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
            dataset_id = "d_address_failure"
            app.state.store.upsert_scanning_dataset(
                dataset_id=dataset_id,
                name="address failure",
                root_id=app.state.storage_roots[0].id,
                relative_path="delivery",
                crs="EPSG:4326",
                now=now,
            )
            app.state.store.finish_dataset_scan(
                dataset_id,
                frames=[{
                    "id": "frame",
                    "ordinal": 0,
                    "track_id": "track",
                    "task": {},
                    "longitude": 127.2,
                    "latitude": 37.6,
                    "altitude": 0.0,
                    "heading": 0.0,
                }],
                tracks=[{"id": "track", "name": "TRACK01", "frame_count": 1}],
                bbox=[127.2, 37.6, 127.2, 37.6],
                warnings=[],
                now=now,
            )
            with (
                mock.patch(
                    "mms_shp_detection.webapp.media._vworld_reverse_geocode",
                    side_effect=TimeoutError,
                ) as reverse_geocode,
                TestClient(app) as client,
            ):
                first = client.get(
                    f"/api/datasets/{dataset_id}/frames/frame/address"
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertIsNone(first.json()["address"])
                self.assertEqual(first.json()["source"], "coordinate_fallback")
                second = client.get(
                    f"/api/datasets/{dataset_id}/frames/frame/address"
                )
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(reverse_geocode.call_count, 1)


if __name__ == "__main__":
    unittest.main()
