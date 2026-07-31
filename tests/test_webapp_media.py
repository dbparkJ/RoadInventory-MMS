from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import numpy as np

try:
    from mms_shp_detection.webapp.media import (
        MMSP_HEADER,
        MMSP_RECORD_BYTES,
        _build_mmsp,
        _finish_preview_after_request_cancel,
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


@unittest.skipIf(MMSP_IMPORT_ERROR is not None, f"point dependencies missing: {MMSP_IMPORT_ERROR}")
class WebAppMediaTests(unittest.TestCase):
    def test_cancelled_request_does_not_cancel_preview_owner(self) -> None:
        async def exercise() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            finished = asyncio.Event()

            async def work() -> int:
                started.set()
                await release.wait()
                finished.set()
                return 7

            owner = asyncio.create_task(
                _finish_preview_after_request_cancel(
                    work(),
                    logger=mock.Mock(),
                    context="test preview",
                )
            )
            await started.wait()
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            self.assertFalse(finished.is_set())
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=1)

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


if __name__ == "__main__":
    unittest.main()
