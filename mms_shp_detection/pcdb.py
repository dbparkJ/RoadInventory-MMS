from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np


PCDB_NAME_PATTERN = re.compile(
    r"(?P<route>\d+)\s*-\s*(?P<stamp>\d{6}_\d{6})_Scanner_(?P<scanner>\d+)\.pcdb$",
    re.IGNORECASE,
)
CATALOG_VERSION = 2


def _parse_pcdb_name(path: Path) -> tuple[str, dt.datetime, int]:
    match = PCDB_NAME_PATTERN.search(path.name)
    if not match:
        raise ValueError(f"Unsupported pcdb filename format: {path.name}")
    route_id = match.group("route")
    timestamp = dt.datetime.strptime(match.group("stamp"), "%y%m%d_%H%M%S")
    scanner_id = int(match.group("scanner"))
    return route_id, timestamp, scanner_id


def _index_single_pcdb(path: Path) -> dict[str, Any]:
    route_id, timestamp, scanner_id = _parse_pcdb_name(path)
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            """
            SELECT NAME, LENGTH(DATA), SUBSTR(DATA, 1, 52)
            FROM CRYSTAL_CUBE
            WHERE NAME LIKE '%.bpc'
            ORDER BY NAME
            """
        ).fetchall()
    finally:
        connection.close()

    blocks: list[dict[str, Any]] = []
    for block_name, blob_length, header in rows:
        header_bytes = bytes(header)
        if len(header_bytes) < 52:
            continue
        min_x, min_y, min_z, max_x, max_y, max_z = struct.unpack("<6d", header_bytes[:48])
        point_count = struct.unpack("<I", header_bytes[48:52])[0]
        blocks.append(
            {
                "name": block_name,
                "min": [min_x, min_y, min_z],
                "max": [max_x, max_y, max_z],
                "point_count": int(point_count),
                "blob_length": int(blob_length),
            }
        )

    file_min = [
        min(block["min"][0] for block in blocks),
        min(block["min"][1] for block in blocks),
        min(block["min"][2] for block in blocks),
    ]
    file_max = [
        max(block["max"][0] for block in blocks),
        max(block["max"][1] for block in blocks),
        max(block["max"][2] for block in blocks),
    ]

    return {
        "path": str(path.resolve()),
        "route_id": route_id,
        "timestamp_iso": timestamp.isoformat(),
        "scanner_id": scanner_id,
        "file_size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "file_min": file_min,
        "file_max": file_max,
        "blocks": blocks,
    }


def build_pcdb_catalog(data_root: Path, cache_path: Path, logger) -> dict[str, Any]:
    las_root = data_root / "LAS"
    if not las_root.exists():
        raise FileNotFoundError(f"LAS root not found: {las_root}")

    pcdb_paths = sorted(las_root.rglob("*.pcdb"))
    if not pcdb_paths:
        raise FileNotFoundError(f"No .pcdb files found under {las_root}")

    signature = [
        {
            "path": str(path.resolve()),
            "file_size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in pcdb_paths
    ]

    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("catalog_version") == CATALOG_VERSION and cached.get("signature") == signature:
            logger.info("Loaded cached pcdb catalog from %s", cache_path)
            return cached

    logger.info("Building pcdb catalog for %d files...", len(pcdb_paths))
    files = []
    total = len(pcdb_paths)
    started_at = time.perf_counter()
    for index, path in enumerate(pcdb_paths, start=1):
        file_started_at = time.perf_counter()
        logger.info("Catalog indexing %d/%d: %s", index, total, path.name)
        files.append(_index_single_pcdb(path))
        logger.info(
            "Catalog indexed %d/%d: %s in %.1fs",
            index,
            total,
            path.name,
            time.perf_counter() - file_started_at,
        )
    catalog = {"catalog_version": CATALOG_VERSION, "signature": signature, "files": files}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Finished pcdb catalog build in %.1fs and saved cache to %s",
        time.perf_counter() - started_at,
        cache_path,
    )
    return catalog


class PcdbConnectionCache:
    def __init__(self) -> None:
        self._connections: dict[str, sqlite3.Connection] = {}

    def _connection(self, pcdb_path: str) -> sqlite3.Connection:
        connection = self._connections.get(pcdb_path)
        if connection is None:
            connection = sqlite3.connect(pcdb_path)
            self._connections[pcdb_path] = connection
        return connection

    def read_block_points(self, pcdb_path: str, block_name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        connection = self._connection(pcdb_path)
        row = connection.execute(
            "SELECT DATA FROM CRYSTAL_CUBE WHERE NAME = ?",
            (block_name,),
        ).fetchone()
        if row is None:
            return (
                np.empty((0, 3), dtype=np.float64),
                np.empty((0, 3), dtype=np.uint8),
                np.empty((0,), dtype=np.uint16),
            )

        data = bytes(row[0])
        if len(data) < 52:
            return (
                np.empty((0, 3), dtype=np.float64),
                np.empty((0, 3), dtype=np.uint8),
                np.empty((0,), dtype=np.uint16),
            )

        # Keep the block origin in float64. At Korean projected northings around
        # 4.1 million metres float32 has a 0.25 m ULP, which would quantize away
        # the centimetre-level local offsets stored by PCDB.
        min_xyz = np.asarray(struct.unpack("<6d", data[:48])[:3], dtype=np.float64)
        max_xyz = np.asarray(struct.unpack("<6d", data[:48])[3:], dtype=np.float64)
        block_center_xyz = (min_xyz + max_xyz) * 0.5
        point_count = struct.unpack("<I", data[48:52])[0]
        expected_bytes = point_count * 17
        available_bytes = max(0, len(data) - 52)
        if expected_bytes > available_bytes:
            point_count = available_bytes // 17
            expected_bytes = point_count * 17
        if point_count == 0:
            return (
                np.empty((0, 3), dtype=np.float64),
                np.empty((0, 3), dtype=np.uint8),
                np.empty((0,), dtype=np.uint16),
            )

        raw = np.frombuffer(data, dtype=np.uint8, count=expected_bytes, offset=52).reshape(point_count, 17)
        offsets = raw[:, :12].copy().view("<f4").reshape(point_count, 3)
        # PCDB stores point offsets around the block center, not the max corner.
        coords = offsets.astype(np.float64) + block_center_xyz[None, :]
        rgb = raw[:, 12:15].copy()
        intensity = raw[:, 15:17].copy().view("<u2").reshape(point_count)
        return coords, rgb.astype(np.uint8), intensity.astype(np.uint16)

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()


def match_nearest_pcdb_files(
    image_task: dict[str, Any],
    catalog: dict[str, Any],
    neighbor_count: int,
) -> list[dict[str, Any]]:
    image_timestamp = dt.datetime.fromisoformat(image_task["timestamp_iso"])
    route_id = image_task["route_id"]
    origin_x, origin_y, _origin_z = image_task["origin"]

    def bbox_distance_xy(item: dict[str, Any]) -> float:
        min_x, min_y, _ = item["file_min"]
        max_x, max_y, _ = item["file_max"]
        dx = 0.0
        dy = 0.0
        if origin_x < min_x:
            dx = min_x - origin_x
        elif origin_x > max_x:
            dx = origin_x - max_x
        if origin_y < min_y:
            dy = min_y - origin_y
        elif origin_y > max_y:
            dy = origin_y - max_y
        return math.hypot(dx, dy)

    candidates = [item for item in catalog["files"] if item["route_id"] == route_id]
    candidates.sort(
        key=lambda item: (
            bbox_distance_xy(item),
            abs((dt.datetime.fromisoformat(item["timestamp_iso"]) - image_timestamp).total_seconds()),
        )
    )
    return candidates[:neighbor_count]


def select_candidate_blocks(
    pcdb_file: dict[str, Any],
    origin_xyz: np.ndarray,
    center_ray: np.ndarray,
    detection_angle_rad: float,
    max_range_m: float,
    angle_margin_rad: float,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for block in pcdb_file["blocks"]:
        min_xyz = np.asarray(block["min"], dtype=np.float64)
        max_xyz = np.asarray(block["max"], dtype=np.float64)
        center_xyz = (min_xyz + max_xyz) * 0.5
        vector = center_xyz - origin_xyz
        distance = float(np.linalg.norm(vector))
        if distance == 0:
            continue

        half_diagonal = float(np.linalg.norm(max_xyz - min_xyz) * 0.5)
        if distance - half_diagonal > max_range_m:
            continue

        direction = vector / distance
        dot_value = float(np.clip(np.dot(direction, center_ray), -1.0, 1.0))
        angle = math.acos(dot_value)

        if distance <= half_diagonal:
            block_radius = math.pi
        else:
            block_radius = math.asin(min(1.0, half_diagonal / distance))

        if angle <= detection_angle_rad + angle_margin_rad + block_radius:
            candidates.append(block)
    return candidates
