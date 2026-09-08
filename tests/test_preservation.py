from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mms_shp_detection.infrastructure import preservation as module
from mms_shp_detection.infrastructure.preservation import (
    OfflineRequired, PreservationError, archive_run, backup_state, restore_preservation, verify_preservation,
)
from mms_shp_detection.shp_writer import write_shapefile
from mms_shp_detection.webapp import security
from mms_shp_detection.webapp.overlays import (
    _ensure_feature_review_tables, _initialize_feature_store,
)
from mms_shp_detection.webapp.store import WebStore
from mms_shp_detection.webapp.task_resolution_outbox import (
    enqueue_task_resolution_intent, ensure_task_resolution_outbox,
)


NOW = "2026-09-08T01:00:00+00:00"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class PreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = {"schema_version": 1, "mode": "offline-stopped-writers", "operator": "synthetic-test",
                         "evidence_reference": "isolated-test-fixture", "observed_at": datetime.now(timezone.utc).isoformat(),
                         "stopped_writers": ["web", "cli", "external"]}
        self.source = self.root / "원본 실행"
        self.source.mkdir()
        self._run(self.source)

    def _run(self, root: Path, *, job_id: str = "run-1") -> None:
        write_shapefile([{"class_id": 59, "class_name": "도로표지", "confidence": 0.9,
                          "x": 1000000.5, "y": 2000000.5, "z": 50.5, "detection_id": "D1",
                          "image_name": "프레임.jpg", "timestamp_iso": NOW, "point_count": 7}],
                        root / "shp/detected_signs.shp")
        manifest = {"schema_version": 1, "job_id": job_id, "attempt": 1, "status": "succeeded",
                    "created_at": NOW, "input": {"fingerprints": {"dataset": "a" * 64}},
                    "versions": {"git_commit": "b" * 40}, "progress": {}, "counts": {},
                    "outputs": {"shapefiles": ["shp/detected_signs.shp"]},
                    "stages": [{"stage_name": "write_outputs", "attempt": 1, "status": "succeeded"}],
                    "errors": [], "config": {"effective_hash": "c" * 64}}
        write_json(root / "run_manifest.json", manifest)
        write_json(root / "logs/effective_config.json", {"config_version": 1, "display_name": "한글 설정"})
        write_json(root / "txt/프레임.txt", {"schema_version": 18, "detections": []})

    def _archive(self, destination: Path, **overrides) -> dict:
        kwargs = dict(config_paths=["logs/effective_config.json"], artifact_paths=["txt/프레임.txt"],
                      offline=True, evidence=self.evidence, source_alias="fixture-output")
        kwargs.update(overrides)
        return archive_run(self.source, destination, **kwargs)

    def _state(self, *, pending: bool = True, legacy: bool = False) -> tuple[Path, Path]:
        state = self.root / "검수 상태"
        state.mkdir()
        store = WebStore(state / "registry.sqlite3")
        store.upsert_scanning_dataset(dataset_id="dataset-a", name="도로 데이터", root_id="private-source-alias",
                                      relative_path="fixture", crs="EPSG:5179", now=NOW)
        store.finish_dataset_scan("dataset-a", frames=[], tracks=[], bbox=None, warnings=[], now=NOW)
        layer = state / "overlays/ds_fixture/ov_fixture"
        layer.mkdir(parents=True)
        geometry = {"type": "Point", "coordinates": [1000000.5, 2000000.5, 50.5]}
        _initialize_feature_store(layer / "features.sqlite3", iter([
            ("f_1", 0, json.dumps(geometry), json.dumps({"NAME": "한글 표지"}, ensure_ascii=False),
             1000000.5, 2000000.5, 50.5, NOW)]), [{"name": "NAME", "type": "C", "size": 40, "decimal": 0}])
        write_json(layer / "manifest.json", {"schema_version": 1, "id": "ov_fixture", "dataset_id": "dataset-a",
                                             "source_files": [], "registered": True, "name": "한글 레이어"})
        write_json(state / "deployment.json", {"schema_version": 1, "source_root": "private-source-alias"})
        store.create_review_session({"id": "session-a", "dataset_id": "dataset-a", "target_layer_ids": ["ov_fixture"],
                                     "status": "active", "created_by": "operator-local", "created_at": NOW, "updated_at": NOW})
        store.create_review_task({"id": "task-a", "session_id": "session-a", "dataset_id": "dataset-a",
                                  "target_layer_id": "ov_fixture", "task_type": "GEOMETRY_REVIEW", "status": "in_progress" if pending else "corrected",
                                  "priority": 1, "created_at": NOW, "updated_at": NOW, "claimed_by": "operator-local",
                                  "resolved_feature_ids": [] if pending else ["f_1"], "resolution": None if pending else "corrected"})
        if not legacy:
            connection = sqlite3.connect(layer / "features.sqlite3")
            connection.row_factory = sqlite3.Row
            try:
                _ensure_feature_review_tables(connection)
                connection.execute("INSERT INTO feature_provenance VALUES(?,?,?)", (
                    "f_1", json.dumps({"feature_id": "f_1", "layer_id": "ov_fixture", "review_status": "corrected", "created_by": "operator-local"}), NOW))
                connection.execute("INSERT INTO edit_transactions(id,idempotency_key,action,feature_id,task_id,revision,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                   ("edit-1", "once-1", "review_update", "f_1", "task-a", 1, "committed", "operator-local", NOW))
                if pending:
                    enqueue_task_resolution_intent(connection, source_key="edit:edit-1", task_id="task-a", feature_id="f_1",
                        transition_kind="resolve", resolution="corrected", expected_status=None, allow_claim=False,
                        actor="operator-local", now=NOW, dataset_id="dataset-a", layer_id="ov_fixture", session_id="session-a")
                connection.commit()
            finally:
                connection.close()
        return state, layer

    def _backup(self, state: Path, destination: Path, **overrides) -> dict:
        kwargs = dict(config_paths=["deployment.json"], offline=True, evidence=self.evidence, source_alias="state-fixture")
        kwargs.update(overrides)
        return backup_state(state, destination, **kwargs)

    def test_explicit_archive_keeps_original_bytes_after_cli_root_reuse_and_restore(self) -> None:
        archive = self.root / "보존본"
        before = (self.source / "shp/detected_signs.shp").read_bytes()
        index = self._archive(archive)
        self.assertEqual(index["run"]["job_id"], "run-1")
        self.assertEqual(index["run"]["attempt"], 1)
        (self.source / "shp/detected_signs.shp").write_bytes(b"next run output")
        self.assertEqual((archive / "shp/detected_signs.shp").read_bytes(), before)
        restored = self.root / "분리 복원"
        restore_preservation(archive, restored)
        self.assertEqual((restored / "shp/detected_signs.shp").read_bytes(), before)
        self.assertEqual(verify_preservation(restored)["files"], index["files"])
        with self.assertRaisesRegex(PreservationError, "already exists"):
            restore_preservation(archive, restored)
        with self.assertRaisesRegex(PreservationError, "already exists"):
            self._archive(archive)

    def test_missing_offline_confirmation_or_incomplete_evidence_blocks_before_copy(self) -> None:
        target = self.root / "blocked"
        with self.assertRaises(OfflineRequired):
            self._archive(target, offline=False)
        with self.assertRaises(OfflineRequired):
            self._archive(target, evidence={**self.evidence, "stopped_writers": ["web"]})
        self.assertFalse(target.exists())

    def test_partial_shp_failed_manifest_and_raw_artifact_are_rejected(self) -> None:
        target = self.root / "archive"
        path = self.source / "shp/detected_signs.dbf"
        path.unlink()
        with self.assertRaisesRegex(PreservationError, "missing or unsafe"):
            self._archive(target)
        self._run(self.source)
        manifest = module._json(self.source / "run_manifest.json")
        manifest["status"] = "failed"
        write_json(self.source / "run_manifest.json", manifest)
        with self.assertRaisesRegex(PreservationError, "succeeded"):
            self._archive(target)
        self._run(self.source)
        with self.assertRaisesRegex(PreservationError, "generated run output"):
            self._archive(target, artifact_paths=["logs/effective_config.json"])
        self.assertFalse(target.exists())

    def test_disk_full_and_publish_replace_failure_preserve_source_and_existing_archive(self) -> None:
        archive = self.root / "good"
        self._archive(archive)
        original = (archive / "shp/detected_signs.shp").read_bytes()
        for failure in ("copy", "replace"):
            target = self.root / f"failed-{failure}"
            patcher = patch.object(module, "_copy_file", side_effect=OSError(errno.ENOSPC, "disk full")) if failure == "copy" else patch.object(module.os, "replace", side_effect=PermissionError("locked"))
            with patcher, self.assertRaises(OSError):
                self._archive(target)
            self.assertFalse(target.exists())
            self.assertEqual((archive / "shp/detected_signs.shp").read_bytes(), original)
            self.assertFalse(list(self.root.glob(f".{target.name}.staging-*")))

    def test_changed_source_during_archive_is_rejected(self) -> None:
        original_copy = module._copy_file
        def mutate(source, target):
            original_copy(source, target)
            if source.name == "프레임.txt":
                source.write_text('{"changed":true}', encoding="utf-8")
        with patch.object(module, "_copy_file", side_effect=mutate), self.assertRaisesRegex(PreservationError, "changed during archive"):
            self._archive(self.root / "changed")
        self.assertFalse((self.root / "changed").exists())

    def test_multidb_backup_and_separate_restore_preserve_pending_outbox_provenance_revision(self) -> None:
        state, layer = self._state()
        backup = self.root / "상태 보존"
        result = self._backup(state, backup)
        self.assertEqual(len(result["databases"]), 2)
        self.assertEqual(len(result["validation"]["pending_outbox"]), 1)
        restored = self.root / "복원 테스트"
        restore_preservation(backup, restored)
        self.assertEqual(verify_preservation(restored)["validation"], result["validation"])
        connection = sqlite3.connect(restored / layer.relative_to(state) / "features.sqlite3")
        try:
            self.assertIn("한글 표지", connection.execute("SELECT properties_json FROM features").fetchone()[0])
            self.assertEqual(connection.execute("SELECT status FROM task_resolution_outbox").fetchone()[0], "pending")
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0], "1")
        finally:
            connection.close()

    def test_resolved_task_missing_authoritative_provenance_blocks_backup(self) -> None:
        state, layer = self._state(pending=False)
        connection = sqlite3.connect(layer / "features.sqlite3")
        connection.execute("DELETE FROM feature_provenance")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(PreservationError, "provenance missing"):
            self._backup(state, self.root / "invalid")

    def test_additive_ownership_block_prevents_offline_validation(self) -> None:
        state, layer = self._state()
        connection = sqlite3.connect(state / "registry.sqlite3")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        if "ownership_blocked" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN ownership_blocked INTEGER NOT NULL DEFAULT 0")
        connection.execute("INSERT INTO runs(id,dataset_id,status,request_json,resolved_json,work_relative,created_at,updated_at,ownership_blocked) VALUES(?,?,?,?,?,?,?,?,?)",
                           ("uncertain", "dataset-a", "interrupted", "{}", "{}", "uncertain", NOW, NOW, 1))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(PreservationError, "ownership"):
            module.validate_state(state, ["registry.sqlite3", (layer / "features.sqlite3").relative_to(state).as_posix()])

    def test_pending_resolve_missing_authoritative_provenance_is_not_called_recoverable(self) -> None:
        state, layer = self._state()
        connection = sqlite3.connect(layer / "features.sqlite3")
        connection.execute("DELETE FROM feature_provenance")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(PreservationError, "authoritative provenance"):
            self._backup(state, self.root / "invalid-pending")

    def test_pending_reopen_keeps_deleted_feature_and_old_task_until_normal_reconciliation(self) -> None:
        state, layer = self._state(pending=False)
        connection = sqlite3.connect(layer / "features.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.execute("UPDATE features SET deleted=1")
        connection.execute("UPDATE edit_transactions SET status='undone'")
        enqueue_task_resolution_intent(connection, source_key="undo:edit-1", task_id="task-a", feature_id="f_1",
            transition_kind="reopen", resolution=None, expected_status="corrected", allow_claim=False,
            actor="operator-local", now=NOW, dataset_id="dataset-a", layer_id="ov_fixture", session_id="session-a")
        connection.commit()
        connection.close()
        backup = self.root / "pending-reopen"
        result = self._backup(state, backup)
        self.assertEqual(result["validation"]["pending_outbox"][0]["transition_kind"], "reopen")
        restore_preservation(backup, self.root / "reopen-restored")

    def test_completed_run_artifacts_and_effective_config_are_part_of_state_backup(self) -> None:
        state, _ = self._state()
        output = state / "runs/run-1/output"
        self._run(output)
        (state / "runs/run-1/config.yaml").write_text("config_version: 1\n", encoding="utf-8")
        connection = sqlite3.connect(state / "registry.sqlite3")
        connection.execute("INSERT INTO runs(id,dataset_id,status,request_json,resolved_json,work_relative,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           ("run-1", "dataset-a", "completed", "{}", "{}", "run-1", NOW, NOW))
        connection.commit()
        connection.close()
        backup = self.root / "complete-state"
        result = self._backup(state, backup)
        paths = {item["path"] for item in result["files"]}
        self.assertIn("runs/run-1/output/shp/detected_signs.shp", paths)
        self.assertIn("runs/run-1/output/logs/effective_config.json", paths)
        restore_preservation(backup, self.root / "complete-restored")
        (output / "shp/detected_signs.dbf").unlink()
        with self.assertRaisesRegex(PreservationError, "missing or unsafe"):
            self._backup(state, self.root / "incomplete-state")

    def test_outbox_orphan_scope_and_error_state_block_backup(self) -> None:
        state, layer = self._state()
        for sql in ("UPDATE task_resolution_outbox SET task_id='missing'", "UPDATE task_resolution_outbox SET dataset_id='other'",
                    "UPDATE task_resolution_outbox SET status='error'"):
            connection = sqlite3.connect(layer / "features.sqlite3")
            try:
                connection.execute("BEGIN")
                connection.execute(sql)
                connection.commit()
                with self.assertRaises(PreservationError):
                    self._backup(state, self.root / "invalid")
                connection.execute("UPDATE task_resolution_outbox SET task_id='task-a',dataset_id='dataset-a',status='pending'")
                connection.commit()
            finally:
                connection.close()

    def test_all_database_locks_held_and_active_writer_rejected(self) -> None:
        state, layer = self._state()
        original_backup = module._backup_database
        blocked = []
        def checked_backup(source, target):
            for path in (state / "registry.sqlite3", layer / "features.sqlite3"):
                connection = sqlite3.connect(path, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("BEGIN IMMEDIATE")
                    blocked.append(path.name)
                finally:
                    connection.close()
            original_backup(source, target)
        with patch.object(module, "_backup_database", side_effect=checked_backup):
            self._backup(state, self.root / "locked-snapshot")
        self.assertEqual(len(blocked), 4)
        holder = sqlite3.connect(layer / "features.sqlite3")
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self._backup(state, self.root / "blocked-writer")
        finally:
            holder.rollback()
            holder.close()

    def test_legacy_additive_migrations_on_restored_copy_are_repeatable(self) -> None:
        state, layer = self._state(legacy=True)
        backup, restored = self.root / "legacy-backup", self.root / "legacy-restored"
        self._backup(state, backup)
        restore_preservation(backup, restored)
        path = restored / layer.relative_to(state) / "features.sqlite3"
        connection = sqlite3.connect(path)
        try:
            before = connection.execute("SELECT * FROM features").fetchall()
            for _ in range(2):
                _ensure_feature_review_tables(connection)
                ensure_task_resolution_outbox(connection)
                connection.commit()
            self.assertEqual(connection.execute("SELECT * FROM features").fetchall(), before)
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0], "1")
        finally:
            connection.close()
        WebStore(restored / "registry.sqlite3")
        WebStore(restored / "registry.sqlite3")
        self.assertEqual(WebStore(restored / "registry.sqlite3").get_review_task("task-a")["status"], "in_progress")

    def test_registry_active_run_and_unknown_database_are_not_silently_omitted(self) -> None:
        state, _ = self._state()
        connection = sqlite3.connect(state / "registry.sqlite3")
        connection.execute("INSERT INTO runs(id,dataset_id,status,request_json,resolved_json,work_relative,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           ("active", "dataset-a", "running", "{}", "{}", "active", NOW, NOW))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(PreservationError, "active or unknown"):
            self._backup(state, self.root / "active")
        connection = sqlite3.connect(state / "registry.sqlite3")
        connection.execute("DELETE FROM runs")
        connection.commit()
        connection.close()
        (state / "unknown.sqlite3").write_bytes(b"unknown")
        with self.assertRaisesRegex(PreservationError, "unsupported/unlisted"):
            self._backup(state, self.root / "unknown")

    def test_corrupt_backup_and_nested_restore_destination_fail(self) -> None:
        archive = self.root / "archive"
        self._archive(archive)
        with self.assertRaisesRegex(PreservationError, "disjoint"):
            restore_preservation(archive, archive / "nested")
        (archive / "txt/프레임.txt").write_text("changed")
        with self.assertRaisesRegex(PreservationError, "hash/size"):
            restore_preservation(archive, self.root / "restored")

    def test_archive_missing_mandatory_config_or_model_manifest_cannot_pass_rewritten_index(self) -> None:
        models = {"schema_version": 2, "models": [{"status": "completed", "published_current_run": True,
                  "final_shapefiles": {"detections": str((self.source / "shp/detected_signs.shp").resolve())}}]}
        write_json(self.source / "models_manifest.json", models)
        manifest = module._json(self.source / "run_manifest.json")
        manifest["outputs"]["models_manifest"] = "models_manifest.json"
        write_json(self.source / "run_manifest.json", manifest)
        for relative in ("models_manifest.json", "logs/effective_config.json"):
            archive = self.root / ("missing-model" if relative.startswith("models") else "missing-config")
            self._archive(archive)
            index = module._json(archive / module.INDEX)
            index["files"] = [entry for entry in index["files"] if entry["path"] != relative]
            (archive / relative).unlink()
            write_json(archive / module.INDEX, index)
            with self.assertRaises(PreservationError):
                verify_preservation(archive)

    def test_competing_empty_destination_is_preserved_by_no_replace_publication(self) -> None:
        destination = self.root / "raced-target"
        original = module._publish_tree
        def create_competing_directory(staging, target):
            target.mkdir()
            original(staging, target)
        with patch.object(module, "_publish_tree", side_effect=create_competing_directory), self.assertRaises(OSError):
            self._archive(destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])

    def test_ancestor_link_detection_blocks_before_copy(self) -> None:
        original = module._linked
        with patch.object(module, "_linked", side_effect=lambda path: path == self.root or original(path)), self.assertRaisesRegex(PreservationError, "ancestor"):
            self._archive(self.root / "linked-archive")

    def test_atomic_cleanup_failure_keeps_original_error(self) -> None:
        target = self.root / "cleanup.json"
        target.write_bytes(b"previous")
        original_unlink = Path.unlink
        def fail_temporary(path, *args, **kwargs):
            if path.name.startswith(".cleanup.json."):
                raise PermissionError("cleanup locked")
            return original_unlink(path, *args, **kwargs)
        with patch.object(security.os, "fsync", side_effect=OSError(errno.ENOSPC, "full")), patch.object(Path, "unlink", fail_temporary):
            with self.assertRaises(OSError) as context:
                security.atomic_replace_bytes(target, b"new")
        self.assertEqual(context.exception.errno, errno.ENOSPC)
        self.assertTrue(context.exception.__notes__)
        self.assertEqual(target.read_bytes(), b"previous")

    def test_atomic_writer_unique_temp_flush_and_replace_failures_preserve_previous(self) -> None:
        target = self.root / "동시.json"
        target.write_bytes(b"previous")
        for failure in ("flush", "replace"):
            patcher = patch.object(security.os, "fsync", side_effect=OSError(errno.ENOSPC, "full")) if failure == "flush" else patch.object(security.os, "replace", side_effect=PermissionError("locked"))
            with patcher, self.assertRaises(OSError):
                security.atomic_replace_bytes(target, b"new")
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertFalse(list(self.root.glob(".동시.json.*.tmp")))
        barrier = threading.Barrier(2)
        original_replace = os.replace
        seen = []
        lock = threading.Lock()
        def synchronized_replace(source, destination):
            with lock:
                first = str(source) not in seen
                seen.append(str(source))
            if first:
                barrier.wait(timeout=5)
            original_replace(source, destination)
        with patch.object(security.os, "replace", side_effect=synchronized_replace), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(security.atomic_replace_bytes, target, payload) for payload in (b"first", b"second")]
            for future in futures:
                future.result()
        self.assertEqual(len(set(seen)), 2)
        self.assertIn(target.read_bytes(), (b"first", b"second"))
        self.assertFalse(list(self.root.glob(".동시.json.*.tmp")))

    def test_cli_offline_precondition_failure_propagates_nonzero(self) -> None:
        evidence = self.root / "offline.json"
        write_json(evidence, self.evidence)
        script = Path(__file__).resolve().parents[1] / "scripts/preserve_mms_state.py"
        result = subprocess.run([sys.executable, str(script), "archive-run", "--source", str(self.source),
                                 "--destination", str(self.root / "cli-archive"), "--source-alias", "fixture",
                                 "--config", "logs/effective_config.json", "--offline-evidence", str(evidence)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCKED_VALIDATION")


if __name__ == "__main__":
    unittest.main()
