from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, field_validator

from mms_shp_detection.dataset import scan_image_tasks

from .security import (
    UnsafePath,
    assert_no_symlink_descendants,
    normalize_relative_path,
    opaque_id,
    resolve_under_root,
)

router = APIRouter(prefix="/api", tags=["datasets"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root_id: str
    relative_path: str = ""
    crs: str | int | dict[str, Any] | None = None

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        try:
            return normalize_relative_path(value)
        except UnsafePath as exc:
            raise ValueError(str(exc)) from exc


class FrameLocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_name: str | None = None
    dataset_position: tuple[float, float] | None = None

    @field_validator("image_name")
    @classmethod
    def normalize_image_name(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("dataset_position")
    @classmethod
    def validate_dataset_position(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        if value is None:
            return None
        if not all(math.isfinite(float(coordinate)) for coordinate in value):
            raise ValueError("dataset_position must contain finite X/Y coordinates.")
        return (float(value[0]), float(value[1]))


def normalize_crs(value: str | int | dict[str, Any] | None) -> str:
    if value is None or value == "":
        raise ValueError(
            "CRS is required. Select the EPSG code supplied with the MMS delivery."
        )
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("EPSG code must be positive.")
        return f"EPSG:{value}"
    if isinstance(value, dict):
        candidate = value.get("epsg", value.get("code", value.get("value")))
        if candidate is None:
            raise ValueError("CRS object must contain epsg, code, or value.")
        return normalize_crs(candidate)
    text = str(value).strip()
    if not text:
        raise ValueError(
            "CRS is required. Select the EPSG code supplied with the MMS delivery."
        )
    if text.isdigit():
        text = f"EPSG:{text}"
    # Validate eagerly when pyproj is available.  The import remains local so a
    # WGS84-only server can still present a clear error in a minimal environment.
    try:
        from pyproj import CRS

        return CRS.from_user_input(text).to_string()
    except ImportError:
        if text.upper() not in {"EPSG:4326", "WGS84", "CRS84"}:
            raise ValueError("pyproj is required to transform this dataset CRS.")
        return "EPSG:4326"
    except Exception as exc:
        raise ValueError("The supplied CRS is not recognized.") from exc


def _configured_catalog_candidate(app: Any, dataset_root: Path) -> Path | None:
    config_path = app.state.config.pipeline_config_path
    if not config_path.is_file():
        return None
    try:
        import yaml

        from mms_shp_detection.config import _Yaml12SafeLoader

        document = yaml.load(
            config_path.read_text(encoding="utf-8-sig"),
            Loader=_Yaml12SafeLoader,
        ) or {}
        if not isinstance(document, dict):
            return None
        configured_root = _leaf_value(document, "data_root")
        configured_cache = _leaf_value(document, "pointcloud_cache_path")
        if not configured_root or not configured_cache:
            return None
        root_path = Path(str(configured_root)).expanduser()
        cache_path = Path(str(configured_cache)).expanduser()
        if not root_path.is_absolute():
            root_path = config_path.parent / root_path
        if not cache_path.is_absolute():
            cache_path = config_path.parent / cache_path
        if root_path.resolve(strict=False) != dataset_root.resolve(strict=True):
            return None
        return cache_path.resolve(strict=False)
    except Exception:
        return None


def _horizontal_crs(candidate: Any) -> Any:
    if getattr(candidate, "is_compound", False):
        projected = [item for item in candidate.sub_crs_list if item.is_projected]
        geographic = [item for item in candidate.sub_crs_list if item.is_geographic]
        return (projected or geographic or [candidate])[0]
    return candidate


def _unique_crs(candidates: list[Any]) -> list[Any]:
    unique: list[Any] = []
    for candidate in candidates:
        horizontal = _horizontal_crs(candidate)
        if not any(
            horizontal.equals(existing, ignore_axis_order=True)
            for existing in unique
        ):
            unique.append(horizontal)
    return unique


def _recover_vendor_las_crs(wkt: str, crs_type: Any) -> Any | None:
    """Recover a horizontal UTM CRS from a malformed vendor LAS WKT.

    Some Leica exports contain a damaged/non-ASCII PROJCS name and an
    unbalanced quote, so PROJ rejects the complete compound WKT even though
    its UTM parameters remain intact.  Recovery is deliberately narrow: only
    a WGS84 UTM name or a complete standard UTM parameter set is accepted.
    """

    text = str(wkt)[:1_000_000]
    upper = text.upper()
    if "WGS_1984" not in upper and "WGS 84" not in upper:
        return None

    named = re.search(r"UTM\s*(?:ZONE\s*)?([1-9]|[1-5][0-9]|60)\s*([NS])", upper)
    if named is not None:
        zone = int(named.group(1))
        north = named.group(2) == "N"
        try:
            return crs_type.from_epsg((32600 if north else 32700) + zone)
        except Exception:
            return None

    if "TRANSVERSE_MERCATOR" not in upper:
        return None

    def parameter(name: str) -> float | None:
        match = re.search(
            rf'PARAMETER\s*\[\s*"{name}"\s*,\s*([-+0-9.eE]+)',
            text,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    central_meridian = parameter("Central_Meridian")
    false_easting = parameter("False_Easting")
    false_northing = parameter("False_Northing")
    scale_factor = parameter("Scale_Factor")
    latitude_origin = parameter("Latitude_Of_Origin")
    if None in (
        central_meridian,
        false_easting,
        false_northing,
        scale_factor,
        latitude_origin,
    ):
        return None
    assert central_meridian is not None
    assert false_easting is not None
    assert false_northing is not None
    assert scale_factor is not None
    assert latitude_origin is not None
    zone = round((central_meridian + 183.0) / 6.0)
    expected_meridian = zone * 6.0 - 183.0
    if not (
        1 <= zone <= 60
        and abs(central_meridian - expected_meridian) < 1e-7
        and abs(false_easting - 500_000.0) < 1e-4
        and abs(scale_factor - 0.9996) < 1e-9
        and abs(latitude_origin) < 1e-9
    ):
        return None
    if abs(false_northing) < 1e-4:
        north = True
    elif abs(false_northing - 10_000_000.0) < 1e-4:
        north = False
    else:
        return None
    try:
        return crs_type.from_epsg((32600 if north else 32700) + zone)
    except Exception:
        return None


def _las_header_crs(header: Any, crs_type: Any) -> Any | None:
    try:
        parsed = header.parse_crs(prefer_wkt=True)
        if parsed is not None:
            return crs_type.from_user_input(parsed)
    except Exception:
        pass
    vlrs = [
        *list(getattr(header, "vlrs", ()) or ()),
        *list(getattr(header, "evlrs", ()) or ()),
    ]
    for vlr in vlrs:
        raw_wkt = getattr(vlr, "string", None)
        if raw_wkt:
            recovered = _recover_vendor_las_crs(str(raw_wkt), crs_type)
            if recovered is not None:
                return recovered
    return None


def detect_dataset_crs(app: Any, dataset_root: Path) -> str:
    """Detect one authoritative CRS from a matching catalog, PRJ, or LAS header."""

    assert_no_symlink_descendants(dataset_root)
    try:
        from pyproj import CRS
        from pyproj.exceptions import CRSError
    except ImportError as exc:
        raise ValueError(
            "CRS could not be auto-detected because pyproj is not installed; enter an EPSG code."
        ) from exc

    catalog_crs: list[Any] = []
    catalog_candidates = [
        _configured_catalog_candidate(app, dataset_root),
        app.state.config.project_root / ".cache" / "pointcloud_catalog.json",
    ]
    for catalog_candidate in catalog_candidates:
        if catalog_candidate is None:
            continue
        try:
            payload = json.loads(catalog_candidate.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("data_root")
                and Path(str(payload["data_root"])).resolve(strict=False)
                == dataset_root.resolve(strict=True)
                and (payload.get("crs_wkt") or payload.get("wkt"))
            ):
                catalog_crs.append(
                    CRS.from_user_input(payload.get("crs_wkt") or payload["wkt"])
                )
        except (OSError, ValueError, CRSError, json.JSONDecodeError):
            continue

    prj_crs: list[Any] = []
    prj_paths = sorted(
        (path for path in dataset_root.rglob("*") if path.is_file() and path.suffix.casefold() == ".prj"),
        key=lambda path: str(path).casefold(),
    )
    if len(prj_paths) > 200:
        raise ValueError(
            "Too many PRJ files to auto-detect a unique CRS safely; enter the delivery EPSG code."
        )
    for path in prj_paths[:200]:
        try:
            prj_crs.append(CRS.from_wkt(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeError, ValueError, CRSError):
            continue

    # LAS/LAZ is the coordinate source used by the point/panorama workspace.
    # Inspect it even if unrelated trajectory SHP files have WGS84 PRJ files.
    las_crs: list[Any] = []
    try:
        import laspy

        las_paths = sorted(
            (
                path
                for path in dataset_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in {".las", ".laz"}
            ),
            key=lambda path: str(path).casefold(),
        )
        if len(las_paths) > 200:
            raise ValueError(
                "Too many LAS/LAZ headers to auto-detect a unique CRS safely; "
                "enter the delivery EPSG code."
            )
        for path in las_paths[:200]:
            try:
                with laspy.open(path) as reader:
                    parsed = _las_header_crs(reader.header, CRS)
                if parsed is not None:
                    las_crs.append(parsed)
            except Exception:
                continue
    except ImportError:
        pass

    selected_source = ""
    unique: list[Any] = []
    for source_name, source_candidates in (
        ("matching point-cloud catalog", catalog_crs),
        ("LAS/LAZ headers", las_crs),
        ("PRJ sidecars", prj_crs),
    ):
        source_unique = _unique_crs(source_candidates)
        if source_unique:
            selected_source = source_name
            unique = source_unique
            break
    if not unique:
        raise ValueError(
            "CRS could not be detected from a matching catalog, PRJ, or LAS header. "
            "Enter the delivery EPSG code."
        )
    if len(unique) > 1:
        authorities = [
            ":".join(item.to_authority()) if item.to_authority() else item.name
            for item in unique
        ]
        raise ValueError(
            f"Multiple horizontal CRSs were detected in {selected_source} "
            f"({', '.join(authorities)}); select the correct CRS explicitly."
        )
    authority = unique[0].to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else unique[0].to_string()


def _transformer(crs: str):
    if crs.upper() in {"EPSG:4326", "WGS84", "CRS84", "OGC:CRS84"}:
        return None
    try:
        from pyproj import Transformer

        return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    except ImportError as exc:
        raise RuntimeError("pyproj is required to transform projected MMS coordinates.") from exc
    except Exception as exc:
        raise ValueError("The dataset CRS cannot be transformed to WGS84.") from exc


def _heading(task: dict[str, Any]) -> float | None:
    direction = task.get("direction")
    if not isinstance(direction, (list, tuple)) or len(direction) < 2:
        return None
    try:
        east, north = float(direction[0]), float(direction[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(east) and math.isfinite(north)) or math.hypot(east, north) < 1e-9:
        return None
    return (math.degrees(math.atan2(east, north)) + 360.0) % 360.0


def _wgs84_distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = (math.radians(value) for value in first)
    lon2, lat2 = (math.radians(value) for value in second)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    haversine = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(haversine)))


def _safe_scan_error(exc: BaseException, dataset_root: Path, project_root: Path) -> str:
    text = str(exc) or type(exc).__name__
    for sensitive in (str(dataset_root), str(dataset_root.resolve()), str(project_root.resolve())):
        text = text.replace(sensitive, "<dataset>")
        text = text.replace(sensitive.replace("\\", "/"), "<dataset>")
    return text[:1000]


def _prepare_scan(
    dataset_id: str,
    dataset_root: Path,
    crs: str,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[float] | None, list[str]]:
    assert_no_symlink_descendants(dataset_root)
    tasks = scan_image_tasks(dataset_root, logger, pose_format="auto")
    transformer = _transformer(crs)
    tracks_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    prepared: list[dict[str, Any]] = []
    warnings: list[str] = []
    coordinates: list[tuple[float, float]] = []

    for ordinal, task in enumerate(tasks):
        job_name = str(task.get("job_name") or "")
        track_name = str(task.get("track_name") or "")
        record_name = str(task.get("record_name") or job_name or "Record")
        track_key = (job_name, track_name, record_name)
        track = tracks_by_key.get(track_key)
        if track is None:
            track_id = opaque_id("t", dataset_id, *track_key)
            display_name = track_name or record_name or f"Track {len(tracks_by_key) + 1}"
            track = {
                "id": track_id,
                "name": display_name,
                "job_name": job_name or None,
                "record_name": record_name or None,
                "frame_count": 0,
            }
            tracks_by_key[track_key] = track
        track["frame_count"] += 1

        origin = task.get("origin") or []
        longitude = latitude = altitude = None
        if len(origin) >= 2:
            try:
                x, y = float(origin[0]), float(origin[1])
                altitude = float(origin[2]) if len(origin) > 2 else None
                if transformer is None:
                    longitude, latitude = x, y
                else:
                    longitude, latitude = transformer.transform(x, y)
                if not (
                    math.isfinite(longitude)
                    and math.isfinite(latitude)
                    and -180.0 <= longitude <= 180.0
                    and -90.0 <= latitude <= 90.0
                ):
                    raise ValueError("coordinate outside WGS84 bounds")
                coordinates.append((longitude, latitude))
            except (TypeError, ValueError, OverflowError) as exc:
                warnings.append(
                    f"Frame {ordinal + 1} has no map coordinate after CRS conversion: {exc}"
                )
                longitude = latitude = altitude = None

        image_path = Path(str(task["image_path"])).resolve()
        try:
            relative_image = image_path.relative_to(dataset_root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("A discovered panorama escaped the selected dataset folder.") from exc
        frame_id = opaque_id(
            "f",
            dataset_id,
            relative_image.casefold(),
            task.get("pose_row_number", ordinal),
        )
        prepared.append(
            {
                "id": frame_id,
                "ordinal": ordinal,
                "track_id": track["id"],
                "task": task,
                "longitude": longitude,
                "latitude": latitude,
                "altitude": altitude,
                "heading": _heading(task),
            }
        )

    bbox = None
    if coordinates:
        longitude_values = [item[0] for item in coordinates]
        latitude_values = [item[1] for item in coordinates]
        bbox = [
            min(longitude_values),
            min(latitude_values),
            max(longitude_values),
            max(latitude_values),
        ]
    # Avoid turning a malformed CRS into a deceptively empty route.
    if not coordinates:
        raise ValueError("No frame coordinate could be transformed to WGS84 with the selected CRS.")
    tracks = list(tracks_by_key.values())
    for track in tracks:
        track_coordinates = [
            (float(frame["longitude"]), float(frame["latitude"]))
            for frame in prepared
            if frame["track_id"] == track["id"]
            and frame.get("longitude") is not None
            and frame.get("latitude") is not None
        ]
        track["distance_m"] = round(
            sum(
                _wgs84_distance_m(first, second)
                for first, second in zip(track_coordinates, track_coordinates[1:])
            ),
            2,
        )
    return prepared, tracks, bbox, warnings[:100]


async def run_dataset_scan(app: Any, dataset_id: str) -> None:
    store = app.state.store
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        return
    root = app.state.storage_roots_by_id.get(dataset["root_id"])
    if root is None:
        store.fail_dataset_scan(dataset_id, "The configured storage root is no longer available.", utc_now())
        return
    try:
        dataset_root = resolve_under_root(
            root.path,
            dataset["relative_path"],
            must_exist=True,
            expect_directory=True,
        )
        prepared, tracks, bbox, warnings = await asyncio.to_thread(
            _prepare_scan,
            dataset_id,
            dataset_root,
            dataset["crs"],
            app.state.logger,
        )
        store.finish_dataset_scan(
            dataset_id,
            frames=prepared,
            tracks=tracks,
            bbox=bbox,
            warnings=warnings,
            now=utc_now(),
        )
    except BaseException as exc:
        try:
            dataset_root
        except UnboundLocalError:
            dataset_root = root.path
        app.state.logger.exception("Dataset scan failed for %s", dataset_id)
        store.fail_dataset_scan(
            dataset_id,
            _safe_scan_error(exc, dataset_root, app.state.config.project_root),
            utc_now(),
        )
    finally:
        app.state.scan_tasks.pop(dataset_id, None)


def schedule_scan(app: Any, dataset_id: str) -> None:
    existing = app.state.scan_tasks.get(dataset_id)
    if existing is not None and not existing.done():
        return
    app.state.scan_tasks[dataset_id] = asyncio.create_task(
        run_dataset_scan(app, dataset_id),
        name=f"mms-dataset-scan-{dataset_id}",
    )


def public_dataset(item: dict[str, Any]) -> dict[str, Any]:
    status_map = {"scanning": "indexing", "ready": "ready", "error": "error"}
    result = {
        "id": item["id"],
        "name": item["name"],
        "relative_path": item.get("relative_path", ""),
        "status": status_map.get(item["status"], item["status"]),
        "frame_count": int(item.get("frame_count") or 0),
        "tracks": [
            {
                "id": track["id"],
                "name": track["name"],
                "frame_count": int(track.get("frame_count") or 0),
                "distance_m": float(track.get("distance_m") or 0.0),
                **(
                    {"job_name": track["job_name"]}
                    if track.get("job_name")
                    else {}
                ),
                **(
                    {"record_name": track["record_name"]}
                    if track.get("record_name")
                    else {}
                ),
            }
            for track in item.get("tracks", [])
        ],
        "bbox": item.get("bbox"),
        "bounds": item.get("bbox"),
        "distance_m": round(
            sum(float(track.get("distance_m") or 0.0) for track in item.get("tracks", [])),
            2,
        ),
        "crs": item["crs"],
        "warnings": item.get("warnings", []),
        "catalog_status": item.get("catalog_status", "missing"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }
    if item.get("error"):
        result["error"] = item["error"]
    if item.get("catalog_error"):
        result["catalog_error"] = item["catalog_error"]
    return result


def public_frame(item: dict[str, Any]) -> dict[str, Any]:
    task = item["task"]
    dataset_position = None
    raw_origin = task.get("origin")
    if isinstance(raw_origin, (list, tuple)) and len(raw_origin) >= 3:
        try:
            candidate = [float(raw_origin[index]) for index in range(3)]
            if all(math.isfinite(value) for value in candidate):
                dataset_position = candidate
        except (TypeError, ValueError):
            pass
    coordinate = None
    if item.get("longitude") is not None and item.get("latitude") is not None:
        coordinate = {
            "lon": item["longitude"],
            "lat": item["latitude"],
            **(
                {"altitude": item["altitude"]}
                if item.get("altitude") is not None
                else {}
            ),
        }
    frame = {
        "id": item["id"],
        "index": int(item["ordinal"]),
        "track_id": item["track_id"],
        "image_name": str(task.get("image_name") or "Panorama"),
        "timestamp": task.get("timestamp_iso"),
        "coordinate": coordinate,
        "heading": item.get("heading"),
        "dataset_position": dataset_position,
        "has_panorama": bool(task.get("image_path")),
        "has_points": True,
        "panorama_url": f"/api/datasets/{item['dataset_id']}/panoramas/{item['id']}",
        "point_url": f"/api/datasets/{item['dataset_id']}/points/{item['id']}",
    }
    # Compatibility for clients written against the early contract.
    if coordinate is not None:
        frame["position"] = [
            coordinate["lon"],
            coordinate["lat"],
            coordinate.get("altitude"),
        ]
    return frame


def require_ready_dataset(request: Request, dataset_id: str) -> dict[str, Any]:
    dataset = request.app.state.store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    if dataset["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Dataset is currently {public_dataset(dataset)['status']}.",
        )
    return dataset


@router.post("/datasets/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_dataset(payload: ScanRequest, request: Request) -> dict[str, Any]:
    root = request.app.state.storage_roots_by_id.get(payload.root_id)
    if root is None:
        raise HTTPException(status_code=404, detail="Storage root not found.")
    try:
        relative_path = normalize_relative_path(payload.relative_path)
        target = resolve_under_root(
            root.path,
            relative_path,
            must_exist=True,
            expect_directory=True,
        )
        if payload.crs is None or payload.crs == "":
            crs = await asyncio.to_thread(
                detect_dataset_crs,
                request.app,
                target,
            )
        else:
            await asyncio.to_thread(assert_no_symlink_descendants, target)
            crs = normalize_crs(payload.crs)
    except (UnsafePath, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    store = request.app.state.store
    existing = store.find_dataset(payload.root_id, relative_path, crs)
    dataset_id = (
        existing["id"]
        if existing is not None
        else opaque_id("d", payload.root_id, relative_path.casefold(), crs)
    )
    now = utc_now()
    existing_catalog_task = request.app.state.catalog_tasks.get(dataset_id)
    if existing_catalog_task is not None and not existing_catalog_task.done():
        existing_catalog_task.cancel()
    store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name=target.name or root.label,
        root_id=payload.root_id,
        relative_path=relative_path,
        crs=crs,
        now=now,
    )
    request.app.state.catalogs.pop(dataset_id, None)
    schedule_scan(request.app, dataset_id)
    return public_dataset(store.get_dataset(dataset_id))  # type: ignore[arg-type]


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
    dataset = request.app.state.store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return public_dataset(dataset)


async def _unregister_dataset_locked(
    dataset_id: str, request: Request
) -> dict[str, Any]:
    """Remove a dataset from the workspace registry, never its source folder."""

    store = request.app.state.store
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    active_run = store.active_run_for_dataset(dataset_id)
    if active_run is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Dataset cannot be removed while run "
                f"{active_run['id']} is {active_run['status']}."
            ),
        )

    # Index/catalog tasks do not mutate source files.  Stop and drain them
    # before clearing indexed frames so they cannot repopulate a hidden row.
    pending_tasks = []
    for registry in (request.app.state.scan_tasks, request.app.state.catalog_tasks):
        task = registry.get(dataset_id)
        if task is not None and not task.done():
            task.cancel()
            pending_tasks.append(task)
    if pending_tasks:
        await asyncio.gather(*pending_tasks, return_exceptions=True)

    from .task_resolution_outbox import reconcile_dataset_task_resolutions

    reconciliation = await asyncio.to_thread(
        reconcile_dataset_task_resolutions, request.app, dataset_id
    )
    if (
        reconciliation["pending"]
        or reconciliation["error"]
        or reconciliation["truncated"]
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Dataset has unresolved or unverified review task transitions."
                ),
                "pending_task_resolutions": reconciliation["pending"],
                "task_resolution_errors": reconciliation["error"],
                "task_resolution_scan_truncated": int(
                    bool(reconciliation["truncated"])
                ),
            },
        )

    result = store.unregister_dataset(dataset_id, now=utc_now())
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Dataset not found.")
    if result["status"] == "active_run":
        raise HTTPException(
            status_code=409,
            detail=(
                "Dataset cannot be removed while run "
                f"{result['run_id']} is {result['run_status']}."
            ),
        )
    if result["status"] == "review_work":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Dataset has non-terminal review work.",
                "open_review_sessions": result["open_sessions"],
                "open_review_tasks": result["open_tasks"],
            },
        )
    request.app.state.catalogs.pop(dataset_id, None)
    return {
        "id": dataset_id,
        "removed": True,
        "source_deleted": False,
        "detail": "Dataset was removed from the workspace; source files were preserved.",
    }


@router.delete("/datasets/{dataset_id}")
async def unregister_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
    from .task_resolution_outbox import review_dataset_lock

    async with review_dataset_lock(request.app, dataset_id):
        return await _unregister_dataset_locked(dataset_id, request)


@router.get("/datasets/{dataset_id}/frames")
async def get_frames(
    dataset_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    track: str | None = None,
) -> dict[str, Any]:
    require_ready_dataset(request, dataset_id)
    frames, total = request.app.state.store.list_frames(
        dataset_id, offset=offset, limit=limit, track_id=track
    )
    items = [public_frame(item) for item in frames]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {
        "items": items,
        "frames": items,
        "offset": offset,
        "limit": limit,
        "total": total,
        "next_offset": next_offset,
    }


@router.get("/datasets/{dataset_id}/frames/{frame_id}")
def get_frame(
    dataset_id: str,
    frame_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return one opaque frame and a track-relative page offset for navigation."""

    require_ready_dataset(request, dataset_id)
    frame = request.app.state.store.get_frame(dataset_id, frame_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found.")
    track_offset = request.app.state.store.frame_offset_in_track(
        dataset_id,
        track_id=str(frame["track_id"]),
        ordinal=int(frame["ordinal"]),
    )
    return {
        "frame": public_frame(frame),
        "page_offset": max(0, track_offset - 120),
    }


@router.post("/datasets/{dataset_id}/frames/locate")
def locate_frame(
    dataset_id: str,
    payload: FrameLocateRequest,
    request: Request,
) -> dict[str, Any]:
    """Resolve a SHP detection to its source image, falling back to nearest pose."""

    require_ready_dataset(request, dataset_id)
    if payload.image_name is None and payload.dataset_position is None:
        raise HTTPException(
            status_code=422,
            detail="image_name or dataset_position is required.",
        )
    frame = None
    match = "image_name"
    if payload.image_name is not None:
        frame = request.app.state.store.locate_frame(
            dataset_id,
            image_name=payload.image_name,
        )
    if frame is None and payload.dataset_position is not None:
        frame = request.app.state.store.locate_frame(
            dataset_id,
            dataset_position=payload.dataset_position,
        )
        match = "nearest_position"
    if frame is None:
        raise HTTPException(status_code=404, detail="A matching MMS frame was not found.")
    # The web client reloads the located frame with the returned track filter.
    # Therefore this offset must be relative to that track, not to the global
    # dataset ordinal.  A global offset can leave the selected frame outside
    # the reloaded page and disable previous/next-frame shortcuts indefinitely.
    track_offset = request.app.state.store.frame_offset_in_track(
        dataset_id,
        track_id=str(frame["track_id"]),
        ordinal=int(frame["ordinal"]),
    )
    return {
        "frame": public_frame(frame),
        "page_offset": max(0, track_offset - 120),
        "match": match,
    }


@router.get("/datasets/{dataset_id}/route")
async def get_route(dataset_id: str, request: Request) -> dict[str, Any]:
    dataset = require_ready_dataset(request, dataset_id)
    tracks = dataset.get("tracks", [])
    frames = await asyncio.to_thread(
        request.app.state.store.sample_route_frames,
        dataset_id,
        track_ids=tuple(track["id"] for track in tracks),
        max_points=request.app.state.config.max_route_points,
    )
    grouped: dict[str, list[dict[str, Any]]] = {
        track["id"]: [] for track in tracks
    }
    for frame in frames:
        point = {
            "lon": frame["longitude"],
            "lat": frame["latitude"],
            **(
                {"altitude": frame["altitude"]}
                if frame.get("altitude") is not None
                else {}
            ),
            "frame_id": frame["id"],
            "track_id": frame["track_id"],
            "index": frame["ordinal"],
            **(
                {"heading": frame["heading"]}
                if frame.get("heading") is not None
                else {}
            ),
        }
        grouped.setdefault(frame["track_id"], []).append(point)
    points = [
        point
        for track in tracks
        for point in grouped.get(track["id"], [])
    ]
    features = []
    for track in tracks:
        track_points = grouped.get(track["id"], [])
        if not track_points:
            continue
        coordinates = [
            [point["lon"], point["lat"], point.get("altitude", 0.0)]
            for point in track_points
        ]
        geometry: dict[str, Any]
        if len(coordinates) == 1:
            geometry = {"type": "Point", "coordinates": coordinates[0]}
        else:
            geometry = {"type": "LineString", "coordinates": coordinates}
        features.append(
            {
                "type": "Feature",
                "id": track["id"],
                "properties": {
                    "track_id": track["id"],
                    "name": track["name"],
                    "frame_count": len(track_points),
                },
                "geometry": geometry,
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "points": points,
        "bbox": dataset.get("bbox"),
    }


def catalog_path(app: Any, dataset_id: str) -> Path:
    return app.state.config.state_dir / "catalogs" / f"{dataset_id}.json"


def _leaf_value(document: dict[str, Any], target: str) -> Any:
    found: list[Any] = []

    def visit(node: dict[str, Any]) -> None:
        for key, value in node.items():
            if key.replace("-", "_") == target:
                found.append(value)
            if isinstance(value, dict):
                visit(value)

    visit(document)
    return found[0] if len(found) == 1 else None


def seed_catalog_cache(
    app: Any,
    dataset_root: Path,
    destination: Path,
    *,
    preferred: Path | None = None,
) -> str | None:
    """Atomically seed a per-dataset/job catalog from a matching core cache.

    The core builder still revalidates every signature after this copy.  Seeding
    only preserves expensive per-file indexes and never treats the copied cache
    as authoritative by itself.
    """

    resolved_root = dataset_root.resolve(strict=True)
    candidates: list[tuple[str, Path]] = []
    if preferred is not None:
        candidates.append(("dataset_catalog", preferred))
    config_path = app.state.config.pipeline_config_path
    if config_path.is_file():
        try:
            import yaml

            from mms_shp_detection.config import _Yaml12SafeLoader

            document = yaml.load(
                config_path.read_text(encoding="utf-8-sig"),
                Loader=_Yaml12SafeLoader,
            ) or {}
            if isinstance(document, dict):
                configured_root = _leaf_value(document, "data_root")
                configured_cache = _leaf_value(document, "pointcloud_cache_path")
                if configured_root and configured_cache:
                    root_path = Path(str(configured_root)).expanduser()
                    cache_path = Path(str(configured_cache)).expanduser()
                    if not root_path.is_absolute():
                        root_path = config_path.parent / root_path
                    if not cache_path.is_absolute():
                        cache_path = config_path.parent / cache_path
                    if root_path.resolve(strict=False) == resolved_root:
                        candidates.append(("pipeline_catalog", cache_path.resolve(strict=False)))
        except Exception:
            app.state.logger.warning("Could not inspect the base point-cloud cache for seeding.")
    candidates.append(
        ("project_catalog", app.state.config.project_root / ".cache" / "pointcloud_catalog.json")
    )

    destination = destination.resolve(strict=False)
    for provenance, candidate in candidates:
        try:
            source = candidate.resolve(strict=True)
            if source == destination or not source.is_file() or source.is_symlink():
                continue
            if source.stat().st_size > 512 * 1024**2:
                continue
            cached = json.loads(source.read_text(encoding="utf-8"))
            cached_root = cached.get("data_root") if isinstance(cached, dict) else None
            if not cached_root or Path(str(cached_root)).resolve(strict=False) != resolved_root:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.seed")
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            return provenance
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


async def build_catalog(app: Any, dataset_id: str) -> None:
    from mms_shp_detection.pointcloud import build_pointcloud_catalog

    dataset = app.state.store.get_dataset(dataset_id)
    if dataset is None:
        return
    root = app.state.storage_roots_by_id.get(dataset["root_id"])
    if root is None:
        return
    try:
        dataset_root = resolve_under_root(
            root.path,
            dataset["relative_path"],
            must_exist=True,
            expect_directory=True,
        )
        app.state.store.set_catalog_status(
            dataset_id, "building", error=None, now=utc_now()
        )
        destination = catalog_path(app, dataset_id)
        if not destination.is_file():
            await asyncio.to_thread(
                seed_catalog_cache,
                app,
                dataset_root,
                destination,
            )
        jobs = sorted(
            {
                str(track.get("job_name"))
                for track in dataset.get("tracks", [])
                if track.get("job_name")
            }
        )
        catalog = await asyncio.to_thread(
            build_pointcloud_catalog,
            dataset_root,
            destination,
            app.state.logger,
            source="auto",
            include_jobs=jobs or None,
            reject_symlinks=True,
        )
        app.state.catalogs[dataset_id] = catalog
        app.state.store.set_catalog_status(
            dataset_id, "ready", error=None, now=utc_now()
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        app.state.logger.exception("Point-cloud catalog failed for %s", dataset_id)
        dataset_root = locals().get("dataset_root", root.path)
        app.state.store.set_catalog_status(
            dataset_id,
            "error",
            error=_safe_scan_error(exc, dataset_root, app.state.config.project_root),
            now=utc_now(),
        )
    finally:
        app.state.catalog_tasks.pop(dataset_id, None)


def schedule_catalog(app: Any, dataset_id: str) -> None:
    existing = app.state.catalog_tasks.get(dataset_id)
    if existing is not None and not existing.done():
        return
    app.state.catalog_tasks[dataset_id] = asyncio.create_task(
        build_catalog(app, dataset_id),
        name=f"mms-point-catalog-{dataset_id}",
    )
