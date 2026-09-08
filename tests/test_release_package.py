from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from mms_shp_detection.build_provenance import (
    REQUIRED_INPUTS,
    BuildValidationError,
    inspect_build,
    source_records,
    write_build_manifest,
)
from scripts.package_release import package_release


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for name in REQUIRED_INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "source\n", encoding="utf-8")
    for directory in ("webui/src", "webui/public", "mms_shp_detection/webapp", "webui/dist/assets"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "mms_shp_detection/webapp/app.py").write_text('API_VERSION = "1"\n', encoding="utf-8")
    (root / "webui/src/main.tsx").write_text("export const title = '도로';\n", encoding="utf-8")
    (root / "webui/dist/assets/app.js").write_text("console.log('도로');\n", encoding="utf-8")
    (root / "webui/dist/index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    write_manifest(root)
    return root


def write_manifest(root: Path) -> None:
    write_build_manifest(
        root, root / "webui/dist", build_id="package-fixture",
        expected_sources=source_records(root),
        identity={"source_commit": "a" * 40, "working_tree_dirty": False},
        toolchain={"python": "test", "node": "test"},
    )


def test_package_verifies_without_git_or_node_and_excludes_private_files(project: Path, tmp_path: Path) -> None:
    for name in ("models/private.pt", "data/customer.txt", ".git/config", ".env", "webui/node_modules/private.js"):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_DATA", encoding="utf-8")
    output = tmp_path / "release.zip"
    result = package_release(project, output)
    assert result["status"] == "verified"
    with zipfile.ZipFile(output) as archive, tempfile.TemporaryDirectory() as extracted:
        assert not any(name.startswith(("models/", "data/", ".git/", ".env", "webui/node_modules/")) for name in archive.namelist())
        archive.extractall(extracted)
        assert inspect_build(Path(extracted), Path(extracted) / "webui/dist")["status"] == "verified"


def test_stale_build_cannot_be_packaged(project: Path, tmp_path: Path) -> None:
    (project / "mms_shp_detection/webapp/app.py").write_text('API_VERSION = "2"\n', encoding="utf-8")
    with pytest.raises(BuildValidationError, match="unverified"):
        package_release(project, tmp_path / "release.zip")
    assert not (tmp_path / "release.zip").exists()


def test_existing_release_is_preserved(project: Path, tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    output.write_bytes(b"previous release")
    with pytest.raises(BuildValidationError, match="already exists"):
        package_release(project, output)
    assert output.read_bytes() == b"previous release"


def test_local_vite_environment_cannot_leak_into_release(project: Path, tmp_path: Path) -> None:
    (project / "webui/.env.local").write_text("SECRET=private\n", encoding="utf-8")
    write_manifest(project)
    with pytest.raises(BuildValidationError, match="Local .env"):
        package_release(project, tmp_path / "release.zip")


def test_source_changes_while_packaging_fail_gitless_validation(project: Path, tmp_path: Path) -> None:
    original_write = zipfile.ZipFile.write
    changed = False

    def racing_write(archive, filename, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            (project / "webui/src/main.tsx").write_text("export const changed = true;\n", encoding="utf-8")
        return original_write(archive, filename, *args, **kwargs)

    with mock.patch.object(zipfile.ZipFile, "write", racing_write), pytest.raises(BuildValidationError, match="Extracted package"):
        package_release(project, tmp_path / "release.zip")
    assert not (tmp_path / "release.zip").exists()


def test_local_config_overrides_do_not_enter_verified_package(project: Path, tmp_path: Path) -> None:
    config = project / "config.yaml"
    config.write_text("paths:\n  data_root: data/example\n", encoding="utf-8")
    commands = [
        ["git", "init", "--quiet"],
        ["git", "add", "config.yaml"],
        ["git", "-c", "user.name=Package Test", "-c", "user.email=package@example.invalid", "commit", "--quiet", "-m", "config fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=project, check=True, capture_output=True)
    package_release(project, tmp_path / "clean.zip")
    config.write_text("paths:\n  data_root: /private/customer\n", encoding="utf-8")
    with pytest.raises(BuildValidationError, match="Local config.yaml"):
        package_release(project, tmp_path / "private.zip")
    assert not (tmp_path / "private.zip").exists()


def test_partial_output_is_removed_on_disk_failure(project: Path, tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    original_copy = __import__("shutil").copyfileobj

    def fail_final_copy(src, dst, *args, **kwargs):
        if getattr(dst, "name", None) and Path(dst.name) == output:
            dst.write(b"partial archive")
            raise OSError("synthetic disk full")
        return original_copy(src, dst, *args, **kwargs)

    with mock.patch("scripts.package_release.shutil.copyfileobj", side_effect=fail_final_copy), pytest.raises(OSError, match="disk full"):
        package_release(project, output)
    assert not output.exists()
