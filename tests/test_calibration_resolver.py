from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mms_shp_detection.calibration import attach_calibration_metadata
from mms_shp_detection.domain.calibration import (
    CALIBRATION_AMBIGUOUS,
    CALIBRATION_INVALID,
    CALIBRATION_NOT_FOUND,
    CALIBRATION_VERSION_UNSUPPORTED,
    CalibrationResolver,
    delivery_calibration_fingerprint,
)
from mms_shp_detection.domain.models import PipelineError


def _track(
    job: str,
    track: str,
    *,
    calibration_id: str | None = None,
) -> dict:
    value = {
        "job": job,
        "track": track,
        "camera": {
            "imaging_sensors": [
                {
                    "sensor_id": 7,
                    "friendly_name": "Sphere",
                    "output_model": "Sphere",
                    "output_width": 8,
                    "output_height": 4,
                    "cameras": [{"serial": "front"}, {"serial": "rear"}],
                }
            ]
        },
        "time": {"gps_week": 2357},
    }
    if calibration_id is not None:
        value["calibration_id"] = calibration_id
    return value


def _write_bundle(path: Path, tracks: list[dict]) -> None:
    path.write_text(
        json.dumps({"schema_version": 2, "tracks": tracks}),
        encoding="utf-8",
    )


def _task(job: str, track: str | None, image_name: str = "frame.jpg") -> dict:
    return {
        "job_name": job,
        "track_name": track,
        "image_name": image_name,
        "gps_week": 2357,
        "panorama": {"image_width": 8, "image_height": 4},
    }


class CalibrationResolverTests(unittest.TestCase):
    def test_exact_match_normalizes_case_whitespace_and_container_suffixes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calibration.json"
            _write_bundle(
                path,
                [_track("Job_2025.job", "Track01.scan", calibration_id="CAL-A")],
            )
            tasks = [_task("  JOB_2025.JOB ", " track01.SCAN ")]
            before = copy.deepcopy(tasks)

            resolution = CalibrationResolver(path).resolve(tasks, required=True)

        self.assertTrue(resolution.ok)
        self.assertEqual(tasks, before)
        self.assertEqual(len(resolution.matches), 1)
        match = resolution.matches[0]
        self.assertEqual(match.calibration_id, "CAL-A")
        self.assertEqual(match.matched_by, "exact_job_track")
        self.assertEqual(match.candidate_count, 1)
        self.assertEqual(resolution.normalized_keys, ("job_2025/track01",))
        self.assertEqual(resolution.available_keys_sample, ("job_2025/track01",))

    def test_delivery_calibration_matches_without_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini_path = root / "MMS.ini"
            internal_path = root / "Internal Orientation.txt"
            ini_path.write_bytes(b"ini")
            internal_path.write_bytes(b"orientation")
            task = _task("Delivery.job", "Track07.scan")
            task["delivery_calibration"] = {
                "ini_path": str(ini_path),
                "internal_orientation_path": str(internal_path),
                "serial_number": "SERIAL-7",
            }

            resolution = CalibrationResolver(root / "missing.json").resolve(
                [task],
                required=True,
            )

        self.assertIsNone(resolution.bundle)
        self.assertEqual(len(resolution.matches), 1)
        match = resolution.matches[0]
        self.assertEqual(match.calibration_id, "SERIAL-7")
        self.assertEqual(match.matched_by, "delivery_job_track")
        self.assertEqual(match.source_path, ini_path.resolve())
        self.assertEqual(len(match.fingerprint), 64)
        self.assertFalse(resolution.issues)

    def test_delivery_fingerprint_is_cached_by_calibration_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini_path = root / "MMS.ini"
            internal_path = root / "Internal Orientation.txt"
            ini_path.write_bytes(b"ini")
            internal_path.write_bytes(b"orientation")
            delivery = {
                "ini_path": str(ini_path),
                "internal_orientation_path": str(internal_path),
                "serial_number": "SERIAL-7",
            }
            tasks = [_task("Delivery.job", "Track07.scan") for _ in range(4)]
            for task in tasks:
                task["delivery_calibration"] = delivery.copy()

            with mock.patch(
                "mms_shp_detection.domain.calibration.delivery_calibration_fingerprint",
                wraps=delivery_calibration_fingerprint,
            ) as fingerprint:
                resolution = CalibrationResolver(None).resolve(tasks, required=True)

        self.assertTrue(resolution.ok)
        self.assertEqual(fingerprint.call_count, 1)
        self.assertEqual(len(resolution.matches), 1)
        for index in range(len(tasks)):
            match = resolution.match_for_index(index)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.fingerprint, resolution.matches[0].fingerprint)

    def test_invalid_and_unsupported_bundle_versions_are_structured_errors(
        self,
    ) -> None:
        cases = (
            ({"tracks": []}, CALIBRATION_INVALID),
            ({"schema_version": "2", "tracks": []}, CALIBRATION_INVALID),
            (
                {"schema_version": 999, "tracks": []},
                CALIBRATION_VERSION_UNSUPPORTED,
            ),
            ({"schema_version": 2, "tracks": [None]}, CALIBRATION_INVALID),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calibration.json"
            for document, expected_code in cases:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(PipelineError) as captured:
                        CalibrationResolver(path, job_id="run-schema").resolve([])

                    info = captured.exception.info
                    self.assertEqual(info.code, expected_code)
                    self.assertEqual(info.job_id, "run-schema")
                    self.assertEqual(info.stage, "attach_calibration")
                    self.assertFalse(info.retryable)
                    self.assertEqual(
                        info.context["supported_schema_versions"],
                        [2],
                    )

            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(PipelineError) as captured:
                CalibrationResolver(path, job_id="run-invalid-json").resolve([])

            self.assertEqual(captured.exception.info.code, CALIBRATION_INVALID)
            self.assertEqual(captured.exception.info.cause_type, "JSONDecodeError")

    def test_ambiguous_and_all_missing_keys_are_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calibration.json"
            _write_bundle(
                path,
                [
                    _track("Job_A.job", "Track01.scan", calibration_id="A1"),
                    _track("job_a", "TRACK01", calibration_id="A2"),
                    _track("Job_B", "Track02", calibration_id="B"),
                ],
            )
            tasks = [
                _task("JOB_A", "track01"),
                _task("Job_Missing", "Track03", "missing-a.jpg"),
                _task("job_missing", "track03.scan", "missing-b.jpg"),
                _task("Job_Other", "Track04"),
            ]
            before = copy.deepcopy(tasks)
            resolver = CalibrationResolver(path, job_id="run-1")

            resolution = resolver.resolve(tasks)
            with self.assertRaises(PipelineError) as captured:
                resolver.resolve(tasks, required=True)

        self.assertEqual(tasks, before)
        self.assertEqual(
            [issue.code for issue in resolution.issues],
            [
                CALIBRATION_AMBIGUOUS,
                CALIBRATION_NOT_FOUND,
                CALIBRATION_NOT_FOUND,
            ],
        )
        self.assertEqual(resolution.issues[0].candidate_count, 2)
        self.assertEqual(
            {issue.normalized_key for issue in resolution.issues},
            {"job_a/track01", "job_missing/track03", "job_other/track04"},
        )
        info = captured.exception.info
        self.assertEqual(info.code, CALIBRATION_AMBIGUOUS)
        self.assertEqual(info.job_id, "run-1")
        self.assertFalse(info.retryable)
        self.assertIn("searched_roots", info.context)
        self.assertIn("normalized_keys", info.context)
        self.assertIn("available_keys_sample", info.context)
        self.assertEqual(len(info.context["issues"]), 3)

    def test_required_missing_only_uses_not_found_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "calibration.json"
            tasks = [_task("Job_A", "Track01"), _task("Job_B", "Track02")]
            with self.assertRaises(PipelineError) as captured:
                CalibrationResolver(missing_path).resolve(tasks, required=True)

        info = captured.exception.info
        self.assertEqual(info.code, CALIBRATION_NOT_FOUND)
        self.assertEqual(
            {item["normalized_key"] for item in info.context["issues"]},
            {"job_a/track01", "job_b/track02"},
        )
        self.assertEqual(
            info.context["normalized_keys"], ["job_a/track01", "job_b/track02"]
        )


class CalibrationAttachCompatibilityTests(unittest.TestCase):
    def test_attach_keeps_legacy_success_payload_and_bundle_return(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calibration.json"
            _write_bundle(path, [_track("Job_A.job", "Track01.scan")])
            expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            task = _task("job_a", "TRACK01")
            resolution = CalibrationResolver(path).resolve([task], required=True)

            with mock.patch(
                "mms_shp_detection.calibration.CalibrationResolver.resolve",
                side_effect=AssertionError("resolution should be reused"),
            ):
                bundle = attach_calibration_metadata(
                    [task],
                    path,
                    mock.Mock(),
                    require_calibration=True,
                    resolution=resolution,
                )

        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.assertEqual(bundle["sha256"], expected_sha256)
        self.assertEqual(task["calibration"]["calibration_sha256"], expected_sha256)
        self.assertEqual(task["calibration"]["job"], "Job_A.job")
        self.assertEqual(task["calibration"]["track"], "Track01.scan")
        self.assertEqual(
            task["calibration"]["application"],
            "validated_only_already_applied_to_leica_sphere",
        )

    def test_attach_required_failure_does_not_partially_mutate_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calibration.json"
            _write_bundle(path, [_track("Job_A", "Track01")])
            tasks = [
                _task("Job_A", "Track01", "matched.jpg"),
                _task("Job_Missing", "Track02", "missing.jpg"),
            ]
            before = copy.deepcopy(tasks)

            with self.assertRaises(PipelineError) as captured:
                attach_calibration_metadata(
                    tasks,
                    path,
                    mock.Mock(),
                    require_calibration=True,
                )

        self.assertEqual(captured.exception.info.code, CALIBRATION_NOT_FOUND)
        self.assertEqual(tasks, before)

    def test_precomputed_delivery_resolution_rejects_same_key_task_reordering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def delivery_task(label: str) -> dict:
                ini_path = root / f"{label}.ini"
                internal_path = root / f"{label}.txt"
                ini_path.write_bytes(label.encode())
                internal_path.write_bytes(f"{label}-orientation".encode())
                task = _task("Delivery.job", "Track07.scan", f"{label}.jpg")
                task["delivery_calibration"] = {
                    "ini_path": str(ini_path),
                    "internal_orientation_path": str(internal_path),
                    "serial_number": label,
                }
                return task

            first = delivery_task("first")
            second = delivery_task("second")
            resolution = CalibrationResolver(None).resolve(
                [first, second],
                required=True,
            )
            before = copy.deepcopy([second, first])

            with self.assertRaisesRegex(ValueError, "task identities or order"):
                attach_calibration_metadata(
                    [second, first],
                    None,
                    mock.Mock(),
                    require_calibration=True,
                    resolution=resolution,
                )

        self.assertEqual([second, first], before)

    def test_precomputed_delivery_resolution_rejects_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ini_path = root / "MMS.ini"
            internal_path = root / "Internal Orientation.txt"
            ini_path.write_bytes(b"ini")
            internal_path.write_bytes(b"orientation")
            task = _task("Delivery.job", "Track07.scan")
            task["delivery_calibration"] = {
                "ini_path": str(ini_path),
                "internal_orientation_path": str(internal_path),
                "serial_number": "before",
            }
            resolution = CalibrationResolver(None).resolve([task], required=True)
            task["delivery_calibration"]["serial_number"] = "after"

            with self.assertRaisesRegex(ValueError, "task identities or order"):
                attach_calibration_metadata(
                    [task],
                    None,
                    mock.Mock(),
                    require_calibration=True,
                    resolution=resolution,
                )


if __name__ == "__main__":
    unittest.main()
