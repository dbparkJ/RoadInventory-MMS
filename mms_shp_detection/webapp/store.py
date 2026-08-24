from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

ACTIVE_RUN_STATUSES = ("queued", "preparing", "starting", "running", "cancelling")


class ReviewSessionReadOnlyError(RuntimeError):
    """Raised when a derived QA write targets a terminal review session."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _effective_review_target_layer_ids(
    connection: sqlite3.Connection,
    session: Mapping[str, Any] | sqlite3.Row,
) -> set[str]:
    """Return the declared scope plus task-owned wildcard target layers."""

    values = dict(session)
    layer_ids = {
        str(value)
        for value in _loads(values.get("target_layer_ids_json"), [])
    }
    session_id = values.get("id")
    if session_id is None:
        return layer_ids
    rows = connection.execute(
        """
        SELECT DISTINCT target_layer_id FROM review_tasks
        WHERE session_id=? AND target_layer_id IS NOT NULL
        """,
        (str(session_id),),
    ).fetchall()
    layer_ids.update(str(row["target_layer_id"]) for row in rows)
    return layer_ids


def _qa_snapshot_blockers(
    session: Mapping[str, Any] | sqlite3.Row,
    current_layer_revisions: Mapping[str, int | None] | None,
) -> dict[str, int]:
    values = dict(session)
    raw_snapshot = values.get("qa_layer_revisions_json")
    snapshot = _loads(raw_snapshot, None)
    qa_not_run = int(
        values.get("qa_ran_at") is None
        or not isinstance(snapshot, dict)
    )
    if qa_not_run:
        return {"qa_not_run": 1, "stale_qa_target_layers": 0}

    target_layer_ids = (
        {str(value) for value in current_layer_revisions}
        if current_layer_revisions is not None
        else {
            str(value)
            for value in _loads(values.get("target_layer_ids_json"), [])
        }
    )
    snapshot_keys = {str(value) for value in snapshot}
    mismatched = target_layer_ids.symmetric_difference(snapshot_keys)
    for layer_id in target_layer_ids.intersection(snapshot_keys):
        stored_revision = snapshot.get(layer_id)
        current_revision = (
            None
            if current_layer_revisions is None
            else current_layer_revisions.get(layer_id)
        )
        if (
            isinstance(stored_revision, bool)
            or not isinstance(stored_revision, int)
            or stored_revision < 1
            or isinstance(current_revision, bool)
            or not isinstance(current_revision, int)
            or current_revision < 1
            or stored_revision != current_revision
        ):
            mismatched.add(layer_id)
    return {
        "qa_not_run": 0,
        "stale_qa_target_layers": len(mismatched),
    }


class WebStore:
    """Small persistent registry with one short-lived SQLite connection per call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        lock = self._write_lock if write else _NullLock()
        with lock:
            connection = self._connect()
            try:
                yield connection
                if write:
                    connection.commit()
            except BaseException:
                if write:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self.connection(write=True) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    crs TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    frame_count INTEGER NOT NULL DEFAULT 0,
                    tracks_json TEXT NOT NULL DEFAULT '[]',
                    bbox_json TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    catalog_status TEXT NOT NULL DEFAULT 'missing',
                    catalog_error TEXT,
                    registered INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(root_id, relative_path, crs)
                );
                CREATE TABLE IF NOT EXISTS frames (
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    track_id TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    longitude REAL,
                    latitude REAL,
                    altitude REAL,
                    heading REAL,
                    PRIMARY KEY(dataset_id, id),
                    UNIQUE(dataset_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS frames_dataset_track
                    ON frames(dataset_id, track_id, ordinal);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    safe_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    destination_relative_path TEXT,
                    error TEXT,
                    total_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upload_files (
                    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    offset INTEGER NOT NULL DEFAULT 0,
                    last_modified INTEGER,
                    PRIMARY KEY(upload_id, id),
                    UNIQUE(upload_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    name TEXT,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    resolved_json TEXT NOT NULL,
                    work_relative TEXT NOT NULL,
                    pid INTEGER,
                    return_code INTEGER,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    dismissed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_status_created
                    ON runs(status, created_at);
                CREATE INDEX IF NOT EXISTS runs_dataset_status_finished
                    ON runs(dataset_id, status, finished_at, updated_at, created_at, id);
                CREATE TABLE IF NOT EXISTS survey_segments (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    coordinates_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS survey_segments_dataset_created
                    ON survey_segments(dataset_id, created_at, id);
                CREATE TABLE IF NOT EXISTS review_sessions (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    source_run_ids_json TEXT NOT NULL DEFAULT '[]',
                    target_layer_ids_json TEXT NOT NULL DEFAULT '[]',
                    track_ids_json TEXT NOT NULL DEFAULT '[]',
                    frame_start INTEGER,
                    frame_end INTEGER,
                    class_filters_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN (
                        'draft','active','paused','completed','archived'
                    )),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_task_id TEXT,
                    qa_layer_revisions_json TEXT,
                    qa_ran_at TEXT,
                    CHECK(
                        (frame_start IS NULL AND frame_end IS NULL)
                        OR (frame_start >= 0 AND frame_end >= frame_start)
                    )
                );
                CREATE INDEX IF NOT EXISTS review_sessions_dataset_created
                    ON review_sessions(dataset_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS review_sessions_dataset_status
                    ON review_sessions(dataset_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS review_tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES review_sessions(id) ON DELETE CASCADE,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    task_type TEXT NOT NULL CHECK(task_type IN (
                        'MANUAL_SCAN','LOW_CONFIDENCE','PROJECTION_FAILED',
                        'GEOMETRY_REVIEW','POLE_BASE_REVIEW','SPACING_ANOMALY',
                        'UNREVIEWED_INTERVAL','MANUAL_FLAG'
                    )),
                    status TEXT NOT NULL CHECK(status IN (
                        'todo','in_progress','confirmed','corrected','manual_added',
                        'false_positive','skipped','field_survey'
                    )),
                    priority REAL NOT NULL CHECK(priority >= 0),
                    queue_priority REAL NOT NULL CHECK(queue_priority >= 0),
                    frame_id TEXT,
                    track_id TEXT,
                    frame_start INTEGER,
                    frame_end INTEGER,
                    source_run_id TEXT,
                    source_detection_id TEXT,
                    target_layer_id TEXT,
                    class_hint TEXT,
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    location_hint_json TEXT,
                    source_fingerprint TEXT,
                    priority_evidence_json TEXT NOT NULL DEFAULT '{}',
                    claimed_by TEXT,
                    resolved_feature_ids_json TEXT NOT NULL DEFAULT '[]',
                    resolution TEXT CHECK(resolution IS NULL OR resolution IN (
                        'confirmed','corrected','manual_added','false_positive',
                        'skipped','field_survey'
                    )),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(
                        (frame_start IS NULL AND frame_end IS NULL)
                        OR (
                            task_type='UNREVIEWED_INTERVAL'
                            AND frame_start >= 0
                            AND frame_end >= frame_start
                        )
                    )
                );
                CREATE INDEX IF NOT EXISTS review_tasks_session_queue
                    ON review_tasks(
                        session_id, queue_priority DESC, created_at, id
                    );
                CREATE INDEX IF NOT EXISTS review_tasks_dataset_frame
                    ON review_tasks(dataset_id, frame_id, created_at);
                CREATE TABLE IF NOT EXISTS review_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL
                        REFERENCES review_tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    actor TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS review_task_events_task_created
                    ON review_task_events(task_id, created_at, id);
                CREATE TABLE IF NOT EXISTS qa_issues (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES review_sessions(id) ON DELETE CASCADE,
                    layer_id TEXT NOT NULL,
                    feature_id TEXT,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
                    message TEXT NOT NULL,
                    related_feature_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ('open','resolved','dismissed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    override_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS qa_issues_session_status
                    ON qa_issues(session_id, status, severity, created_at, id);
                CREATE INDEX IF NOT EXISTS qa_issues_session_severity
                    ON qa_issues(session_id, severity, created_at, id);
                CREATE INDEX IF NOT EXISTS qa_issues_session_rule
                    ON qa_issues(session_id, rule_id, created_at, id);
                CREATE INDEX IF NOT EXISTS qa_issues_session_layer
                    ON qa_issues(session_id, layer_id, created_at, id);
                """
            )
            # ``CREATE TABLE IF NOT EXISTS`` does not migrate an existing
            # registry.  A dataset is retained as a small tombstone when it
            # has historical runs, while this flag keeps it out of the active
            # workspace without breaking the runs.dataset_id foreign key.
            dataset_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(datasets)").fetchall()
            }
            if "registered" not in dataset_columns:
                connection.execute(
                    "ALTER TABLE datasets ADD COLUMN registered INTEGER NOT NULL DEFAULT 1"
                )
            run_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "dismissed" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0"
                )
            if "name" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN name TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_dismissed_created "
                "ON runs(dismissed, created_at)"
            )
            review_session_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(review_sessions)"
                ).fetchall()
            }
            if "qa_layer_revisions_json" not in review_session_columns:
                connection.execute(
                    "ALTER TABLE review_sessions ADD COLUMN qa_layer_revisions_json TEXT"
                )
            if "qa_ran_at" not in review_session_columns:
                connection.execute(
                    "ALTER TABLE review_sessions ADD COLUMN qa_ran_at TEXT"
                )
            review_task_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(review_tasks)"
                ).fetchall()
            }
            if "source_fingerprint" not in review_task_columns:
                connection.execute(
                    "ALTER TABLE review_tasks ADD COLUMN source_fingerprint TEXT"
                )
            if "priority_evidence_json" not in review_task_columns:
                connection.execute(
                    "ALTER TABLE review_tasks ADD COLUMN "
                    "priority_evidence_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "queue_priority" not in review_task_columns:
                connection.execute(
                    "ALTER TABLE review_tasks ADD COLUMN queue_priority REAL"
                )
            if "frame_start" not in review_task_columns:
                connection.execute(
                    "ALTER TABLE review_tasks ADD COLUMN frame_start INTEGER"
                )
            if "frame_end" not in review_task_columns:
                connection.execute(
                    "ALTER TABLE review_tasks ADD COLUMN frame_end INTEGER"
                )
            # Queue pagination must remain stable while task status and the
            # operator-editable priority change.  Existing registries inherit
            # the priority that was current at migration time; newly created
            # tasks persist their creation priority below.
            connection.execute(
                "UPDATE review_tasks SET queue_priority=priority "
                "WHERE queue_priority IS NULL"
            )
            queue_index = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='review_tasks_session_queue'"
            ).fetchone()
            if queue_index is None or "queue_priority" not in str(queue_index[0]):
                connection.execute("DROP INDEX IF EXISTS review_tasks_session_queue")
                connection.execute(
                    "CREATE INDEX review_tasks_session_queue "
                    "ON review_tasks(session_id,queue_priority DESC,created_at,id)"
                )
            # Earlier P1 builds kept an interval's ordinal range only inside
            # priority evidence.  Normalize it once so completed interval tasks
            # cover every frame they represented after an in-place upgrade.
            legacy_intervals = connection.execute(
                "SELECT id,priority_evidence_json FROM review_tasks "
                "WHERE task_type='UNREVIEWED_INTERVAL' "
                "AND (frame_start IS NULL OR frame_end IS NULL)"
            ).fetchall()
            for row in legacy_intervals:
                evidence = _loads(row["priority_evidence_json"], {})
                details = evidence.get("details") if isinstance(evidence, dict) else None
                if not isinstance(details, dict):
                    continue
                try:
                    frame_start = int(details["start_ordinal"])
                    frame_end = int(details["end_ordinal"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if frame_start < 0 or frame_end < frame_start:
                    continue
                connection.execute(
                    "UPDATE review_tasks SET frame_start=?,frame_end=? WHERE id=?",
                    (frame_start, frame_end, row["id"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS review_tasks_session_span "
                "ON review_tasks(session_id,track_id,frame_start,frame_end,status)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "review_tasks_session_source_fingerprint "
                "ON review_tasks(session_id,source_fingerprint) "
                "WHERE source_fingerprint IS NOT NULL"
            )

    @staticmethod
    def dataset_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["tracks"] = _loads(item.pop("tracks_json", None), [])
        item["bbox"] = _loads(item.pop("bbox_json", None), None)
        item["warnings"] = _loads(item.pop("warnings_json", None), [])
        return item

    def get_dataset(
        self,
        dataset_id: str,
        *,
        include_unregistered: bool = False,
    ) -> dict[str, Any] | None:
        where = "id = ?" if include_unregistered else "id = ? AND registered = 1"
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM datasets WHERE {where}", (dataset_id,)
            ).fetchone()
        return self.dataset_from_row(row)

    def find_dataset(
        self, root_id: str, relative_path: str, crs: str
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM datasets WHERE root_id=? AND relative_path=? AND crs=?",
                (root_id, relative_path, crs),
            ).fetchone()
        return self.dataset_from_row(row)

    def list_datasets(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM datasets
                WHERE registered = 1
                ORDER BY updated_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self.dataset_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def upsert_scanning_dataset(
        self,
        *,
        dataset_id: str,
        name: str,
        root_id: str,
        relative_path: str,
        crs: str,
        now: str,
    ) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO datasets(
                    id,name,root_id,relative_path,crs,status,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,'scanning',NULL,?,?)
                ON CONFLICT(root_id,relative_path,crs) DO UPDATE SET
                    name=excluded.name,status='scanning',error=NULL,registered=1,
                    frame_count=0,tracks_json='[]',bbox_json=NULL,warnings_json='[]',
                    catalog_status='missing',catalog_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (dataset_id, name, root_id, relative_path, crs, now, now),
            )

    def finish_dataset_scan(
        self,
        dataset_id: str,
        *,
        frames: list[dict[str, Any]],
        tracks: list[dict[str, Any]],
        bbox: list[float] | None,
        warnings: list[str],
        now: str,
    ) -> None:
        with self.connection(write=True) as connection:
            registered = connection.execute(
                "SELECT registered FROM datasets WHERE id=?", (dataset_id,)
            ).fetchone()
            if registered is None or not bool(registered["registered"]):
                return
            connection.execute("DELETE FROM frames WHERE dataset_id=?", (dataset_id,))
            connection.executemany(
                """
                INSERT INTO frames(
                    dataset_id,id,ordinal,track_id,task_json,
                    longitude,latitude,altitude,heading
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        dataset_id,
                        frame["id"],
                        frame["ordinal"],
                        frame["track_id"],
                        _json(frame["task"]),
                        frame.get("longitude"),
                        frame.get("latitude"),
                        frame.get("altitude"),
                        frame.get("heading"),
                    )
                    for frame in frames
                ],
            )
            connection.execute(
                """
                UPDATE datasets SET status='ready',error=NULL,frame_count=?,
                    tracks_json=?,bbox_json=?,warnings_json=?,updated_at=?
                WHERE id=?
                """,
                (
                    len(frames),
                    _json(tracks),
                    _json(bbox) if bbox is not None else None,
                    _json(warnings),
                    now,
                    dataset_id,
                ),
            )

    def fail_dataset_scan(self, dataset_id: str, error: str, now: str) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                UPDATE datasets SET status='error',error=?,updated_at=?
                WHERE id=? AND registered=1
                """,
                (error, now, dataset_id),
            )

    def set_catalog_status(
        self, dataset_id: str, status: str, *, error: str | None, now: str
    ) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                UPDATE datasets SET catalog_status=?,catalog_error=?,updated_at=?
                WHERE id=? AND registered=1
                """,
                (status, error, now, dataset_id),
            )

    def active_run_for_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT id,status FROM runs
                WHERE dataset_id=? AND status IN ({placeholders})
                ORDER BY created_at LIMIT 1
                """,
                (dataset_id, *ACTIVE_RUN_STATUSES),
            ).fetchone()
        return dict(row) if row is not None else None

    def unregister_dataset(self, dataset_id: str, *, now: str) -> dict[str, Any]:
        """Hide an indexed dataset without deleting its source delivery.

        Completed run rows keep their dataset foreign-key target.  Active or
        queued work blocks removal so a worker never loses its indexed frames
        midway through a job.
        """

        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            dataset = connection.execute(
                "SELECT id,registered FROM datasets WHERE id=?", (dataset_id,)
            ).fetchone()
            if dataset is None or not bool(dataset["registered"]):
                return {"status": "not_found"}
            active_run = connection.execute(
                f"""
                SELECT id,status FROM runs
                WHERE dataset_id=? AND status IN ({placeholders})
                ORDER BY created_at LIMIT 1
                """,
                (dataset_id, *ACTIVE_RUN_STATUSES),
            ).fetchone()
            if active_run is not None:
                return {
                    "status": "active_run",
                    "run_id": str(active_run["id"]),
                    "run_status": str(active_run["status"]),
                }
            session_counts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM review_sessions
                WHERE dataset_id=? AND (
                    status IN ('active','paused')
                    OR (
                        status='draft' AND (
                            source_run_ids_json!='[]'
                            OR target_layer_ids_json!='[]'
                            OR track_ids_json!='[]'
                            OR frame_start IS NOT NULL
                            OR frame_end IS NOT NULL
                            OR class_filters_json!='[]'
                            OR qa_ran_at IS NOT NULL
                            OR qa_layer_revisions_json IS NOT NULL
                            OR EXISTS(
                                SELECT 1 FROM review_tasks task
                                WHERE task.session_id=review_sessions.id
                            )
                            OR EXISTS(
                                SELECT 1 FROM qa_issues issue
                                WHERE issue.session_id=review_sessions.id
                            )
                        )
                    )
                )
                """,
                (dataset_id,),
            ).fetchone()
            open_task_counts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM review_tasks
                WHERE dataset_id=? AND status IN ('todo','in_progress')
                """,
                (dataset_id,),
            ).fetchone()
            open_sessions = int(session_counts["count"])
            open_tasks = int(open_task_counts["count"])
            if open_sessions or open_tasks:
                return {
                    "status": "review_work",
                    "open_sessions": open_sessions,
                    "open_tasks": open_tasks,
                }
            connection.execute(
                """
                DELETE FROM review_sessions
                WHERE dataset_id=? AND status='draft'
                  AND source_run_ids_json='[]' AND target_layer_ids_json='[]'
                  AND track_ids_json='[]'
                  AND frame_start IS NULL AND frame_end IS NULL
                  AND class_filters_json='[]'
                  AND qa_ran_at IS NULL AND qa_layer_revisions_json IS NULL
                  AND NOT EXISTS(
                      SELECT 1 FROM review_tasks task
                      WHERE task.session_id=review_sessions.id
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM qa_issues issue
                      WHERE issue.session_id=review_sessions.id
                  )
                """,
                (dataset_id,),
            )
            connection.execute("DELETE FROM frames WHERE dataset_id=?", (dataset_id,))
            connection.execute(
                """
                UPDATE datasets SET registered=0,status='removed',error=NULL,
                    frame_count=0,tracks_json='[]',bbox_json=NULL,warnings_json='[]',
                    catalog_status='missing',catalog_error=NULL,updated_at=?
                WHERE id=?
                """,
                (now, dataset_id),
            )
            return {"status": "unregistered"}

    @staticmethod
    def frame_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["task"] = _loads(item.pop("task_json", None), {})
        return item

    def get_frame(self, dataset_id: str, frame_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM frames WHERE dataset_id=? AND id=?",
                (dataset_id, frame_id),
            ).fetchone()
        return self.frame_from_row(row)

    def locate_frame(
        self,
        dataset_id: str,
        *,
        image_name: str | None = None,
        dataset_position: tuple[float, float] | None = None,
    ) -> dict[str, Any] | None:
        """Find the source frame for an imported detection or nearby SHP point."""

        normalized_image_name = str(image_name or "").strip().casefold()
        with self.connection() as connection:
            row = None
            if normalized_image_name:
                row = connection.execute(
                    """
                    SELECT * FROM frames
                    WHERE dataset_id=?
                      AND lower(json_extract(task_json, '$.image_name'))=?
                    ORDER BY ordinal
                    LIMIT 1
                    """,
                    (dataset_id, normalized_image_name),
                ).fetchone()
            if row is None and dataset_position is not None:
                x, y = dataset_position
                row = connection.execute(
                    """
                    SELECT * FROM frames
                    WHERE dataset_id=?
                      AND json_type(task_json, '$.origin[0]') IN ('integer', 'real')
                      AND json_type(task_json, '$.origin[1]') IN ('integer', 'real')
                    ORDER BY
                      ((CAST(json_extract(task_json, '$.origin[0]') AS REAL)-?) *
                       (CAST(json_extract(task_json, '$.origin[0]') AS REAL)-?)) +
                      ((CAST(json_extract(task_json, '$.origin[1]') AS REAL)-?) *
                       (CAST(json_extract(task_json, '$.origin[1]') AS REAL)-?)),
                      ordinal
                    LIMIT 1
                    """,
                    (dataset_id, x, x, y, y),
                ).fetchone()
        return self.frame_from_row(row)

    def frame_offset_in_track(
        self,
        dataset_id: str,
        *,
        track_id: str,
        ordinal: int,
    ) -> int:
        """Return the zero-based position used by a track-filtered frame page."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM frames
                WHERE dataset_id=? AND track_id=? AND ordinal<?
                """,
                (dataset_id, track_id, int(ordinal)),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_frames(
        self,
        dataset_id: str,
        *,
        offset: int = 0,
        limit: int = 200,
        track_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = "dataset_id=?"
        params: list[Any] = [dataset_id]
        if track_id:
            where += " AND track_id=?"
            params.append(track_id)
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM frames WHERE {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM frames WHERE {where} ORDER BY ordinal LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
        return [
            self.frame_from_row(row)
            for row in rows
            if row is not None  # type: ignore[misc]
        ], total

    def all_frames(self, dataset_id: str) -> list[dict[str, Any]]:
        return self.list_frames(dataset_id, offset=0, limit=2_000_000)[0]

    def sample_route_frames(
        self,
        dataset_id: str,
        *,
        track_ids: Iterable[str],
        max_points: int,
    ) -> list[dict[str, Any]]:
        """Stream an evenly distributed, memory-bounded route sample.

        Route rendering needs only seven scalar columns.  In particular, it
        must not materialize every frame's potentially large ``task_json``.
        The first count query lets us precompute exact sample ordinals while
        the indexed cursor streams all coordinate rows with O(max_points)
        retained memory.
        """

        ordered_track_ids = list(dict.fromkeys(str(value) for value in track_ids))
        if not ordered_track_ids or max_points <= 0:
            return []
        with self.connection() as connection:
            # Keep the count and streaming sample on one WAL read snapshot if
            # an operator starts a rescan concurrently with this request.
            connection.execute("BEGIN")
            count_rows = connection.execute(
                """
                SELECT track_id, COUNT(*) AS frame_count
                FROM frames
                WHERE dataset_id=?
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL
                GROUP BY track_id
                """,
                (dataset_id,),
            ).fetchall()
            counts = {
                str(row["track_id"]): int(row["frame_count"])
                for row in count_rows
                if int(row["frame_count"]) > 0
            }
            active = [track_id for track_id in ordered_track_ids if track_id in counts]
            # At least one point per retained track.  A pathological delivery
            # with more tracks than the response budget remains strictly
            # bounded and deterministic.
            active = active[: int(max_points)]
            if not active:
                return []

            quotas = {track_id: 0 for track_id in active}
            remaining = int(max_points)
            while remaining:
                progressed = False
                for track_id in active:
                    if quotas[track_id] >= counts[track_id]:
                        continue
                    quotas[track_id] += 1
                    remaining -= 1
                    progressed = True
                    if remaining == 0:
                        break
                if not progressed:
                    break

            wanted: dict[str, set[int]] = {}
            for track_id in active:
                count = counts[track_id]
                quota = quotas[track_id]
                if quota >= count:
                    wanted[track_id] = set(range(count))
                elif quota == 1:
                    wanted[track_id] = {0}
                else:
                    wanted[track_id] = {
                        round(index * (count - 1) / (quota - 1))
                        for index in range(quota)
                    }

            grouped: dict[str, list[dict[str, Any]]] = {
                track_id: [] for track_id in active
            }
            seen = {track_id: 0 for track_id in active}
            cursor = connection.execute(
                """
                SELECT id, ordinal, track_id, longitude, latitude, altitude, heading
                FROM frames
                WHERE dataset_id=?
                  AND longitude IS NOT NULL
                  AND latitude IS NOT NULL
                ORDER BY track_id, ordinal
                """,
                (dataset_id,),
            )
            for row in cursor:
                track_id = str(row["track_id"])
                if track_id not in wanted:
                    continue
                index = seen[track_id]
                seen[track_id] = index + 1
                if index in wanted[track_id]:
                    grouped[track_id].append(dict(row))
        return [item for track_id in active for item in grouped[track_id]]

    def create_upload(
        self,
        upload: dict[str, Any],
        files: Iterable[dict[str, Any]],
    ) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO uploads(
                    id,name,safe_name,status,root_id,total_size,created_at,updated_at
                ) VALUES(?,?,?,'uploading',?,?,?,?)
                """,
                (
                    upload["id"],
                    upload["name"],
                    upload["safe_name"],
                    upload["root_id"],
                    upload["total_size"],
                    upload["created_at"],
                    upload["updated_at"],
                ),
            )
            connection.executemany(
                """
                INSERT INTO upload_files(
                    upload_id,id,relative_path,size,offset,last_modified
                ) VALUES(?,?,?,?,0,?)
                """,
                [
                    (
                        upload["id"],
                        item["id"],
                        item["relative_path"],
                        item["size"],
                        item.get("last_modified"),
                    )
                    for item in files
                ],
            )

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE id=?", (upload_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_upload_files(self, upload_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM upload_files WHERE upload_id=? ORDER BY relative_path",
                (upload_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_upload_file(self, upload_id: str, file_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM upload_files WHERE upload_id=? AND id=?",
                (upload_id, file_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_upload_offset(
        self,
        upload_id: str,
        file_id: str,
        expected_offset: int,
        new_offset: int,
        now: str,
    ) -> bool:
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE upload_files SET offset=? WHERE upload_id=? AND id=? AND offset=?
                """,
                (new_offset, upload_id, file_id, expected_offset),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE uploads SET updated_at=? WHERE id=?", (now, upload_id)
                )
            return bool(cursor.rowcount)

    def complete_upload(
        self, upload_id: str, destination_relative_path: str, now: str
    ) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                UPDATE uploads SET status='complete',destination_relative_path=?,
                    error=NULL,updated_at=? WHERE id=?
                """,
                (destination_relative_path, now, upload_id),
            )

    @staticmethod
    def run_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["request"] = _loads(item.pop("request_json", None), {})
        item["resolved"] = _loads(item.pop("resolved_json", None), {})
        item["cancel_requested"] = bool(item["cancel_requested"])
        item["dismissed"] = bool(item.get("dismissed", 0))
        return item

    def create_run(self, run: dict[str, Any]) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id,dataset_id,name,status,request_json,resolved_json,work_relative,
                    created_at,updated_at
                ) VALUES(?,?,?,'queued',?,?,?,?,?)
                """,
                (
                    run["id"],
                    run["dataset_id"],
                    run.get("name"),
                    _json(run["request"]),
                    _json(run["resolved"]),
                    run["work_relative"],
                    run["created_at"],
                    run["updated_at"],
                ),
            )

    def rename_completed_run(
        self,
        run_id: str,
        name: str,
        now: str,
        *,
        expected_updated_at: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Rename one completed result with an optional optimistic-lock token."""

        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return "missing", None
            if str(row["status"]) != "completed":
                return "not_completed", self.run_from_row(row)
            if (
                expected_updated_at is not None
                and str(row["updated_at"]) != expected_updated_at
            ):
                return "stale", self.run_from_row(row)
            connection.execute(
                "UPDATE runs SET name=?,updated_at=? WHERE id=?",
                (name, now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            return "updated", self.run_from_row(updated)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return self.run_from_row(row)

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE dismissed=0 ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def list_completed_runs_for_dataset(
        self,
        dataset_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        snapshot_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return most recently finished durable results, including hidden runs."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE dataset_id=? AND status='completed'
                  AND (? IS NULL OR julianday(COALESCE(finished_at, updated_at, created_at))
                      <= julianday(?))
                ORDER BY
                    COALESCE(finished_at, updated_at, created_at) DESC,
                    created_at DESC,
                    id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    dataset_id,
                    snapshot_at,
                    snapshot_at,
                    int(limit),
                    int(offset),
                ),
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def count_completed_runs_for_dataset(
        self,
        dataset_id: str,
        *,
        snapshot_at: str | None = None,
    ) -> int:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM runs
                WHERE dataset_id=? AND status='completed'
                  AND (? IS NULL OR julianday(COALESCE(finished_at, updated_at, created_at))
                      <= julianday(?))
                """,
                (dataset_id, snapshot_at, snapshot_at),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    @staticmethod
    def survey_segment_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["coordinates"] = _loads(item.pop("coordinates_json", None), [])
        return item

    def list_survey_segments(self, dataset_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM survey_segments
                WHERE dataset_id=?
                ORDER BY created_at ASC, id ASC
                """,
                (dataset_id,),
            ).fetchall()
        return [self.survey_segment_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def create_survey_segment(self, segment: dict[str, Any]) -> dict[str, Any]:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO survey_segments(
                    id,dataset_id,name,color,coordinates_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    segment["id"],
                    segment["dataset_id"],
                    segment["name"],
                    segment["color"],
                    _json(segment["coordinates"]),
                    segment["created_at"],
                    segment["updated_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM survey_segments WHERE id=? AND dataset_id=?",
                (segment["id"], segment["dataset_id"]),
            ).fetchone()
        result = self.survey_segment_from_row(row)
        if result is None:  # pragma: no cover - SQLite INSERT guarantees the row
            raise RuntimeError("Survey segment was not persisted.")
        return result

    def delete_survey_segment(self, dataset_id: str, segment_id: str) -> bool:
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM survey_segments WHERE dataset_id=? AND id=?",
                (dataset_id, segment_id),
            )
        return bool(cursor.rowcount)

    @staticmethod
    def review_session_from_row(
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["source_run_ids"] = _loads(item.pop("source_run_ids_json", None), [])
        item["target_layer_ids"] = _loads(item.pop("target_layer_ids_json", None), [])
        item["track_ids"] = _loads(item.pop("track_ids_json", None), [])
        item["class_filters"] = _loads(item.pop("class_filters_json", None), [])
        item["qa_layer_revisions"] = _loads(
            item.pop("qa_layer_revisions_json", None), None
        )
        frame_start = item.pop("frame_start", None)
        frame_end = item.pop("frame_end", None)
        item["frame_range"] = (
            None
            if frame_start is None or frame_end is None
            else [int(frame_start), int(frame_end)]
        )
        return item

    def create_review_session(self, session: dict[str, Any]) -> dict[str, Any]:
        frame_range = session.get("frame_range")
        with self.connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO review_sessions(
                    id,dataset_id,source_run_ids_json,target_layer_ids_json,
                    track_ids_json,frame_start,frame_end,class_filters_json,status,
                    created_by,created_at,updated_at,last_task_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session["id"],
                    session["dataset_id"],
                    _json(session.get("source_run_ids", [])),
                    _json(session.get("target_layer_ids", [])),
                    _json(session.get("track_ids", [])),
                    None if frame_range is None else int(frame_range[0]),
                    None if frame_range is None else int(frame_range[1]),
                    _json(session.get("class_filters", [])),
                    session["status"],
                    session["created_by"],
                    session["created_at"],
                    session["updated_at"],
                    session.get("last_task_id"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session["id"],)
            ).fetchone()
        result = self.review_session_from_row(row)
        if result is None:  # pragma: no cover - SQLite INSERT guarantees the row
            raise RuntimeError("Review session was not persisted.")
        return result

    def get_review_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return self.review_session_from_row(row)

    def review_session_effective_target_layer_ids(
        self,
        session_id: str,
    ) -> list[str]:
        """Return stable QA/completion target layers for explicit or wildcard scope."""

        with self.connection() as connection:
            session = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                return []
            layer_ids = _effective_review_target_layer_ids(connection, session)
        return sorted(layer_ids)

    def list_review_sessions(
        self,
        dataset_id: str,
        *,
        offset: int,
        limit: int,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = "dataset_id=?"
        parameters: list[Any] = [dataset_id]
        if status is not None:
            where += " AND status=?"
            parameters.append(status)
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM review_sessions WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM review_sessions WHERE {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (*parameters, int(limit), int(offset)),
            ).fetchall()
        return [
            self.review_session_from_row(row) for row in rows if row is not None
        ], total  # type: ignore[misc]

    def update_review_session(
        self,
        session_id: str,
        *,
        expected_status: str,
        now: str,
        fields: dict[str, Any],
        current_layer_revisions: Mapping[str, int | None] | None = None,
        pending_resolution_blockers: int = 0,
    ) -> tuple[str, dict[str, Any] | None]:
        selected: dict[str, Any] = {}
        for public_name, column_name in (
            ("source_run_ids", "source_run_ids_json"),
            ("target_layer_ids", "target_layer_ids_json"),
            ("track_ids", "track_ids_json"),
            ("class_filters", "class_filters_json"),
        ):
            if public_name in fields:
                selected[column_name] = _json(fields[public_name])
        if "frame_range" in fields:
            frame_range = fields["frame_range"]
            selected["frame_start"] = (
                None if frame_range is None else int(frame_range[0])
            )
            selected["frame_end"] = None if frame_range is None else int(frame_range[1])
        for name in ("status", "last_task_id"):
            if name in fields:
                selected[name] = fields[name]
        selected["updated_at"] = now
        assignments = ", ".join(f"{column}=?" for column in selected)
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if current is None:
                return "missing", None
            if str(current["status"]) != expected_status:
                return "stale", self.review_session_from_row(current)
            scope_columns = {
                "source_run_ids_json",
                "target_layer_ids_json",
                "track_ids_json",
                "frame_start",
                "frame_end",
                "class_filters_json",
            }
            if scope_columns.intersection(selected):
                task_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM review_tasks WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                )
                if str(current["status"]) != "draft" or task_count:
                    return "scope_locked", self.review_session_from_row(current)
            if selected.get("status") == "completed":
                open_tasks = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM review_tasks
                        WHERE session_id=? AND status IN ('todo','in_progress')
                        """,
                        (session_id,),
                    ).fetchone()[0]
                )
                open_errors = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM qa_issues
                        WHERE session_id=? AND status='open' AND severity='error'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                )
                qa_blockers = _qa_snapshot_blockers(current, current_layer_revisions)
                if (
                    open_tasks
                    or open_errors
                    or pending_resolution_blockers
                    or any(qa_blockers.values())
                ):
                    return "blocked", self.review_session_from_row(current)
            connection.execute(
                f"UPDATE review_sessions SET {assignments} WHERE id=? AND status=?",
                (*selected.values(), session_id, expected_status),
            )
            updated = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return "updated", self.review_session_from_row(updated)

    @staticmethod
    def review_task_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        # ``queue_priority`` remains available to internal keyset pagination;
        # the API adapter strips it from the public ReviewTask contract.
        item["reason_codes"] = _loads(item.pop("reason_codes_json", None), [])
        item["location_hint"] = _loads(item.pop("location_hint_json", None), None)
        item["priority_evidence"] = _loads(item.pop("priority_evidence_json", None), {})
        item["resolved_feature_ids"] = _loads(
            item.pop("resolved_feature_ids_json", None), []
        )
        return item

    def _create_review_tasks(
        self,
        tasks: list[dict[str, Any]],
        *,
        ignore_existing: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        if not tasks:
            return [], 0
        persisted: list[dict[str, Any]] = []
        created_count = 0
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for task in tasks:
                verb = "INSERT OR IGNORE" if ignore_existing else "INSERT"
                cursor = connection.execute(
                    f"""
                    {verb} INTO review_tasks(
                        id,session_id,dataset_id,task_type,status,priority,
                        queue_priority,frame_id,
                        track_id,frame_start,frame_end,source_run_id,
                        source_detection_id,target_layer_id,
                        class_hint,reason_codes_json,location_hint_json,
                        source_fingerprint,priority_evidence_json,claimed_by,
                        resolved_feature_ids_json,resolution,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task["id"],
                        task["session_id"],
                        task["dataset_id"],
                        task["task_type"],
                        task["status"],
                        float(task["priority"]),
                        float(task["priority"]),
                        task.get("frame_id"),
                        task.get("track_id"),
                        task.get("frame_start"),
                        task.get("frame_end"),
                        task.get("source_run_id"),
                        task.get("source_detection_id"),
                        task.get("target_layer_id"),
                        task.get("class_hint"),
                        _json(task.get("reason_codes", [])),
                        _json(task.get("location_hint"))
                        if task.get("location_hint") is not None
                        else None,
                        task.get("source_fingerprint"),
                        _json(task.get("priority_evidence", {})),
                        task.get("claimed_by"),
                        _json(task.get("resolved_feature_ids", [])),
                        task.get("resolution"),
                        task["created_at"],
                        task["updated_at"],
                    ),
                )
                if cursor.rowcount:
                    created_count += 1
                    connection.execute(
                        """
                        INSERT INTO review_task_events(
                            task_id,event_type,from_status,to_status,actor,payload_json,
                            created_at
                        ) VALUES(?,'created',NULL,?,?,?,?)
                        """,
                        (
                            task["id"],
                            task["status"],
                            task.get("claimed_by"),
                            _json({}),
                            task["created_at"],
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM review_tasks WHERE id=?", (task["id"],)
                    ).fetchone()
                else:
                    fingerprint = task.get("source_fingerprint")
                    if fingerprint is None:
                        raise sqlite3.IntegrityError(
                            "A non-fingerprinted review task already exists."
                        )
                    row = connection.execute(
                        """
                        SELECT * FROM review_tasks
                        WHERE session_id=? AND source_fingerprint=?
                        """,
                        (task["session_id"], fingerprint),
                    ).fetchone()
                    if row is None:
                        raise sqlite3.IntegrityError(
                            "A review task identity collision could not be reconciled."
                        )
                item = self.review_task_from_row(row)
                if item is None:  # pragma: no cover - INSERT guarantees the row
                    raise RuntimeError("Review task was not persisted.")
                persisted.append(item)
        return persisted, created_count

    def create_review_tasks(
        self,
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self._create_review_tasks(tasks, ignore_existing=False)[0]

    def create_review_tasks_idempotent(
        self,
        tasks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Create generated tasks once per session/source fingerprint."""

        return self._create_review_tasks(tasks, ignore_existing=True)

    def create_review_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return self.create_review_tasks([task])[0]

    def get_review_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self.review_task_from_row(row)

    def list_review_tasks(
        self,
        session_id: str,
        *,
        offset: int,
        limit: int,
        status: str | None = None,
        task_type: str | None = None,
        after: tuple[float, str, str] | None = None,
        include_queue_priority: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        where = "session_id=?"
        parameters: list[Any] = [session_id]
        if status is not None:
            where += " AND status=?"
            parameters.append(status)
        if task_type is not None:
            where += " AND task_type=?"
            parameters.append(task_type)
        count_where = where
        count_parameters = tuple(parameters)
        if after is not None:
            queue_priority, created_at, task_id = after
            where += (
                " AND (queue_priority<? OR (queue_priority=? AND "
                "(created_at>? OR (created_at=? AND id>?))))"
            )
            parameters.extend(
                [queue_priority, queue_priority, created_at, created_at, task_id]
            )
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM review_tasks WHERE {count_where}",
                    count_parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM review_tasks WHERE {where}
                ORDER BY
                    queue_priority DESC, created_at, id
                LIMIT ? OFFSET ?
                """,
                (*parameters, int(limit), int(offset)),
            ).fetchall()
        items = [
            self.review_task_from_row(row) for row in rows if row is not None
        ]
        if not include_queue_priority:
            for item in items:
                if item is not None:
                    item.pop("queue_priority", None)
        return items, total  # type: ignore[return-value]

    def review_task_status_counts(self, session_id: str) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT status,COUNT(*) AS count FROM review_tasks
                WHERE session_id=? GROUP BY status ORDER BY status
                """,
                (session_id,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def update_review_task(
        self,
        task_id: str,
        *,
        expected_status: str,
        now: str,
        fields: dict[str, Any],
        event_type: str,
        actor: str | None,
        event_payload: dict[str, Any] | None = None,
        set_session_last_task: bool = False,
    ) -> tuple[str, dict[str, Any] | None]:
        selected: dict[str, Any] = {}
        for name in ("status", "priority", "claimed_by", "resolution"):
            if name in fields:
                selected[name] = fields[name]
        if "resolved_feature_ids" in fields:
            selected["resolved_feature_ids_json"] = _json(
                fields["resolved_feature_ids"]
            )
        selected["updated_at"] = now
        assignments = ", ".join(f"{column}=?" for column in selected)
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM review_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if current is None:
                return "missing", None
            if str(current["status"]) != expected_status:
                return "stale", self.review_task_from_row(current)
            parent = connection.execute(
                "SELECT status FROM review_sessions WHERE id=?",
                (current["session_id"],),
            ).fetchone()
            if parent is None:
                return "missing", None
            parent_status = str(parent["status"])
            if parent_status in {"completed", "archived"}:
                return "immutable", self.review_task_from_row(current)
            if parent_status != "active":
                return "inactive", self.review_task_from_row(current)
            cursor = connection.execute(
                f"UPDATE review_tasks SET {assignments} WHERE id=? AND status=?",
                (*selected.values(), task_id, expected_status),
            )
            if not cursor.rowcount:
                latest = connection.execute(
                    "SELECT * FROM review_tasks WHERE id=?", (task_id,)
                ).fetchone()
                return "stale", self.review_task_from_row(latest)
            to_status = str(fields.get("status", expected_status))
            connection.execute(
                """
                INSERT INTO review_task_events(
                    task_id,event_type,from_status,to_status,actor,payload_json,
                    created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    event_type,
                    expected_status,
                    to_status,
                    actor,
                    _json(event_payload or {}),
                    now,
                ),
            )
            if set_session_last_task:
                connection.execute(
                    """
                    UPDATE review_sessions SET last_task_id=?,updated_at=?
                    WHERE id=(SELECT session_id FROM review_tasks WHERE id=?)
                    """,
                    (task_id, now, task_id),
                )
            updated = connection.execute(
                "SELECT * FROM review_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return "updated", self.review_task_from_row(updated)

    def resolve_review_task(
        self,
        task_id: str,
        *,
        resolution: str,
        resolved_feature_ids: list[str],
        now: str,
        actor: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically resolve an in-progress task and append its audit event."""

        return self.update_review_task(
            task_id,
            expected_status="in_progress",
            now=now,
            fields={
                "status": resolution,
                "resolution": resolution,
                "resolved_feature_ids": resolved_feature_ids,
            },
            event_type="resolved",
            actor=actor,
            event_payload={"resolved_feature_ids": resolved_feature_ids},
        )

    @staticmethod
    def review_qa_issue_from_row(
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["related_feature_ids"] = _loads(
            item.pop("related_feature_ids_json", None), []
        )
        return item

    def replace_review_qa_issues(
        self,
        session_id: str,
        issues: list[dict[str, Any]],
        *,
        layer_revisions: Mapping[str, int] | None = None,
        ran_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Replace one session's derived QA snapshot in a single transaction."""

        persisted: list[dict[str, Any]] = []
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError("Review session does not exist.")
            if str(session["status"]) in {"completed", "archived"}:
                raise ReviewSessionReadOnlyError(
                    "Completed or archived review sessions are read-only."
                )
            if (layer_revisions is None) != (ran_at is None):
                raise ValueError("QA layer revisions and ran_at must be stored together.")
            normalized_revisions: dict[str, int] | None = None
            if layer_revisions is not None:
                target_layer_ids = _effective_review_target_layer_ids(
                    connection, session
                )
                normalized_revisions = {}
                for raw_layer_id, raw_revision in layer_revisions.items():
                    layer_id = str(raw_layer_id)
                    if (
                        isinstance(raw_revision, bool)
                        or not isinstance(raw_revision, int)
                        or raw_revision < 1
                    ):
                        raise ValueError("QA layer revisions must be positive integers.")
                    normalized_revisions[layer_id] = raw_revision
                if set(normalized_revisions) != target_layer_ids:
                    raise ValueError(
                        "QA snapshot must cover every review target layer exactly once."
                    )
            previous_by_id = {
                str(row["id"]): row
                for row in connection.execute(
                    """
                    SELECT id,status,created_at,updated_at,override_reason
                    FROM qa_issues WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchall()
            }
            connection.execute(
                "DELETE FROM qa_issues WHERE session_id=?", (session_id,)
            )
            for issue in issues:
                if str(issue["session_id"]) != session_id:
                    raise ValueError(
                        "QA issue session_id does not match the replacement scope."
                    )
                previous = previous_by_id.get(str(issue["id"]))
                is_error = str(issue["severity"]) == "error"
                issue_status = (
                    "open"
                    if is_error
                    else issue["status"]
                    if previous is None
                    else previous["status"]
                )
                created_at = (
                    issue["created_at"] if previous is None else previous["created_at"]
                )
                updated_at = (
                    issue["updated_at"]
                    if is_error or previous is None
                    else previous["updated_at"]
                )
                override_reason = (
                    None
                    if is_error
                    else issue.get("override_reason")
                    if previous is None
                    else previous["override_reason"]
                )
                connection.execute(
                    """
                    INSERT INTO qa_issues(
                        id,session_id,layer_id,feature_id,rule_id,severity,message,
                        related_feature_ids_json,status,created_at,updated_at,
                        override_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        issue["id"],
                        session_id,
                        issue["layer_id"],
                        issue.get("feature_id"),
                        issue["rule_id"],
                        issue["severity"],
                        issue["message"],
                        _json(issue.get("related_feature_ids", [])),
                        issue_status,
                        created_at,
                        updated_at,
                        override_reason,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM qa_issues WHERE id=?", (issue["id"],)
                ).fetchone()
                item = self.review_qa_issue_from_row(row)
                if item is None:  # pragma: no cover - INSERT guarantees the row
                    raise RuntimeError("Review QA issue was not persisted.")
                persisted.append(item)
            if normalized_revisions is not None and ran_at is not None:
                connection.execute(
                    """
                    UPDATE review_sessions
                    SET qa_layer_revisions_json=?,qa_ran_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (_json(normalized_revisions), ran_at, ran_at, session_id),
                )
        return persisted

    def list_review_qa_issues(
        self,
        session_id: str,
        *,
        offset: int,
        limit: int,
        status: str | None = None,
        severity: str | None = None,
        rule_id: str | None = None,
        layer_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = "session_id=?"
        parameters: list[Any] = [session_id]
        for column, value in (
            ("status", status),
            ("severity", severity),
            ("rule_id", rule_id),
            ("layer_id", layer_id),
        ):
            if value is not None:
                where += f" AND {column}=?"
                parameters.append(value)
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM qa_issues WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM qa_issues WHERE {where}
                ORDER BY
                    CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    created_at, id
                LIMIT ? OFFSET ?
                """,
                (*parameters, int(limit), int(offset)),
            ).fetchall()
        return [
            self.review_qa_issue_from_row(row) for row in rows if row is not None
        ], total  # type: ignore[misc]

    def update_review_qa_issue(
        self,
        issue_id: str,
        status: str,
        override_reason: str | None,
        now: str,
    ) -> tuple[str, dict[str, Any] | None]:
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM qa_issues WHERE id=?", (issue_id,)
            ).fetchone()
            if current is None:
                return "missing", None
            parent = connection.execute(
                "SELECT status FROM review_sessions WHERE id=?",
                (current["session_id"],),
            ).fetchone()
            if parent is None:
                return "missing", None
            if str(parent["status"]) in {"completed", "archived"}:
                return "session_immutable", self.review_qa_issue_from_row(current)
            if str(current["severity"]) == "error" and status != "open":
                return "error_immutable", self.review_qa_issue_from_row(current)
            connection.execute(
                """
                UPDATE qa_issues SET status=?,override_reason=?,updated_at=?
                WHERE id=?
                """,
                (status, override_reason, now, issue_id),
            )
            updated = connection.execute(
                "SELECT * FROM qa_issues WHERE id=?", (issue_id,)
            ).fetchone()
        return "updated", self.review_qa_issue_from_row(updated)

    def frame_ordinal_bounds(self, dataset_id: str) -> tuple[int, int] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(ordinal) AS minimum, MAX(ordinal) AS maximum
                FROM frames WHERE dataset_id=?
                """,
                (dataset_id,),
            ).fetchone()
        if row is None or row["minimum"] is None or row["maximum"] is None:
            return None
        return int(row["minimum"]), int(row["maximum"])

    def reviewed_frame_ids(self, session_id: str) -> set[str]:
        """Return frames with a terminal task outcome in one review session."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT frame.id FROM frames frame
                JOIN review_sessions session ON session.id=?
                WHERE frame.dataset_id=session.dataset_id
                  AND EXISTS(
                      SELECT 1 FROM review_tasks terminal
                      WHERE terminal.session_id=session.id
                        AND terminal.status IN (
                          'confirmed','corrected','manual_added','false_positive',
                          'skipped','field_survey'
                        )
                        AND (
                          terminal.frame_id=frame.id
                          OR (
                            terminal.task_type='UNREVIEWED_INTERVAL'
                            AND terminal.track_id=frame.track_id
                            AND terminal.frame_start IS NOT NULL
                            AND terminal.frame_end IS NOT NULL
                            AND frame.ordinal BETWEEN
                              terminal.frame_start AND terminal.frame_end
                          )
                        )
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM review_tasks open_task
                      WHERE open_task.session_id=session.id
                        AND open_task.status IN ('todo','in_progress')
                        AND (
                          open_task.frame_id=frame.id
                          OR (
                            open_task.task_type='UNREVIEWED_INTERVAL'
                            AND open_task.track_id=frame.track_id
                            AND open_task.frame_start IS NOT NULL
                            AND open_task.frame_end IS NOT NULL
                            AND frame.ordinal BETWEEN
                              open_task.frame_start AND open_task.frame_end
                          )
                        )
                  )
                """,
                (session_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def review_report_aggregates(self, session_id: str) -> dict[str, Any]:
        """Return bounded task/QA/operator aggregates for one report."""

        terminal = (
            "confirmed",
            "corrected",
            "manual_added",
            "false_positive",
            "skipped",
            "field_survey",
        )
        placeholders = ",".join("?" for _ in terminal)
        with self.connection() as connection:
            task_status_rows = connection.execute(
                """
                SELECT status,COUNT(*) AS count FROM review_tasks
                WHERE session_id=? GROUP BY status ORDER BY status
                """,
                (session_id,),
            ).fetchall()
            task_type_rows = connection.execute(
                """
                SELECT task_type,COUNT(*) AS count FROM review_tasks
                WHERE session_id=? GROUP BY task_type ORDER BY task_type
                """,
                (session_id,),
            ).fetchall()
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END)
                           AS completed,
                       COUNT(DISTINCT CASE WHEN status IN ({placeholders})
                           THEN frame_id END) AS reviewed_frames,
                       SUM(CASE WHEN task_type IN (
                           'GEOMETRY_REVIEW','POLE_BASE_REVIEW'
                       ) AND status IN ('todo','in_progress') THEN 1 ELSE 0 END)
                           AS unresolved_review
                FROM review_tasks WHERE session_id=?
                """,
                (*terminal, *terminal, session_id),
            ).fetchone()
            qa_row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open
                FROM qa_issues WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            timing_row = connection.execute(
                f"""
                SELECT COALESCE(SUM(
                    MAX(0.0, (julianday(e.created_at) - julianday((
                        SELECT MAX(started.created_at)
                        FROM review_task_events started
                        WHERE started.task_id=e.task_id
                          AND started.to_status='in_progress'
                          AND started.id < e.id
                    ))) * 86400.0)
                ), 0.0) AS operator_seconds,
                MIN(e.created_at) AS first_event_at,
                MAX(e.created_at) AS last_event_at
                FROM review_task_events e
                JOIN review_tasks task ON task.id=e.task_id
                WHERE task.session_id=? AND e.to_status IN ({placeholders})
                """,
                (session_id, *terminal),
            ).fetchone()
            actor_rows = connection.execute(
                """
                SELECT DISTINCT actor FROM review_task_events e
                JOIN review_tasks task ON task.id=e.task_id
                WHERE task.session_id=? AND actor IS NOT NULL AND trim(actor)<>''
                ORDER BY actor LIMIT 1000
                """,
                (session_id,),
            ).fetchall()
        return {
            "task_status_counts": {
                str(row["status"]): int(row["count"]) for row in task_status_rows
            },
            "task_source_counts": {
                str(row["task_type"]): int(row["count"]) for row in task_type_rows
            },
            "total_tasks": int(totals["total"] or 0),
            "completed_tasks": int(totals["completed"] or 0),
            "reviewed_frames": int(totals["reviewed_frames"] or 0),
            "unresolved_review": int(totals["unresolved_review"] or 0),
            "qa_issues": int(qa_row["total"] or 0),
            "open_qa_issues": int(qa_row["open"] or 0),
            "operator_seconds": round(float(timing_row["operator_seconds"] or 0.0), 3),
            "first_operator_event_at": timing_row["first_event_at"],
            "last_operator_event_at": timing_row["last_event_at"],
            "operators": [str(row["actor"]) for row in actor_rows],
        }

    def review_session_completion_blockers(
        self,
        session_id: str,
        *,
        current_layer_revisions: Mapping[str, int | None] | None = None,
    ) -> dict[str, int]:
        """Return the shared API/report completion gate counters."""

        with self.connection() as connection:
            session = connection.execute(
                "SELECT * FROM review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            task_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM review_tasks
                    WHERE session_id=? AND status IN ('todo','in_progress')
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            error_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM qa_issues
                    WHERE session_id=? AND status='open' AND severity='error'
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
        qa_blockers = (
            {"qa_not_run": 1, "stale_qa_target_layers": 0}
            if session is None
            else _qa_snapshot_blockers(session, current_layer_revisions)
        )
        return {
            "open_tasks": task_count,
            "open_error_qa_issues": error_count,
            **qa_blockers,
        }

    def iter_review_scope_frames(
        self,
        session: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """Yield scoped frames in track order with a terminal-review marker."""

        where = ["frame.dataset_id=?"]
        parameters: list[Any] = [session["dataset_id"]]
        track_ids = [str(item) for item in session.get("track_ids", [])]
        if track_ids:
            placeholders = ",".join("?" for _ in track_ids)
            where.append(f"frame.track_id IN ({placeholders})")
            parameters.extend(track_ids)
        frame_range = session.get("frame_range")
        if frame_range is not None:
            where.append("frame.ordinal BETWEEN ? AND ?")
            parameters.extend((int(frame_range[0]), int(frame_range[1])))
        query = f"""
            SELECT frame.*,
                   EXISTS(
                       SELECT 1 FROM review_tasks task
                       WHERE task.session_id=?
                         AND task.status IN (
                             'confirmed','corrected','manual_added','false_positive',
                             'skipped','field_survey'
                         )
                         AND (
                             task.frame_id=frame.id
                             OR (
                                 task.task_type='UNREVIEWED_INTERVAL'
                                 AND task.track_id=frame.track_id
                                 AND task.frame_start IS NOT NULL
                                 AND task.frame_end IS NOT NULL
                                 AND frame.ordinal BETWEEN
                                     task.frame_start AND task.frame_end
                             )
                         )
                   ) AND NOT EXISTS(
                       SELECT 1 FROM review_tasks open_task
                       WHERE open_task.session_id=?
                         AND open_task.status IN ('todo','in_progress')
                         AND (
                             open_task.frame_id=frame.id
                             OR (
                                 open_task.task_type='UNREVIEWED_INTERVAL'
                                 AND open_task.track_id=frame.track_id
                                 AND open_task.frame_start IS NOT NULL
                                 AND open_task.frame_end IS NOT NULL
                                 AND frame.ordinal BETWEEN
                                     open_task.frame_start AND open_task.frame_end
                             )
                         )
                   ) AS reviewed
            FROM frames frame WHERE {" AND ".join(where)}
            ORDER BY frame.track_id,frame.ordinal,frame.id
        """
        with self.connection() as connection:
            rows = connection.execute(
                query,
                (session["id"], session["id"], *parameters),
            )
            for row in rows:
                item = self.frame_from_row(row)
                if item is not None:
                    item["reviewed"] = bool(row["reviewed"])
                    yield item

    def dismiss_terminal_run(
        self,
        run_id: str,
        now: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Hide a completed/failed run without deleting its durable artifacts.

        The status check and visibility update share one immediate transaction so
        a concurrent lifecycle transition cannot turn an active job into a hidden
        one. Repeated requests are idempotent and keep direct run lookup intact.
        """

        allowed = ("completed", "failed", "interrupted")
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return "missing", None
            if str(row["status"]) not in allowed:
                return "conflict", self.run_from_row(row)
            if not bool(row["dismissed"]):
                cursor = connection.execute(
                    """
                    UPDATE runs SET dismissed=1,updated_at=?
                    WHERE id=? AND dismissed=0
                      AND status IN ('completed','failed','interrupted')
                    """,
                    (now, run_id),
                )
                if not cursor.rowcount:
                    current = connection.execute(
                        "SELECT * FROM runs WHERE id=?", (run_id,)
                    ).fetchone()
                    if current is None:
                        return "missing", None
                    if str(current["status"]) not in allowed:
                        return "conflict", self.run_from_row(current)
            dismissed = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            return "dismissed", self.run_from_row(dismissed)

    def list_runs_with_statuses(
        self,
        statuses: Iterable[str],
    ) -> list[dict[str, Any]]:
        selected = tuple(dict.fromkeys(str(value) for value in statuses))
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs WHERE dismissed=0 "
                f"AND status IN ({placeholders}) ORDER BY created_at",
                selected,
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def list_runs_requiring_restart_reconciliation(
        self,
        execution_contract_version: int,
    ) -> list[dict[str, Any]]:
        """Select active rows plus contract terminal rows that may need resync."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN (
                    'preparing','starting','running','cancelling','interrupted'
                )
                   OR (
                        status IN ('failed','cancelled')
                        AND json_extract(
                            resolved_json,
                            '$.run_execution_contract_version'
                        ) = ?
                   )
                ORDER BY created_at
                """,
                (int(execution_contract_version),),
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def next_queued_run(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status='queued' AND cancel_requested=0 AND dismissed=0
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
        return self.run_from_row(row)

    def claim_next_queued_run(self, now: str) -> dict[str, Any] | None:
        """Atomically claim one queued run across possible ASGI processes."""

        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM runs
                WHERE status IN ('preparing','starting','running','cancelling')
                LIMIT 1
                """
            ).fetchone()
            if active is not None:
                return None
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status='queued' AND cancel_requested=0 AND dismissed=0
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE runs SET status='preparing',updated_at=?
                WHERE id=? AND status='queued' AND cancel_requested=0
                """,
                (now, row["id"]),
            )
            if not cursor.rowcount:
                return None
            claimed = dict(row)
            claimed["status"] = "preparing"
            claimed["updated_at"] = now
            return self.run_from_row(_MappingRow(claimed))

    def begin_run_start(self, run_id: str, now: str) -> bool:
        """Atomically reserve the final pre-spawn transition for one run."""

        with self.connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET status='starting',updated_at=?
                WHERE id=? AND status='preparing' AND cancel_requested=0
                """,
                (now, run_id),
            )
            return bool(cursor.rowcount)

    def mark_run_running(
        self,
        run_id: str,
        *,
        pid: int,
        started_at: str,
    ) -> bool:
        """Publish a spawned process only if cancellation did not win the race."""

        with self.connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET status='running',pid=?,started_at=?,updated_at=?
                WHERE id=? AND status='starting' AND cancel_requested=0
                """,
                (int(pid), started_at, started_at, run_id),
            )
            return bool(cursor.rowcount)

    def update_run(self, run_id: str, now: str, **fields: Any) -> None:
        allowed = {
            "status",
            "pid",
            "return_code",
            "error",
            "cancel_requested",
            "started_at",
            "finished_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        selected["updated_at"] = now
        assignments = ", ".join(f"{key}=?" for key in selected)
        with self.connection(write=True) as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id=?",
                (*selected.values(), run_id),
            )

    def transition_run(
        self,
        run_id: str,
        now: str,
        *,
        from_statuses: Iterable[str],
        to_status: str,
        **fields: Any,
    ) -> bool:
        """Atomically move a run only from an explicitly allowed state.

        ``update_run`` remains available for migrations and compatibility, while
        lifecycle code uses this compare-and-set operation so a late worker or
        cancellation request cannot overwrite a terminal result.
        """

        expected = tuple(dict.fromkeys(str(value) for value in from_statuses))
        if not expected:
            raise ValueError("from_statuses must contain at least one status.")
        allowed = {
            "pid",
            "return_code",
            "error",
            "cancel_requested",
            "started_at",
            "finished_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        selected = {"status": str(to_status), **selected, "updated_at": now}
        assignments = ", ".join(f"{key}=?" for key in selected)
        placeholders = ",".join("?" for _ in expected)
        with self.connection(write=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE runs SET {assignments}
                WHERE id=? AND status IN ({placeholders})
                """,
                (*selected.values(), run_id, *expected),
            )
            return bool(cursor.rowcount)

    def recover_after_restart(self, now: str) -> int:
        with self.connection(write=True) as connection:
            connection.execute(
                """
                UPDATE runs SET status='queued',pid=NULL,updated_at=?
                WHERE status='preparing' AND cancel_requested=0
                """,
                (now,),
            )
            cancelled_cursor = connection.execute(
                """
                UPDATE runs SET status='cancelled',finished_at=?,updated_at=?
                WHERE status IN ('preparing','starting','running','cancelling')
                    AND cancel_requested=1
                """,
                (now, now),
            )
            interrupted_cursor = connection.execute(
                """
                UPDATE runs SET status='interrupted',
                    error='Server restarted while this run was active.',
                    finished_at=?,updated_at=?
                WHERE status IN ('starting','running','cancelling')
                    AND cancel_requested=0
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE datasets SET status='error',
                    error='Server restarted before dataset scanning completed.',
                    updated_at=? WHERE status='scanning'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE datasets SET catalog_status='missing',catalog_error=NULL,
                    updated_at=? WHERE catalog_status='building'
                """,
                (now,),
            )
            return int(cancelled_cursor.rowcount) + int(interrupted_cursor.rowcount)

    def ping(self) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1


class _NullLock:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _MappingRow:
    """Minimal sqlite.Row-compatible adapter used for an atomically claimed row."""

    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def __iter__(self):
        return iter(self._value)

    def keys(self):
        return self._value.keys()

    def __getitem__(self, key: str) -> Any:
        return self._value[key]
