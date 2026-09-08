from __future__ import annotations

import math
import os
import sqlite3
import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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


def _write_raw_attribute_las(
    path: Path,
    *,
    point_format: int,
    xyz: np.ndarray,
    rgb16: np.ndarray | None = None,
    scan_angle: np.ndarray | None = None,
    scan_angle_rank: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Write a compact fixture whose raw fields are intentionally nontrivial."""

    version = "1.4" if point_format >= 6 else "1.2"
    header = laspy.LasHeader(point_format=point_format, version=version)
    header.scales = np.asarray([0.001, 0.001, 0.001])
    header.offsets = np.asarray([300_000.0, 4_100_000.0, 100.0])
    header.global_encoding.gps_time_type = 1
    las = laspy.LasData(header)
    count = len(xyz)
    las.x = xyz[:, 0]
    las.y = xyz[:, 1]
    las.z = xyz[:, 2]
    values = {
        "intensity": np.asarray([0, 65_535, 12_345, 54_321][:count], dtype=np.uint16),
        "classification": np.asarray(
            ([1, 84, 20, 255] if point_format >= 6 else [1, 20, 30, 31])[
                :count
            ],
            dtype=np.uint8,
        ),
        "gps_time": np.asarray([10.25, 20.5, 30.75, 40.125][:count], dtype=np.float64),
        "return_number": np.asarray([1, 2, 1, 3][:count], dtype=np.uint8),
        "number_of_returns": np.asarray([1, 2, 3, 3][:count], dtype=np.uint8),
        "point_source_id": np.asarray([0, 1, 32_768, 65_535][:count], dtype=np.uint16),
        "user_data": np.asarray([0, 1, 128, 255][:count], dtype=np.uint8),
        "edge_of_flight_line": np.asarray([0, 1, 0, 1][:count], dtype=np.uint8),
        "scan_direction_flag": np.asarray([1, 0, 1, 0][:count], dtype=np.uint8),
    }
    for name, value in values.items():
        setattr(las, name, value)
    if rgb16 is not None:
        las.red = rgb16[:, 0]
        las.green = rgb16[:, 1]
        las.blue = rgb16[:, 2]
    if scan_angle is not None:
        # Assign the underlying PF6+ int16 storage, not a presentation view.
        las.points.array["scan_angle"] = np.asarray(scan_angle, dtype=np.int16)
        values["scan_angle"] = np.asarray(scan_angle, dtype=np.int16)
    if scan_angle_rank is not None:
        las.scan_angle_rank = np.asarray(scan_angle_rank, dtype=np.int8)
        values["scan_angle_rank"] = np.asarray(scan_angle_rank, dtype=np.int8)
    las.write(path)
    return values


class PointCloudLasTests(unittest.TestCase):
    def test_web_catalog_mode_checks_every_discovered_source_for_links(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"not-opened")
            original_is_symlink = Path.is_symlink

            def report_source_as_link(path: Path) -> bool:
                return path == source or original_is_symlink(path)

            with (
                mock.patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=report_source_as_link,
                ),
                self.assertRaisesRegex(ValueError, "Symbolic links"),
            ):
                build_pointcloud_catalog(
                    root,
                    root / "catalog.json",
                    source="las",
                    reject_symlinks=True,
                )

    def test_web_catalog_mode_rejects_symlinked_point_sources(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as outside_text:
            root = Path(root_text)
            outside = Path(outside_text) / "Job_A_Track01.las"
            outside.write_bytes(b"not-opened")
            linked = root / "Job_A_Track01.las"
            try:
                os.symlink(outside, linked)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "Symbolic links"):
                build_pointcloud_catalog(
                    root,
                    root / "catalog.json",
                    source="las",
                    reject_symlinks=True,
                )

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

    def test_modern_las_records_preserve_raw_fields_and_source_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "Job_Raw_Track01.las"
            xyz = np.asarray(
                [
                    [300_000.001, 4_100_000.001, 100.001],
                    [300_000.002, 4_100_000.002, 100.002],
                    [300_000.003, 4_100_000.003, 100.003],
                    [300_000.004, 4_100_000.004, 100.004],
                ]
            )
            rgb_raw = np.asarray(
                [
                    [1, 258, 65_534],
                    [257, 32_769, 65_535],
                    [513, 1_025, 2_049],
                    [4_095, 16_385, 49_153],
                ],
                dtype=np.uint16,
            )
            scan_angle = np.asarray(
                [-32_768, -1_234, 2_345, 32_767], dtype=np.int16
            )
            expected = _write_raw_attribute_las(
                path,
                point_format=7,
                xyz=xyz,
                rgb16=rgb_raw,
                scan_angle=scan_angle,
            )
            catalog = build_pointcloud_catalog(
                path.parent,
                path.parent / "catalog.json",
                source="las",
            )

            with PointCloudReaderCache() as readers:
                records = readers.read_block_records(
                    path,
                    {"source_type": "las", "start": 1, "count": 2},
                )

            np.testing.assert_allclose(records["xyz"], xyz[1:3], atol=0.00051)
            np.testing.assert_array_equal(records["source_index"], [1, 2])
            self.assertEqual(records["source_index"].dtype, np.dtype(np.int64))
            np.testing.assert_array_equal(records["rgb_raw"], rgb_raw[1:3])
            self.assertEqual(records["rgb_raw"].dtype, np.dtype(np.uint16))
            expected_rgb8 = (
                (rgb_raw[1:3].astype(np.uint32) + 128) // 257
            ).astype(np.uint8)
            np.testing.assert_array_equal(records["rgb"], expected_rgb8)
            for name, dtype in (
                ("point_source_id", np.uint16),
                ("user_data", np.uint8),
                ("edge_of_flight_line", np.uint8),
                ("scan_direction_flag", np.uint8),
            ):
                np.testing.assert_array_equal(records[name], expected[name][1:3])
                self.assertEqual(records[name].dtype, np.dtype(dtype))
            np.testing.assert_array_equal(records["scan_angle"], scan_angle[1:3])
            self.assertEqual(records["scan_angle"].dtype, np.dtype(np.int16))
            np.testing.assert_array_equal(records["scan_angle_rank"], [0, 0])
            self.assertEqual(records["scan_angle_rank"].dtype, np.dtype(np.int8))

            availability = records["field_availability"]
            self.assertEqual(availability.shape, (2,))
            self.assertTrue(np.all(availability["rgb_raw"]))
            self.assertTrue(np.all(availability["scan_angle"]))
            self.assertFalse(np.any(availability["scan_angle_rank"]))
            self.assertTrue(np.all(availability["source_index"]))
            scan_metadata = catalog["files"][0]["record_field_metadata"][
                "scan_angle"
            ]
            self.assertEqual(scan_metadata["source_dimension"], "scan_angle")
            self.assertEqual(scan_metadata["source_dtype"], "int16")
            self.assertEqual(scan_metadata["scale_to_degrees"], 0.006)

    def test_legacy_las_keeps_scan_angle_rank_distinct_from_modern_angle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "Job_Raw_Track01.las"
            xyz = np.asarray(
                [
                    [300_000.001, 4_100_000.001, 100.001],
                    [300_000.002, 4_100_000.002, 100.002],
                ]
            )
            rank = np.asarray([-128, 127], dtype=np.int8)
            _write_raw_attribute_las(
                path,
                point_format=3,
                xyz=xyz,
                rgb16=np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.uint16),
                scan_angle_rank=rank,
            )
            catalog = build_pointcloud_catalog(
                path.parent,
                path.parent / "catalog.json",
                source="las",
            )

            with PointCloudReaderCache() as readers:
                records = readers.read_block_records(
                    path,
                    {"source_type": "las", "start": 0, "count": 2},
                )

            np.testing.assert_array_equal(records["scan_angle_rank"], rank)
            np.testing.assert_array_equal(records["scan_angle"], [0, 0])
            self.assertTrue(np.all(records["field_availability"]["scan_angle_rank"]))
            self.assertFalse(np.any(records["field_availability"]["scan_angle"]))
            scan_metadata = catalog["files"][0]["record_field_metadata"][
                "scan_angle_rank"
            ]
            self.assertEqual(scan_metadata["source_dimension"], "scan_angle_rank")
            self.assertEqual(scan_metadata["source_dtype"], "int8")
            self.assertEqual(scan_metadata["scale_to_degrees"], 1.0)

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

    def test_exact_include_scope_excludes_other_track_before_las_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            track01 = root / "Job_20250311_1043_Track01.las"
            track02 = root / "Job_20250311_1043_Track02.las"
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            _write_las(track01, xyz)
            # The excluded source is intentionally invalid.  Catalog success
            # proves exact scope filtering happens before laspy opens Track02.
            track02.write_bytes(b"excluded invalid Track02")
            cache_path = root / "catalog.json"

            catalog01 = build_pointcloud_catalog(
                root,
                cache_path,
                source="las",
                include_scope=[
                    {
                        "job_id": "Job_20250311_1043",
                        "tracks": ["Track01"],
                    }
                ],
            )

            self.assertEqual(
                [item["path"] for item in catalog01["files"]],
                [str(track01.resolve())],
            )
            self.assertEqual(
                catalog01["include_scope"],
                {
                    "strict": True,
                    "jobs": [
                        {
                            "job_id": "job_20250311_1043",
                            "tracks": ["track01"],
                        }
                    ],
                },
            )
            self.assertEqual(
                [item["reason"] for item in catalog01["scope_filtered_files"]],
                ["job_track_not_included"],
            )

            alias_catalog = build_pointcloud_catalog(
                root,
                cache_path,
                source="las",
                include_scope={
                    "strict": True,
                    "jobs": [
                        {
                            "job_id": "job_20250311_1043",
                            "track_ids": ["track01"],
                        }
                    ],
                },
            )
            self.assertEqual(alias_catalog["signature"], catalog01["signature"])

            # A different exact pair produces a distinct cache identity.
            _write_las(track02, xyz + [10.0, 0.0, 0.0])
            catalog02 = build_pointcloud_catalog(
                root,
                cache_path,
                source="las",
                include_scope=[
                    {
                        "job_id": "Job_20250311_1043",
                        "track_ids": ["Track02"],
                    }
                ],
            )
            self.assertEqual(
                [item["path"] for item in catalog02["files"]],
                [str(track02.resolve())],
            )
            self.assertNotEqual(catalog01["signature"], catalog02["signature"])

    def test_strict_include_scope_rejects_unknown_las_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            _write_las(root / "Job_A_Track01.las", xyz)
            (root / "unknown.las").write_bytes(b"must not be opened")

            with self.assertRaisesRegex(ValueError, "could not parse exact Job/Track"):
                build_pointcloud_catalog(
                    root,
                    root / "catalog.json",
                    source="las",
                    include_scope={
                        "strict": True,
                        "jobs": [{"job_id": "Job_A", "tracks": ["Track01"]}],
                    },
                )

    def test_non_strict_empty_include_scope_preserves_legacy_allow_all(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            xyz = np.asarray([[300_000.0, 4_100_000.0, 100.0]])
            track01 = root / "Job_A_Track01.las"
            track02 = root / "Job_A_Track02.las"
            _write_las(track01, xyz)
            _write_las(track02, xyz + [1.0, 0.0, 0.0])

            catalog = build_pointcloud_catalog(
                root,
                root / "catalog.json",
                source="las",
                include_scope={"strict": False, "jobs": []},
            )

            self.assertEqual(
                {item["path"] for item in catalog["files"]},
                {str(track01.resolve()), str(track02.resolve())},
            )
            self.assertEqual(
                catalog["include_scope"], {"strict": False, "jobs": []}
            )

    def test_strict_include_scope_rejects_pcdb_before_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pcdb_path = root / "legacy.pcdb"
            # Invalid bytes make an accidental decoder/index open observable.
            pcdb_path.write_bytes(b"must not be opened")
            las_path = root / "Job_A_Track01.las"
            _write_las(
                las_path,
                np.asarray([[300_000.0, 4_100_000.0, 100.0]]),
            )
            scope = {
                "strict": True,
                "jobs": [{"job_id": "Job_A", "tracks": ["Track01"]}],
            }

            for source in ("pcdb", "auto"):
                with (
                    self.subTest(source=source),
                    mock.patch(
                        "mms_shp_detection.pointcloud._index_single_pcdb"
                    ) as pcdb_index,
                    mock.patch(
                        "mms_shp_detection.pointcloud._index_single_las"
                    ) as las_index,
                    self.assertRaisesRegex(
                        ValueError,
                        "PCDB source without exact Job/Track identity",
                    ),
                ):
                    build_pointcloud_catalog(
                        root,
                        root / f"{source}.json",
                        source=source,
                        include_scope=scope,
                    )
                pcdb_index.assert_not_called()
                las_index.assert_not_called()

            # A LAS-only request does not admit or inspect the unrelated PCDB.
            las_catalog = build_pointcloud_catalog(
                root,
                root / "las.json",
                source="las",
                include_scope=scope,
            )
            self.assertEqual(
                [item["path"] for item in las_catalog["files"]],
                [str(las_path.resolve())],
            )

    def test_standard_delivery_las_uses_track_identity_scope_and_nearest_prj(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            track_dir = root / "SEC006" / "SURV01" / "TRACK01"
            laser_dir = track_dir / "Laser01"
            laser_dir.mkdir(parents=True)
            path = laser_dir / "Track01_Scanner Profiler.zfs_0.las"
            xyz = np.asarray(
                [
                    [465_216.0, 3_911_273.0, 47.0],
                    [465_217.0, 3_911_274.0, 48.0],
                ]
            )
            _write_las(path, xyz, with_wkt=False)
            (track_dir / "TRACK01_Trajectory.prj").write_text(
                TEST_WKT,
                encoding="utf-8",
            )

            catalog = build_pointcloud_catalog(
                root,
                root / "catalog.json",
                source="las",
                include_jobs={"SURV01"},
            )

            self.assertEqual(len(catalog["files"]), 1)
            item = catalog["files"][0]
            self.assertEqual(item["job_name"], "SURV01")
            self.assertEqual(item["track_name"], "TRACK01")
            self.assertEqual(item["split_index"], 0)
            self.assertEqual(item["provenance"]["crs_source"], "nearest_delivery_prj")
            self.assertIn("Synthetic MMS metres", item["crs_wkt"])

            matched = match_nearest_pointcloud_files(
                {
                    "origin": [465_216.5, 3_911_273.5, 47.5],
                    "job_name": "SURV01",
                    "track_name": "TRACK01",
                    "pointcloud_scope": str(track_dir),
                },
                catalog,
                neighbor_count=1,
            )
            self.assertEqual([entry["path"] for entry in matched], [str(path.resolve())])

    def test_auto_catalog_keeps_independent_pcdb_and_las_deliveries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pcdb_path = root / "legacy" / "synthetic.pcdb"
            pcdb_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(pcdb_path)
            try:
                connection.execute("CREATE TABLE CRYSTAL_CUBE (NAME TEXT, DATA BLOB)")
                header = struct.pack(
                    "<6dI",
                    100.0,
                    200.0,
                    10.0,
                    101.0,
                    201.0,
                    11.0,
                    1,
                )
                connection.execute(
                    "INSERT INTO CRYSTAL_CUBE (NAME, DATA) VALUES (?, ?)",
                    ("block.bpc", header),
                )
                connection.commit()
            finally:
                connection.close()

            track_dir = root / "delivery" / "SURV01" / "TRACK01"
            laser_dir = track_dir / "Laser01"
            laser_dir.mkdir(parents=True)
            las_path = laser_dir / "Track01_Scanner Profiler.zfs_0.las"
            _write_las(
                las_path,
                np.asarray([[465_216.0, 3_911_273.0, 47.0]]),
                with_wkt=False,
            )
            (track_dir / "TRACK01_Trajectory.prj").write_text(
                TEST_WKT,
                encoding="utf-8",
            )

            catalog = build_pointcloud_catalog(
                root,
                root / "catalog.json",
                source="auto",
                include_jobs={"SURV01"},
            )

            self.assertEqual(catalog["selected_source_type"], "mixed")
            self.assertEqual(
                {item["source_type"] for item in catalog["files"]},
                {"pcdb", "las"},
            )
            pcdb_item = next(
                item for item in catalog["files"] if item["source_type"] == "pcdb"
            )
            self.assertFalse(pcdb_item["provenance_complete"])
            self.assertFalse(pcdb_item["training"])
            self.assertFalse(pcdb_item["provenance"]["provenance_complete"])
            self.assertFalse(pcdb_item["provenance"]["training"])
            self.assertFalse(catalog["provenance_complete"])
            self.assertFalse(catalog["training"])

            non_strict_catalog = build_pointcloud_catalog(
                root,
                root / "non-strict-catalog.json",
                source="auto",
                include_scope={
                    "strict": False,
                    "jobs": [{"job_id": "SURV01", "tracks": ["TRACK01"]}],
                },
            )
            self.assertEqual(non_strict_catalog["selected_source_type"], "mixed")
            self.assertEqual(
                {item["source_type"] for item in non_strict_catalog["files"]},
                {"pcdb", "las"},
            )

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
            self.assertTrue(catalog["provenance_complete"])
            self.assertTrue(catalog["training"])
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
                records = readers.read_block_records(
                    path, {"source_type": "las", "start": 0, "count": 1}
                )
            self.assertEqual(actual_xyz.dtype, np.float64)
            np.testing.assert_array_equal(rgb, [[128, 128, 128]])
            np.testing.assert_array_equal(intensity, [10])
            np.testing.assert_array_equal(records["rgb_raw"], [[0, 0, 0]])
            self.assertFalse(records["field_availability"]["rgb_raw"][0])
            self.assertFalse(records["field_availability"]["rgb"][0])

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


class PointCloudDecodedBlockCacheTests(unittest.TestCase):
    def test_las_points_and_records_share_one_immutable_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "shared.las"
            xyz = np.asarray(
                [
                    [300_000.0, 4_100_000.0, 100.0],
                    [300_001.0, 4_100_001.0, 101.0],
                ]
            )
            rgb16 = np.asarray(
                [[0, 32768, 65535], [65535, 32768, 0]],
                dtype=np.uint16,
            )
            _write_las(path, xyz, rgb16=rgb16)
            block = {"source_type": "las", "start": 0, "count": 2}

            with PointCloudReaderCache() as readers, mock.patch.object(
                readers,
                "_read_las_records",
                wraps=readers._read_las_records,
            ) as decode:
                points_xyz, points_rgb, points_intensity = (
                    readers.read_block_points(path, block)
                )
                records = readers.read_block_records(path, block)

                self.assertEqual(decode.call_count, 1)
                self.assertIs(points_xyz, records["xyz"])
                self.assertIs(points_rgb, records["rgb"])
                self.assertIs(points_intensity, records["intensity"])
                self.assertTrue(all(not value.flags.writeable for value in records.values()))
                with self.assertRaises(ValueError):
                    points_xyz[0, 0] = 0.0

                # The mapping itself is per-call, so replacing one of its values
                # also cannot change the cached record mapping.
                records["xyz"] = np.zeros_like(points_xyz)
                self.assertIs(
                    readers.read_block_records(path, block)["xyz"],
                    points_xyz,
                )

    def test_reindexed_las_version_reopens_reader_and_replaces_decoded_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "replace-in-place.las"
            block = {"source_type": "las", "start": 0, "count": 1}
            first_xyz = np.asarray([[300_001.0, 4_100_001.0, 101.0]])
            second_xyz = np.asarray([[300_099.0, 4_100_099.0, 109.0]])
            _write_las(path, first_xyz)
            first_stat = path.stat()
            first_file = {
                "path": str(path.resolve()),
                "source_type": "las",
                "file_size": int(first_stat.st_size),
                "mtime_ns": int(first_stat.st_mtime_ns),
            }

            with PointCloudReaderCache() as readers, mock.patch.object(
                readers,
                "_read_las_records",
                wraps=readers._read_las_records,
            ) as decode:
                first = readers.read_block_records(first_file, block)
                first_again = readers.read_block_records(first_file, block)
                resolved = str(path.resolve())
                first_reader = readers._las_readers[resolved][1]

                self.assertEqual(decode.call_count, 1)
                self.assertIs(first_again["xyz"], first["xyz"])
                np.testing.assert_allclose(first["xyz"], first_xyz, atol=0.0051)

                # Replace the source at the same path with the same point count,
                # then force a distinct catalog version even on coarse filesystems.
                _write_las(path, second_xyz)
                rewritten_stat = path.stat()
                os.utime(
                    path,
                    ns=(
                        int(rewritten_stat.st_atime_ns),
                        max(
                            int(rewritten_stat.st_mtime_ns),
                            int(first_stat.st_mtime_ns) + 1_000_000_000,
                        ),
                    ),
                )
                second_stat = path.stat()
                second_file = {
                    "path": str(path.resolve()),
                    "source_type": "las",
                    "file_size": int(second_stat.st_size),
                    "mtime_ns": int(second_stat.st_mtime_ns),
                }
                self.assertNotEqual(
                    (first_file["file_size"], first_file["mtime_ns"]),
                    (second_file["file_size"], second_file["mtime_ns"]),
                )

                second = readers.read_block_records(second_file, block)
                second_again = readers.read_block_records(second_file, block)
                second_reader = readers._las_readers[resolved][1]

                self.assertEqual(decode.call_count, 2)
                self.assertIs(second_again["xyz"], second["xyz"])
                self.assertIsNot(second_reader, first_reader)
                self.assertEqual(len(readers._decoded_blocks), 1)
                np.testing.assert_allclose(second["xyz"], second_xyz, atol=0.0051)

    def test_pcdb_points_and_records_share_one_decode_across_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "shared.pcdb"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE CRYSTAL_CUBE (NAME TEXT, DATA BLOB)")
                header = struct.pack(
                    "<6dI",
                    300_000.0,
                    4_100_000.0,
                    100.0,
                    300_002.0,
                    4_100_002.0,
                    102.0,
                    1,
                )
                point = struct.pack("<3f3BH", 0.25, 0.5, 0.75, 1, 2, 3, 10)
                connection.execute(
                    "INSERT INTO CRYSTAL_CUBE (NAME, DATA) VALUES (?, ?)",
                    ("block.bpc", header + point),
                )
                connection.commit()
            finally:
                connection.close()

            with PointCloudReaderCache() as readers, mock.patch.object(
                readers,
                "_read_pcdb_records",
                wraps=readers._read_pcdb_records,
            ) as decode:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = [
                        executor.submit(
                            readers.read_block_records
                            if index % 2
                            else readers.read_block_points,
                            path,
                            "block.bpc",
                        )
                        for index in range(24)
                    ]
                    results = [future.result() for future in futures]

                self.assertEqual(decode.call_count, 1)
                point_results = [
                    result for result in results if isinstance(result, tuple)
                ]
                record_results = [
                    result for result in results if isinstance(result, dict)
                ]
                self.assertTrue(point_results)
                self.assertTrue(record_results)
                self.assertIs(point_results[0][0], record_results[0]["xyz"])
                self.assertFalse(point_results[0][0].flags.writeable)

    def test_lru_enforces_entry_and_byte_limits_and_close_clears_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bounded.las"
            xyz = np.asarray(
                [
                    [300_000.0, 4_100_000.0, 100.0],
                    [300_001.0, 4_100_001.0, 101.0],
                ]
            )
            _write_las(path, xyz)
            first = {"source_type": "las", "start": 0, "count": 1}
            second = {"source_type": "las", "start": 1, "count": 1}
            readers = PointCloudReaderCache(
                decoded_cache_max_entries=1,
                decoded_cache_max_bytes=1_024,
            )
            with mock.patch.object(
                readers,
                "_read_las_records",
                wraps=readers._read_las_records,
            ) as decode:
                readers.read_block_records(path, first)
                readers.read_block_records(path, second)
                readers.read_block_records(path, first)

            self.assertEqual(decode.call_count, 3)
            self.assertEqual(len(readers._decoded_blocks), 1)
            self.assertLessEqual(readers._decoded_cache_bytes, 1_024)
            self.assertTrue(readers._las_readers)

            readers.close()
            self.assertEqual(len(readers._decoded_blocks), 0)
            self.assertEqual(readers._decoded_cache_bytes, 0)
            self.assertEqual(readers._las_readers, {})

            uncached = PointCloudReaderCache(
                decoded_cache_max_entries=10,
                decoded_cache_max_bytes=1,
            )
            try:
                with mock.patch.object(
                    uncached,
                    "_read_las_records",
                    wraps=uncached._read_las_records,
                ) as decode:
                    uncached.read_block_records(path, first)
                    uncached.read_block_records(path, first)
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(len(uncached._decoded_blocks), 0)
                self.assertEqual(uncached._decoded_cache_bytes, 0)
            finally:
                uncached.close()


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
                records = readers.read_block_records(path, "block.bpc")
            self.assertEqual(xyz.dtype, np.float64)
            self.assertAlmostEqual(float(xyz[1, 1] - xyz[0, 1]), 0.01, places=6)
            np.testing.assert_array_equal(rgb, [[1, 2, 3], [4, 5, 6]])
            np.testing.assert_array_equal(intensity, [10, 20])
            np.testing.assert_array_equal(records["source_index"], [-1, -1])
            np.testing.assert_array_equal(records["rgb_raw"], np.zeros((2, 3)))
            availability = records["field_availability"]
            self.assertTrue(np.all(availability["xyz"]))
            self.assertTrue(np.all(availability["rgb"]))
            self.assertTrue(np.all(availability["intensity"]))
            self.assertFalse(np.any(availability["rgb_raw"]))
            self.assertFalse(np.any(availability["source_index"]))


if __name__ == "__main__":
    unittest.main()
