from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mms_shp_detection.build_provenance import (
    INCOMPLETE_NAME, MANIFEST_NAME, REQUIRED_INPUTS, BuildValidationError,
    enforce_build_policy, git_identity, inspect_build, manifest_hash,
    source_records, write_build_manifest,
)
from scripts.build_web import build, main


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in REQUIRED_INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if path.suffix == ".json" else "source\n", encoding="utf-8")
        self.app = self.root / "mms_shp_detection/webapp/app.py"
        self.app.parent.mkdir(parents=True)
        self.app.write_text('API_VERSION = "1"\n', encoding="utf-8")
        self.source = self.root / "webui/src/main.tsx"
        self.source.parent.mkdir()
        self.source.write_text("export const text = '도로'\n", encoding="utf-8")
        (self.root / "webui/public").mkdir()
        self.dist = self.root / "webui/dist"
        self.write_assets(self.dist)

    @staticmethod
    def write_assets(dist: Path) -> None:
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "assets/app-hash.js").write_bytes(b"console.log('built')\n")
        (dist / "index.html").write_text(
            '<script src="/assets/app-hash.js"></script>', encoding="utf-8",
        )

    def write_manifest(self, dist: Path | None = None) -> dict:
        return write_build_manifest(
            self.root, dist or self.dist, build_id="fixture-build",
            expected_sources=source_records(self.root),
            identity={"source_commit": "a" * 40, "working_tree_dirty": True},
            toolchain={"python": "test", "node": "test"},
        )

    def verify(self) -> dict:
        return inspect_build(self.root, self.dist)

    def test_matching_source_assets_and_dirty_commit_are_explicit(self) -> None:
        self.write_manifest()
        result = self.verify()
        self.assertEqual(result["status"], "verified")
        self.assertIs(result["frontend"]["working_tree_dirty"], True)
        self.assertEqual(result["backend"]["source_fingerprint"], result["frontend"]["source_fingerprint"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_source_and_backend_changes_are_stale(self) -> None:
        for path in (self.source, self.app):
            with self.subTest(path=path.name):
                self.write_manifest()
                path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
                self.assertIn("source_mismatch", self.verify()["issues"])

    def test_gitless_node_free_package_can_be_verified(self) -> None:
        self.write_manifest()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify", "--project-root", str(self.root)]), 0)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(git_identity(self.root), {"source_commit": "unavailable", "working_tree_dirty": None})

    def test_index_only_and_source_free_packages_are_never_approved(self) -> None:
        self.assertIn("metadata_missing", self.verify()["issues"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify", "--project-root", str(self.root)]), 2)
        self.write_manifest()
        self.source.unlink()
        self.app.unlink()
        self.assertNotEqual(self.verify()["status"], "verified")

    def test_asset_tamper_missing_and_extra_are_rejected(self) -> None:
        self.write_manifest()
        asset = self.dist / "assets/app-hash.js"
        asset.write_bytes(b"tampered")
        self.assertIn("assets_mismatch", self.verify()["issues"])
        asset.unlink()
        self.assertIn("index_asset_missing", self.verify()["issues"])
        self.write_assets(self.dist)
        self.write_manifest()
        (self.dist / "unexpected.js").write_bytes(b"extra")
        self.assertIn("assets_mismatch", self.verify()["issues"])
        self.write_manifest()
        (self.dist / "__pycache__").mkdir()
        (self.dist / "__pycache__/unexpected.pyc").write_bytes(b"extra")
        self.assertIn("assets_mismatch", self.verify()["issues"])

    def test_index_cannot_reference_missing_or_parent_assets(self) -> None:
        for reference in ("/assets/missing.js", "../private.js", "/assets/%2e%2e/private.js"):
            with self.subTest(reference=reference):
                (self.dist / "index.html").write_text(f'<script src="{reference}"></script>', encoding="utf-8")
                with self.assertRaisesRegex(BuildValidationError, "index_asset_missing"):
                    self.write_manifest()

    def test_lf_and_crlf_sources_have_equal_fingerprints_with_posix_paths(self) -> None:
        original = source_records(self.root)
        for name in original:
            path = self.root / name
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        current = source_records(self.root)
        self.assertEqual(manifest_hash(original), manifest_hash(current))
        self.assertTrue(all("\\" not in key for key in current))
        for name in original:
            path = self.root / name
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(source_records(self.root), original)

    def test_binary_source_and_raw_artifact_bytes_are_not_normalized(self) -> None:
        binary = self.root / "webui/public/logo.png"
        binary.write_bytes(b"a\r\nb")
        original = source_records(self.root)
        binary.write_bytes(b"a\nb")
        self.assertNotEqual(source_records(self.root), original)
        self.write_manifest()
        asset = self.dist / "assets/app-hash.js"
        asset.write_bytes(asset.read_bytes().replace(b"\n", b"\r\n"))
        self.assertIn("assets_mismatch", self.verify()["issues"])

    def test_tests_docs_and_generated_outputs_do_not_change_source_identity(self) -> None:
        original = source_records(self.root)
        for name in ("docs/readme.md", "webui/src/example.test.tsx", "webui/src/test/setup.ts", "webui/dist/build-info.json"):
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text("not a runtime input", encoding="utf-8")
        self.assertEqual(source_records(self.root), original)

    def test_local_vite_environment_is_an_input_without_exposing_values(self) -> None:
        original = source_records(self.root)
        path = self.root / "webui/.env.production.local"
        path.write_text("VITE_EXAMPLE=private-value\n", encoding="utf-8")
        current = source_records(self.root)
        self.assertNotEqual(current, original)
        self.assertNotIn("private-value", json.dumps(current))

    def test_missing_or_bad_manifest_and_api_mismatch_block_production(self) -> None:
        result = self.verify()
        self.assertIn("unverified", enforce_build_policy(result, "development"))
        with self.assertRaises(BuildValidationError):
            enforce_build_policy(result, "production")
        with self.assertRaises(ValueError):
            enforce_build_policy(result, "unknown")
        self.write_manifest()
        self.assertIsNone(enforce_build_policy(self.verify(), "production"))
        self.assertIn("api_version_mismatch", inspect_build(self.root, self.dist, api_version="2")["issues"])
        (self.dist / MANIFEST_NAME).write_text('{"schema_version":99}', encoding="utf-8")
        self.assertIn("metadata_invalid", self.verify()["issues"])

    def test_manifest_cannot_inject_absolute_paths_into_public_metadata(self) -> None:
        for key in ("build_id", "source_commit", "built_at", "source_fingerprint", "api_contract_version"):
            with self.subTest(key=key):
                manifest = self.write_manifest()
                manifest[key] = str(self.root)
                (self.dist / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
                self.assertNotIn(str(self.root), json.dumps(self.verify()))

    def test_malformed_inventory_and_dirty_types_fail_closed(self) -> None:
        for key, value in (("source_files", []), ("source_files", None), ("assets", 42), ("assets", {"index.html": []}), ("working_tree_dirty", "false"), ("working_tree_dirty", 1)):
            with self.subTest(key=key, value=value):
                manifest = self.write_manifest()
                manifest[key] = value
                (self.dist / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
                self.assertIn("metadata_invalid", self.verify()["issues"])

    def test_nested_metadata_named_assets_are_checksummed(self) -> None:
        for name in (MANIFEST_NAME, INCOMPLETE_NAME):
            with self.subTest(name=name):
                nested = self.dist / "assets" / name
                nested.write_bytes(b"an ordinary public asset")
                self.write_manifest()
                nested.write_bytes(b"tampered")
                self.assertIn("assets_mismatch", self.verify()["issues"])

    def test_build_failure_keeps_old_assets_but_invalidates_attempt(self) -> None:
        self.write_manifest()
        original = (self.dist / "index.html").read_bytes()
        with mock.patch("scripts.build_web.subprocess.run", side_effect=subprocess.CalledProcessError(9, "tsc")):
            with self.assertRaises(subprocess.CalledProcessError):
                build(self.root, "node")
        self.assertEqual((self.dist / "index.html").read_bytes(), original)
        self.assertIn("build_incomplete", self.verify()["issues"])

    def test_successful_staged_build_preserves_old_hashed_assets(self) -> None:
        (self.dist / "assets/old-hash.js").write_bytes(b"old open tab")
        def run(command, **kwargs):
            if "--outDir" in command:
                self.write_assets(Path(command[command.index("--outDir") + 1]))
            return subprocess.CompletedProcess(command, 0)
        with (
            mock.patch("scripts.build_web.git_identity", return_value={"source_commit": "unavailable", "working_tree_dirty": None}),
            mock.patch("scripts.build_web.subprocess.run", side_effect=run),
            mock.patch("scripts.build_web.subprocess.check_output", return_value="v22.17.0"),
        ):
            build(self.root, "node")
        self.assertEqual(self.verify()["status"], "verified")
        self.assertEqual((self.dist / "assets/old-hash.js").read_bytes(), b"old open tab")
        self.assertFalse((self.dist / INCOMPLETE_NAME).exists())
        self.assertFalse(list((self.root / "webui").glob(".web-build-*")))

    def test_source_change_while_build_runs_does_not_publish(self) -> None:
        def run(command, **kwargs):
            if "--outDir" in command:
                self.write_assets(Path(command[command.index("--outDir") + 1]))
                self.source.write_text("changed during build", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)
        with (
            mock.patch("scripts.build_web.git_identity", return_value={"source_commit": "unavailable", "working_tree_dirty": None}),
            mock.patch("scripts.build_web.subprocess.run", side_effect=run),
            mock.patch("scripts.build_web.subprocess.check_output", return_value="v22.17.0"),
        ):
            with self.assertRaisesRegex(BuildValidationError, "source_changed_during_build"):
                build(self.root, "node")
        self.assertIn("build_incomplete", self.verify()["issues"])

    def test_static_directory_override_checks_actual_served_build(self) -> None:
        self.write_manifest()
        alternate = self.root / "release/dist"
        shutil.copytree(self.dist, alternate)
        self.assertEqual(inspect_build(self.root, alternate)["status"], "verified")
        (alternate / "assets/app-hash.js").write_bytes(b"bad")
        self.assertIn("assets_mismatch", inspect_build(self.root, alternate)["issues"])
        self.assertEqual(self.verify()["status"], "verified")

    def test_previous_assets_root_link_is_rejected_without_reading_target(self) -> None:
        assets = self.dist / "assets"
        (assets / "app-hash.js").unlink()
        assets.rmdir()
        outside = self.root / "outside-assets"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"original")
        try:
            os.symlink(outside, assets, target_is_directory=True)
        except OSError:
            # Junctions are a supported, unprivileged Windows alias too.
            if os.name != "nt":
                raise
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(assets), str(outside)], capture_output=True)
            if completed.returncode:
                self.skipTest("Filesystem aliases cannot be created in this environment.")
        def run(command, **kwargs):
            if "--outDir" in command:
                self.write_assets(Path(command[command.index("--outDir") + 1]))
            return subprocess.CompletedProcess(command, 0)
        with (
            mock.patch("scripts.build_web.git_identity", return_value={"source_commit": "unavailable", "working_tree_dirty": None}),
            mock.patch("scripts.build_web.subprocess.run", side_effect=run),
        ):
            with self.assertRaisesRegex(BuildValidationError, "Linked old assets"):
                build(self.root, "node")
        self.assertEqual((outside / "keep.txt").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
