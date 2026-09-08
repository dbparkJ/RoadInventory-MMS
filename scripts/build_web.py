"""Build in isolation and verify the exact frontend/backend source package."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mms_shp_detection.build_provenance import (
    INCOMPLETE_NAME, BuildValidationError, git_identity, inspect_build,
    source_records, write_build_manifest,
)


def build(project_root: Path, node: str) -> dict:
    project_root = project_root.resolve(strict=True)
    web_root = project_root / "webui"
    static_dir = web_root / "dist"
    if static_dir.is_symlink() or (hasattr(static_dir, "is_junction") and static_dir.is_junction()):
        raise BuildValidationError("Refusing to replace a linked dist directory.")
    # Both rename targets are verified to stay inside the declared project.
    if not static_dir.resolve().is_relative_to(project_root):
        raise BuildValidationError("Build output must stay inside the project.")
    sources = source_records(project_root)
    identity = git_identity(project_root)
    build_id = uuid.uuid4().hex
    static_dir.mkdir(exist_ok=True)
    (static_dir / INCOMPLETE_NAME).write_text(build_id + "\n", encoding="ascii")
    with tempfile.TemporaryDirectory(prefix=".web-build-", dir=web_root) as stage_text:
        stage = Path(stage_text).resolve()
        if not stage.is_relative_to(web_root.resolve()):
            raise BuildValidationError("Invalid build staging directory.")
        output = stage / "dist"
        subprocess.run([node, str(web_root / "node_modules/typescript/bin/tsc"), "-b"], cwd=web_root, check=True)
        subprocess.run([
            node, str(web_root / "node_modules/vite/bin/vite.js"), "build",
            "--mode", "production", "--outDir", str(output),
        ], cwd=web_root, check=True)
        # Keep old hashed assets addressable by tabs opened before this build.
        previous_assets = static_dir / "assets"
        if previous_assets.is_symlink() or (hasattr(previous_assets, "is_junction") and previous_assets.is_junction()):
            raise BuildValidationError("Linked old assets cannot be retained.")
        if previous_assets.is_dir():
            for path in previous_assets.rglob("*"):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise BuildValidationError("Linked old assets cannot be retained.")
                if path.is_file():
                    destination = output / "assets" / path.relative_to(previous_assets)
                    if not destination.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
        npm_version = re.search(r"(?:^|\s)npm/([0-9]+\.[0-9]+\.[0-9]+)", os.environ.get("npm_config_user_agent", ""))
        manifest = write_build_manifest(
            project_root, output, build_id=build_id, expected_sources=sources, identity=identity,
            toolchain={
                "python": platform.python_version(),
                "node": subprocess.check_output([node, "--version"], text=True).strip(),
                "npm": npm_version.group(1) if npm_version else "unavailable",
            },
        )
        result = inspect_build(project_root, output)
        if result["status"] != "verified":
            raise BuildValidationError("Staged build failed verification: " + ", ".join(result["issues"]))
        previous = stage / "previous-dist"
        static_dir.rename(previous)
        try:
            output.rename(static_dir)
        except OSError:
            previous.rename(static_dir)
            raise
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--static-dir", type=Path, help="Actual served directory (verify only).")
    parser.add_argument("--node", default="node")
    args = parser.parse_args(argv)
    if args.command == "build" and args.static_dir is not None:
        parser.error("Build always targets webui/dist; use a separate release checkout.")
    try:
        if args.command == "build":
            manifest = build(args.project_root, args.node)
            print(json.dumps({"status": "verified", "build_id": manifest["build_id"]}))
            return 0
        result = inspect_build(args.project_root, args.static_dir or args.project_root / "webui/dist")
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "verified" else 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Web build failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
