from __future__ import annotations

import argparse
import importlib
import json
import unittest

import numpy as np

from mms_shp_detection.object_crops import (
    AvailabilityBit,
    GeometryFamily,
    OBJECT_CROP_SCHEMA_VERSION,
    ObjectCropCanonicalizationConfig,
    ObjectCropConfig,
    ObjectCropOutputConfig,
    ObjectCropRayConfig,
    ObjectCropRoutingConfig,
    ObjectCropSelectionConfig,
    PointRole,
    SourcePointBatch,
    SourceScope,
    canonical_json_bytes,
    concat_record_batches,
    dedupe_source_records,
    parse_object_crop_config,
    source_record_keys,
    take_records,
)


class ObjectCropContractTests(unittest.TestCase):
    def test_package_skeleton_is_import_safe(self) -> None:
        modules = (
            "ids",
            "observation_evidence",
            "assembly_graph",
            "geometry_router",
            "selection",
            "roles",
            "canonicalization",
            "writer",
            "validator",
            "publisher",
            "manifest",
        )
        for module in modules:
            imported = importlib.import_module(f"mms_shp_detection.object_crops.{module}")
            self.assertEqual(imported.__all__, [])

    def test_default_config_is_disabled_and_serializes_all_frozen_defaults(self) -> None:
        config = ObjectCropConfig.from_value(None)

        self.assertEqual(
            config.to_dict(),
            {
                "enabled": False,
                "schema_version": "1.0.0",
                "fail_pipeline_on_error": False,
                "source_scope": {"strict": True, "jobs": []},
                "output": {
                    "directory_name": "object_crops_v1",
                    "evidence_directory_name": "object_crop_evidence",
                    "write_audit_laz": True,
                    "write_preview": True,
                },
                "selection": {
                    "point_order": "source_file_hash_then_record_index",
                    "panel_margin_m": [0.5, 1.0, 0.5],
                    "pole_radial_margin_m": 0.5,
                    "base_ground_radius_m": 1.5,
                    "vertical_bottom_margin_m": 0.3,
                    "vertical_top_margin_m": 0.5,
                    "context_margin_m": 0.75,
                    "max_points": 200000,
                },
                "canonicalization": {
                    "convention": "panel_front_plus_y_z_up",
                    "min_confidence_for_model_input": 0.5,
                    "min_confidence_for_training": 0.8,
                },
                "routing": {
                    "direct_max_panel_support_offset_m": 0.75,
                    "keep_unsupported_samples": True,
                },
                "rays": {"mode": "off"},
            },
        )
        self.assertEqual(config.schema_version, OBJECT_CROP_SCHEMA_VERSION)

    def test_source_scope_is_canonical_and_exact_without_substring_matching(self) -> None:
        left = SourceScope.from_value(
            {
                "strict": True,
                "jobs": [
                    {"job_id": " Job_B ", "tracks": ["Track02", "Track01"]},
                    {"job_id": "Job_A", "tracks": ["Track03"]},
                ],
            }
        )
        right = SourceScope.from_value(
            {
                "jobs": [
                    {"job_id": "job_a", "tracks": ["track03"]},
                    {"job_id": "job_b", "tracks": ["track01", "track02"]},
                ],
                "strict": True,
            }
        )

        self.assertEqual(
            left.to_dict(),
            {
                "strict": True,
                "jobs": [
                    {"job_id": "Job_A", "tracks": ["Track03"]},
                    {"job_id": "Job_B", "tracks": ["Track01", "Track02"]},
                ],
            },
        )
        self.assertEqual(left.scope_id, right.scope_id)
        self.assertTrue(left.allows("job_a", "TRACK03"))
        self.assertFalse(left.allows("Job_A_old", "Track03"))
        self.assertFalse(left.allows("Job_A", "Track030"))
        self.assertFalse(left.allows(None, "Track03"))

    def test_non_strict_empty_scope_allows_only_parsed_identity(self) -> None:
        scope = SourceScope.from_value({"strict": False, "jobs": []})

        self.assertTrue(scope.allows("Job_A", "Track01"))
        self.assertFalse(scope.allows(None, "Track01"))
        self.assertFalse(scope.allows("Job_A", None))

    def test_scope_rejects_duplicate_and_unsafe_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate track"):
            SourceScope.from_value(
                {
                    "jobs": [
                        {"job_id": "Job_A", "tracks": ["Track01", "track01"]}
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "duplicate job"):
            SourceScope.from_value(
                {
                    "jobs": [
                        {"job_id": "Job_A", "tracks": ["Track01"]},
                        {"job_id": "job_a", "tracks": ["Track02"]},
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "path-safe"):
            SourceScope.from_value(
                {"jobs": [{"job_id": "../Job_A", "tracks": ["Track01"]}]}
            )

    def test_enabled_strict_config_requires_exact_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires at least one job"):
            ObjectCropConfig.from_value({"enabled": True})

        config = ObjectCropConfig.from_value(
            {
                "enabled": True,
                "source_scope": {
                    "strict": True,
                    "jobs": [{"job_id": "Job_A", "tracks": ["Track01"]}],
                },
            }
        )
        self.assertTrue(config.enabled)
        self.assertTrue(config.source_scope.allows("Job_A", "Track01"))

    def test_config_rejects_unknown_keys_types_ranges_and_unsafe_paths(self) -> None:
        invalid_values = (
            ({"mystery": True}, "unknown key"),
            ({"enabled": 1}, "boolean"),
            ({"schema_version": "2.0.0"}, "must be '1.0.0'"),
            ({"output": {"directory_name": "../escape"}}, "safe relative"),
            ({"selection": {"panel_margin_m": [0.1, 0.2]}}, "exactly three"),
            ({"selection": {"max_points": 0}}, "greater than zero"),
            (
                {
                    "canonicalization": {
                        "min_confidence_for_model_input": 0.9,
                        "min_confidence_for_training": 0.8,
                    }
                },
                "must be >=",
            ),
            ({"rays": {"mode": "guessed"}}, "must be 'off'"),
        )
        for value, message in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(
                (TypeError, ValueError), message
            ):
                ObjectCropConfig.from_value(value)

    def test_direct_nested_dataclass_construction_cannot_bypass_validation(self) -> None:
        invalid_constructors = (
            lambda: ObjectCropOutputConfig(directory_name="../escape"),
            lambda: ObjectCropOutputConfig(write_preview=1),
            lambda: ObjectCropSelectionConfig(max_points=True),
            lambda: ObjectCropSelectionConfig(panel_margin_m=(0.5, -1.0, 0.5)),
            lambda: ObjectCropCanonicalizationConfig(
                min_confidence_for_model_input=0.9,
                min_confidence_for_training=0.8,
            ),
            lambda: ObjectCropRoutingConfig(keep_unsupported_samples="yes"),
            lambda: ObjectCropRayConfig(mode="guessed"),
        )
        for constructor in invalid_constructors:
            with self.subTest(constructor=constructor), self.assertRaises(
                (TypeError, ValueError)
            ):
                constructor()

    def test_argparse_adapter_accepts_json_and_returns_canonical_mapping(self) -> None:
        parsed = parse_object_crop_config(
            json.dumps(
                {
                    "enabled": True,
                    "source_scope": {
                        "strict": True,
                        "jobs": [
                            {"job_id": "Job_A", "tracks": ["Track02", "Track01"]}
                        ],
                    },
                }
            )
        )

        self.assertIsInstance(parsed, dict)
        self.assertEqual(
            parsed["source_scope"]["jobs"][0]["tracks"],
            ["Track01", "Track02"],
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_object_crop_config("{not-json")

    def test_canonical_json_is_order_independent_and_rejects_nan(self) -> None:
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            canonical_json_bytes({"a": 1, "b": 2}),
        )
        with self.assertRaises(ValueError):
            canonical_json_bytes({"bad": float("nan")})

    def test_vocabulary_values_and_availability_bits_are_stable(self) -> None:
        self.assertEqual(PointRole.PANEL.value, "panel")
        self.assertEqual(
            GeometryFamily.DIRECT_SINGLE_POLE_SINGLE_PANEL.value,
            "direct_single_pole_single_panel",
        )
        combined = AvailabilityBit.RGB_RAW | AvailabilityBit.SOURCE_INDEX
        self.assertTrue(combined & AvailabilityBit.RGB_RAW)
        self.assertTrue(combined & AvailabilityBit.SOURCE_INDEX)


class SourceRecordContractTests(unittest.TestCase):
    @staticmethod
    def _batch(source_indices: list[int]) -> SourcePointBatch:
        count = len(source_indices)
        availability = np.zeros(
            count,
            dtype=[("rgb_raw", np.bool_), ("scan_angle", np.bool_)],
        )
        availability["rgb_raw"] = [index % 2 == 0 for index in range(count)]
        records = {
            "xyz": np.arange(count * 3, dtype=np.float64).reshape(count, 3),
            "rgb_raw": np.arange(count * 3, dtype=np.uint16).reshape(count, 3),
            "intensity": np.arange(count, dtype=np.uint16),
            "source_index": np.asarray(source_indices, dtype=np.int64),
            "field_availability": availability,
        }
        return SourcePointBatch(
            records=records,
            source_file_uri="Job_A/Track01/source.las",
            source_file_index=7,
            source_file_sha256="a" * 64,
            block_start=max(0, min(source_indices)) if source_indices else 0,
            field_availability={"intensity": True},
            field_metadata={
                "scan_angle": {
                    "source_dimension": "scan_angle",
                    "source_dtype": "int16",
                    "semantic": "las_1_4_scan_angle_raw",
                }
            },
        )

    def test_batch_reads_structured_and_explicit_availability(self) -> None:
        batch = self._batch([10, 11, 12])

        np.testing.assert_array_equal(batch.availability_for("rgb_raw"), [True, False, True])
        np.testing.assert_array_equal(batch.availability_for("intensity"), [True, True, True])
        np.testing.assert_array_equal(batch.availability_for("gps_time"), [False, False, False])
        self.assertEqual(batch.point_count, 3)

    def test_take_applies_one_selection_to_every_row_array(self) -> None:
        batch = self._batch([10, 11, 12, 13])

        selected = take_records(batch, np.asarray([False, True, False, True]))

        np.testing.assert_array_equal(selected.records["source_index"], [11, 13])
        np.testing.assert_array_equal(selected.records["intensity"], [1, 3])
        np.testing.assert_array_equal(
            selected.records["field_availability"]["rgb_raw"],
            [False, False],
        )
        self.assertEqual(selected.block_start, 11)
        self.assertEqual(selected.field_metadata, batch.field_metadata)

    def test_empty_boolean_mask_still_must_match_batch_length(self) -> None:
        batch = self._batch([10, 11])

        with self.assertRaisesRegex(ValueError, r"shape \(N,\)"):
            take_records(batch, np.asarray([], dtype=np.bool_))

    def test_concat_preserves_fields_and_dedupe_is_stable(self) -> None:
        original = self._batch([4, 5, 5, -1, -1])
        left = take_records(original, np.asarray([0, 1]))
        right = take_records(original, np.asarray([2, 3, 4]))

        combined = concat_record_batches([left, right])
        deduplicated = dedupe_source_records(combined)

        np.testing.assert_array_equal(combined.records["source_index"], [4, 5, 5, -1, -1])
        np.testing.assert_array_equal(deduplicated.records["source_index"], [4, 5, -1, -1])
        np.testing.assert_array_equal(
            source_record_keys(deduplicated),
            [[7, 4], [7, 5], [7, -1], [7, -1]],
        )

    def test_batch_rejects_misaligned_records_and_unsafe_provenance(self) -> None:
        records = {
            "xyz": np.zeros((2, 3), dtype=np.float64),
            "source_index": np.zeros(1, dtype=np.int64),
        }
        with self.assertRaisesRegex(ValueError, "not aligned"):
            SourcePointBatch(
                records=records,
                source_file_uri="source.las",
                source_file_index=0,
                source_file_sha256="a" * 64,
                block_start=0,
            )
        with self.assertRaisesRegex(ValueError, "POSIX"):
            SourcePointBatch(
                records={
                    "xyz": np.zeros((1, 3), dtype=np.float64),
                    "source_index": np.zeros(1, dtype=np.int64),
                },
                source_file_uri="..\\source.las",
                source_file_index=0,
                source_file_sha256="a" * 64,
                block_start=0,
            )
        with self.assertRaisesRegex(TypeError, "source_file_index must be an integer"):
            SourcePointBatch(
                records={
                    "xyz": np.zeros((1, 3), dtype=np.float64),
                    "source_index": np.zeros(1, dtype=np.int64),
                },
                source_file_uri="source.las",
                source_file_index=1.5,  # type: ignore[arg-type]
                source_file_sha256="a" * 64,
                block_start=0,
            )


if __name__ == "__main__":
    unittest.main()
