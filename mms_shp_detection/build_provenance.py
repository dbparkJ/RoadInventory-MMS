"""Dependency-free source/build consistency checks (not signature verification)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MANIFEST_NAME = "build-info.json"
INCOMPLETE_NAME = ".build-incomplete"
SOURCE_POLICY = "mms-source-v1-text-lf"
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".css", ".html", ".svg", ".txt", ".sh", ".ps1"}
REQUIRED_INPUTS = (
    "webui/package.json", "webui/package-lock.json", "webui/index.html",
    "webui/vite.config.ts", "webui/tsconfig.json", "webui/tsconfig.app.json",
    "webui/tsconfig.node.json", "webui/scripts/build.mjs",
    "requirements.txt", "scripts/build_web.py", "scripts/run_web.py",
    "scripts/run_pipeline.py", "scripts/setup_web.sh", "scripts/setup_web.ps1",
)


class BuildValidationError(ValueError):
    """A build is incomplete or cannot be tied to the declared sources."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_hash(records: dict[str, str]) -> str:
    return _digest(json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _files(directory: Path, *, skip_python_cache: bool = False) -> list[Path]:
    if _is_link(directory):
        raise BuildValidationError("linked_build_input")
    result = []
    for current, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not skip_python_cache or name != "__pycache__")
        for name in [*dirs, *names]:
            if _is_link(Path(current) / name):
                raise BuildValidationError("linked_build_input")
        result.extend(Path(current) / name for name in sorted(names))
    return result


def source_records(project_root: Path) -> dict[str, str]:
    """POSIX relative paths; declared UTF-8 text normalizes CRLF, binaries do not."""
    paths = [project_root / name for name in REQUIRED_INPUTS]
    for name in ("webui/src", "webui/public", "mms_shp_detection"):
        directory = project_root / name
        if not directory.is_dir():
            raise BuildValidationError("source_unavailable")
        for path in _files(directory, skip_python_cache=True):
            relative = path.relative_to(project_root).as_posix()
            if name == "mms_shp_detection" and path.suffix != ".py":
                continue
            if name == "webui/src" and (
                "/test/" in relative or "/__tests__/" in relative
                or re.search(r"\.(test|spec)\.[^.]+$", path.name)
            ):
                continue
            paths.append(path)
    # Vite reads these files during its fixed production-mode build.
    paths.extend(path for path in (project_root / "webui").glob(".env*") if path.is_file())
    records = {}
    for path in sorted(set(paths)):
        relative = path.relative_to(project_root)
        ancestors = [project_root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)]
        if any(_is_link(ancestor) for ancestor in ancestors) or not path.is_file():
            raise BuildValidationError("source_unavailable")
        data = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES or path.name.startswith(".env"):
            data = data.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
        records[path.relative_to(project_root).as_posix()] = _digest(data)
    return records


def api_contract_version(project_root: Path) -> str:
    """Read the actual API constant without importing the web/GPU dependencies."""
    path = project_root / "mms_shp_detection/webapp/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "API_VERSION" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise BuildValidationError("api_version_unavailable")


def artifact_records(static_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(static_dir).as_posix(): _digest(path.read_bytes())
        for path in _files(static_dir)
        if path.relative_to(static_dir).as_posix() not in {MANIFEST_NAME, INCOMPLETE_NAME}
    }


class _IndexAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "img", "source"} and attributes.get("src"):
            self.references.append(attributes["src"])
        if tag == "link" and attributes.get("href"):
            self.references.append(attributes["href"])


def validate_index(static_dir: Path, assets: dict[str, str]) -> None:
    if "index.html" not in assets:
        raise BuildValidationError("index_missing")
    parser = _IndexAssets()
    parser.feed((static_dir / "index.html").read_text(encoding="utf-8"))
    for reference in parser.references:
        url = urlsplit(reference)
        if url.scheme or url.netloc or not url.path:
            continue
        relative = unquote(url.path).lstrip("/")
        if relative.startswith("./"):
            relative = relative[2:]
        if "\\" in relative or ".." in relative.split("/") or relative not in assets:
            raise BuildValidationError("index_asset_missing")


def git_identity(project_root: Path) -> dict[str, Any]:
    try:
        git_root = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if Path(git_root).resolve() != project_root.resolve():
            raise BuildValidationError("Package is not the Git checkout root.")
        commit = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain", "--untracked-files=normal"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
        return {"source_commit": commit, "working_tree_dirty": bool(dirty)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"source_commit": "unavailable", "working_tree_dirty": None}


def write_build_manifest(
    project_root: Path, static_dir: Path, *, build_id: str,
    expected_sources: dict[str, str], identity: dict[str, Any], toolchain: dict[str, str],
) -> dict[str, Any]:
    sources = source_records(project_root)
    if sources != expected_sources:
        raise BuildValidationError("source_changed_during_build")
    assets = artifact_records(static_dir)
    validate_index(static_dir, assets)
    manifest = {
        "schema_version": 1,
        "build_id": build_id,
        "source_policy": SOURCE_POLICY,
        **identity,
        "source_fingerprint": manifest_hash(sources),
        "source_files": sources,
        "frontend_lock_hash": sources["webui/package-lock.json"],
        "api_contract_version": api_contract_version(project_root),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "assets_manifest_hash": manifest_hash(assets),
        "assets": assets,
        "toolchain": toolchain,
        "vite_environment_hash": manifest_hash({
            key: _digest(value.encode("utf-8")) for key, value in os.environ.items()
            if key.startswith("VITE_")
        }),
    }
    (static_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    return manifest


def inspect_build(project_root: Path, static_dir: Path, *, api_version: str | None = None) -> dict[str, Any]:
    """Return only public identifiers/codes, never exception paths or env values."""
    result: dict[str, Any] = {
        "status": "unverified", "issues": [], "backend": {}, "frontend": None,
    }
    try:
        sources = source_records(project_root)
        current_api = api_version or api_contract_version(project_root)
        result["backend"] = {
            "source_fingerprint": manifest_hash(sources), "api_contract_version": current_api,
        }
    except (OSError, ValueError, SyntaxError):
        sources = None
        current_api = api_version
        result["issues"].append("source_unavailable")
    try:
        if (static_dir / INCOMPLETE_NAME).exists():
            result["issues"].append("build_incomplete")
        path = static_dir / MANIFEST_NAME
        if not path.is_file():
            raise BuildValidationError("metadata_missing")
        if _is_link(path) or path.stat().st_size > 4 * 1024**2:
            raise BuildValidationError("metadata_invalid")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("source_policy") != SOURCE_POLICY:
            raise BuildValidationError("metadata_invalid")
        for name in ("build_id", "source_commit", "built_at", "api_contract_version"):
            if not isinstance(manifest.get(name), str) or len(manifest[name]) > 128:
                raise BuildValidationError("metadata_invalid")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", manifest["build_id"]):
            raise BuildValidationError("metadata_invalid")
        if manifest["source_commit"] != "unavailable" and not re.fullmatch(r"[a-f0-9]{40,64}", manifest["source_commit"]):
            raise BuildValidationError("metadata_invalid")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", manifest["api_contract_version"]):
            raise BuildValidationError("metadata_invalid")
        datetime.fromisoformat(manifest["built_at"])
        if manifest.get("working_tree_dirty") is not None and not isinstance(manifest["working_tree_dirty"], bool):
            raise BuildValidationError("metadata_invalid")
        for key in ("source_fingerprint", "frontend_lock_hash", "assets_manifest_hash"):
            if not isinstance(manifest.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[key]):
                raise BuildValidationError("metadata_invalid")
        for key in ("source_files", "assets"):
            records = manifest.get(key)
            if not isinstance(records, dict) or not records or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in records.values()
            ):
                raise BuildValidationError("metadata_invalid")
        if sources is not None and (
            manifest.get("source_files") != sources
            or manifest["source_fingerprint"] != manifest_hash(sources)
            or manifest["frontend_lock_hash"] != sources["webui/package-lock.json"]
        ):
            result["issues"].append("source_mismatch")
        if current_api is not None and manifest["api_contract_version"] != current_api:
            result["issues"].append("api_version_mismatch")
        assets = artifact_records(static_dir)
        if manifest.get("assets") != assets or manifest["assets_manifest_hash"] != manifest_hash(assets):
            result["issues"].append("assets_mismatch")
        validate_index(static_dir, assets)
        result["frontend"] = {key: manifest[key] for key in (
            "build_id", "source_commit", "working_tree_dirty", "source_fingerprint",
            "frontend_lock_hash", "api_contract_version", "built_at", "assets_manifest_hash",
        )}
    except BuildValidationError as exc:
        result["issues"].append(str(exc))
    except (OSError, ValueError, TypeError, KeyError):
        result["issues"].append("metadata_invalid")
    if not result["issues"]:
        result["status"] = "verified"
    return result


def enforce_build_policy(result: dict[str, Any], mode: str) -> str | None:
    if mode not in {"development", "production"}:
        raise ValueError("build_mode must be development or production")
    if result["status"] == "verified":
        return None
    message = (
        "Web UI build is unverified (" + ", ".join(result["issues"]) + "). "
        "Run npm ci && npm run build in webui, or install a matching verified release."
    )
    if mode == "production":
        raise BuildValidationError(message)
    return message
