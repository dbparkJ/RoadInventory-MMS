from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ACTIVE_RUN_STATUSES = ("queued", "preparing", "starting", "running", "cancelling")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_dismissed_created "
                "ON runs(dismissed, created_at)"
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
                    id,dataset_id,status,request_json,resolved_json,work_relative,
                    created_at,updated_at
                ) VALUES(?,?,'queued',?,?,?,?,?)
                """,
                (
                    run["id"],
                    run["dataset_id"],
                    _json(run["request"]),
                    _json(run["resolved"]),
                    run["work_relative"],
                    run["created_at"],
                    run["updated_at"],
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return self.run_from_row(row)

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE dismissed=0 "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def list_completed_runs_for_dataset(
        self,
        dataset_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return most recently finished durable results, including hidden runs."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE dataset_id=? AND status='completed'
                ORDER BY
                    COALESCE(finished_at, updated_at, created_at) DESC,
                    created_at DESC,
                    id DESC
                LIMIT ?
                """,
                (dataset_id, int(limit)),
            ).fetchall()
        return [self.run_from_row(row) for row in rows if row is not None]  # type: ignore[misc]

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
    def __enter__(self) -> "_NullLock":
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
