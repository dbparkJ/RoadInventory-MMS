from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mms_shp_detection.config import (
    PipelineConfig,
    canonical_config_json,
    config_sha256,
)
from mms_shp_detection.domain.models import JobStatus, StageResult
from mms_shp_detection.infrastructure.manifest_writer import (
    RunManifestStore,
    validate_manifest_document,
)
from mms_shp_detection.pipeline import build_arg_parser, run_pipeline


class PipelineConfigContractTests(unittest.TestCase):
    def test_canonical_hash_is_order_independent_and_normalizes_paths(self) -> None:
        first = {
            "threshold": 0.5,
            "classes": (2, 11),
            "root": Path("sample"),
            "nested": {"b": True, "a": None},
        }
        second = {
            "nested": {"a": None, "b": True},
            "root": Path("sample"),
            "classes": [2, 11],
            "threshold": 0.5,
        }
        self.assertEqual(canonical_config_json(first), canonical_config_json(second))
        self.assertEqual(config_sha256(first), config_sha256(second))

    def test_pipeline_config_excludes_private_runtime_metadata(self) -> None:
        args = argparse.Namespace(
            data_root=Path("data"),
            output_dir=Path("output"),
            conf=0.8,
            _config_path=None,
            _worker_handle=object(),
        )
        config = PipelineConfig.from_namespace(args)
        self.assertNotIn("_worker_handle", config.values)
        self.assertEqual(config.config_hash, config_sha256(config.values))


class RunManifestContractTests(unittest.TestCase):
    def _config(self) -> PipelineConfig:
        return PipelineConfig.from_namespace(
            argparse.Namespace(data_root=Path("data"), output_dir=Path("output"))
        )

    def test_manifest_lifecycle_is_atomic_and_records_stage_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunManifestStore(root / "run_manifest.json")
            document = store.create(
                job_id="Job_A_Track01_20260804T010203Z_deadbeef",
                config=self._config(),
                input_root=root / "input",
                dataset_job="Job_A",
                track="Track01",
            )
            self.assertEqual(document["status"], "pending")
            self.assertEqual(validate_manifest_document(document), ())

            store.transition(JobStatus.VALIDATING)
            started = store.begin_stage("discover_inputs")
            store.record_stage(
                StageResult(
                    stage_name="discover_inputs",
                    stage_version="1",
                    status="succeeded",
                    started_at=started,
                    finished_at=started + timedelta(milliseconds=12),
                    input_count=1,
                    output_count=3,
                )
            )
            store.transition(JobStatus.RUNNING)
            store.update_processing_progress(
                completed_images=2,
                total_images=4,
                detections=5,
                projected_points=4,
                failures=1,
            )
            store.transition(JobStatus.SUCCEEDED)
            summary_json, summary_md = store.write_summary()

            saved = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual(saved["progress"]["percent"], 100.0)
            self.assertEqual(saved["counts"]["images"], 2)
            self.assertEqual(saved["stages"][0]["elapsed_ms"], 12)
            self.assertTrue(summary_json.is_file())
            self.assertTrue(summary_md.is_file())
            self.assertFalse(any(root.glob("*.tmp")))

    def test_terminal_manifest_cannot_return_to_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-1",
                config=self._config(),
                input_root=Path(temp_dir),
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            store.transition(JobStatus.SUCCEEDED)
            with self.assertRaisesRegex(ValueError, "succeeded -> running"):
                store.transition(JobStatus.RUNNING)

    def test_failed_active_stage_is_closed_for_crash_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-2",
                config=self._config(),
                input_root=Path(temp_dir),
                created_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
            store.transition(JobStatus.VALIDATING)
            store.begin_stage("attach_calibration")
            store.fail_active_stage()
            saved = store.read()
            self.assertEqual(saved["progress"]["failed_stage"], "attach_calibration")
            self.assertEqual(saved["stages"][-1]["status"], "failed")


class PipelineManifestIntegrationTests(unittest.TestCase):
    def _args(self, root: Path) -> argparse.Namespace:
        model = root / "fixture.pt"
        model.write_bytes(b"model")
        return build_arg_parser().parse_args(
            [
                "--data-root",
                str(root),
                "--model-path",
                str(model),
                "--output-dir",
                str(root / "output"),
            ]
        )

    def test_public_pipeline_wrapper_finalizes_manifest_without_changing_result_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root)
            with (
                mock.patch.dict(
                    "os.environ",
                    {"MMS_PIPELINE_JOB_ID": "run-integration-success"},
                    clear=False,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline",
                    return_value={
                        "run_fingerprint": "a" * 64,
                        "feature_counts": {"detections": 0, "poles": 0},
                    },
                ),
            ):
                run_pipeline(args)

            manifest = json.loads(
                (root / "output" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["job_id"], "run-integration-success")
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["progress"]["percent"], 100.0)
            self.assertTrue((root / "output" / "run_summary.json").is_file())

    def test_public_pipeline_wrapper_records_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root)
            with (
                mock.patch.dict(
                    "os.environ",
                    {"MMS_PIPELINE_JOB_ID": "run-integration-failure"},
                    clear=False,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline",
                    side_effect=RuntimeError("fixture failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "fixture failed"),
            ):
                run_pipeline(args)

            manifest = json.loads(
                (root / "output" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["errors"][-1]["code"], "PIPELINE_FAILED")
            self.assertEqual(manifest["errors"][-1]["job_id"], "run-integration-failure")


if __name__ == "__main__":
    unittest.main()
