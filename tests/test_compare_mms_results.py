from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import shapefile
from pyproj import CRS

from mms_shp_detection.shp_writer import make_detection_id, write_pole_shapefile, write_shapefile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_mms_results.py"
SPEC = importlib.util.spec_from_file_location("compare_mms_results", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)
FINGERPRINT = "a" * 64
CRS_WKT = CRS.from_epsg(5179).to_wkt()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class CompareMmsResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline, self.candidate = self.root / "baseline", self.root / "candidate"
        self.profile = self.root / "profile.json"
        write_json(self.profile, {"id": "synthetic-exact-v1", "reviewed_by": "test-fixture",
                                 "rationale": "deterministic synthetic values, exact equality",
                                 "xy_tolerance": 0, "z_tolerance": 0,
                                 "exclude": [], "numeric_tolerances": []})
        self.records = []
        groups = []
        for index in (1, 2):
            image = f"frame-{index}.jpg"
            record = {"detection_index": 1, "image_name": image, "class_id": 59,
                      "class_name": "도로표지", "confidence": 0.9,
                      "x": 1000000.0 + index, "y": 2000000.0, "z": 50.25,
                      "timestamp_iso": "2026-09-08T01:00:00", "point_count": 100,
                      "accepted_for_shp": True, "point_crop_path": None,
                      "pole": {"found": True, "x": 1000002.0, "y": 2000000.0, "z": 45.0,
                               "status": "valid", "reason": "axis_ground_intersection"}}
            payload = {"schema_version": 18, "record_name": "track-a", "image_name": image,
                       "crs_wkt": CRS_WKT, "run_fingerprint": FINGERPRINT,
                       "model_sha256": FINGERPRINT, "calibration": {"calibration_sha256": FINGERPRINT},
                       "detections": [record], "metadata": {"quality": "valid"}}
            write_json(self.baseline / "txt" / f"frame-{index}.txt", payload)
            det_id = make_detection_id("track-a", image, 1)
            self.records.append({**record, "detection_id": det_id, "support_id": "P1", "run_fingerprint": FINGERPRINT})
            groups.append(det_id)
        self.canonical = groups[-1]
        self.sign_path = Path("shp/detected_signs.shp")
        self.pole_path = Path("shp/pole_bottoms.shp")
        self._write_shapes(self.baseline)
        manifest = {"schema_version": 1, "fixture_id": "synthetic-merged-observations",
                    "source_kind": "synthetic", "source_commit": "b" * 40,
                    "comparison_profile": "synthetic-exact-v1", "frame_schema_version": 18,
                    "coordinate_contract": {"horizontal_crs": "EPSG:5179", "vertical_reference": "unknown",
                                            "units": "metre", "axis_order": "x,y,z"},
                    "run": {"status": "succeeded", "job_id": "fixture-run", "attempt": 1, "fingerprint": FINGERPRINT},
                    "frames_directory": "txt", "layers": [
                        {"name": "signs", "kind": "sign", "path": self.sign_path.as_posix()},
                        {"name": "poles", "kind": "pole", "path": self.pole_path.as_posix()}],
                    "observation_groups": {self.canonical: groups}, "assets": []}
        manifest.update({f"{key}_fingerprint": FINGERPRINT for key in HARNESS.FINGERPRINTS})
        write_json(self.baseline / "comparison.json", manifest)
        shutil.copytree(self.baseline, self.candidate)

    def _write_shapes(self, root: Path, *, sign_records=None, pole_records=None) -> None:
        sign = self.records[-1]
        write_shapefile(sign_records if sign_records is not None else [sign], root / self.sign_path, crs_wkt=CRS_WKT)
        pole = {**sign, "pole_x": 1000002.0, "pole_y": 2000000.0, "pole_z": 45.0,
                "pole_status": "valid", "obs_count": 2}
        write_pole_shapefile(pole_records if pole_records is not None else [pole], root / self.pole_path, crs_wkt=CRS_WKT)

    def compare(self, **kwargs) -> dict:
        return HARNESS.compare(self.baseline, self.candidate, self.profile, **kwargs)

    def mutate(self, relative: str, change, *, root: Path | None = None) -> None:
        path = (root or self.candidate) / relative
        data = read_json(path)
        change(data)
        write_json(path, data)

    def test_merged_frames_match_one_sign_and_preserve_hangul(self) -> None:
        report = self.compare()
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["counts"]["candidate"], {"frames": 2, "observations": 2, "signs": 1, "poles": 1})
        self.assertFalse(report["real_data_verified"])

    def test_require_real_and_relabel_without_evidence_are_blocked(self) -> None:
        self.assertEqual(self.compare(require_real=True)["status"], "BLOCKED_VALIDATION")
        for root in (self.baseline, self.candidate):
            self.mutate("comparison.json", lambda data: data.update(source_kind="real_mms"), root=root)
        self.assertEqual(self.compare(require_real=True)["status"], "BLOCKED_VALIDATION")

    def test_cli_propagates_blocked_and_failed_exit_codes(self) -> None:
        command = [sys.executable, str(SCRIPT), "--baseline", str(self.baseline),
                   "--candidate", str(self.candidate), "--profile", str(self.profile)]
        completed = subprocess.run(command + ["--require-real"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.mutate("txt/frame-1.txt", lambda data: data["metadata"].update(quality="failed"))
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "FAIL")

    def test_missing_profile_tolerance_and_missing_data_do_not_pass(self) -> None:
        profile = read_json(self.profile)
        del profile["xy_tolerance"]
        write_json(self.profile, profile)
        self.assertEqual(self.compare()["status"], "BLOCKED_VALIDATION")
        self.profile.unlink()
        self.assertEqual(self.compare()["status"], "BLOCKED_VALIDATION")

    def test_coordinate_drift_reports_xy_z_maximum_even_when_internally_consistent(self) -> None:
        self.records[-1]["x"] += 0.125
        self.records[-1]["z"] += 0.0625
        self._write_shapes(self.candidate)
        self.mutate("txt/frame-2.txt", lambda data: data["detections"][0].update(
            x=self.records[-1]["x"], z=self.records[-1]["z"]))
        report = self.compare()
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(report["max_xy"]["error"], 0.125)
        self.assertEqual(report["max_z"]["error"], 0.0625)

    def test_duplicate_feature_and_unmapped_accepted_observation_fail(self) -> None:
        self._write_shapes(self.candidate, sign_records=[self.records[-1], self.records[-1]])
        self.assertEqual(self.compare()["status"], "FAIL")
        self._write_shapes(self.candidate)
        self.mutate("comparison.json", lambda data: data["observation_groups"].update({self.canonical: [self.canonical]}))
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_duplicate_frame_detection_and_reused_group_member_fail(self) -> None:
        self.mutate("txt/frame-1.txt", lambda data: data["detections"].append(copy.deepcopy(data["detections"][0])))
        self.assertEqual(self.compare()["status"], "FAIL")
        shutil.copyfile(self.baseline / "txt/frame-1.txt", self.candidate / "txt/frame-1.txt")
        self.mutate("comparison.json", lambda data: data["observation_groups"][self.canonical].append(self.canonical))
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_missing_extra_frame_and_schema_change_fail(self) -> None:
        path = self.candidate / "txt/frame-1.txt"
        original = path.read_bytes()
        path.unlink()
        self.assertEqual(self.compare()["status"], "FAIL")
        path.write_bytes(original)
        extra = read_json(path)
        extra.update(image_name="extra.jpg", detections=[])
        write_json(self.candidate / "txt/extra.txt", extra)
        self.assertEqual(self.compare()["status"], "FAIL")
        (self.candidate / "txt/extra.txt").unlink()
        self.mutate("txt/frame-1.txt", lambda data: data.update(schema_version=19))
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_relationship_class_and_failure_reason_changes_fail(self) -> None:
        self.mutate("txt/frame-1.txt", lambda data: data["detections"][0]["pole"].update(reason="no_ground"))
        self.assertEqual(self.compare()["status"], "FAIL")
        shutil.copyfile(self.baseline / "txt/frame-1.txt", self.candidate / "txt/frame-1.txt")
        self.records[-1]["support_id"] = "P2"
        self._write_shapes(self.candidate)
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_nonfinite_partial_coordinates_and_duplicate_json_keys_fail(self) -> None:
        for updates in ({"x": float("nan")}, {"y": None}, {"confidence": float("inf")}):
            with self.subTest(updates=updates):
                shutil.copyfile(self.baseline / "txt/frame-1.txt", self.candidate / "txt/frame-1.txt")
                self.mutate("txt/frame-1.txt", lambda data: data["detections"][0].update(updates))
                self.assertEqual(self.compare()["status"], "FAIL")
        (self.candidate / "txt/frame-1.txt").write_text('{"schema_version":18,"schema_version":18}')
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_crs_encoding_and_missing_sidecar_fail_or_block(self) -> None:
        path = (self.candidate / self.sign_path).with_suffix(".prj")
        path.write_text(CRS.from_epsg(4326).to_wkt())
        self.assertEqual(self.compare()["status"], "FAIL")
        self._write_shapes(self.candidate)
        (self.candidate / self.sign_path).with_suffix(".cpg").write_text("CP949")
        self.assertEqual(self.compare()["status"], "FAIL")
        self._write_shapes(self.candidate)
        path.unlink()
        self.assertEqual(self.compare()["status"], "BLOCKED_VALIDATION")

    def test_malformed_crs_and_pole_payload_return_structured_failure(self) -> None:
        self.mutate("txt/frame-1.txt", lambda data: data.update(crs_wkt="not-a-coordinate-system"))
        self.assertEqual(self.compare()["status"], "FAIL")
        shutil.copyfile(self.baseline / "txt/frame-1.txt", self.candidate / "txt/frame-1.txt")
        self.mutate("txt/frame-1.txt", lambda data: data["detections"][0].update(pole="invalid"))
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_point_instead_of_pointz_fails(self) -> None:
        path = self.candidate / self.sign_path
        with shapefile.Writer(str(path), shapeType=shapefile.POINT) as writer:
            writer.field("det_id", "C", size=20)
            writer.point(1, 2)
            writer.record(self.canonical)
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_explicit_leaf_exclusion_does_not_hide_other_metadata_or_schema(self) -> None:
        profile = read_json(self.profile)
        profile["exclude"] = [{"path": "/frames/*/metadata/generated_at", "reason": "wall clock only"}]
        write_json(self.profile, profile)
        for root, time in ((self.baseline, "one"), (self.candidate, "two")):
            self.mutate("txt/frame-1.txt", lambda data: data["metadata"].update(generated_at=time), root=root)
        self.assertEqual(self.compare()["status"], "PASS")
        self.mutate("txt/frame-1.txt", lambda data: data["metadata"].update(quality="bad"))
        self.assertEqual(self.compare()["status"], "FAIL")
        profile["exclude"] = [{"path": "/frames/*/metadata/*", "reason": "too broad"}]
        write_json(self.profile, profile)
        self.assertEqual(self.compare()["status"], "FAIL")
        profile["exclude"] = [{"path": "/frames/*/metadata", "reason": "whole object"}]
        write_json(self.profile, profile)
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_unreviewed_tolerance_and_path_escape_fail_closed(self) -> None:
        profile = read_json(self.profile)
        profile["xy_tolerance"] = 0.1
        profile["reviewed_by"] = ""
        write_json(self.profile, profile)
        self.assertEqual(self.compare()["status"], "BLOCKED_VALIDATION")
        profile["reviewed_by"] = "synthetic-test"
        write_json(self.profile, profile)
        self.mutate("comparison.json", lambda data: data.update(frames_directory="../candidate/txt"))
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_failed_run_stale_frame_and_stale_shp_cannot_pass(self) -> None:
        self.mutate("comparison.json", lambda data: data["run"].update(status="failed"))
        self.assertEqual(self.compare()["status"], "FAIL")
        self.mutate("comparison.json", lambda data: data["run"].update(status="succeeded"))
        self.mutate("txt/frame-1.txt", lambda data: data.update(run_fingerprint="c" * 64))
        self.assertEqual(self.compare()["status"], "FAIL")
        shutil.copyfile(self.baseline / "txt/frame-1.txt", self.candidate / "txt/frame-1.txt")
        self.records[-1]["run_fingerprint"] = "c" * 64
        self._write_shapes(self.candidate)
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_empty_detection_frame_is_supported_without_equating_detection_shp_counts(self) -> None:
        for root in (self.baseline, self.candidate):
            self._write_shapes(root, sign_records=[], pole_records=[])
            self.mutate("comparison.json", lambda data: data.update(observation_groups={}), root=root)
            for index in (1, 2):
                self.mutate(f"txt/frame-{index}.txt", lambda data: data.update(detections=[]), root=root)
        self.assertEqual(self.compare()["status"], "PASS")

    def test_source_record_assets_require_explicit_semantics_and_matching_bytes(self) -> None:
        for root in (self.baseline, self.candidate):
            path = root / "crop.bin"
            path.write_bytes(b"source-record-fields")
            asset = {"path": "crop.bin", "semantics": "source LAS record bytes",
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            self.mutate("comparison.json", lambda data: data.update(assets=[asset]), root=root)
        self.assertEqual(self.compare()["status"], "PASS")
        (self.candidate / "crop.bin").write_bytes(b"RGB preview is not source records")
        self.assertEqual(self.compare()["status"], "FAIL")

    def test_dbf_width_change_is_not_hidden_by_identical_feature_values(self) -> None:
        path = self.candidate / self.sign_path
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            fields = [list(field) for field in reader.fields[1:]]
            rows = [list(row) for row in reader.iterRecords()]
            shapes = list(reader.iterShapes())
        fields[1][2] += 1
        with shapefile.Writer(str(path), shapeType=shapefile.POINTZ, encoding="utf-8") as writer:
            for field in fields:
                writer.field(*field)
            for shape, row in zip(shapes, rows):
                writer.shape(shape)
                writer.record(*row)
        report = self.compare()
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(any("/fields/" in item.get("path", "") for item in report["differences"]))

    def test_real_evidence_requires_current_manifest_and_binds_output_digests(self) -> None:
        # Fabricated evidence tests the evidence validator only. This does not
        # represent real MMS processing or satisfy the project's P0-3b gate.
        manifest = read_json(self.candidate / "comparison.json")
        evidence = {"reviewed_by": "synthetic-evidence-test", "input_inventory_reference": "private-fixture",
                    "execution_log_reference": "private-test-log", "input_inventory_sha256": FINGERPRINT,
                    "execution_log_sha256": FINGERPRINT, "run_manifest": "run_manifest.json",
                    "execution": {key: manifest[key] for key in (
                        "source_commit", "input_fingerprint", "model_fingerprint", "calibration_fingerprint", "config_fingerprint")}}
        evidence["execution"]["run"] = manifest["run"]
        run_document = {"schema_version": 1, "job_id": "fixture-run", "attempt": 1, "status": "succeeded",
                        "created_at": "2026-09-08T01:00:00Z", "input": {},
                        "versions": {"git_commit": manifest["source_commit"],
                                     "model_hashes": {"fixture-model": FINGERPRINT},
                                     "calibration_hash": FINGERPRINT}, "progress": {},
                        "counts": {}, "errors": [], "stages": [{"stage_name": "write_outputs", "status": "succeeded", "attempt": 1}],
                        "config": {"effective_hash": FINGERPRINT},
                        "outputs": {"shapefiles": [self.sign_path.as_posix(), self.pole_path.as_posix()]}}
        write_json(self.candidate / "run_manifest.json", run_document)
        manifest["real_evidence"] = evidence
        write_json(self.candidate / "comparison.json", manifest)
        bundle = HARNESS._load_bundle(self.candidate, HARNESS._profile(self.profile))
        evidence["output_sha256"] = {relative: HARNESS._hash(self.candidate / relative)
                                     for relative in bundle["files"] | {"run_manifest.json"}}
        bundle["manifest"]["real_evidence"] = evidence
        HARNESS._real_evidence(self.candidate, bundle)
        run_document["attempt"] = 2
        write_json(self.candidate / "run_manifest.json", run_document)
        with self.assertRaisesRegex(ValueError, "current-attempt identity"):
            HARNESS._real_evidence(self.candidate, bundle)
        run_document["attempt"] = 1
        write_json(self.candidate / "run_manifest.json", run_document)
        original_commit = manifest["source_commit"]
        bundle["manifest"]["source_commit"] = "d" * 40
        evidence["execution"]["source_commit"] = "d" * 40
        with self.assertRaisesRegex(ValueError, "source commit mismatch"):
            HARNESS._real_evidence(self.candidate, bundle)
        bundle["manifest"]["source_commit"] = original_commit
        evidence["execution"]["source_commit"] = original_commit
        for key in ("model_hashes", "calibration_hash"):
            original = run_document["versions"][key]
            run_document["versions"][key] = {"fixture-model": "c" * 64} if key == "model_hashes" else "c" * 64
            write_json(self.candidate / "run_manifest.json", run_document)
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                HARNESS._real_evidence(self.candidate, bundle)
            run_document["versions"][key] = original
        write_json(self.candidate / "run_manifest.json", run_document)
        evidence["output_sha256"][self.sign_path.as_posix()] = "c" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            HARNESS._real_evidence(self.candidate, bundle)


if __name__ == "__main__":
    unittest.main()
