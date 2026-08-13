from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import WebAppConfig, create_app
from mms_shp_detection.webapp.security import UnsafePath, normalize_relative_path
from scripts.run_web import is_loopback_bind


class WebAppHealthSafetyTests(unittest.TestCase):
    def test_health_bootstrap_and_tree_never_expose_absolute_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            (root / "dataset-a").mkdir()
            (root / "readme.txt").write_text("ok", encoding="utf-8")
            app = create_app(
                allowed_roots={"MMS storage": root},
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")

                bootstrap = client.get("/api/bootstrap")
                self.assertEqual(bootstrap.status_code, 200)
                bootstrap_payload = bootstrap.json()
                self.assertTrue(bootstrap_payload["capabilities"]["resumable_uploads"])
                self.assertEqual(
                    bootstrap_payload["capabilities"]["max_point_budget"],
                    1_000_000,
                )
                self.assertEqual(
                    bootstrap_payload["map"],
                    {
                        "provider": "vworld",
                        "engine": "webgl",
                        "version": "3.0",
                    },
                )
                self.assertNotIn("map_style_url", bootstrap_payload)
                serialized = bootstrap.text
                self.assertNotIn(str(root), serialized)

                storage = client.get("/api/storage").json()
                root_id = storage["roots"][0]["id"]
                tree = client.get(f"/api/storage/{root_id}/tree")
                self.assertEqual(tree.status_code, 200)
                entries = tree.json()["entries"]
                self.assertEqual(entries[0]["relative_path"], "dataset-a")
                self.assertIn("size_bytes", entries[-1])
                self.assertNotIn(str(root), tree.text)

                traversal = client.get(
                    f"/api/storage/{root_id}/tree", params={"path": "../outside"}
                )
                self.assertEqual(traversal.status_code, 422)
                absolute = client.get(
                    f"/api/storage/{root_id}/tree", params={"path": "C:/Windows"}
                )
                self.assertEqual(absolute.status_code, 422)

    def test_symlink_directory_is_not_selectable_or_traversable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as outside_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            root = Path(root_text)
            link = root / "linked"
            try:
                os.symlink(outside_text, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                tree = client.get(f"/api/storage/{root_id}/tree").json()
                linked = next(
                    item for item in tree["entries"] if item["name"] == "linked"
                )
                self.assertFalse(linked["selectable"])
                self.assertTrue(linked["symlink"])
                self.assertEqual(
                    client.get(
                        f"/api/storage/{root_id}/tree", params={"path": "linked"}
                    ).status_code,
                    422,
                )
                scan = client.post(
                    "/api/datasets/scan",
                    json={
                        "root_id": root_id,
                        "relative_path": "",
                        "crs": "EPSG:4326",
                    },
                )
                self.assertEqual(scan.status_code, 422)
                self.assertIn("Symbolic links", scan.json()["detail"])

    def test_portable_relative_path_rejects_backslashes(self) -> None:
        with self.assertRaises(UnsafePath):
            normalize_relative_path(r"folder\..\outside")

    def test_portable_relative_path_rejects_windows_ads_and_device_names(self) -> None:
        for unsafe in (
            "track/image.jpg:payload",
            "track/CON",
            "track/nul.txt",
            "track/COM1.bin",
            "track/trailing.",
            "track/trailing ",
        ):
            with self.subTest(path=unsafe), self.assertRaises(UnsafePath):
                normalize_relative_path(unsafe, allow_empty=False)

    def test_dataset_scan_runs_symlink_preflight_before_background_work(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                root_id = client.get("/api/storage").json()["roots"][0]["id"]
                with mock.patch(
                    "mms_shp_detection.webapp.datasets.assert_no_symlink_descendants",
                    side_effect=UnsafePath("Symbolic links are not allowed."),
                ):
                    response = client.post(
                        "/api/datasets/scan",
                        json={
                            "root_id": root_id,
                            "relative_path": "",
                            "crs": "EPSG:4326",
                        },
                    )
            self.assertEqual(response.status_code, 422)
            self.assertIn("Symbolic links", response.json()["detail"])

    def test_web_runner_only_recognizes_explicit_loopback_hosts(self) -> None:
        self.assertTrue(is_loopback_bind("127.0.0.1"))
        self.assertTrue(is_loopback_bind("::1"))
        self.assertTrue(is_loopback_bind("LOCALHOST."))
        self.assertFalse(is_loopback_bind("0.0.0.0"))
        self.assertFalse(is_loopback_bind("192.0.2.10"))
        self.assertFalse(is_loopback_bind("mms.internal.example"))

    def test_storage_root_environment_is_used_without_cli_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            with mock.patch.dict(
                os.environ,
                {"MMS_WEB_STORAGE_ROOTS": root_text},
            ):
                config = WebAppConfig(
                    state_dir=Path(state_text),
                    allowed_roots=None,
                    enable_run_worker=False,
                )
            self.assertEqual(
                tuple(Path(path).resolve() for path in config.allowed_roots or ()),
                (Path(root_text).resolve(),),
            )


if __name__ == "__main__":
    unittest.main()
