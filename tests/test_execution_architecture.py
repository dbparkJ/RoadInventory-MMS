from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mms_shp_detection.config import (
    PipelineConfig,
    canonical_config_json,
    config_file_sha256,
    config_sha256,
)
from mms_shp_detection.domain.models import JobStatus, PipelineErrorInfo, StageResult
from mms_shp_detection.infrastructure import manifest_writer
from mms_shp_detection.infrastructure.manifest_writer import (
    PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES,
    RunManifestStore,
    validate_manifest_document,
    validate_published_outputs,
)
from mms_shp_detection.pipeline import build_arg_parser, run_pipeline


def write_bundle(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in PUBLISHED_SHAPEFILE_COMPONENT_SUFFIXES:
        path.with_suffix(suffix).write_bytes(f"fixture:{suffix}".encode("ascii"))


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

    def test_pipeline_config_detaches_and_deeply_freezes_values(self) -> None:
        source = {"nested": {"classes": [2, 11]}}
        config = PipelineConfig(values=source, config_hash=config_sha256(source))
        source["nested"]["classes"].append(99)

        self.assertEqual(config.to_dict(), {"nested": {"classes": [2, 11]}})
        with self.assertRaises(TypeError):
            config.values["nested"]["classes"] = ()  # type: ignore[index]

    def test_pipeline_config_rejects_a_mismatched_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            PipelineConfig(values={"a": 1}, config_hash="0" * 64)

    def test_config_file_hash_tracks_the_exact_launcher_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.yaml"
            path.write_text("config_version: 1\n", encoding="utf-8")
            first = config_file_sha256(path)
            path.write_text("config_version: 1  # changed bytes\n", encoding="utf-8")

            self.assertNotEqual(first, config_file_sha256(path))


class RunManifestContractTests(unittest.TestCase):
    def _config(self) -> PipelineConfig:
        return PipelineConfig.from_namespace(
            argparse.Namespace(data_root=Path("data"), output_dir=Path("output"))
        )

    def test_manifest_write_retries_a_transient_windows_reader_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            real_replace = os.replace
            attempts = 0

            def transient_reader_lock(source: Path, target: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    error = PermissionError(5, "manifest is temporarily locked")
                    error.winerror = 5  # type: ignore[attr-defined]
                    raise error
                real_replace(source, target)

            with (
                mock.patch(
                    "mms_shp_detection.infrastructure.manifest_writer._is_transient_windows_replace_error",
                    return_value=True,
                ),
                mock.patch(
                    "mms_shp_detection.infrastructure.manifest_writer.os.replace",
                    side_effect=transient_reader_lock,
                ),
                mock.patch("mms_shp_detection.infrastructure.manifest_writer.time.sleep"),
            ):
                document = store.create(
                    job_id="job-reader-lock",
                    config=self._config(),
                    input_root=Path(temp_dir),
                )

            self.assertEqual(attempts, 2)
            self.assertEqual(document["job_id"], "job-reader-lock")

    def test_manifest_write_falls_back_when_a_share_denies_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run_manifest.json"
            store = RunManifestStore(path)
            access_denied = PermissionError(5, "share does not grant rename rights")
            access_denied.winerror = 5  # type: ignore[attr-defined]

            with (
                mock.patch(
                    "mms_shp_detection.infrastructure.manifest_writer._is_transient_windows_replace_error",
                    return_value=True,
                ),
                mock.patch(
                    "mms_shp_detection.infrastructure.manifest_writer.os.replace",
                    side_effect=access_denied,
                ) as replace,
                mock.patch("mms_shp_detection.infrastructure.manifest_writer.time.sleep"),
            ):
                store.create(
                    job_id="job-restricted-share",
                    config=self._config(),
                    input_root=Path(temp_dir),
                )

            self.assertEqual(
                replace.call_count,
                len(manifest_writer._WINDOWS_REPLACE_RETRY_DELAYS_SECONDS) + 1,
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["job_id"], "job-restricted-share")

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

            archived = store.archive_terminal(next_job_id="job-next")
            self.assertIsNotNone(archived)
            assert archived is not None
            self.assertTrue(archived.is_file())
            self.assertTrue(
                archived.with_name(
                    archived.name.replace(".manifest.json", ".summary.json")
                ).is_file()
            )
            self.assertTrue(
                archived.with_name(
                    archived.name.replace(".manifest.json", ".summary.md")
                ).is_file()
            )

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
            with self.assertRaisesRegex(
                ValueError, "Terminal run manifests are immutable"
            ):
                store.set_outputs({"late": "artifact.json"})
            with self.assertRaisesRegex(ValueError, "succeeded -> failed"):
                store.transition_terminal(
                    JobStatus.FAILED,
                    error=PipelineErrorInfo(
                        code="LATE_FAILURE",
                        message="must not contaminate a committed success",
                        stage="worker",
                        job_id="job-1",
                        retryable=False,
                    ),
                )
            self.assertEqual(store.read()["errors"], [])

    def test_terminal_failure_and_structured_error_commit_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-atomic-failure",
                config=self._config(),
                input_root=Path(temp_dir),
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            store.begin_stage("worker")

            store.transition_terminal(
                JobStatus.FAILED,
                error=PipelineErrorInfo(
                    code="WORKER_RESTARTED",
                    message="worker stopped",
                    stage="worker",
                    job_id="job-atomic-failure",
                    retryable=True,
                ),
            )

            saved = store.read()
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["errors"][-1]["code"], "WORKER_RESTARTED")
            self.assertEqual(saved["progress"]["failed_stage"], "worker")
            self.assertEqual(saved["stages"][-1]["status"], "failed")
            self.assertIsNotNone(saved["stages"][-1]["finished_at"])

    def test_terminal_error_is_recorded_for_each_retry_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-attempt-errors",
                config=self._config(),
                input_root=Path(temp_dir),
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            store.transition_terminal(
                JobStatus.FAILED,
                error=PipelineErrorInfo(
                    code="ATTEMPT_ONE",
                    message="first failure",
                    stage="pipeline",
                    job_id="job-attempt-errors",
                    retryable=True,
                ),
            )
            store.transition(JobStatus.RETRYING)
            store.transition(JobStatus.RUNNING)
            store.begin_stage("second_attempt")

            store.transition_terminal(
                JobStatus.FAILED,
                error=PipelineErrorInfo(
                    code="ATTEMPT_TWO",
                    message="second failure",
                    stage="second_attempt",
                    job_id="job-attempt-errors",
                    retryable=False,
                ),
            )

            saved = store.read()
            self.assertEqual(
                [(item["attempt"], item["code"]) for item in saved["errors"]],
                [(1, "ATTEMPT_ONE"), (2, "ATTEMPT_TWO")],
            )
            self.assertEqual(saved["stages"][-1]["status"], "failed")

    def test_failed_manifest_retry_starts_a_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-retry",
                config=self._config(),
                input_root=Path(temp_dir),
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            store.set_outputs({"partial": "ignored"})
            store.transition(JobStatus.FAILED)

            store.transition(JobStatus.RETRYING)
            retried = store.read()

            self.assertEqual(retried["status"], "retrying")
            self.assertEqual(retried["attempt"], 2)
            self.assertEqual(retried["progress"]["percent"], 0.0)
            self.assertEqual(retried["outputs"], {})
            self.assertIsNone(retried["finished_at"])
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

    def test_pending_manifest_can_be_claimed_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-claim",
                config=self._config(),
                input_root=Path(temp_dir),
            )

            self.assertTrue(store.claim_pending_for_validation())
            self.assertFalse(store.claim_pending_for_validation())
            self.assertEqual(store.read()["status"], "validating")

    def test_existing_manifest_rejects_changed_execution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunManifestStore(root / "run_manifest.json")
            store.create(
                job_id="job-identity",
                config=self._config(),
                input_root=root / "input",
                request_file_hash="a" * 64,
            )

            with self.assertRaisesRegex(
                ValueError, "request configuration file hash differs"
            ):
                store.create(
                    job_id="job-identity",
                    config=self._config(),
                    input_root=root / "input",
                    request_file_hash="b" * 64,
                )
            with self.assertRaisesRegex(ValueError, "input root"):
                store.create(
                    job_id="job-identity",
                    config=self._config(),
                    input_root=root / "different-input",
                    request_file_hash="a" * 64,
                )
            changed_config = PipelineConfig(
                values={"changed": True},
                config_hash=config_sha256({"changed": True}),
            )
            with self.assertRaisesRegex(
                ValueError, "effective configuration hash differs"
            ):
                store.create(
                    job_id="job-identity",
                    config=changed_config,
                    input_root=root / "input",
                    request_file_hash="a" * 64,
                )

    def test_config_provenance_preserves_the_launcher_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunManifestStore(root / "run_manifest.json")
            store.create(
                job_id="job-provenance",
                config=self._config(),
                input_root=root,
                request_file_hash="c" * 64,
            )

            store.set_config_provenance(self._config())

            self.assertEqual(
                store.read()["config"]["request_file_hash"],
                "c" * 64,
            )

    def test_launcher_manifest_accepts_then_commits_normalized_effective_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunManifestStore(root / "run_manifest.json")
            launcher_values = {"config_version": 1}
            launcher_config = PipelineConfig(
                values=launcher_values,
                config_hash=config_sha256(launcher_values),
            )
            effective_values = {"config_version": 1, "defaulted_option": 42}
            effective_config = PipelineConfig(
                values=effective_values,
                config_hash=config_sha256(effective_values),
            )
            store.create(
                job_id="job-launcher-handoff",
                config=launcher_config,
                input_root=root / "input",
                request_file_hash="d" * 64,
                config_is_effective=False,
            )

            existing = store.create(
                job_id="job-launcher-handoff",
                config=effective_config,
                input_root=root / "input",
                request_file_hash="d" * 64,
            )
            store.set_config_provenance(effective_config)

            self.assertEqual(existing["status"], "pending")
            self.assertEqual(
                store.read()["config"]["effective_hash"],
                effective_config.config_hash,
            )

    def test_published_output_contract_requires_a_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shp_path = root / "shp" / "detected_signs.shp"
            write_bundle(shp_path)
            outputs = {
                "shapefiles": ["shp/detected_signs.shp"],
                "models_manifest": None,
            }
            self.assertEqual(validate_published_outputs(root, outputs), ())

            shp_path.with_suffix(".dbf").unlink()
            errors = validate_published_outputs(root, outputs)
            self.assertTrue(any(".dbf" in error for error in errors))

    def test_published_output_contract_rejects_an_escaping_sidecar_symlink(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as outside_text,
        ):
            root = Path(root_text)
            shp_path = root / "shp" / "detected_signs.shp"
            write_bundle(shp_path)
            sidecar = shp_path.with_suffix(".dbf")
            sidecar.unlink()
            outside = Path(outside_text) / "secret.dbf"
            outside.write_bytes(b"private")
            try:
                os.symlink(outside, sidecar)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            errors = validate_published_outputs(
                root,
                {
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                },
            )

            self.assertTrue(any(".dbf" in error for error in errors))

    def test_published_output_contract_rejects_a_linked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_shp = root / "real" / "detected_signs.shp"
            write_bundle(real_shp)
            linked_parent = root / "shp"
            try:
                os.symlink(real_shp.parent, linked_parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symbolic links are unavailable: {exc}")

            errors = validate_published_outputs(
                root,
                {
                    "shapefiles": ["shp/detected_signs.shp"],
                    "models_manifest": None,
                },
            )

            self.assertTrue(any("component" in error for error in errors))

    def test_models_manifest_must_match_completed_published_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shp_path = root / "model_a" / "shp" / "detected_signs.shp"
            write_bundle(shp_path)
            models_manifest = root / "models_manifest.json"
            models_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "models": [
                            {
                                "status": "completed",
                                "published_current_run": True,
                                "final_shapefiles": {
                                    "detections": str(shp_path.resolve()),
                                    "poles": None,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            outputs = {
                "shapefiles": ["model_a/shp/detected_signs.shp"],
                "models_manifest": "models_manifest.json",
            }

            self.assertEqual(validate_published_outputs(root, outputs), ())
            document = json.loads(models_manifest.read_text(encoding="utf-8"))
            document["models"][0]["status"] = "failed"
            models_manifest.write_text(json.dumps(document), encoding="utf-8")

            errors = validate_published_outputs(root, outputs)
            self.assertTrue(any("completed publication" in error for error in errors))

    def test_succeeded_transition_rejects_failed_stage_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            store.create(
                job_id="job-failed-stage",
                config=self._config(),
                input_root=Path(temp_dir),
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            started = store.begin_stage("detect_project_and_estimate")
            store.record_stage(
                StageResult(
                    stage_name="detect_project_and_estimate",
                    stage_version="1",
                    status="failed",
                    started_at=started,
                    finished_at=started,
                )
            )

            with self.assertRaisesRegex(ValueError, "failed stage evidence"):
                store.transition(JobStatus.SUCCEEDED)

            self.assertEqual(store.read()["status"], "running")

    def test_archive_rolls_back_summaries_when_a_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = RunManifestStore(root / "run_manifest.json")
            store.create(
                job_id="job-archive-rollback",
                config=self._config(),
                input_root=root,
            )
            store.transition(JobStatus.VALIDATING)
            store.transition(JobStatus.RUNNING)
            store.transition(JobStatus.SUCCEEDED)
            summary_json, summary_md = store.write_summary()
            real_replace = os.replace

            def fail_markdown_move(source: Path, target: Path) -> None:
                if Path(source).name == "run_summary.md":
                    raise PermissionError("summary is locked")
                real_replace(source, target)

            with (
                mock.patch(
                    "mms_shp_detection.infrastructure.manifest_writer.os.replace",
                    side_effect=fail_markdown_move,
                ),
                self.assertRaises(PermissionError),
            ):
                store.archive_terminal(next_job_id="job-next")

            self.assertTrue(store.path.is_file())
            self.assertTrue(summary_json.is_file())
            self.assertTrue(summary_md.is_file())
            self.assertFalse(any((root / "run_history").glob("*.manifest.json")))

    def test_manifest_schema_rejects_boolean_numeric_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunManifestStore(Path(temp_dir) / "run_manifest.json")
            document = store.create(
                job_id="job-types",
                config=self._config(),
                input_root=Path(temp_dir),
            )

            for key in ("schema_version", "attempt"):
                invalid = dict(document)
                invalid[key] = True
                self.assertTrue(
                    any(key in error for error in validate_manifest_document(invalid))
                )

            invalid_progress = dict(document)
            invalid_progress["progress"] = {
                **document["progress"],
                "percent": False,
            }
            self.assertTrue(
                any(
                    "progress.percent" in error
                    for error in validate_manifest_document(invalid_progress)
                )
            )


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

    def test_public_pipeline_wrapper_finalizes_manifest_without_changing_result_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root)
            shp_path = root / "output" / "shp" / "detected_signs.shp"
            write_bundle(shp_path)
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
                        "final_shapefiles": {
                            "detections": str(shp_path),
                            "poles": None,
                        },
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
            self.assertEqual(
                manifest["outputs"]["shapefiles"],
                ["shp/detected_signs.shp"],
            )
            self.assertTrue((root / "output" / "run_summary.json").is_file())

    def test_manifest_does_not_claim_a_stale_pole_shapefile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root)
            signs = root / "output" / "shp" / "detected_signs.shp"
            stale_poles = root / "output" / "shp" / "pole_bottoms.shp"
            write_bundle(signs)
            write_bundle(stale_poles)
            with (
                mock.patch.dict(
                    "os.environ",
                    {"MMS_PIPELINE_JOB_ID": "run-current-outputs-only"},
                    clear=False,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline",
                    return_value={
                        "run_fingerprint": "b" * 64,
                        "final_shapefiles": {
                            "detections": str(signs),
                            "poles": None,
                        },
                        "feature_counts": {"detections": 0, "poles": 0},
                    },
                ),
            ):
                run_pipeline(args)

            manifest = json.loads(
                (root / "output" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["outputs"]["shapefiles"],
                ["shp/detected_signs.shp"],
            )

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
            self.assertEqual(
                manifest["errors"][-1]["job_id"], "run-integration-failure"
            )


if __name__ == "__main__":
    unittest.main()
