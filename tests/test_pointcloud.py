from __future__ import annotations

import math
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import laspy
import numpy as np
from laspy.vlrs.known import WktCoordinateSystemVlr

from mms_shp_detection.pointcloud import (
    PointCloudReaderCache,
    build_pointcloud_catalog,
    match_nearest_pointcloud_files,
    select_candidate_blocks,
)


TEST_WKT = 'LOCAL_CS["Synthetic MMS metres"]'


def _write_las(
    path: Path,
    xyz: np.ndarray,
    *,
    rgb16: np.ndarray | None = None,
    with_wkt: bool = True,
    wkt: str = TEST_WKT,
    scales: tuple[float, float, float] = (0.01, 0.01, 0.01),
    offsets: tuple[float, float, float] = (300_000.0, 4_100_000.0, 100.0),
    gps_time_type: int = 0,
    classification: np.ndarray | None = None,
) -> None:
    point_format = 3 if rgb16 is not None else 0
    header = laspy.LasHeader(point_format=point_format, version="1.2")
    header.scales = np.asarray(scales)
    header.offsets = np.asarray(offsets)
    header.global_encoding.gps_time_type = gps_time_type
    if with_wkt:
        header.vlrs.append(WktCoordinateSystemVlr(wkt))
    las = laspy.LasData(header)
    las.x = xyz[:, 0]
    las.y = xyz[:, 1]
    las.z = xyz[:, 2]
    las.intensity = np.arange(len(xyz), dtype=np.uint16) + 10
    if classification is not None:
        las.classification = np.asarray(classification, dtype=np.uint8)
    if rgb16 is not None:
        las.red = rgb16[:, 0]
        las.green = rgb16[:, 1]
        las.blue = rgb16[:, 2]
    las.write(path)


class PointCloudLasTests(unittest.TestCase):
    def test_catalog_records_exact_las_classification_histograms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "Job_20250311_1043_Track01.las"
            xyz = np.asarray(
                [
                    [300_000.0, 4_100_000.0, 100.0],
                    [300_001.0, 4_100_001.0, 101.0],
                    [300_002.0, 4_100_002.0, 102.0],
                    [300_003.0, 4_100_003.0, 103.0],
                ]
            )
            _write_las(
                path,
                xyz,
                classification=np.asarray([0, 2, 2, 20], dtype=np.uint8),
            )

            catalog = build_pointcloud_catalog(
                root,
                root / "catalog.json",
                source="las",
                las_chunk_size=2,
            )

            expected = {"0": 1, "2": 2, "20": 1}
            self.assertEqual(
                catalog["files"][0]["classification_summary"]["class_counts"],
                expected,
            )
            self.assertEqual(catalog["classification_summary"]["class_counts"], expected)
            self.assertEqual(
                catalog["classification_summary"]["files_with_nonzero_classes"],
                1,
            )

    def test_las_records_carry_source_gps_time_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "Job_20250311_1043_Track01.las"
            xyz = np.asarray(
                [
                    [300_000.0, 4_100_000.0, 100.0],
                    [300_001.0, 4_100_001.0, 101.0],
                ]
            )
            rgb = np.zeros((2, 3), dtype=np.uint16)
            _write_las(path, xyz, rgb16=rgb, gps_time_type=1)

            with PointCloudReaderCache() as readers:
                records = readers.read_block_records(
                    {"path": str(path), "source_type": "las"},
                    {
                        "name": "las:0:2",
                        "source_type": "las",
                        "start": 0,
                        "count": 2,
                    },
                )

            np.testing.assert_array_equal(records["gps_time_type"], [1, 1])

    def test_las_records_mark_gps_time_encoding_unknown_without_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "Job_20250311_1043_Track01.las"
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            # Point format 0 has no GPS time dimension.  A set header bit must
            # not make the absent point attribute appear to have an encoding.
            _write_las(path, xyz, rgb16=None, gps_time_type=1)

            with PointCloudReaderCache() as readers:
                records = readers.read_block_records(
                    {"path": str(path), "source_type": "las"},
                    {
                        "name": "las:0:1",
                        "source_type": "las",
                        "start": 0,
                        "count": 1,
                    },
                )

            self.assertTrue(np.isnan(records["gps_time"][0]))
            np.testing.assert_array_equal(records["gps_time_type"], [-1])

    def test_include_jobs_filters_before_opening_las_and_changes_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            las_root = root / "LAS"
            las_root.mkdir()
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            job_a = las_root / "Job_20250311_1043_Track01.las"
            job_b = las_root / "Job_20250311_1043_C_Track01.las"
            _write_las(job_a, xyz, rgb16=None)
            _write_las(job_b, xyz + [10.0, 0.0, 0.0], rgb16=None)
            # This deliberately is not a valid LAS.  Successful catalog creation
            # proves a non-matching historical job is filtered before laspy opens it.
            old_job = las_root / "Job_20250102_1434_Track01.las"
            old_job.write_bytes(b"historical LAS placeholder")
            cache_path = root / "pointcloud.json"

            catalog_a = build_pointcloud_catalog(
                root,
                cache_path,
                source="las",
                include_jobs=["job-20250311-1043"],
            )
            self.assertEqual([item["path"] for item in catalog_a["files"]], [str(job_a.resolve())])
            self.assertEqual(catalog_a["include_job_keys"], ["job202503111043"])
            self.assertEqual(len(catalog_a["job_filtered_files"]), 2)

            catalog_b = build_pointcloud_catalog(
                root,
                cache_path,
                source="las",
                include_jobs="Job_20250311_1043_C",
            )
            self.assertEqual([item["path"] for item in catalog_b["files"]], [str(job_b.resolve())])
            self.assertNotEqual(catalog_a["signature"], catalog_b["signature"])
            self.assertNotEqual(catalog_a["include_job_keys"], catalog_b["include_job_keys"])

    def test_catalog_prefers_splits_indexes_chunks_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            las_root = root / "LAS"
            las_root.mkdir()
            xyz1 = np.asarray(
                [
                    [300_000.01, 4_100_000.01, 101.0],
                    [300_001.00, 4_100_001.00, 102.0],
                    [300_002.00, 4_100_002.00, 103.0],
                ]
            )
            xyz2 = xyz1 + np.asarray([10.0, 10.0, 0.0])
            rgb = np.asarray(
                [[0, 32768, 65535], [65535, 0, 32768], [32768, 65535, 0]],
                dtype=np.uint16,
            )
            _write_las(las_root / "Job_20250311_1043_Track01.las", np.vstack((xyz1, xyz2)), rgb16=rgb.repeat(2, axis=0))
            split1 = las_root / "Job_20250311_1043_Track01_1.las"
            split2 = las_root / "Job_20250311_1043_Track01_2.las"
            # Independent split integer origins are valid and occur in the real
            # Leica export.  Header validation must not require offset equality.
            split_offsets = (300_000.005, 4_100_000.005, 100.005)
            _write_las(split1, xyz1, rgb16=rgb, offsets=split_offsets)
            _write_las(split2, xyz2, rgb16=rgb, offsets=split_offsets)
            cache_path = root / ".cache" / "pointcloud.json"

            catalog = build_pointcloud_catalog(
                root, cache_path, source="las", las_chunk_size=2
            )

            self.assertEqual(catalog["selected_source_type"], "las")
            self.assertEqual(len(catalog["files"]), 2)
            self.assertEqual(len(catalog["excluded_files"]), 1)
            # pyproj may normalize legacy LOCAL_CS WKT into WKT2 ENGCRS.
            self.assertIn("Synthetic MMS metres", catalog["crs_wkt"])
            self.assertEqual(catalog["files"][0]["scales"], [0.01, 0.01, 0.01])
            self.assertEqual(catalog["files"][0]["point_format_id"], 3)
            self.assertEqual(
                [(block["start"], block["count"]) for block in catalog["files"][0]["blocks"]],
                [(0, 2), (2, 1)],
            )
            self.assertEqual(
                catalog["files"][0]["provenance"]["selection_policy"],
                "numbered_splits_validated",
            )
            split_validation = catalog["files"][0]["provenance"]["split_validation"]
            self.assertEqual(split_validation["status"], "passed")
            self.assertTrue(split_validation["offsets_compatible"])
            self.assertTrue(split_validation["bounds_match"])

            with mock.patch(
                "mms_shp_detection.pointcloud._index_single_las",
                side_effect=AssertionError("cache hit must not rescan LAS"),
            ):
                cached = build_pointcloud_catalog(
                    root, cache_path, source="las", las_chunk_size=2
                )
            self.assertEqual(cached, catalog)

            with PointCloudReaderCache() as readers:
                first_file = catalog["files"][0]
                xyz, rgb8, intensity = readers.read_block_points(
                    first_file, first_file["blocks"][0]
                )
            self.assertEqual(xyz.dtype, np.float64)
            np.testing.assert_allclose(xyz, xyz1[:2], atol=0.0051)
            np.testing.assert_array_equal(
                rgb8,
                np.asarray([[0, 128, 255], [255, 0, 128]], dtype=np.uint8),
            )
            np.testing.assert_array_equal(intensity, [10, 11])

    def test_incomplete_numbered_splits_fall_back_to_full_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            las_root = root / "LAS"
            las_root.mkdir()
            xyz1 = np.asarray(
                [[300_000.0, 4_100_000.0, 100.0], [300_001.0, 4_100_001.0, 101.0]]
            )
            xyz2 = np.asarray(
                [[300_002.0, 4_100_002.0, 102.0], [300_003.0, 4_100_003.0, 103.0]]
            )
            full = las_root / "Job_20250311_1043_Track01.las"
            split1 = las_root / "Job_20250311_1043_Track01_1.las"
            split3 = las_root / "Job_20250311_1043_Track01_3.las"
            _write_las(full, np.vstack((xyz1, xyz2)))
            _write_las(split1, xyz1)
            _write_las(split3, xyz2)

            catalog = build_pointcloud_catalog(
                root, root / "catalog.json", source="las", las_chunk_size=2
            )

            self.assertEqual([item["path"] for item in catalog["files"]], [str(full.resolve())])
            provenance = catalog["files"][0]["provenance"]
            self.assertEqual(
                provenance["selection_policy"], "full_preferred_split_validation_failed"
            )
            self.assertIn(
                "non_contiguous_split_indices",
                provenance["split_validation"]["reasons"],
            )
            self.assertEqual(
                {item["path"] for item in catalog["excluded_files"]},
                {str(split1.resolve()), str(split3.resolve())},
            )
            self.assertTrue(
                all(
                    item["reason"] == "split_validation_failed_full_preferred"
                    for item in catalog["excluded_files"]
                )
            )

    def test_split_header_mismatches_fall_back_to_full(self) -> None:
        mismatch_cases = ("point_count", "crs", "scale", "point_format", "bounds")
        for mismatch in mismatch_cases:
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                las_root = root / "LAS"
                las_root.mkdir()
                xyz1 = np.asarray(
                    [[300_000.0, 4_100_000.0, 100.0], [300_001.0, 4_100_001.0, 101.0]]
                )
                xyz2 = np.asarray(
                    [[300_002.0, 4_100_002.0, 102.0], [300_003.0, 4_100_003.0, 103.0]]
                )
                rgb1 = np.full((2, 3), 10, dtype=np.uint16)
                rgb2 = np.full((2, 3), 20, dtype=np.uint16)
                full = las_root / "Job_20250311_1043_Track01.las"
                split1 = las_root / "Job_20250311_1043_Track01_1.las"
                split2 = las_root / "Job_20250311_1043_Track01_2.las"
                _write_las(full, np.vstack((xyz1, xyz2)), rgb16=np.vstack((rgb1, rgb2)))
                _write_las(split1, xyz1, rgb16=rgb1)

                split2_xyz = xyz2
                split2_wkt = TEST_WKT
                split2_scales = (0.01, 0.01, 0.01)
                split2_rgb: np.ndarray | None = rgb2
                if mismatch == "point_count":
                    split2_xyz = xyz2[:1]
                    split2_rgb = rgb2[:1]
                elif mismatch == "crs":
                    split2_wkt = 'LOCAL_CS["Different CRS"]'
                elif mismatch == "scale":
                    split2_scales = (0.02, 0.02, 0.02)
                elif mismatch == "point_format":
                    split2_rgb = None
                elif mismatch == "bounds":
                    split2_xyz = xyz2 + np.asarray([10.0, 0.0, 0.0])
                _write_las(
                    split2,
                    split2_xyz,
                    rgb16=split2_rgb,
                    wkt=split2_wkt,
                    scales=split2_scales,
                )

                catalog = build_pointcloud_catalog(
                    root, root / "catalog.json", source="las", las_chunk_size=2
                )
                self.assertEqual(
                    [item["path"] for item in catalog["files"]], [str(full.resolve())]
                )
                validation = catalog["files"][0]["provenance"]["split_validation"]
                expected_reason = {
                    "point_count": "point_count_mismatch",
                    "crs": "crs_mismatch",
                    "scale": "scale_mismatch",
                    "point_format": "point_format_mismatch",
                    "bounds": "bounds_mismatch",
                }[mismatch]
                self.assertIn(expected_reason, validation["reasons"])

    def test_las_without_rgb_uses_neutral_colour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "plain.las"
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            _write_las(path, xyz, rgb16=None, with_wkt=False)
            with PointCloudReaderCache() as readers:
                actual_xyz, rgb, intensity = readers.read_block_points(
                    path, {"source_type": "las", "start": 0, "count": 1}
                )
            self.assertEqual(actual_xyz.dtype, np.float64)
            np.testing.assert_array_equal(rgb, [[128, 128, 128]])
            np.testing.assert_array_equal(intensity, [10])

    def test_job_track_match_precedes_bbox_distance(self) -> None:
        catalog = {
            "files": [
                {
                    "path": "wrong-but-near.las",
                    "job_name": "Job_B",
                    "track_name": "Track01",
                    "file_min": [0.0, 0.0, 0.0],
                    "file_max": [1.0, 1.0, 1.0],
                },
                {
                    "path": "right-2.las",
                    "job_name": "Job_A",
                    "track_name": "Track01",
                    "file_min": [20.0, 0.0, 0.0],
                    "file_max": [21.0, 1.0, 1.0],
                },
                {
                    "path": "right-1.las",
                    "job_name": "Job_A",
                    "track_name": "Track01",
                    "file_min": [10.0, 0.0, 0.0],
                    "file_max": [11.0, 1.0, 1.0],
                },
            ]
        }
        matches = match_nearest_pointcloud_files(
            {
                "job_name": "job-a",
                "track_name": "track_01",
                "origin": [0.0, 0.0, 0.0],
            },
            catalog,
            neighbor_count=1,
        )
        self.assertEqual([item["path"] for item in matches], ["right-1.las", "right-2.las"])

        fallback_matches = match_nearest_pointcloud_files(
            {
                "job_name": "job-without-match",
                "track_name": "track_99",
                "origin": [0.0, 0.0, 0.0],
            },
            catalog,
            neighbor_count=1,
        )
        self.assertEqual(len(fallback_matches), 1)

    def test_candidate_block_cone_selection_supports_las_blocks(self) -> None:
        pointcloud_file = {
            "blocks": [
                {"name": "front", "min": [9.0, -1.0, -1.0], "max": [11.0, 1.0, 1.0]},
                {"name": "back", "min": [-11.0, -1.0, -1.0], "max": [-9.0, 1.0, 1.0]},
            ]
        }
        selected = select_candidate_blocks(
            pointcloud_file,
            np.asarray([0.0, 0.0, 0.0]),
            np.asarray([1.0, 0.0, 0.0]),
            detection_angle_rad=math.radians(5.0),
            max_range_m=20.0,
            angle_margin_rad=0.0,
        )
        self.assertEqual([block["name"] for block in selected], ["front"])


class PointCloudPcdbPrecisionTests(unittest.TestCase):
    def test_pcdb_wrapper_preserves_large_world_coordinate_precision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "synthetic.pcdb"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE CRYSTAL_CUBE (NAME TEXT, DATA BLOB)")
                minimum = [329_700.0, 4_153_507.0, 50.0]
                maximum = [329_702.0, 4_153_509.0, 52.0]
                header = struct.pack("<6dI", *(minimum + maximum), 2)
                point1 = struct.pack("<3f3BH", 0.01, 0.01, 0.01, 1, 2, 3, 10)
                point2 = struct.pack("<3f3BH", 0.02, 0.02, 0.02, 4, 5, 6, 20)
                connection.execute(
                    "INSERT INTO CRYSTAL_CUBE (NAME, DATA) VALUES (?, ?)",
                    ("block.bpc", header + point1 + point2),
                )
                connection.commit()
            finally:
                connection.close()

            with PointCloudReaderCache() as readers:
                xyz, rgb, intensity = readers.read_block_points(path, "block.bpc")
            self.assertEqual(xyz.dtype, np.float64)
            self.assertAlmostEqual(float(xyz[1, 1] - xyz[0, 1]), 0.01, places=6)
            np.testing.assert_array_equal(rgb, [[1, 2, 3], [4, 5, 6]])
            np.testing.assert_array_equal(intensity, [10, 20])


if __name__ == "__main__":
    unittest.main()
