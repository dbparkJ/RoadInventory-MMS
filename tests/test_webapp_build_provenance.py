from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from mms_shp_detection.build_provenance import BuildValidationError
from mms_shp_detection.webapp import WebAppConfig, create_app
from scripts.run_web import build_parser


class WebBuildRuntimeTests(unittest.TestCase):
    def test_legacy_development_reports_unverified_in_browser_and_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "separate-ui"
            static.mkdir()
            (static / "index.html").write_text("legacy UI", encoding="utf-8")
            config = WebAppConfig(
                project_root=root, allowed_roots=[root], state_dir=root / "state",
                static_dir=static, enable_run_worker=False,
            )
            with warnings.catch_warnings(record=True) as reported:
                warnings.simplefilter("always")
                app = create_app(config)
            self.assertTrue(any("unverified" in str(item.message) for item in reported))
            with TestClient(app) as client:
                result = client.get("/api/build")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.headers["cache-control"], "no-store")
                self.assertEqual(result.json()["status"], "unverified")
                self.assertEqual(client.get("/api/bootstrap").json()["build"], result.json())
                self.assertEqual(client.get("/").text, "legacy UI")
                self.assertNotIn(str(root), result.text)

    def test_production_blocks_before_state_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = WebAppConfig(
                project_root=root, allowed_roots=[root], state_dir=root / "state",
                build_mode="production", enable_run_worker=False,
            )
            with self.assertRaises(BuildValidationError):
                create_app(config)
            self.assertFalse(config.state_dir.exists())

    def test_actual_static_override_and_runtime_api_are_passed_to_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = WebAppConfig(
                project_root=root, allowed_roots=[root], state_dir=root / "state",
                static_dir=root / "release-ui", enable_run_worker=False, build_mode="production",
            )
            with mock.patch("mms_shp_detection.webapp.app.inspect_build", return_value={"status": "verified", "issues": []}) as verify:
                app = create_app(config)
            verify.assert_called_once_with(root, root / "release-ui", api_version="1")
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/build").json()["status"], "verified")

    def test_launcher_explicit_mode_and_environment_default(self) -> None:
        with mock.patch.dict("os.environ", {"MMS_WEB_BUILD_MODE": "production"}):
            self.assertEqual(build_parser().parse_args([]).build_mode, "production")
            self.assertEqual(build_parser().parse_args(["--build-mode", "development"]).build_mode, "development")


if __name__ == "__main__":
    unittest.main()
