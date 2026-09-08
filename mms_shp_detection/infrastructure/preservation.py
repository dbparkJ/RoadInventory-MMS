"""Opt-in, offline preservation; never changes CLI output or live registry state.

The operator must stop ALL writers before invoking this module. SQLite locks,
backup(), and before/after checks are additional defenses, not proof that other
file writers were stopped or a distributed/global snapshot protocol.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
from typing import Any, Iterator

import shapefile

from .manifest_writer import validate_manifest_document, validate_published_outputs
from ..webapp.security import atomic_replace_bytes, normalize_relative_path


INDEX = "preservation.json"
SHP_COMPONENTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2")
ACTIVE_RUNS = {"queued", "preparing", "starting", "running", "cancelling"}
TERMINAL_RUNS = {"completed", "failed", "cancelled", "interrupted"}


class PreservationError(ValueError):
    """Invalid, changed, incomplete, or unsupported preservation evidence."""


class OfflineRequired(PreservationError):
    """The deployment's stopped-writer precondition has not been acknowledged."""


def _check(condition: Any, message: str) -> None:
    if not condition:
        raise PreservationError(message)


def _json(path: Path) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            _check(key not in result, "duplicate JSON object key")
            result[key] = value
        return result

    def constant(_value):
        raise PreservationError("non-finite JSON value")

    result = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    _check(isinstance(result, dict), "expected JSON object")
    return result


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _assert_unlinked(path: Path) -> None:
    absolute = path.absolute()
    _check(all(not _linked(component) for component in (absolute, *absolute.parents)),
           "linked path ancestor is unsupported")


def _safe(root: Path, relative: str) -> Path:
    normalized = normalize_relative_path(relative, allow_empty=False)
    _check(normalized == relative, "paths must use normalized relative POSIX names")
    _assert_unlinked(root)
    current = root
    for part in Path(relative).parts:
        current = current / part
        _check(not _linked(current), "symbolic links/junctions are not preserved")
    _check(current.resolve().is_relative_to(root.resolve()), "path escapes preservation root")
    return current


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _file_evidence(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = _hash(path)
    after = path.stat()
    _check((before.st_size, before.st_mtime_ns, before.st_ino) ==
           (after.st_size, after.st_mtime_ns, after.st_ino), "file changed while hashing")
    return {"size": after.st_size, "sha256": digest}


def _offline(evidence: dict[str, Any], offline: bool) -> dict[str, Any]:
    if not offline:
        raise OfflineRequired("--offline requires all web, CLI, and external writers to be stopped first")
    required = {"schema_version", "mode", "operator", "evidence_reference", "observed_at", "stopped_writers"}
    if not isinstance(evidence, dict):
        raise OfflineRequired("offline evidence must be an object")
    if not required <= evidence.keys() or evidence.get("schema_version") != 1 or evidence.get("mode") != "offline-stopped-writers":
        raise OfflineRequired("a stopped-writer evidence document is required")
    if set(evidence["stopped_writers"]) != {"web", "cli", "external"}:
        raise OfflineRequired("evidence must cover web, CLI, and external writers")
    for key in ("operator", "evidence_reference", "observed_at"):
        if not isinstance(evidence[key], str) or not evidence[key].strip() or "<" in evidence[key]:
            raise OfflineRequired(f"offline evidence {key} is required")
    observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
    _check(observed.tzinfo is not None, "offline observed_at must include timezone")
    return {key: evidence[key] for key in sorted(required)}


@contextmanager
def _new_tree(source: Path, destination: Path) -> Iterator[Path]:
    _assert_unlinked(source)
    _assert_unlinked(destination)
    source, destination = source.resolve(), destination.absolute()
    _check(destination.parent.is_dir(), "destination parent must already exist")
    _safe(destination.parent, destination.name)
    resolved = destination.resolve()
    _check(not resolved.is_relative_to(source) and not source.is_relative_to(resolved),
           "source and destination must be disjoint")
    _check(not destination.exists(), "destination already exists; immutable preservation never overwrites")
    lock = destination.with_name(f".{destination.name}.preservation.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
        staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
        yield staging
        _assert_unlinked(destination)
        _assert_unlinked(staging)
        _check((staging.stat().st_dev, staging.stat().st_ino) == staging_identity, "staging directory ownership changed")
        _publish_tree(staging, destination)
        staging = None
    finally:
        primary_error = sys.exception()
        try:
            if staging is not None:
                _assert_unlinked(staging)
                _check(staging.resolve().parent == destination.parent.resolve()
                       and (staging.stat().st_dev, staging.stat().st_ino) == staging_identity, "unsafe staging cleanup")
                shutil.rmtree(staging)
            _assert_unlinked(lock)
            lock.unlink(missing_ok=True)
        except (OSError, PreservationError) as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"Owned staging cleanup also failed: {type(cleanup_error).__name__}")


def _publish_tree(staging: Path, destination: Path) -> None:
    """Atomic directory publication that never replaces any existing target."""
    if os.name == "nt":
        os.rename(staging, destination)  # Windows refuses existing destinations.
        return
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        _check(rename is not None, "atomic no-replace publication is unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "atomic no-replace publication failed")
        return
    raise PreservationError("atomic no-replace publication is unsupported on this platform")


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _run_inventory(root: Path, *, portable: bool = False) -> tuple[dict[str, str], dict[str, Any]]:
    manifest = _json(_safe(root, "run_manifest.json"))
    _check(not validate_manifest_document(manifest), "invalid run manifest")
    _check(manifest["status"] == "succeeded", "only succeeded runs can be archived as immutable results")
    current = [stage for stage in manifest["stages"] if stage.get("attempt", 1) == manifest["attempt"]]
    _check(current and not manifest["progress"].get("failed_stage")
           and all(stage.get("status") in {"succeeded", "skipped"} for stage in current),
           "run has incomplete or failed current-attempt stages")
    outputs = manifest["outputs"]
    check_outputs = {"shapefiles": outputs.get("shapefiles")} if portable else outputs
    _check(not validate_published_outputs(root, check_outputs), "published run artifacts are missing or unsafe")
    files = {"run_manifest.json": "run_manifest"}
    for relative in outputs["shapefiles"]:
        try:
            with shapefile.Reader(str(_safe(root, relative)), encoding="utf-8") as reader:
                _check(reader.shapeType == shapefile.POINTZ, "published SHP must be PointZ")
                count = 0
                for shape in reader.iterShapes():
                    _check(shape.shapeType == shapefile.POINTZ and len(shape.points) == 1 and len(shape.z) == 1
                           and all(math.isfinite(value) for value in [*shape.points[0], shape.z[0]]), "invalid published PointZ")
                    count += 1
                _check(count == reader.numRecords == sum(1 for _ in reader.iterRecords()),
                       "published SHP geometry/record contract is incomplete")
        except (shapefile.ShapefileException, struct.error) as exc:
            raise PreservationError("unreadable published SHP bundle") from exc
        for suffix in SHP_COMPONENTS:
            files[Path(relative).with_suffix(suffix).as_posix()] = "published_shapefile"
    if outputs.get("models_manifest"):
        files[outputs["models_manifest"]] = "model_manifest"
    return files, {"job_id": manifest["job_id"], "attempt": manifest["attempt"],
                   "versions": manifest["versions"], "config": manifest.get("config", {}),
                   "input_fingerprints": manifest["input"].get("fingerprints", {})}


def _config_inventory(root: Path, paths: list[str]) -> dict[str, str]:
    _check(isinstance(paths, list) and paths, "explicit configuration snapshots are required")
    result = {}
    for relative in paths:
        path = _safe(root, relative)
        _check(path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}, "invalid config snapshot")
        _check(relative not in result, "duplicate config snapshot")
        result[relative] = "config_snapshot"
    return result


def _extra_artifacts(root: Path, paths: list[str]) -> dict[str, str]:
    result = {}
    allowed = {"txt", "image_crops", "point_crops", "pole_crops", "point_previews", "forward_views", "pole_debug"}
    for relative in paths:
        path = _safe(root, relative)
        _check(Path(relative).parts[0] in allowed and path.is_file(), "extra artifact must be a generated run output")
        result[relative] = "generated_artifact"
    return result


def _write_index(root: Path, kind: str, files: dict[str, str], metadata: dict[str, Any]) -> dict[str, Any]:
    entries = [{"path": relative, "role": role, **_file_evidence(_safe(root, relative))}
               for relative, role in sorted(files.items())]
    index = {"schema_version": 1, "kind": kind, "created_at": datetime.now(timezone.utc).isoformat(),
             "files": entries, **metadata}
    atomic_replace_bytes(root / INDEX, json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
    return index


def archive_run(source: Path, destination: Path, *, config_paths: list[str],
                artifact_paths: list[str], offline: bool, evidence: dict[str, Any], source_alias: str) -> dict[str, Any]:
    evidence = _offline(evidence, offline)
    _check(not destination.exists(), "destination already exists; immutable preservation never overwrites")
    _check(source_alias and "<" not in source_alias, "safe source alias is required")
    files, identity = _run_inventory(source)
    configs, artifacts = _config_inventory(source, config_paths), _extra_artifacts(source, artifact_paths)
    _check(not (files.keys() & configs.keys() or files.keys() & artifacts.keys() or configs.keys() & artifacts.keys()),
           "archive file roles must not overlap")
    files.update(configs)
    files.update(artifacts)
    before = {name: _file_evidence(_safe(source, name)) for name in files}
    with _new_tree(source, destination) as staging:
        for relative in files:
            _copy_file(_safe(source, relative), _safe(staging, relative))
        _check(before == {name: _file_evidence(_safe(source, name)) for name in files}, "run files changed during archive")
        index = _write_index(staging, "immutable_run_archive", files,
                             {"source_alias": source_alias, "offline_evidence": evidence, "run": identity,
                              "config_paths": config_paths, "artifact_paths": artifact_paths})
        _check(before == {item["path"]: {"size": item["size"], "sha256": item["sha256"]} for item in index["files"]},
               "archived bytes differ from source")
        verify_preservation(staging)
    return index


def _connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _database_locks(root: Path, relatives: list[str]) -> Iterator[None]:
    with ExitStack() as stack:
        for relative in sorted(relatives):
            connection = _connect(_safe(root, relative), writable=True)
            stack.callback(connection.close)
            connection.execute("BEGIN IMMEDIATE")
            stack.callback(connection.rollback)
        yield


def _db_digest(path: Path) -> str:
    connection = _connect(path)
    try:
        digest = hashlib.sha256()
        for line in connection.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def _backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    reader, writer = _connect(source), sqlite3.connect(target)
    try:
        reader.backup(writer)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.commit()
    finally:
        writer.close()
        reader.close()
    with target.open("r+b") as handle:
        os.fsync(handle.fileno())


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    # table is always an internal literal, never a user-provided identifier.
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def _integrity(connection: sqlite3.Connection) -> None:
    _check([row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
    _check(not list(connection.execute("PRAGMA foreign_key_check")), "SQLite foreign key check failed")


def _state_inventory(root: Path, config_paths: list[str], *, portable: bool = False) -> tuple[dict[str, str], list[str]]:
    files = {"registry.sqlite3": "registry_database", **_config_inventory(root, config_paths)}
    database_paths = ["registry.sqlite3"]
    _check(_safe(root, "registry.sqlite3").is_file(), "registry database is missing")
    for path in sorted(root.rglob("features.sqlite3")):
        relative = path.relative_to(root).as_posix()
        _safe(root, relative)
        _check(Path(relative).parts[0] in {"overlays", "overlay_archive"}, "unrecognized feature DB location")
        database_paths.append(relative)
        files[relative] = "feature_database"
        manifest_path = path.parent / "manifest.json"
        manifest = _json(_safe(root, manifest_path.relative_to(root).as_posix()))
        files[manifest_path.relative_to(root).as_posix()] = "layer_manifest"
        for filename in manifest.get("source_files", []):
            _check(Path(filename).name == filename, "invalid layer source filename")
            relative_source = (path.parent / "source" / filename).relative_to(root).as_posix()
            _check(_safe(root, relative_source).is_file(), "imported layer source component missing")
            files[relative_source] = "imported_layer_source"
    # Unknown DBs must be reviewed instead of silently omitted.
    discovered = {path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.suffix.lower() in {".sqlite3", ".sqlite", ".db"} and path.is_file()}
    _check(discovered == set(database_paths), "state contains an unsupported/unlisted database")
    connection = _connect(root / "registry.sqlite3")
    try:
        for run in _rows(connection, "runs"):
            _check(run["status"] in TERMINAL_RUNS, "state includes an active or unknown run")
            run_dir = Path("runs") / run["work_relative"]
            config = (run_dir / "config.yaml").as_posix()
            files.update(_config_inventory(root, [config]))
            output_relative = run_dir / "output"
            output_root = _safe(root, output_relative.as_posix())
            if run["status"] == "completed":
                run_files, identity = _run_inventory(output_root, portable=portable)
                _check(identity["job_id"] == run["id"], "registry/run manifest identity mismatch")
                for relative, role in run_files.items():
                    files[(output_relative / relative).as_posix()] = role
                effective = list(output_root.rglob("effective_config.json"))
                _check(effective, "completed run effective configuration snapshot is missing")
                for path in effective:
                    _check(path.parent.name == "logs", "unexpected effective configuration location")
                    files[path.relative_to(root).as_posix()] = "effective_config_snapshot"
                # Web runs already have distinct roots. Preserve their generated
                # frame/crop evidence, never follow dataset/model/source paths.
                model_roots = {Path(path).parent.parent for path in run_files if path.endswith(".shp")}
                for model_root in model_roots:
                    for folder in ("txt", "image_crops", "point_crops", "pole_crops", "point_previews", "forward_views", "pole_debug"):
                        directory = _safe(output_root, (model_root / folder).as_posix())
                        if directory.is_dir():
                            for path in directory.rglob("*"):
                                if path.is_file():
                                    _safe(root, path.relative_to(root).as_posix())
                                    files[path.relative_to(root).as_posix()] = "run_scoped_generated_artifact"
            else:
                manifest_path = output_root / "run_manifest.json"
                _check(manifest_path.is_file(), "terminal run manifest is unavailable")
                manifest = _json(manifest_path)
                _check(not validate_manifest_document(manifest) and manifest["job_id"] == run["id"]
                       and manifest["status"] in {"failed", "cancelled"}, "terminal run evidence is inconsistent")
                files[(output_relative / "run_manifest.json").as_posix()] = "failed_run_manifest"
    finally:
        connection.close()
    return files, sorted(database_paths)


def validate_state(root: Path, database_paths: list[str]) -> dict[str, Any]:
    """Read-only cross-DB validation; pending outbox intents are preserved."""
    with ExitStack() as stack:
        databases = {}
        for relative in database_paths:
            connection = _connect(_safe(root, relative))
            stack.callback(connection.close)
            _integrity(connection)
            databases[relative] = connection
        registry = databases["registry.sqlite3"]
        datasets = {row["id"]: row for row in _rows(registry, "datasets")}
        _check(all(row["status"] != "scanning" for row in datasets.values()), "dataset scan is incomplete")
        _check(all(row["status"] != "uploading" for row in _rows(registry, "uploads")), "upload is incomplete")
        sessions = {row["id"]: row for row in _rows(registry, "review_sessions")}
        tasks = {row["id"]: row for row in _rows(registry, "review_tasks")}
        runs = {row["id"]: row for row in _rows(registry, "runs")}
        _check(all(run["status"] in TERMINAL_RUNS for run in runs.values()), "active/unknown run blocks offline snapshot")
        _check(not any(run.get("ownership_blocked") for run in runs.values()),
               "unresolved process ownership blocks offline snapshot")
        layers = {}
        pending = []
        for relative, connection in databases.items():
            if relative == "registry.sqlite3":
                continue
            manifest = _json(_safe(root, (Path(relative).parent / "manifest.json").as_posix()))
            identity = (manifest["dataset_id"], manifest["id"])
            _check(identity[0] in datasets and identity not in layers, "orphan or duplicate layer identity")
            revision_row = connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
            _check(revision_row is not None and int(revision_row[0]) >= 1, "invalid layer revision")
            revision = int(revision_row[0])
            features = {row["id"]: row for row in _rows(connection, "features")}
            for feature in features.values():
                _check(isinstance(json.loads(feature["properties_json"]), dict), "invalid feature properties")
                geometry = json.loads(feature["geometry_json"]) if feature["geometry_json"] else None
                if geometry and geometry.get("type") == "Point":
                    coordinates = geometry["coordinates"]
                    _check(len(coordinates) in {2, 3} and all(type(value) in {int, float} and math.isfinite(value) for value in coordinates),
                           "invalid Point feature coordinates")
                    _check(list(coordinates) == [feature["point_x"], feature["point_y"], feature["point_z"]][:len(coordinates)],
                           "feature geometry/index coordinates disagree")
            tables = _tables(connection)
            provenance = {row["feature_id"]: json.loads(row["provenance_json"])
                          for row in _rows(connection, "feature_provenance")} if "feature_provenance" in tables else {}
            for feature_id, value in provenance.items():
                _check(feature_id in features and value.get("feature_id") == feature_id and value.get("layer_id") == identity[1],
                       "feature provenance identity mismatch")
            edits = _rows(connection, "edit_transactions") if "edit_transactions" in tables else []
            for edit in edits:
                _check(edit["feature_id"] is None or edit["feature_id"] in features, "edit references missing feature")
                _check(edit.get("revision") is None or 1 <= edit["revision"] <= revision, "edit revision exceeds layer revision")
                _check(edit["task_id"] is None or edit["task_id"] in tasks, "edit references missing task")
            for audit in _rows(connection, "audit"):
                field_change = audit["action"] == "delete_field" and str(audit["feature_id"]).startswith("field:")
                _check((audit["feature_id"] in features or field_change) and 1 <= audit["revision"] <= revision, "audit/feature revision mismatch")
            intents = _rows(connection, "task_resolution_outbox") if "task_resolution_outbox" in tables else []
            for intent in intents:
                _check(intent["status"] in {"pending", "reconciled"}, "outbox error requires recovery before backup")
                task = tasks.get(intent["task_id"])
                feature = features.get(intent["feature_id"])
                _check(task is not None and feature is not None, "outbox references missing task/feature")
                _check(task["dataset_id"] == identity[0] and task["target_layer_id"] in {None, identity[1]}
                       and intent["dataset_id"] in {None, identity[0]} and intent["layer_id"] in {None, identity[1]}
                       and intent["session_id"] in {None, task["session_id"]}, "outbox scope mismatch")
                if intent["status"] == "pending":
                    _check(sessions[task["session_id"]]["status"] in {"draft", "active", "paused"}, "pending outbox in immutable session")
                    targets = json.loads(sessions[task["session_id"]]["target_layer_ids_json"])
                    _check(not targets or identity[1] in targets, "pending outbox outside session layer scope")
                    _check(task["claimed_by"] in {None, intent["actor"]}, "pending outbox task ownership mismatch")
                    linked = intent["feature_id"] in json.loads(task["resolved_feature_ids_json"])
                    if intent["transition_kind"] == "resolve":
                        _check(not feature["deleted"] and (task["status"] == "in_progress"
                               or (task["status"] == "todo" and intent["allow_claim"])
                               or (task["status"] == intent["resolution"] and linked)), "unrecoverable pending resolve")
                        if intent["resolution"] in {"corrected", "manual_added"}:
                            feature_provenance = provenance.get(intent["feature_id"], {})
                            _check(feature_provenance.get("review_status") == intent["resolution"]
                                   and any(edit["feature_id"] == intent["feature_id"] and edit["task_id"] == task["id"]
                                           and edit["status"] == "committed" for edit in edits),
                                   "pending resolve authoritative provenance/edit linkage missing")
                    else:
                        _check(task["status"] == "todo" or (task["status"] == intent["expected_status"] and linked),
                               "unrecoverable pending reopen")
                    pending.append({"layer": list(identity), "id": intent["id"], "task_id": task["id"],
                                    "feature_id": feature["id"], "transition_kind": intent["transition_kind"]})
            layers[identity] = {"revision": revision, "features": features, "provenance": provenance, "edits": edits,
                                "intents": intents, "registered": manifest.get("registered", True)}
        for session in sessions.values():
            _check(session["dataset_id"] in datasets, "session references missing dataset")
            for layer_id in json.loads(session["target_layer_ids_json"]):
                _check((session["dataset_id"], layer_id) in layers, "session references missing layer")
            for run_id in json.loads(session["source_run_ids_json"]):
                _check(run_id in runs and runs[run_id]["dataset_id"] == session["dataset_id"], "session/run scope mismatch")
        for task in tasks.values():
            session = sessions.get(task["session_id"])
            _check(session is not None and session["dataset_id"] == task["dataset_id"], "task/session dataset mismatch")
            if task["source_run_id"]:
                _check(task["source_run_id"] in runs and runs[task["source_run_id"]]["dataset_id"] == task["dataset_id"], "task/run scope mismatch")
            layer = layers.get((task["dataset_id"], task["target_layer_id"]))
            _check(task["target_layer_id"] is None or layer is not None, "task references missing layer")
            resolved = json.loads(task["resolved_feature_ids_json"])
            if task["status"] in {"corrected", "manual_added"}:
                _check(layer is not None and resolved, "resolved task requires authoritative features")
                for feature_id in resolved:
                    _check(feature_id in layer["features"] and feature_id in layer["provenance"], "resolved task provenance missing")
                    reopening = any(item["task_id"] == task["id"] and item["feature_id"] == feature_id
                                    and item["transition_kind"] == "reopen" for item in pending)
                    if not reopening:
                        _check(not layer["features"][feature_id]["deleted"]
                               and layer["provenance"][feature_id].get("review_status") == task["status"]
                               and any(edit["task_id"] == task["id"] and edit["feature_id"] == feature_id
                                       and edit["status"] == "committed" for edit in layer["edits"]),
                               "resolved task/feature/edit linkage inconsistent")
        return {"datasets": len(datasets), "runs": len(runs), "sessions": len(sessions), "tasks": len(tasks),
                "layers": [{"dataset_id": identity[0], "layer_id": identity[1], "revision": layer["revision"],
                            "features": len(layer["features"]), "active_features": sum(not row["deleted"] for row in layer["features"].values()),
                            "provenance": len(layer["provenance"]), "edits": len(layer["edits"])} for identity, layer in sorted(layers.items())],
                "pending_outbox": sorted(pending, key=lambda item: item["id"])}


def backup_state(source: Path, destination: Path, *, config_paths: list[str], offline: bool,
                 evidence: dict[str, Any], source_alias: str) -> dict[str, Any]:
    evidence = _offline(evidence, offline)
    files, databases = _state_inventory(source, config_paths)
    with _database_locks(source, databases):
        _check((files, databases) == _state_inventory(source, config_paths), "state inventory changed while acquiring DB locks")
        validation = validate_state(source, databases)
        before = {name: _db_digest(_safe(source, name)) if name in databases else _file_evidence(_safe(source, name)) for name in files}
        with _new_tree(source, destination) as staging:
            for relative in files:
                if relative in databases:
                    _backup_database(_safe(source, relative), _safe(staging, relative))
                else:
                    _copy_file(_safe(source, relative), _safe(staging, relative))
            _check((files, databases) == _state_inventory(source, config_paths), "state inventory changed during backup")
            after = {name: _db_digest(_safe(source, name)) if name in databases else _file_evidence(_safe(source, name)) for name in files}
            _check(before == after, "source state changed during backup")
            _check(validation == validate_state(staging, databases), "restored state invariants differ")
            for relative in databases:
                _check(before[relative] == _db_digest(_safe(staging, relative)), "database logical content changed during backup")
            index = _write_index(staging, "offline_state_backup", files, {"source_alias": source_alias,
                "offline_evidence": evidence, "databases": databases, "validation": validation,
                "database_logical_sha256": {relative: before[relative] for relative in databases},
                "config_paths": config_paths})
            verify_preservation(staging)
    return index


def verify_preservation(root: Path) -> dict[str, Any]:
    index = _json(_safe(root, INDEX))
    _check(index.get("schema_version") == 1 and index.get("kind") in {"immutable_run_archive", "offline_state_backup"}, "unknown preservation format")
    expected = set()
    for entry in index["files"]:
        relative = entry["path"]
        _check(relative not in expected and relative != INDEX, "duplicate preservation entry")
        expected.add(relative)
        _check(_file_evidence(_safe(root, relative)) == {"size": entry["size"], "sha256": entry["sha256"]}, "preserved file hash/size mismatch")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    _check(actual == expected | {INDEX}, "preservation inventory is incomplete or contains unlisted files")
    if index["kind"] == "immutable_run_archive":
        files, identity = _run_inventory(root, portable=True)
        configs = _config_inventory(root, index["config_paths"])
        artifacts = _extra_artifacts(root, index["artifact_paths"])
        _check(not (files.keys() & configs.keys() or files.keys() & artifacts.keys() or configs.keys() & artifacts.keys()),
               "archive file roles must not overlap")
        files.update(configs)
        files.update(artifacts)
        _check(files == {entry["path"]: entry["role"] for entry in index["files"]},
               "archive mandatory run/config/artifact coverage differs")
        _check(identity == index["run"], "archived run/attempt identity changed")
    else:
        files, databases = _state_inventory(root, index["config_paths"], portable=True)
        _check(files == {item["path"]: item["role"] for item in index["files"]}
               and databases == index["databases"], "state backup coverage differs from registry/layer declarations")
        _check(validate_state(root, index["databases"]) == index["validation"], "state validation evidence differs")
        for relative in index["databases"]:
            _check(_db_digest(_safe(root, relative)) == index["database_logical_sha256"][relative], "DB logical checksum mismatch")
    return index


def restore_preservation(source: Path, destination: Path) -> dict[str, Any]:
    """Restore into a new disjoint directory only; no live replacement or replay."""
    before = verify_preservation(source)
    with _new_tree(source, destination) as staging:
        for relative in [entry["path"] for entry in before["files"]] + [INDEX]:
            _copy_file(_safe(source, relative), _safe(staging, relative))
        _check(verify_preservation(source) == before, "backup evidence changed during restore")
        _check(verify_preservation(staging) == before, "restored evidence differs")
    return before
