from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote

import shapefile
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from pyproj.exceptions import ProjError
from starlette.background import BackgroundTask

from .datasets import require_ready_dataset, utc_now
from .security import (
    UnsafePath,
    atomic_replace_bytes,
    normalize_relative_path,
    opaque_id,
    resolve_under_root,
)

router = APIRouter(prefix="/api", tags=["overlays"])

OVERLAY_ID = re.compile(r"^ov_[0-9a-f]{32}$")
SUPPORTED_SIDECARS = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qpj",
    ".wkt2",
}
REQUIRED_SIDECARS = {".shp", ".shx", ".dbf"}
ZIP_RATIO_LIMIT = 200
MANIFEST_MAX_BYTES = 2 * 1024**2
ENCODING_ALIASES = {
    "UTF-8": "utf-8",
    "UTF8": "utf-8",
    "65001": "utf-8",
    "CP949": "cp949",
    "MS949": "cp949",
    "WINDOWS-949": "cp949",
    "949": "cp949",
    "EUC-KR": "euc-kr",
    "EUCKR": "euc-kr",
    "EUC_KR": "euc-kr",
    "CP1252": "cp1252",
    "WINDOWS-1252": "cp1252",
    "1252": "cp1252",
    "ISO-8859-1": "latin-1",
    "LATIN-1": "latin-1",
    "LATIN1": "latin-1",
}
CPG_LABELS = {
    "utf-8": "UTF-8",
    "cp949": "949",
    "euc-kr": "EUC-KR",
    "cp1252": "1252",
    "latin-1": "ISO-8859-1",
}


class OverlayTooLarge(ValueError):
    pass


class FeaturePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    geometry: dict[str, Any] | None = None
    coordinate_space: Literal["dataset", "wgs84"] = "dataset"
    properties: dict[str, Any] | None = None
    expected_revision: int | None = Field(None, ge=1)


class ResultImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    name: str | None = None
    crs: str | None = None
    encoding: str | None = None


class PanoramaPickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    u: float = Field(ge=0.0, le=1.0)
    v: float = Field(ge=0.0, le=1.0)
    depth: float = Field(gt=0.01, le=10_000.0)
    yaw_offset_deg: float | None = Field(None, ge=-180.0, le=180.0)
    pitch_offset_deg: float | None = Field(None, ge=-45.0, le=45.0)


def _clean_layer_name(value: str | None, fallback: str) -> str:
    candidate = str(value or fallback).strip()
    candidate = "".join(character for character in candidate if ord(character) >= 32)
    candidate = candidate.replace("/", "_").replace("\\", "_").strip(" .")
    return (candidate or "SHP overlay")[:120]


def _overlay_root(app: Any, dataset_id: str) -> Path:
    directory = (
        app.state.config.state_dir
        / "overlays"
        / opaque_id("ds", dataset_id, length=32)
    )
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise UnsafePath("Overlay storage cannot be a symbolic link.")
    return directory.resolve(strict=True)


def _overlay_archive_root(app: Any, dataset_id: str) -> Path:
    directory = (
        app.state.config.state_dir
        / "overlay_archive"
        / opaque_id("ds", dataset_id, length=32)
    )
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise UnsafePath("Overlay archive storage cannot be a symbolic link.")
    return directory.resolve(strict=True)


def _layer_directory(app: Any, dataset_id: str, layer_id: str) -> Path:
    if OVERLAY_ID.fullmatch(layer_id) is None:
        raise FileNotFoundError("Overlay layer not found.")
    root = _overlay_root(app, dataset_id)
    candidate = (root / layer_id).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafePath("Overlay layer escaped its storage directory.") from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise FileNotFoundError("Overlay layer not found.")
    return candidate


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_manifest(
    layer_dir: Path,
    *,
    include_unregistered: bool = False,
) -> dict[str, Any]:
    path = layer_dir / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MANIFEST_MAX_BYTES:
        raise ValueError("Overlay manifest is missing or invalid.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Overlay manifest is invalid.")
    if not include_unregistered and value.get("registered", True) is False:
        raise FileNotFoundError("Overlay layer not found.")
    return value


@contextmanager
def _feature_db(layer_dir: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = layer_dir / "features.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Overlay feature store is missing.")
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        if write:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if write:
            connection.commit()
    except BaseException:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _db_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
    if row is None:
        raise ValueError("Overlay revision is missing.")
    return int(row[0])


def _active_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM features WHERE deleted=0"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _public_layer(layer_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    with _feature_db(layer_dir) as connection:
        revision = _db_revision(connection)
        active_count = _active_count(connection)
    dataset_id = str(manifest["dataset_id"])
    layer_id = str(manifest["id"])
    base = f"/api/datasets/{quote(dataset_id, safe='')}/overlays/{layer_id}"
    return {
        "id": layer_id,
        "dataset_id": dataset_id,
        "name": manifest["name"],
        "source_kind": manifest["source_kind"],
        "source_crs": manifest["source_crs"],
        "source_encoding": manifest.get("source_encoding", "utf-8"),
        "edited_download_encoding": manifest.get("source_encoding", "utf-8"),
        "dataset_crs": manifest["dataset_crs"],
        "map_crs": "EPSG:4326",
        "geometry_type": manifest["geometry_type"],
        "shape_type": manifest["shape_type"],
        "feature_count": active_count,
        "original_feature_count": int(manifest["original_feature_count"]),
        "revision": revision,
        "fields": manifest["fields"],
        "warnings": manifest.get("warnings", []),
        "created_at": manifest["created_at"],
        "features_url": f"{base}/features",
        "project_url_template": f"{base}/project/{{frame_id}}",
        "download_url": f"{base}/download",
        "source_preserved": True,
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _field_definition(field: Any) -> dict[str, Any]:
    if hasattr(field, "name"):
        return {
            "name": str(field.name),
            "type": str(field.field_type),
            "size": int(field.size),
            "decimal": int(field.decimal),
        }
    return {
        "name": str(field[0]),
        "type": str(field[1]),
        "size": int(field[2]),
        "decimal": int(field[3]),
    }


def _map_coordinates(value: Any, transform: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        raise TypeError("SHP geometry contains invalid coordinates.")
    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        x, y = float(value[0]), float(value[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("SHP geometry contains non-finite coordinates.")
        try:
            mapped_x, mapped_y = transform(x, y)
        except ProjError as exc:
            raise ValueError("SHP coordinate transformation failed.") from exc
        if not (math.isfinite(float(mapped_x)) and math.isfinite(float(mapped_y))):
            raise ValueError("SHP coordinate transformation produced an invalid point.")
        result = [float(mapped_x), float(mapped_y)]
        for item in value[2:]:
            number = float(item)
            result.append(number if math.isfinite(number) else None)
        return result
    return [_map_coordinates(item, transform) for item in value]


def _transform_geometry(geometry: dict[str, Any] | None, transformer: Any) -> dict[str, Any] | None:
    if geometry is None:
        return None
    geometry_type = str(geometry.get("type") or "")
    if geometry_type == "GeometryCollection":
        return {
            "type": geometry_type,
            "geometries": [
                _transform_geometry(item, transformer)
                for item in geometry.get("geometries", [])
            ],
        }
    coordinates = geometry.get("coordinates")
    if coordinates is None:
        return None
    return {
        "type": geometry_type,
        "coordinates": _map_coordinates(coordinates, transformer.transform),
    }


def _shape_geometry_type(shape_type: int) -> str:
    import shapefile

    name = str(shapefile.SHAPETYPE_LOOKUP.get(shape_type, "UNKNOWN"))
    normalized = re.sub(r"[MZ]$", "", name.upper())
    return {
        "POINT": "Point",
        "MULTIPOINT": "MultiPoint",
        "POLYLINE": "LineString",
        "POLYGON": "Polygon",
        "NULL": "Null",
    }.get(normalized, name)


def _point_columns(geometry: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    if geometry is None or geometry.get("type") != "Point":
        return None, None, None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None, None, None
    z = coordinates[2] if len(coordinates) >= 3 else None
    return float(coordinates[0]), float(coordinates[1]), None if z is None else float(z)


def _bundle_member(bundle_dir: Path, stem: str, suffix: str) -> Path | None:
    expected_stem = stem.casefold()
    expected_suffix = suffix.casefold()
    for candidate in bundle_dir.iterdir():
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and candidate.stem.casefold() == expected_stem
            and candidate.suffix.casefold() == expected_suffix
        ):
            return candidate
    return None


def _normalize_dbf_encoding(value: str) -> str:
    normalized = ENCODING_ALIASES.get(value.strip().strip("\"'").upper())
    if normalized is None:
        raise ValueError(
            "Unsupported DBF encoding. Use auto, UTF-8, CP949, or EUC-KR."
        )
    try:
        codecs.lookup(normalized)
    except LookupError as exc:
        raise ValueError("The selected DBF encoding is unavailable.") from exc
    return normalized


def _infer_dbf_encoding(bundle_dir: Path, stem: str) -> str:
    dbf = _bundle_member(bundle_dir, stem, ".dbf")
    if dbf is None:
        raise ValueError("SHP bundle has no DBF table.")
    with dbf.open("rb") as handle:
        header = handle.read(32)
        if len(header) < 12:
            raise ValueError("DBF header is incomplete.")
        header_length = int.from_bytes(header[8:10], "little")
        record_length = int.from_bytes(header[10:12], "little")
        if header_length < 33 or header_length > dbf.stat().st_size:
            raise ValueError("DBF header length is invalid.")
        if record_length <= 0:
            raise ValueError("DBF record length is invalid.")
        handle.seek(header_length)
        remaining = dbf.stat().st_size - header_length
        max_records = max(1, (8 * 1024**2) // record_length)
        sample = handle.read(min(remaining, max_records * record_length))
    try:
        sample.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        try:
            sample.decode("cp949", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "DBF encoding could not be inferred safely; select UTF-8, CP949, or EUC-KR."
            ) from exc
        return "cp949"
    return "utf-8"


def _resolve_dbf_encoding(
    bundle_dir: Path,
    stem: str,
    supplied_encoding: str | None,
) -> tuple[str, list[str]]:
    requested = str(supplied_encoding or "auto").strip()
    if requested and requested.casefold() != "auto":
        return _normalize_dbf_encoding(requested), []
    cpg = _bundle_member(bundle_dir, stem, ".cpg")
    if cpg is not None:
        if cpg.stat().st_size > 256:
            raise ValueError("The SHP .cpg sidecar is too large.")
        try:
            declared = cpg.read_text(encoding="utf-8-sig", errors="strict").strip()
        except UnicodeError as exc:
            raise ValueError("The SHP .cpg sidecar is not valid text.") from exc
        if not declared:
            raise ValueError("The SHP .cpg sidecar is empty.")
        return _normalize_dbf_encoding(declared), []
    inferred = _infer_dbf_encoding(bundle_dir, stem)
    return inferred, [
        (
            "SHP .cpg was missing; DBF encoding was inferred as "
            f"{CPG_LABELS[inferred]}. Select an encoding explicitly if text looks incorrect."
        )
    ]


def _resolve_crs(
    bundle_dir: Path,
    stem: str,
    supplied_crs: str | None,
    dataset_crs: str,
) -> tuple[Any, list[str]]:
    from pyproj import CRS

    warnings: list[str] = []
    candidate: str | None = supplied_crs.strip() if supplied_crs else None
    if not candidate:
        for suffix in (".prj", ".qpj", ".wkt2"):
            path = _bundle_member(bundle_dir, stem, suffix)
            if path is not None and path.stat().st_size <= 2 * 1024**2:
                candidate = path.read_text(encoding="utf-8-sig", errors="replace").strip()
                if candidate:
                    break
    if not candidate:
        candidate = dataset_crs
        warnings.append(
            "SHP CRS sidecar was missing; dataset CRS was used for the overlay."
        )
    try:
        return CRS.from_user_input(candidate), warnings
    except Exception as exc:
        raise ValueError("The SHP coordinate system is not recognized.") from exc


def _initialize_feature_store(
    path: Path,
    rows: Iterator[tuple[Any, ...]],
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE features (
                id TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL UNIQUE,
                geometry_json TEXT,
                properties_json TEXT NOT NULL,
                point_x REAL,
                point_y REAL,
                point_z REAL,
                deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX features_active_ordinal ON features(deleted, ordinal);
            CREATE INDEX features_point_xy ON features(deleted, point_x, point_y);
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision INTEGER NOT NULL,
                action TEXT NOT NULL,
                feature_id TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO metadata(key,value) VALUES('revision','1')")
        connection.executemany(
            """
            INSERT INTO features(
                id,ordinal,geometry_json,properties_json,
                point_x,point_y,point_z,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _import_bundle(
    app: Any,
    dataset: dict[str, Any],
    staging: Path,
    *,
    layer_id: str,
    name: str | None,
    supplied_crs: str | None,
    supplied_encoding: str | None,
    source_kind: str,
    source_reference: str | None,
) -> dict[str, Any]:
    from pyproj import CRS, Transformer

    bundle_dir = staging / "source"
    primaries = sorted(
        path
        for path in bundle_dir.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".shp"
    )
    if len(primaries) != 1:
        raise ValueError("Exactly one SHP layer must be supplied.")
    primary = primaries[0]
    stem = primary.stem
    mismatched = sorted(
        path.name
        for path in bundle_dir.iterdir()
        if path.is_file() and path.stem.casefold() != stem.casefold()
    )
    if mismatched:
        raise ValueError(
            "All SHP sidecars must share one base name; unexpected file(s): "
            + ", ".join(mismatched[:5])
        )
    available = {
        path.suffix.casefold()
        for path in bundle_dir.iterdir()
        if path.is_file() and path.stem.casefold() == stem.casefold()
    }
    missing = REQUIRED_SIDECARS - available
    if missing:
        raise ValueError(
            "A complete SHP bundle requires .shp, .shx, and .dbf files "
            f"(missing: {', '.join(sorted(missing))})."
        )

    source_crs, warnings = _resolve_crs(
        bundle_dir,
        stem,
        supplied_crs,
        str(dataset["crs"]),
    )
    dataset_crs = CRS.from_user_input(dataset["crs"])
    transformer = Transformer.from_crs(source_crs, dataset_crs, always_xy=True)
    encoding, encoding_warnings = _resolve_dbf_encoding(
        bundle_dir, stem, supplied_encoding
    )
    warnings.extend(encoding_warnings)
    reader: shapefile.Reader | None = None
    try:
        shx_path = _bundle_member(bundle_dir, stem, ".shx")
        dbf_path = _bundle_member(bundle_dir, stem, ".dbf")
        if shx_path is None or dbf_path is None:
            raise ValueError("SHP bundle lost a required sidecar during import.")
        reader = shapefile.Reader(
            shp=str(primary),
            shx=str(shx_path),
            dbf=str(dbf_path),
            encoding=encoding,
            encodingErrors="strict",
        )
        feature_count = int(reader.numRecords)
        if feature_count > app.state.config.max_overlay_features:
            raise OverlayTooLarge(
                "SHP feature count exceeds the configured overlay limit."
            )
        fields = [_field_definition(field) for field in reader.fields[1:]]
        field_names = [field["name"] for field in fields]
        now = utc_now()

        def rows() -> Iterator[tuple[Any, ...]]:
            for ordinal, shape_record in enumerate(reader.iterShapeRecords()):
                raw_geometry = shape_record.shape.__geo_interface__
                if raw_geometry.get("type") == "Point":
                    z_values = getattr(shape_record.shape, "z", None)
                    if z_values and math.isfinite(float(z_values[0])):
                        raw_geometry = {
                            **raw_geometry,
                            "coordinates": [
                                *raw_geometry["coordinates"][:2],
                                float(z_values[0]),
                            ],
                        }
                geometry = _transform_geometry(raw_geometry, transformer)
                values = list(shape_record.record)
                properties = {}
                for index, field_name in enumerate(field_names):
                    value = values[index] if index < len(values) else None
                    normalized = _json_value(value)
                    if fields[index]["type"].upper() == "D" and isinstance(value, date):
                        normalized = value.strftime("%Y%m%d")
                    properties[field_name] = normalized
                x, y, z = _point_columns(geometry)
                yield (
                    f"f_{ordinal + 1:09d}",
                    ordinal,
                    None if geometry is None else _json_bytes(geometry).decode("utf-8"),
                    _json_bytes(properties).decode("utf-8"),
                    x,
                    y,
                    z,
                    now,
                )

        _initialize_feature_store(staging / "features.sqlite3", rows())
        shape_type = int(reader.shapeType)
    except OverlayTooLarge:
        raise
    except UnicodeError as exc:
        raise ValueError(
            "DBF text does not match "
            f"{CPG_LABELS[encoding]}; retry the upload with an explicit encoding."
        ) from exc
    except (OSError, shapefile.ShapefileException) as exc:
        raise ValueError("The SHP bundle could not be read safely.") from exc
    finally:
        if reader is not None:
            reader.close()

    manifest = {
        "schema_version": 1,
        "id": layer_id,
        "dataset_id": dataset["id"],
        "name": _clean_layer_name(name, stem),
        "source_kind": source_kind,
        "source_reference": source_reference,
        "source_files": sorted(path.name for path in bundle_dir.iterdir() if path.is_file()),
        "source_crs": source_crs.to_string(),
        "source_encoding": encoding,
        "dataset_crs": dataset_crs.to_string(),
        "geometry_type": _shape_geometry_type(shape_type),
        "shape_type": shape_type,
        "original_feature_count": feature_count,
        "fields": fields,
        "warnings": warnings,
        "registered": True,
        "created_at": utc_now(),
    }
    atomic_replace_bytes(staging / "manifest.json", _json_bytes(manifest))
    return manifest


def _safe_upload_filename(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ValueError("Every upload must have a plain file name.")
    if Path(text).suffix.casefold() not in SUPPORTED_SIDECARS | {".zip"}:
        raise ValueError(f"Unsupported overlay file type: {Path(text).suffix or '(none)'}")
    return text[:200]


async def _save_uploads(app: Any, uploads: list[UploadFile], staging: Path) -> None:
    if not uploads or len(uploads) > app.state.config.max_overlay_upload_files:
        raise OverlayTooLarge("Overlay upload file count is outside the configured limit.")
    names = [_safe_upload_filename(upload.filename) for upload in uploads]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Overlay upload contains duplicate file names.")
    zip_upload = len(uploads) == 1 and Path(names[0]).suffix.casefold() == ".zip"
    if any(Path(name).suffix.casefold() == ".zip" for name in names) and not zip_upload:
        raise ValueError("A ZIP archive must be uploaded by itself.")

    target_dir = staging / ("original" if zip_upload else "source")
    target_dir.mkdir(parents=True)
    total = 0
    for upload, name in zip(uploads, names, strict=True):
        target = target_dir / name
        written = 0
        try:
            with target.open("xb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > app.state.config.max_overlay_file_bytes:
                        raise OverlayTooLarge("An overlay upload file exceeds the size limit.")
                    if total > app.state.config.max_overlay_total_bytes:
                        raise OverlayTooLarge("Overlay upload exceeds the total size limit.")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            await upload.close()
    if zip_upload:
        await asyncio.to_thread(
            _extract_zip,
            target_dir / names[0],
            staging / "source",
            max_files=app.state.config.max_overlay_upload_files,
            max_file_bytes=app.state.config.max_overlay_file_bytes,
            max_total_bytes=app.state.config.max_overlay_total_bytes,
        )


def _safe_zip_member(info: zipfile.ZipInfo) -> str | None:
    if info.is_dir():
        return None
    value = info.filename
    if "\\" in value or "\x00" in value:
        raise ValueError("ZIP entry has an unsafe path.")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("ZIP entry has an unsafe path.")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise ValueError("ZIP symbolic links are not allowed.")
    name = pure.name
    if Path(name).suffix.casefold() not in SUPPORTED_SIDECARS:
        return None
    return _safe_upload_filename(name)


def _extract_zip(
    archive_path: Path,
    target_dir: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=False)
    total = 0
    accepted: list[tuple[zipfile.ZipInfo, str]] = []
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > max_files * 4:
            raise OverlayTooLarge("ZIP contains too many entries.")
        for info in members:
            name = _safe_zip_member(info)
            if name is None:
                continue
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted ZIP entries are not supported.")
            if info.file_size > max_file_bytes:
                raise OverlayTooLarge("A decompressed SHP sidecar exceeds the size limit.")
            total += int(info.file_size)
            if total > max_total_bytes:
                raise OverlayTooLarge("Decompressed SHP bundle exceeds the total size limit.")
            if info.file_size and info.compress_size == 0:
                raise OverlayTooLarge("ZIP entry has an invalid compression ratio.")
            if info.compress_size and info.file_size / info.compress_size > ZIP_RATIO_LIMIT:
                raise OverlayTooLarge("ZIP entry exceeds the safe compression ratio.")
            accepted.append((info, name))
            if len(accepted) > max_files:
                raise OverlayTooLarge("ZIP contains too many SHP sidecars.")
        if not accepted:
            raise ValueError("ZIP does not contain a supported SHP bundle.")
        if len({name.casefold() for _, name in accepted}) != len(accepted):
            raise ValueError("ZIP contains duplicate flattened file names.")
        for info, name in accepted:
            target = target_dir / name
            with archive.open(info) as source, target.open("xb") as destination:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > info.file_size or copied > max_file_bytes:
                        raise OverlayTooLarge("ZIP entry expanded beyond its declared size.")
                    destination.write(chunk)
            if copied != info.file_size:
                raise ValueError("ZIP entry size did not match its declaration.")


def _layer_lock(app: Any, dataset_id: str, layer_id: str) -> asyncio.Lock:
    key = f"{dataset_id}:{layer_id}"
    return app.state.overlay_locks.setdefault(key, asyncio.Lock())


def _decode_feature(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": row["id"],
        "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None,
        "properties": json.loads(row["properties_json"]),
    }


def _transform_feature(feature: dict[str, Any], transformer: Any | None) -> dict[str, Any]:
    if transformer is None:
        return feature
    return {
        **feature,
        "geometry": _transform_geometry(feature["geometry"], transformer),
    }


def _to_wgs84_transformer(dataset_crs: str) -> Any:
    from pyproj import Transformer

    return Transformer.from_crs(dataset_crs, "EPSG:4326", always_xy=True)


def _from_wgs84_transformer(dataset_crs: str) -> Any:
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:4326", dataset_crs, always_xy=True)


def _validate_point_geometry(
    geometry: dict[str, Any],
    *,
    old_geometry: dict[str, Any] | None,
) -> dict[str, Any]:
    if old_geometry is None or old_geometry.get("type") != "Point":
        raise ValueError("Only existing Point feature coordinates can be edited.")
    if geometry.get("type") != "Point":
        raise ValueError("Only Point feature coordinates can be edited.")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or not 2 <= len(coordinates) <= 3:
        raise ValueError("Point coordinates must be [x, y] or [x, y, z].")
    result = [float(item) for item in coordinates]
    if not all(math.isfinite(item) for item in result):
        raise ValueError("Point coordinates must be finite numbers.")
    if len(result) == 2 and old_geometry and old_geometry.get("type") == "Point":
        old_coordinates = old_geometry.get("coordinates")
        if isinstance(old_coordinates, list) and len(old_coordinates) >= 3:
            result.append(float(old_coordinates[2]))
    return {"type": "Point", "coordinates": result}


def _coerce_property(
    value: Any,
    field: dict[str, Any],
    *,
    encoding: str,
) -> Any:
    if value is None:
        return None
    field_type = str(field["type"]).upper()
    if field_type in {"N", "F"}:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field['name']} must be a finite number.")
        decimal = int(field.get("decimal", 0))
        result: int | float = (
            int(number) if field_type == "N" and decimal == 0 else number
        )
        rendered = str(result) if decimal == 0 else f"{number:.{decimal}f}"
        if len(rendered) > int(field.get("size") or 20):
            raise ValueError(f"{field['name']} exceeds its SHP numeric field width.")
        return result
    if field_type == "L":
        if not isinstance(value, bool):
            raise ValueError(f"{field['name']} must be true or false.")
        return value
    text = str(value)
    if field_type == "D":
        compact = text.replace("-", "")
        if len(compact) != 8 or not compact.isdigit():
            raise ValueError(f"{field['name']} must be a YYYY-MM-DD date.")
        return compact
    try:
        encoded = text.encode(encoding, errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field['name']} contains text that cannot be represented as {CPG_LABELS[encoding]}."
        ) from exc
    if len(encoded) > int(field.get("size") or 254):
        raise ValueError(f"{field['name']} exceeds its SHP field length.")
    return text


def _updated_revision(
    connection: sqlite3.Connection,
    expected: int | None,
) -> int:
    revision = _db_revision(connection)
    if expected is not None and expected != revision:
        raise RuntimeError(f"revision:{revision}")
    return revision + 1


def _write_edited_bundle(layer_dir: Path, manifest: dict[str, Any], output_dir: Path) -> Path:
    from mms_shp_detection.shp_writer import write_crs_sidecars

    safe_stem = re.sub(r"[^0-9A-Za-z_-]+", "_", str(manifest["name"])).strip("_")
    safe_stem = safe_stem[:80] or "overlay"
    shp_path = output_dir / f"{safe_stem}.shp"
    original_shape_type = int(manifest["shape_type"])
    # GeoJSON has no M dimension and pyshp intentionally maps most GeoJSON
    # geometries to their 2-D shape type. PointZ is kept explicitly because
    # pole/sign elevation is central to the review workflow; other M/Z layer
    # types are exported as a standards-compliant 2-D editable copy while the
    # untouched source bundle remains available in layer storage.
    export_shape_type = {
        shapefile.POLYLINEZ: shapefile.POLYLINE,
        shapefile.POLYGONZ: shapefile.POLYGON,
        shapefile.MULTIPOINTZ: shapefile.MULTIPOINT,
        shapefile.POINTM: shapefile.POINT,
        shapefile.POLYLINEM: shapefile.POLYLINE,
        shapefile.POLYGONM: shapefile.POLYGON,
        shapefile.MULTIPOINTM: shapefile.MULTIPOINT,
    }.get(original_shape_type, original_shape_type)
    writer = shapefile.Writer(
        str(shp_path),
        shapeType=export_shape_type,
        encoding=str(manifest.get("source_encoding", "utf-8")),
        encodingErrors="strict",
    )
    writer.autoBalance = 1
    try:
        for field in manifest["fields"]:
            writer.field(
                field["name"],
                field["type"],
                size=int(field["size"]),
                decimal=int(field["decimal"]),
            )
        with _feature_db(layer_dir) as connection:
            rows = connection.execute(
                "SELECT * FROM features WHERE deleted=0 ORDER BY ordinal"
            )
            for row in rows:
                geometry = json.loads(row["geometry_json"]) if row["geometry_json"] else None
                properties = json.loads(row["properties_json"])
                if geometry is None:
                    writer.null()
                elif export_shape_type == shapefile.POINTZ:
                    coordinates = geometry["coordinates"]
                    writer.pointz(
                        float(coordinates[0]),
                        float(coordinates[1]),
                        float(coordinates[2]) if len(coordinates) >= 3 else 0.0,
                    )
                else:
                    writer.shape(geometry)
                writer.record(*(properties.get(field["name"]) for field in manifest["fields"]))
    finally:
        writer.close()
    write_crs_sidecars(shp_path, str(manifest["dataset_crs"]))
    encoding = str(manifest.get("source_encoding", "utf-8"))
    shp_path.with_suffix(".cpg").write_text(CPG_LABELS[encoding], encoding="ascii")
    zip_path = output_dir / f"{safe_stem}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.glob(f"{safe_stem}.*")):
            if path != zip_path and path.suffix.casefold() in SUPPORTED_SIDECARS:
                archive.write(path, arcname=path.name)
    return zip_path


def _temporary_download_dir(app: Any, prefix: str) -> Path:
    root = app.state.config.state_dir / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _write_zip_bundle(members: list[Path], zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.write(member, arcname=member.name)


def _copy_bundle(members: list[Path], target_dir: Path) -> None:
    target_dir.mkdir()
    for member in members:
        shutil.copy2(member, target_dir / member.name)


def _bundle_files(primary: Path) -> list[Path]:
    if primary.suffix.casefold() != ".shp" or not primary.is_file() or primary.is_symlink():
        raise FileNotFoundError("Shapefile result not found.")
    return [
        candidate
        for candidate in sorted(primary.parent.iterdir())
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.stem.casefold() == primary.stem.casefold()
        and candidate.suffix.casefold() in SUPPORTED_SIDECARS
    ]


def _result_shapefile(app: Any, run: dict[str, Any], raw_path: str) -> Path:
    from .runs import _run_work_dir

    relative = normalize_relative_path(raw_path, allow_empty=False)
    if Path(relative).suffix.casefold() != ".shp":
        raise UnsafePath("Result path must identify a .shp file.")
    output = _run_work_dir(app, run) / "output"
    candidate = resolve_under_root(
        output,
        relative,
        must_exist=True,
        expect_directory=False,
        reject_symlinks=True,
    )
    _bundle_files(candidate)
    return candidate


@router.post(
    "/datasets/{dataset_id}/overlays",
    status_code=status.HTTP_201_CREATED,
)
async def upload_overlay(
    dataset_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    name: Annotated[str | None, Form()] = None,
    crs: Annotated[str | None, Form()] = None,
    encoding: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    dataset = require_ready_dataset(request, dataset_id)
    root = _overlay_root(request.app, dataset_id)
    layer_id = f"ov_{uuid.uuid4().hex}"
    staging = Path(tempfile.mkdtemp(prefix=f".{layer_id}-", dir=root))
    try:
        await _save_uploads(request.app, files, staging)
        manifest = await asyncio.to_thread(
            _import_bundle,
            request.app,
            dataset,
            staging,
            layer_id=layer_id,
            name=name,
            supplied_crs=crs,
            supplied_encoding=encoding,
            source_kind="upload",
            source_reference=None,
        )
        final = root / layer_id
        staging.replace(final)
    except OverlayTooLarge as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"layer": _public_layer(final, manifest)}


@router.get("/datasets/{dataset_id}/overlays")
def list_overlays(dataset_id: str, request: Request) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    root = _overlay_root(request.app, dataset_id)
    items: list[dict[str, Any]] = []
    valid_inspected = 0
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if len(items) >= 500:
            break
        if OVERLAY_ID.fullmatch(candidate.name) is None:
            continue
        valid_inspected += 1
        if valid_inspected > 1_000:
            break
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            manifest = _read_manifest(candidate)
            if manifest.get("dataset_id") != dataset_id:
                continue
            items.append(_public_layer(candidate, manifest))
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
            continue
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"items": items, "layers": items}


@router.get("/datasets/{dataset_id}/overlays/{layer_id}")
def get_overlay(dataset_id: str, layer_id: str, request: Request) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        return _public_layer(layer_dir, _read_manifest(layer_dir))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc


@router.delete("/datasets/{dataset_id}/overlays/{layer_id}")
async def unregister_overlay(
    dataset_id: str,
    layer_id: str,
    request: Request,
) -> dict[str, Any]:
    """Hide an overlay while retaining its source bundle and complete edit audit."""

    require_ready_dataset(request, dataset_id)
    lock = _layer_lock(request.app, dataset_id, layer_id)
    async with lock:
        try:
            layer_dir = _layer_directory(request.app, dataset_id, layer_id)
            manifest = _read_manifest(layer_dir, include_unregistered=True)
            if manifest.get("dataset_id") != dataset_id or manifest.get("registered", True) is False:
                raise FileNotFoundError("Overlay layer not found.")
            manifest["registered"] = False
            manifest["removed_at"] = utc_now()
            atomic_replace_bytes(layer_dir / "manifest.json", _json_bytes(manifest))
            archive_root = _overlay_archive_root(request.app, dataset_id)
            archived = archive_root / layer_id
            if archived.exists():
                raise FileExistsError("Overlay archive entry already exists.")
            layer_dir.replace(archived)
        except (
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    return {
        "id": layer_id,
        "deleted": True,
        "source_deleted": False,
        "source_preserved": True,
        "detail": "Overlay was removed from the workspace; source files and edit history were preserved.",
    }


@router.get("/datasets/{dataset_id}/overlays/{layer_id}/features")
def get_overlay_features(
    dataset_id: str,
    layer_id: str,
    request: Request,
    coordinate_space: Literal["dataset", "wgs84"] = Query("wgs84"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=10_000),
    center_x: float | None = Query(None),
    center_y: float | None = Query(None),
    radius: float | None = Query(None, gt=0.0, le=100_000.0),
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    if limit > request.app.state.config.max_overlay_response_features:
        raise HTTPException(status_code=422, detail="Overlay response limit is too large.")
    spatial_values = (center_x, center_y, radius)
    if any(value is not None for value in spatial_values) and not all(
        value is not None for value in spatial_values
    ):
        raise HTTPException(
            status_code=422,
            detail="center_x, center_y, and radius must be supplied together.",
        )
    if center_x is not None and center_y is not None and not (
        math.isfinite(center_x) and math.isfinite(center_y)
    ):
        raise HTTPException(status_code=422, detail="Spatial filter center must be finite.")
    spatial_filter = None
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        with _feature_db(layer_dir) as connection:
            revision = _db_revision(connection)
            if center_x is not None and center_y is not None and radius is not None:
                minimum_x = center_x - radius
                maximum_x = center_x + radius
                minimum_y = center_y - radius
                maximum_y = center_y + radius
                radius_squared = radius * radius
                spatial_parameters = (
                    minimum_x,
                    maximum_x,
                    minimum_y,
                    maximum_y,
                    center_x,
                    center_x,
                    center_y,
                    center_y,
                    radius_squared,
                )
                where = """
                    deleted=0 AND point_x IS NOT NULL AND point_y IS NOT NULL
                    AND point_x BETWEEN ? AND ? AND point_y BETWEEN ? AND ?
                    AND (((point_x-?)*(point_x-?))+((point_y-?)*(point_y-?))) <= ?
                """
                total_row = connection.execute(
                    f"SELECT COUNT(*) FROM features WHERE {where}",
                    spatial_parameters,
                ).fetchone()
                total = int(total_row[0]) if total_row is not None else 0
                rows = connection.execute(
                    f"""
                    SELECT * FROM features WHERE {where}
                    ORDER BY (((point_x-?)*(point_x-?))+((point_y-?)*(point_y-?))),
                        ordinal
                    LIMIT ? OFFSET ?
                    """,
                    (
                        *spatial_parameters,
                        center_x,
                        center_x,
                        center_y,
                        center_y,
                        limit,
                        offset,
                    ),
                ).fetchall()
                spatial_filter = {
                    "coordinate_space": "dataset",
                    "center": [center_x, center_y],
                    "radius": radius,
                    "geometry_type": "Point",
                }
            else:
                total = _active_count(connection)
                rows = connection.execute(
                    """
                    SELECT * FROM features WHERE deleted=0
                    ORDER BY ordinal LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        transformer = (
            _to_wgs84_transformer(manifest["dataset_crs"])
            if coordinate_space == "wgs84"
            else None
        )
        features = [_transform_feature(_decode_feature(row), transformer) for row in rows]
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    next_offset = offset + len(features) if offset + len(features) < total else None
    return {
        "type": "FeatureCollection",
        "features": features,
        "layer_id": layer_id,
        "coordinate_space": coordinate_space,
        "crs": "EPSG:4326" if coordinate_space == "wgs84" else manifest["dataset_crs"],
        "fields": manifest["fields"],
        "revision": revision,
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "spatial_filter": spatial_filter,
    }


@router.get("/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}")
def get_overlay_feature(
    dataset_id: str,
    layer_id: str,
    feature_id: str,
    request: Request,
    coordinate_space: Literal["dataset", "wgs84"] = Query("wgs84"),
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        with _feature_db(layer_dir) as connection:
            revision = _db_revision(connection)
            row = connection.execute(
                "SELECT * FROM features WHERE id=? AND deleted=0", (feature_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Feature not found.")
        transformer = (
            _to_wgs84_transformer(manifest["dataset_crs"])
            if coordinate_space == "wgs84"
            else None
        )
        feature = _transform_feature(_decode_feature(row), transformer)
    except (
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise HTTPException(status_code=404, detail="Overlay feature not found.") from exc
    return {
        "feature": feature,
        "revision": revision,
        "coordinate_space": coordinate_space,
        "crs": "EPSG:4326" if coordinate_space == "wgs84" else manifest["dataset_crs"],
        "fields": manifest["fields"],
    }


@router.patch("/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}")
async def update_overlay_feature(
    dataset_id: str,
    layer_id: str,
    feature_id: str,
    payload: FeaturePatch,
    request: Request,
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    if payload.geometry is None and payload.properties is None:
        raise HTTPException(status_code=422, detail="Geometry or properties must be supplied.")
    lock = _layer_lock(request.app, dataset_id, layer_id)
    async with lock:
        try:
            layer_dir = _layer_directory(request.app, dataset_id, layer_id)
            manifest = _read_manifest(layer_dir)
            with _feature_db(layer_dir, write=True) as connection:
                row = connection.execute(
                    "SELECT * FROM features WHERE id=? AND deleted=0", (feature_id,)
                ).fetchone()
                if row is None:
                    raise FileNotFoundError("Feature not found.")
                before = _decode_feature(row)
                geometry = before["geometry"]
                if payload.geometry is not None:
                    incoming = _validate_point_geometry(
                        payload.geometry,
                        old_geometry=geometry,
                    )
                    if payload.coordinate_space == "wgs84":
                        incoming = _transform_geometry(
                            incoming,
                            _from_wgs84_transformer(manifest["dataset_crs"]),
                        )
                    geometry = incoming
                properties = dict(before["properties"])
                if payload.properties is not None:
                    field_map = {field["name"]: field for field in manifest["fields"]}
                    unknown = set(payload.properties) - set(field_map)
                    if unknown:
                        raise ValueError(
                            f"Unknown SHP field(s): {', '.join(sorted(unknown))}"
                        )
                    for key, value in payload.properties.items():
                        properties[key] = _coerce_property(
                            value,
                            field_map[key],
                            encoding=str(manifest.get("source_encoding", "utf-8")),
                        )
                revision = _updated_revision(connection, payload.expected_revision)
                x, y, z = _point_columns(geometry)
                after = {
                    "type": "Feature",
                    "id": feature_id,
                    "geometry": geometry,
                    "properties": properties,
                }
                now = utc_now()
                connection.execute(
                    """
                    UPDATE features SET geometry_json=?,properties_json=?,
                        point_x=?,point_y=?,point_z=?,updated_at=? WHERE id=?
                    """,
                    (
                        None if geometry is None else _json_bytes(geometry).decode("utf-8"),
                        _json_bytes(properties).decode("utf-8"),
                        x,
                        y,
                        z,
                        now,
                        feature_id,
                    ),
                )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)
                )
                connection.execute(
                    """
                    INSERT INTO audit(revision,action,feature_id,before_json,after_json,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        revision,
                        "update",
                        feature_id,
                        _json_bytes(before).decode("utf-8"),
                        _json_bytes(after).decode("utf-8"),
                        now,
                    ),
                )
        except RuntimeError as exc:
            if str(exc).startswith("revision:"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Overlay was edited by another request.",
                        "current_revision": int(str(exc).split(":", 1)[1]),
                    },
                ) from exc
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    public_feature = _transform_feature(
        after,
        _to_wgs84_transformer(manifest["dataset_crs"])
        if payload.coordinate_space == "wgs84"
        else None,
    )
    return {"feature": public_feature, "revision": revision, "coordinate_space": payload.coordinate_space}


@router.delete("/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}")
async def delete_overlay_feature(
    dataset_id: str,
    layer_id: str,
    feature_id: str,
    request: Request,
    expected_revision: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    lock = _layer_lock(request.app, dataset_id, layer_id)
    async with lock:
        try:
            layer_dir = _layer_directory(request.app, dataset_id, layer_id)
            with _feature_db(layer_dir, write=True) as connection:
                row = connection.execute(
                    "SELECT * FROM features WHERE id=? AND deleted=0", (feature_id,)
                ).fetchone()
                if row is None:
                    raise FileNotFoundError("Feature not found.")
                revision = _updated_revision(connection, expected_revision)
                now = utc_now()
                connection.execute(
                    "UPDATE features SET deleted=1,updated_at=? WHERE id=?",
                    (now, feature_id),
                )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'", (str(revision),)
                )
                connection.execute(
                    """
                    INSERT INTO audit(revision,action,feature_id,before_json,after_json,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        revision,
                        "delete",
                        feature_id,
                        _json_bytes(_decode_feature(row)).decode("utf-8"),
                        None,
                        now,
                    ),
                )
        except RuntimeError as exc:
            if str(exc).startswith("revision:"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Overlay was edited by another request.",
                        "current_revision": int(str(exc).split(":", 1)[1]),
                    },
                ) from exc
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "id": feature_id,
        "deleted": True,
        "revision": revision,
        "source_preserved": True,
    }


@router.get("/datasets/{dataset_id}/overlays/{layer_id}/project/{frame_id}")
def project_overlay_on_panorama(
    dataset_id: str,
    layer_id: str,
    frame_id: str,
    request: Request,
    limit: int = Query(2_000, ge=1, le=10_000),
    max_distance: float = Query(200.0, gt=0.1, le=2_000.0),
    yaw_offset_deg: float | None = Query(None, ge=-180.0, le=180.0),
    pitch_offset_deg: float | None = Query(None, ge=-45.0, le=45.0),
) -> dict[str, Any]:
    import numpy as np

    from mms_shp_detection.geometry import project_points_equirectangular

    from .media import _panorama_axes

    require_ready_dataset(request, dataset_id)
    if limit > request.app.state.config.max_overlay_response_features:
        raise HTTPException(status_code=422, detail="Overlay projection limit is too large.")
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    try:
        origin = np.asarray(frame["task"].get("origin"), dtype=np.float64)
        if origin.shape != (3,) or not np.all(np.isfinite(origin)):
            raise ValueError("Frame has no valid dataset-space origin.")
        resolved_yaw = (
            float(request.app.state.panorama_yaw_offset_deg)
            if yaw_offset_deg is None
            else yaw_offset_deg
        )
        resolved_pitch = (
            float(request.app.state.panorama_pitch_offset_deg)
            if pitch_offset_deg is None
            else pitch_offset_deg
        )
        axes = _panorama_axes(
            frame["task"],
            yaw_offset_deg=resolved_yaw,
            pitch_offset_deg=resolved_pitch,
        )
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        with _feature_db(layer_dir) as connection:
            revision = _db_revision(connection)
            rows = connection.execute(
                """
                SELECT * FROM features
                WHERE deleted=0 AND point_x IS NOT NULL AND point_y IS NOT NULL
                  AND point_x BETWEEN ? AND ? AND point_y BETWEEN ? AND ?
                ORDER BY ((point_x-?)*(point_x-?))+((point_y-?)*(point_y-?))
                LIMIT ?
                """,
                (
                    float(origin[0] - max_distance),
                    float(origin[0] + max_distance),
                    float(origin[1] - max_distance),
                    float(origin[1] + max_distance),
                    float(origin[0]),
                    float(origin[0]),
                    float(origin[1]),
                    float(origin[1]),
                    limit * 2,
                ),
            ).fetchall()
        if rows:
            points = np.asarray(
                [
                    [
                        row["point_x"],
                        row["point_y"],
                        origin[2] if row["point_z"] is None else row["point_z"],
                    ]
                    for row in rows
                ],
                dtype=np.float64,
            )
            u, v, depth = project_points_equirectangular(
                points, origin, axes[0], axes[1], axes[2], 1, 1
            )
        else:
            points = np.empty((0, 3), dtype=np.float64)
            u = v = depth = np.empty(0, dtype=np.float64)
        projected: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not math.isfinite(float(depth[index])) or not 0.05 < depth[index] <= max_distance:
                continue
            projected.append(
                {
                    "feature_id": row["id"],
                    "u": float(u[index] % 1.0),
                    "v": float(min(1.0, max(0.0, v[index]))),
                    "depth": float(depth[index]),
                    "dataset_position": points[index].tolist(),
                    "z_inferred": row["point_z"] is None,
                    "properties": json.loads(row["properties_json"]),
                }
            )
            if len(projected) >= limit:
                break
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "layer_id": layer_id,
        "frame_id": frame_id,
        "coordinate_space": "normalized_equirectangular",
        "dataset_crs": manifest["dataset_crs"],
        "revision": revision,
        "items": projected,
        "count": len(projected),
        "yaw_offset_deg": resolved_yaw,
        "pitch_offset_deg": resolved_pitch,
    }


@router.post("/datasets/{dataset_id}/frames/{frame_id}/panorama-pick")
def panorama_pick(
    dataset_id: str,
    frame_id: str,
    payload: PanoramaPickRequest,
    request: Request,
) -> dict[str, Any]:
    import numpy as np

    from mms_shp_detection.geometry import pixel_to_world_ray

    from .media import _panorama_axes

    dataset = require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    try:
        origin = np.asarray(frame["task"].get("origin"), dtype=np.float64)
        if origin.shape != (3,) or not np.all(np.isfinite(origin)):
            raise ValueError("Frame has no valid dataset-space origin.")
        yaw = (
            float(request.app.state.panorama_yaw_offset_deg)
            if payload.yaw_offset_deg is None
            else payload.yaw_offset_deg
        )
        pitch = (
            float(request.app.state.panorama_pitch_offset_deg)
            if payload.pitch_offset_deg is None
            else payload.pitch_offset_deg
        )
        forward, right, up = _panorama_axes(
            frame["task"], yaw_offset_deg=yaw, pitch_offset_deg=pitch
        )
        ray = pixel_to_world_ray(payload.u, payload.v, 1, 1, forward, right, up)
        position = origin + (ray * payload.depth)
        transformer = _to_wgs84_transformer(str(dataset["crs"]))
        lon, lat = transformer.transform(float(position[0]), float(position[1]))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "dataset_position": position.tolist(),
        "dataset_crs": dataset["crs"],
        "wgs84": {
            "lon": float(lon),
            "lat": float(lat),
            "altitude": float(position[2]),
        },
        "source": {
            "u": payload.u,
            "v": payload.v,
            "depth": payload.depth,
            "yaw_offset_deg": yaw,
            "pitch_offset_deg": pitch,
        },
    }


@router.get("/datasets/{dataset_id}/overlays/{layer_id}/download")
async def download_edited_overlay(
    dataset_id: str,
    layer_id: str,
    request: Request,
) -> FileResponse:
    require_ready_dataset(request, dataset_id)
    temp_dir: Path | None = None
    try:
        layer_dir = _layer_directory(request.app, dataset_id, layer_id)
        manifest = _read_manifest(layer_dir)
        temp_dir = _temporary_download_dir(request.app, f"overlay-{layer_id}-")
        zip_path = await asyncio.to_thread(
            _write_edited_bundle, layer_dir, manifest, temp_dir
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="Overlay layer not found.") from exc
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


@router.get("/runs/{run_id}/shapefile")
async def download_result_shapefile(
    run_id: str,
    path: str,
    request: Request,
) -> FileResponse:
    run = request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run["status"] != "completed":
        raise HTTPException(status_code=409, detail="Run results are not ready for download.")
    temp_dir: Path | None = None
    try:
        primary = _result_shapefile(request.app, run, path)
        members = _bundle_files(primary)
        temp_dir = _temporary_download_dir(request.app, f"run-{run_id}-")
        zip_path = temp_dir / f"{primary.stem}.zip"
        await asyncio.to_thread(_write_zip_bundle, members, zip_path)
    except (FileNotFoundError, OSError, TypeError, UnsafePath, ValueError) as exc:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="Shapefile result not found.") from exc
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


@router.post("/runs/{run_id}/shapefile/import", status_code=status.HTTP_201_CREATED)
async def import_result_shapefile(
    run_id: str,
    payload: ResultImportRequest,
    request: Request,
) -> dict[str, Any]:
    run = request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run["status"] != "completed":
        raise HTTPException(status_code=409, detail="Run results are not ready to import.")
    dataset = require_ready_dataset(request, run["dataset_id"])
    try:
        primary = _result_shapefile(request.app, run, payload.path)
        members = _bundle_files(primary)
        layer_id = f"ov_{uuid.uuid4().hex}"
        root = _overlay_root(request.app, dataset["id"])
        staging = Path(tempfile.mkdtemp(prefix=f".{layer_id}-", dir=root))
        source = staging / "source"
        await asyncio.to_thread(_copy_bundle, members, source)
        manifest = await asyncio.to_thread(
            _import_bundle,
            request.app,
            dataset,
            staging,
            layer_id=layer_id,
            name=payload.name or primary.stem,
            supplied_crs=payload.crs,
            supplied_encoding=payload.encoding,
            source_kind="run_result",
            source_reference=f"run:{run_id}:{payload.path}",
        )
        final = root / layer_id
        staging.replace(final)
    except OverlayTooLarge as exc:
        if "staging" in locals():
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (FileNotFoundError, OSError, TypeError, UnsafePath, ValueError) as exc:
        if "staging" in locals():
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"layer": _public_layer(final, manifest)}
