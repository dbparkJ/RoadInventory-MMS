"""Package verified source and UI together; verify the extracted Gitless copy."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mms_shp_detection.build_provenance import (
    MANIFEST_NAME,
    BuildValidationError,
    artifact_records,
    inspect_build,
    source_records,
)

SUPPORT_FILES = (
    "README.md", "ENV_SETUP.md", "config.yaml", "requirements-dev.txt",
    "scripts/setup.ps1", "scripts/setup.sh", "scripts/bootstrap_environment.py",
    "scripts/verify_environment.py", "scripts/package_release.py",
    "webui/API_CONTRACT.md", "docs/refactor/BUILD_PROVENANCE.md", "docs/refactor/CI.md",
)


def _tracked_config(root: Path) -> bytes | None:
    """Only ship the unchanged, already-reviewed repository config template."""
    path = root / "config.yaml"
    if not path.exists():
        return None
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True, timeout=10)
        if Path(top.stdout.strip()).resolve() != root:
            raise BuildValidationError("The config template must come from this checkout.")
        result = subprocess.run(["git", "-C", str(root), "show", "HEAD:config.yaml"], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildValidationError("Cannot verify the config template against Git; omit local config or package from a clean checkout.") from exc
    if path.read_bytes().replace(b"\r\n", b"\n") != result.stdout.replace(b"\r\n", b"\n"):
        raise BuildValidationError("Local config.yaml overrides are not allowed in a release package.")
    return result.stdout


def package_release(project_root: Path, output: Path) -> dict:
    root = project_root.resolve(strict=True)
    static_dir = root / "webui/dist"
    status = inspect_build(root, static_dir)
    if status["status"] != "verified":
        raise BuildValidationError("Cannot package an unverified build: " + ", ".join(status["issues"]))
    sources = source_records(root)
    if any(Path(name).name.startswith(".env") for name in sources):
        raise BuildValidationError("Local .env inputs are not packaged; build a release from a checkout without local environment files.")
    names = set(sources)
    names.update(f"webui/dist/{name}" for name in artifact_records(static_dir))
    names.add(f"webui/dist/{MANIFEST_NAME}")
    names.update(name for name in SUPPORT_FILES if (root / name).is_file())
    config_template = _tracked_config(root)
    for name in names:
        source = root / name
        if not source.resolve(strict=True).is_relative_to(root) or source.is_symlink():
            raise BuildValidationError("Linked or external package input is not allowed.")
    output = output.absolute()
    if output.is_symlink() or output.exists():
        raise BuildValidationError("Release output already exists; choose a new filename.")
    if output.resolve().is_relative_to(static_dir.resolve()):
        raise BuildValidationError("Release output must not be written inside the verified UI directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # The temporary directory contains only files created by this function.
    # No raw data, model, DB, .git, local config overrides, or node_modules enter it.
    with tempfile.TemporaryDirectory(prefix="mms-release-") as temporary:
        stage = Path(temporary)
        archive = stage / "release.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for directory in ("webui/src/", "webui/public/", "mms_shp_detection/"):
                bundle.writestr(directory, b"")
            for name in sorted(names):
                if name == "config.yaml":
                    bundle.writestr(name, config_template)
                else:
                    bundle.write(root / name, name)
        extracted = stage / "gitless"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)  # entries are exclusively the checked relative names above
        verified = inspect_build(extracted, extracted / "webui/dist")
        if verified["status"] != "verified":
            raise BuildValidationError("Extracted package verification failed: " + ", ".join(verified["issues"]))
        # Exclusive creation prevents overwriting an artifact written concurrently.
        with archive.open("rb") as src:
            dst = output.open("xb")
            try:
                with dst:
                    shutil.copyfileobj(src, dst)
            except OSError:
                # Only remove the new file that this invocation created with xb.
                output.unlink(missing_ok=True)
                raise
    return {"status": "verified", "files": len(names), "build_id": status["frontend"]["build_id"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = package_release(args.project_root, args.output)
    except (OSError, ValueError) as exc:
        print(f"Release packaging failed: {exc}", file=sys.stderr)
        return 2
    print(f"package={result['status']} files={result['files']} build_id={result['build_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
