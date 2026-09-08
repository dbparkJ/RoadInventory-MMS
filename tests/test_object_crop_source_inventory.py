from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import laspy
import numpy as np

from mms_shp_detection.object_crops import SourceScope
from mms_shp_detection.object_crops.source_inventory import (
    SourceHashCache,
    SourceInventoryError,
    SourceMutationError,
    SourceScopeError,
    build_source_inventory,
    validate_source_inventory,
    write_source_inventory,
)
from mms_shp_detection.pointcloud import PointCloudReaderCache


def _scope(*tracks: str, strict: bool = True) -> SourceScope:
    return SourceScope.from_value(
        {
            "strict": strict,
            "jobs": [{"job_id": "Job_A", "tracks": list(tracks)}],
        }
    )


def _catalog_row(path: Path, track: str, source_type: str = "las") -> dict[str, object]:
    return {
        "path": str(path),
        "source_type": source_type,
        "job_name": "Job_A",
        "track_name": track,
    }


class SourceInventoryTests(unittest.TestCase):
    def test_inventory_is_root_relative_sorted_hashed_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_b = root / "LAS" / "Job_A_Track01_b.las"
            source_a = root / "LAS" / "Job_A_Track01_a.las"
            source_a.parent.mkdir()
            source_a.write_bytes(b"source-a")
            source_b.write_bytes(b"source-b")
            # The excluded path deliberately does not exist.  Successful build
            # proves Track02 is rejected before resolve/open/hash.
            excluded = root / "LAS" / "missing-track02.las"
            catalog = {
                "files": [
                    _catalog_row(source_b, "Track01"),
                    _catalog_row(excluded, "Track02"),
                    _catalog_row(source_a, "Track01"),
                ]
            }

            inventory = build_source_inventory(catalog, root, _scope("Track01"))
            repeated = build_source_inventory(
                {"files": list(reversed(catalog["files"]))},
                root,
                _scope("Track01"),
            )

            self.assertEqual(inventory, repeated)
            self.assertEqual(inventory["source_count"], 2)
            self.assertEqual(
                [item["source_file_uri"] for item in inventory["sources"]],
                ["LAS/Job_A_Track01_a.las", "LAS/Job_A_Track01_b.las"],
            )
            self.assertEqual(
                [item["source_file_index"] for item in inventory["sources"]],
                [0, 1],
            )
            self.assertEqual(
                inventory["sources"][0]["source_file_sha256"],
                hashlib.sha256(b"source-a").hexdigest(),
            )
            serialized = json.dumps(inventory, ensure_ascii=False)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("missing-track02", serialized)

    def test_hash_cache_reuses_unchanged_file_and_invalidates_size_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"first")
            catalog = {"files": [_catalog_row(source, "Track01")]}
            cache = SourceHashCache()

            first = build_source_inventory(catalog, root, _scope("Track01"), cache)
            second = build_source_inventory(catalog, root, _scope("Track01"), cache)
            self.assertEqual(first, second)
            self.assertEqual(cache.hash_count, 1)

            old_mtime = source.stat().st_mtime_ns
            source.write_bytes(b"second-longer")
            os.utime(source, ns=(old_mtime + 10_000_000, old_mtime + 10_000_000))
            third = build_source_inventory(catalog, root, _scope("Track01"), cache)

            self.assertEqual(cache.hash_count, 2)
            self.assertNotEqual(
                first["sources"][0]["source_file_sha256"],
                third["sources"][0]["source_file_sha256"],
            )

    def test_plain_mapping_is_supported_as_run_hash_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"content")
            cache: dict[str, object] = {}
            catalog = {"files": [_catalog_row(source, "Track01")]}

            build_source_inventory(catalog, root, _scope("Track01"), cache)
            with mock.patch(
                "mms_shp_detection.object_crops.source_inventory._hash_source"
            ) as hash_source:
                build_source_inventory(catalog, root, _scope("Track01"), cache)

            hash_source.assert_not_called()
            self.assertEqual(len(cache), 1)

    def test_source_mutation_aborts_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"before")
            catalog = {"files": [_catalog_row(source, "Track01")]}

            def mutate_after_hash(path: Path, snapshot: object) -> tuple[str, object]:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                path.write_bytes(b"after-and-a-different-size")
                return digest, snapshot

            with (
                mock.patch(
                    "mms_shp_detection.object_crops.source_inventory._hash_source",
                    side_effect=mutate_after_hash,
                ),
                self.assertRaises(SourceMutationError),
            ):
                build_source_inventory(catalog, root, _scope("Track01"))

    def test_strict_scope_rejects_unknown_identity_and_empty_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "unknown.las"
            source.write_bytes(b"not-opened")

            with self.assertRaises(SourceScopeError):
                build_source_inventory(
                    {"files": [{"path": str(source), "source_type": "las"}]},
                    root,
                    _scope("Track01"),
                )
            with self.assertRaisesRegex(SourceScopeError, "at least one job"):
                build_source_inventory(
                    {"files": []},
                    root,
                    SourceScope.from_value({"strict": True, "jobs": []}),
                )

    def test_strict_scope_rejects_every_invalid_identity_even_with_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"source")
            valid = _catalog_row(source, "Track01")
            invalid_pairs = (
                (None, "Track01"),
                ("", "Track01"),
                ("../Job_A", "Track01"),
                ("Job_A", " "),
                (42, "Track01"),
            )

            for job_id, track_id in invalid_pairs:
                invalid = {
                    "path": str(source),
                    "source_type": "las",
                    "job_name": job_id,
                    "track_name": track_id,
                }
                with self.subTest(job_id=job_id, track_id=track_id), self.assertRaises(
                    SourceScopeError
                ):
                    build_source_inventory(
                        {"files": [valid, invalid]},
                        root,
                        _scope("Track01"),
                    )

    def test_strict_scope_rejects_when_only_another_track_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track02.las"
            source.write_bytes(b"track02")

            with self.assertRaisesRegex(SourceScopeError, "selected no"):
                build_source_inventory(
                    {"files": [_catalog_row(source, "Track02")]},
                    root,
                    _scope("Track01"),
                )

    def test_non_strict_scope_skips_unknown_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "unknown.las"
            source.write_bytes(b"not-opened")
            inventory = build_source_inventory(
                {"files": [{"path": str(source), "source_type": "las"}]},
                root,
                SourceScope.from_value({"strict": False, "jobs": []}),
            )

            self.assertEqual(inventory["sources"], [])
            self.assertEqual(inventory["source_count"], 0)

    def test_pcdb_is_always_provenance_incomplete_and_not_training(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.pcdb"
            source.write_bytes(b"legacy-pcdb")
            row = _catalog_row(source, "Track01", source_type="pcdb")
            row["provenance_complete"] = True
            row["training"] = True

            inventory = build_source_inventory(
                {"files": [row]}, root, _scope("Track01")
            )
            entry = inventory["sources"][0]

            self.assertEqual(entry["source_type"], "pcdb")
            self.assertFalse(entry["provenance_complete"])
            self.assertFalse(entry["training"])

    def test_catalog_source_type_must_match_actual_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            disguised_pcdb = root / "Job_A_Track01.pcdb"
            disguised_pcdb.write_bytes(b"legacy-pcdb")
            row = _catalog_row(disguised_pcdb, "Track01", source_type="las")

            with self.assertRaisesRegex(SourceInventoryError, "does not match"):
                build_source_inventory({"files": [row]}, root, _scope("Track01"))

            valid = build_source_inventory(
                {"files": [_catalog_row(disguised_pcdb, "Track01", "pcdb")]},
                root,
                _scope("Track01"),
            )
            valid["sources"][0]["source_type"] = "las"
            with self.assertRaisesRegex(SourceInventoryError, "source_file_uri"):
                validate_source_inventory(valid)

    def test_source_type_validation_is_case_normalized_for_pcdb_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.pcdb"
            source.write_bytes(b"legacy")
            inventory = build_source_inventory(
                {"files": [_catalog_row(source, "Track01", "PCDB")]},
                root,
                _scope("Track01"),
            )
            entry = inventory["sources"][0]
            self.assertEqual(entry["source_type"], "pcdb")

            entry["source_type"] = "PCDB"
            validate_source_inventory(inventory)
            entry["provenance_complete"] = True
            with self.assertRaisesRegex(SourceInventoryError, "PCDB"):
                validate_source_inventory(inventory)

    def test_root_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as outside_text:
            root = Path(root_text)
            outside = Path(outside_text) / "Job_A_Track01.las"
            outside.write_bytes(b"outside")

            with self.assertRaisesRegex(SourceInventoryError, "escaped"):
                build_source_inventory(
                    {"files": [_catalog_row(outside, "Track01")]},
                    root,
                    _scope("Track01"),
                )

    def test_atomic_writer_persists_canonical_bytes_and_returns_exact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"source")
            inventory = build_source_inventory(
                {"files": [_catalog_row(source, "Track01")]},
                root,
                _scope("Track01"),
            )
            output = root / "object_crops_v1" / "source_inventory.json"

            first = write_source_inventory(inventory, output)
            first_bytes = output.read_bytes()
            second = write_source_inventory(inventory, output)

            self.assertEqual(first, second)
            self.assertEqual(first["sha256"], hashlib.sha256(first_bytes).hexdigest())
            self.assertEqual(first["bytes"], len(first_bytes))
            self.assertEqual(json.loads(first_bytes), inventory)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_inventory_validator_rejects_pcdb_training_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.pcdb"
            source.write_bytes(b"legacy")
            inventory = build_source_inventory(
                {"files": [_catalog_row(source, "Track01", "pcdb")]},
                root,
                _scope("Track01"),
            )
            inventory["sources"][0]["training"] = True

            with self.assertRaisesRegex(SourceInventoryError, "complete provenance"):
                validate_source_inventory(inventory)

    def test_inventory_validator_requires_exact_integer_count_and_nonempty_strict_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Job_A_Track01.las"
            source.write_bytes(b"source")
            inventory = build_source_inventory(
                {"files": [_catalog_row(source, "Track01")]},
                root,
                _scope("Track01"),
            )

            for invalid_count in (True, 1.0, "1"):
                invalid = {**inventory, "source_count": invalid_count}
                with self.subTest(value=invalid_count), self.assertRaisesRegex(
                    SourceInventoryError, "source_count"
                ):
                    validate_source_inventory(invalid)

            empty = {**inventory, "source_count": 0, "sources": []}
            with self.assertRaisesRegex(SourceInventoryError, "at least one source"):
                validate_source_inventory(empty)

    def test_real_las_inventory_tuple_reloads_bit_exact_source_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "delivery" / "Job_A_Track01.las"
            source.parent.mkdir()
            header = laspy.LasHeader(point_format=7, version="1.4")
            header.scales = np.asarray([0.001, 0.001, 0.001])
            header.offsets = np.asarray([300_000.0, 4_100_000.0, 100.0])
            las = laspy.LasData(header)
            xyz = np.asarray(
                [
                    [300_000.001, 4_100_000.002, 100.003],
                    [300_000.011, 4_100_000.012, 100.013],
                    [300_000.021, 4_100_000.022, 100.023],
                ],
                dtype=np.float64,
            )
            rgb_raw = np.asarray(
                [[0, 257, 65_535], [1, 32_768, 65_534], [17, 500, 60_000]],
                dtype=np.uint16,
            )
            point_source_id = np.asarray([0, 32_768, 65_535], dtype=np.uint16)
            scan_angle_raw = np.asarray([-30_000, 1234, 30_000], dtype=np.int16)
            las.x = xyz[:, 0]
            las.y = xyz[:, 1]
            las.z = xyz[:, 2]
            las.red = rgb_raw[:, 0]
            las.green = rgb_raw[:, 1]
            las.blue = rgb_raw[:, 2]
            las.point_source_id = point_source_id
            las.points.array["scan_angle"] = scan_angle_raw
            las.write(source)

            catalog_row = _catalog_row(source, "Track01")
            inventory = build_source_inventory(
                {"files": [catalog_row]},
                root,
                _scope("Track01"),
            )
            inventory_source = inventory["sources"][0]
            self.assertEqual(inventory_source["source_file_index"], 0)
            self.assertEqual(
                inventory_source["source_file_uri"],
                "delivery/Job_A_Track01.las",
            )
            self.assertEqual(
                inventory_source["source_file_sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )

            source_point_index = 1
            reloaded_path = root / inventory_source["source_file_uri"]
            with PointCloudReaderCache() as readers:
                records = readers.read_block_records(
                    {
                        "path": str(reloaded_path),
                        "source_type": inventory_source["source_type"],
                    },
                    {
                        "name": "las:1:1",
                        "source_type": "las",
                        "start": source_point_index,
                        "count": 1,
                    },
                )

            self.assertEqual(
                (inventory_source["source_file_index"], records["source_index"][0]),
                (0, source_point_index),
            )
            np.testing.assert_array_equal(records["xyz"], xyz[1:2])
            np.testing.assert_array_equal(records["rgb_raw"], rgb_raw[1:2])
            np.testing.assert_array_equal(
                records["point_source_id"], point_source_id[1:2]
            )
            np.testing.assert_array_equal(
                records["scan_angle"], scan_angle_raw[1:2]
            )
            self.assertTrue(records["field_availability"]["rgb_raw"][0])
            self.assertTrue(records["field_availability"]["point_source_id"][0])
            self.assertTrue(records["field_availability"]["scan_angle"][0])
            self.assertFalse(records["field_availability"]["scan_angle_rank"][0])


if __name__ == "__main__":
    unittest.main()
