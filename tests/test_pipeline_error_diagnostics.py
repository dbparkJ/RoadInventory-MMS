from __future__ import annotations

import errno
import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mms_shp_detection import pipeline
from mms_shp_detection.app.pipeline_service import (
    pipeline_error_info,
    pipeline_permission_operation,
    tracked_stage,
)
from mms_shp_detection.config import ConfigError
from mms_shp_detection.domain.models import PipelineError, PipelineErrorInfo
from mms_shp_detection.infrastructure.manifest_writer import RunManifestStore


class PipelinePermissionDiagnosticTests(unittest.TestCase):
    def test_unclassified_errors_keep_existing_api_contract(self) -> None:
        for error, code in (
            (PermissionError("legacy message"), "OUTPUT_WRITE_FAILED"),
            (FileNotFoundError("missing input"), "INPUT_FILE_MISSING"),
            (ConfigError("invalid option"), "CONFIG_INVALID"),
            (RuntimeError("failed operation"), "PIPELINE_FAILED"),
        ):
            with self.subTest(code=code):
                info = pipeline_error_info(
                    error, job_id="run-one", stage="discover_inputs"
                )
                self.assertEqual(info.code, code)
                self.assertEqual(info.message, str(error))
                self.assertEqual(info.context, {})
                self.assertFalse(info.retryable)

    def test_known_permission_operations_use_safe_distinct_diagnostics(self) -> None:
        for operation, code in (
            ("read_input", "INPUT_READ_FAILED"),
            ("write_output", "OUTPUT_WRITE_FAILED"),
        ):
            with self.subTest(operation=operation):
                error = PermissionError(
                    errno.EACCES, "private detail", "C:/private/source.csv"
                )
                info = pipeline_error_info(
                    error, job_id="run-one", stage="mixed_stage", operation=operation
                )
                self.assertEqual(info.code, code)
                self.assertEqual(info.context, {"operation": operation})
                self.assertEqual(info.cause_type, "PermissionError")
                self.assertEqual(info.job_id, "run-one")
                self.assertEqual(info.stage, "mixed_stage")
                self.assertFalse(info.retryable)
                self.assertNotIn("private", json.dumps(info.to_dict()))

    def test_innermost_permission_boundary_preserves_original_exception(self) -> None:
        original = PermissionError("read denied")
        with (
            self.assertRaises(PermissionError) as raised,
            pipeline_permission_operation("write_output"),
            pipeline_permission_operation("read_input"),
        ):
            raise original
        self.assertIs(raised.exception, original)
        info = pipeline_error_info(original, job_id="run-one", stage="mixed_stage")
        self.assertEqual(info.code, "INPUT_READ_FAILED")

    def test_operation_boundary_does_not_reclassify_other_failures(self) -> None:
        original = OSError(errno.ENOSPC, "disk full")
        with (
            self.assertRaises(OSError) as raised,
            pipeline_permission_operation("write_output"),
        ):
            raise original
        self.assertIs(raised.exception, original)
        info = pipeline_error_info(original, job_id="run-one", stage="output")
        self.assertEqual(info.code, "PIPELINE_FAILED")
        self.assertEqual(info.context, {})

    def test_existing_structured_error_is_not_overwritten(self) -> None:
        info = PipelineErrorInfo(
            code="CALIBRATION_INVALID",
            message="Invalid calibration.",
            stage="attach_calibration",
            job_id="run-one",
            retryable=False,
        )
        self.assertIs(
            pipeline_error_info(
                PipelineError(info),
                job_id="other",
                stage="other",
                operation="read_input",
            ),
            info,
        )


class PipelineDiagnosticIntegrationTests(unittest.TestCase):
    def _args(self, root: Path):
        model = root / "fixture.pt"
        model.write_bytes(b"contract fixture; inference is not executed")
        return pipeline.build_arg_parser().parse_args(
            [
                "--data-root",
                str(root / "input"),
                "--model-path",
                str(model),
                "--output-dir",
                str(root / "output"),
                "--pose-format",
                "leica-sphere",
            ]
        )

    def test_actual_pose_read_failure_reaches_manifest_as_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            args = self._args(root)
            sphere = (
                root
                / "input"
                / "Export"
                / "JPEG"
                / "Job_20250311_1043"
                / "Track01"
                / "Sphere"
            )
            sphere.mkdir(parents=True)
            pose = sphere / "Job_20250311_1043_Track01_Sphere.csv"
            pose.write_text("fixture", encoding="utf-8")
            original = PermissionError(errno.EACCES, "private input", str(pose))
            real_open = Path.open

            def open_with_denied_pose(path, *open_args, **kwargs):
                if path == pose:
                    raise original
                return real_open(path, *open_args, **kwargs)

            def prepare_inputs(_args):
                return pipeline.prepare_shared_pipeline_context(
                    _args,
                    alignment_report_path=root / "output" / "alignment.json",
                    logger=logging.getLogger(__name__),
                )

            with (
                mock.patch.dict(
                    "os.environ", {"MMS_PIPELINE_JOB_ID": "diagnostic-input"}
                ),
                mock.patch.object(Path, "open", open_with_denied_pose),
                mock.patch.object(
                    pipeline, "_run_pipeline_impl", side_effect=prepare_inputs
                ),
                mock.patch.object(pipeline, "build_pointcloud_catalog") as catalog,
                self.assertRaises(PermissionError) as raised,
            ):
                pipeline.run_pipeline(args)
            self.assertIs(raised.exception, original)
            catalog.assert_not_called()
            document = json.loads(
                (root / "output" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(document["status"], "failed")
            info = document["errors"][-1]
            self.assertEqual(info["code"], "INPUT_READ_FAILED")
            self.assertEqual(info["stage"], "discover_inputs")
            self.assertEqual(info["context"], {"operation": "read_input"})
            self.assertNotIn(str(pose), info["message"])

    def test_atomic_output_write_and_replace_failures_keep_output_diagnostic(
        self,
    ) -> None:
        for failure_at in ("write_text", "replace"):
            with (
                self.subTest(failure_at=failure_at),
                tempfile.TemporaryDirectory() as temporary,
            ):
                path = Path(temporary) / "result.json"
                path.write_text("previous result", encoding="utf-8")
                original = PermissionError(errno.EACCES, "private output", str(path))
                with (
                    mock.patch.object(Path, failure_at, side_effect=original),
                    self.assertRaises(PermissionError) as raised,
                ):
                    pipeline.atomic_write_text(path, "candidate result")
                self.assertIs(raised.exception, original)
                self.assertEqual(path.read_text(encoding="utf-8"), "previous result")
                info = pipeline_error_info(
                    original, job_id="output-run", stage="write_outputs"
                )
                self.assertEqual(info.code, "OUTPUT_WRITE_FAILED")
                self.assertEqual(info.context, {"operation": "write_output"})
                self.assertNotIn("private", info.message)

    def test_failed_stage_recording_keeps_primary_failure(self) -> None:
        manifest = mock.Mock(spec=RunManifestStore)
        manifest.begin_stage.return_value = datetime.now(timezone.utc)
        manifest.record_stage.side_effect = OSError("stage storage unavailable")
        original = RuntimeError("primary processing failure")
        with (
            self.assertRaises(RuntimeError) as raised,
            tracked_stage(manifest, "discover_inputs"),
        ):
            raise original
        self.assertIs(raised.exception, original)
        self.assertTrue(
            any("stage storage unavailable" in note for note in original.__notes__)
        )

    def test_successful_stage_does_not_hide_recording_failure(self) -> None:
        manifest = mock.Mock(spec=RunManifestStore)
        manifest.begin_stage.return_value = datetime.now(timezone.utc)
        manifest.record_stage.side_effect = OSError("stage storage unavailable")
        with (
            self.assertRaisesRegex(OSError, "stage storage unavailable"),
            tracked_stage(manifest, "discover_inputs"),
        ):
            pass

    def test_terminal_manifest_failure_preserves_failure_or_cancellation(self) -> None:
        for error_type in (RuntimeError, KeyboardInterrupt):
            for failure_at in ("read", "transition_terminal"):
                with self.subTest(error_type=error_type, failure_at=failure_at):
                    self._assert_terminal_failure_preserves_primary(
                        error_type, failure_at
                    )

    def _assert_terminal_failure_preserves_primary(
        self, error_type, failure_at
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._args(root)
            original = error_type("primary failure")
            processing_failed = False
            real_method = getattr(RunManifestStore, failure_at)

            def fail_processing(_args):
                nonlocal processing_failed
                processing_failed = True
                raise original

            def fail_manifest(store, *method_args, **kwargs):
                if processing_failed:
                    raise OSError("manifest storage unavailable")
                return real_method(store, *method_args, **kwargs)

            with (
                mock.patch.dict(
                    "os.environ", {"MMS_PIPELINE_JOB_ID": "diagnostic-terminal"}
                ),
                mock.patch.object(
                    pipeline, "_run_pipeline_impl", side_effect=fail_processing
                ),
                mock.patch.object(RunManifestStore, failure_at, fail_manifest),
                self.assertRaises(error_type) as raised,
            ):
                pipeline.run_pipeline(args)
            self.assertIs(raised.exception, original)
            self.assertTrue(
                any("Could not finalize" in note for note in original.__notes__)
            )
            self.assertTrue(
                any(
                    "manifest storage unavailable" in note
                    for note in original.__notes__
                )
            )


if __name__ == "__main__":
    unittest.main()
