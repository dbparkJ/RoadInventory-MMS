from __future__ import annotations

import csv
import datetime as dt
import math
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np


POSE_COLUMN_COUNT = 17
LEICA_JOB_DATE_PATTERN = re.compile(r"Job_(?P<date>\d{8})_(?P<time>\d{4})", re.IGNORECASE)
SUPPORTED_POSE_FORMATS = ("auto", "legacy", "leica-sphere")


def parse_image_timestamp(name: str) -> dt.datetime:
    stem = Path(name).stem
    return dt.datetime.strptime(stem, "%y%m%d_%H%M%S%f")


def _detect_delimiter(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ";" if sample.count(";") > sample.count(",") else ","


def _iter_rows(path: Path) -> Iterable[tuple[int, list[str]]]:
    delimiter = _detect_delimiter(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.reader(handle, delimiter=delimiter), start=1):
            if row and any(value.strip() for value in row):
                yield row_number, [value.strip() for value in row]


def _looks_like_image(value: str) -> bool:
    return Path(value.strip()).suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def _parse_number_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",")]


def parse_sphere_metadata(path: Path) -> dict[str, Any]:
    """Parse the Leica Sphere sidecar without guessing undocumented fields."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, raw_value = line.partition("=")
        if separator:
            values[key.strip()] = raw_value.strip()

    image_size = _parse_number_list(values["ImageSize"]) if "ImageSize" in values else None
    width_limits = _parse_number_list(values["WidthLimits"]) if "WidthLimits" in values else None
    height_limits = _parse_number_list(values["HeightLimits"]) if "HeightLimits" in values else None
    hotspot = _parse_number_list(values["PanoramaHotSpot"]) if "PanoramaHotSpot" in values else None
    return {
        "projection": "equirectangular",
        "sidecar_path": str(path.resolve()),
        "image_width": int(image_size[0]) if image_size and len(image_size) == 2 else None,
        "image_height": int(image_size[1]) if image_size and len(image_size) == 2 else None,
        "longitude_limits_deg": width_limits,
        "latitude_limits_deg": height_limits,
        # Leica's hotspot semantics are kept as provenance until the vendor defines
        # its reference frame. It must not silently become an angular offset.
        "panorama_hotspot": hotspot,
        "sphere_radius_m": float(values["SphereRadius"]) if "SphereRadius" in values else None,
    }


def _job_datetime_from_name(value: str) -> dt.datetime | None:
    match = LEICA_JOB_DATE_PATTERN.search(value)
    if match is None:
        return None
    return dt.datetime.strptime(
        f"{match.group('date')}_{match.group('time')}",
        "%Y%m%d_%H%M",
    ).replace(tzinfo=dt.timezone.utc)


def gps_sow_to_utc(
    seconds_of_week: float,
    *,
    job_name: str,
    gps_week: int | None = None,
    gps_utc_offset_seconds: int = 18,
) -> tuple[dt.datetime | None, int | None, bool]:
    """Convert GPS SOW to UTC, inferring only the week number from the job name.

    Returns ``(timestamp, week, inferred_week)``. When neither an explicit GPS
    week nor a parseable Leica job date is available, timestamp and week are None.
    """
    gps_epoch = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)
    inferred = False
    if gps_week is None:
        job_datetime = _job_datetime_from_name(job_name)
        if job_datetime is None:
            return None, None, False
        approximate_week = (job_datetime - gps_epoch).days // 7
        candidate_weeks = range(approximate_week - 1, approximate_week + 2)
        gps_week = min(
            candidate_weeks,
            key=lambda week: abs(
                (
                    gps_epoch
                    + dt.timedelta(weeks=week, seconds=seconds_of_week - gps_utc_offset_seconds)
                    - job_datetime
                ).total_seconds()
            ),
        )
        inferred = True

    timestamp = gps_epoch + dt.timedelta(
        weeks=gps_week,
        seconds=seconds_of_week - gps_utc_offset_seconds,
    )
    return timestamp, gps_week, inferred


def _validate_leica_rotation(rotation: np.ndarray, path: Path, row_number: int) -> None:
    orthogonality_error = float(np.max(np.abs((rotation.T @ rotation) - np.eye(3))))
    determinant = float(np.linalg.det(rotation))
    if orthogonality_error > 1e-5 or not math.isclose(determinant, 1.0, abs_tol=1e-5):
        raise ValueError(
            f"Invalid Leica orientation matrix at {path}:{row_number}: "
            f"orthogonality_error={orthogonality_error:.3g}, det={determinant:.9g}"
        )


def _scan_leica_sphere_tasks(
    data_root: Path,
    logger,
    *,
    gps_week: int | None,
    gps_utc_offset_seconds: int,
) -> list[dict[str, Any]]:
    csv_files = sorted(data_root.rglob("*_Sphere.csv"))
    logger.info("Found %d Leica Sphere pose CSV files under %s", len(csv_files), data_root)
    tasks: list[dict[str, Any]] = []

    for csv_path in csv_files:
        sphere_dir = csv_path.parent
        track_name = sphere_dir.parent.name
        job_name = sphere_dir.parent.parent.name
        record_name = f"{job_name}_{track_name}"
        sidecar_path = csv_path.with_suffix(".txt")
        panorama = parse_sphere_metadata(sidecar_path) if sidecar_path.is_file() else {
            "projection": "equirectangular",
            "sidecar_path": None,
            "image_width": None,
            "image_height": None,
            "longitude_limits_deg": [-180.0, 180.0],
            "latitude_limits_deg": [-90.0, 90.0],
            "panorama_hotspot": None,
            "sphere_radius_m": None,
        }

        for row_number, row in _iter_rows(csv_path):
            if not _looks_like_image(row[0]):
                # A header is permitted for future exports, although current Leica
                # Sphere CSVs are headerless.
                continue
            if len(row) != POSE_COLUMN_COUNT:
                logger.warning(
                    "Skipping malformed Leica Sphere row at %s:%d with %d columns",
                    csv_path,
                    row_number,
                    len(row),
                )
                continue

            try:
                gps_sow_seconds = float(row[1])
                origin = [float(value) for value in row[2:5]]
                omega_gon, phi_gon, kappa_gon = (float(value) for value in row[5:8])
                rotation = np.asarray([float(value) for value in row[8:17]], dtype=np.float64).reshape(3, 3)
                _validate_leica_rotation(rotation, csv_path, row_number)
            except ValueError as exc:
                logger.warning("Skipping invalid Leica Sphere row at %s:%d: %s", csv_path, row_number, exc)
                continue

            # Leica exports a local->world rotation. Its columns are right, up,
            # and camera-back; therefore panorama forward is -R[:, 2].
            right = rotation[:, 0]
            up = rotation[:, 1]
            direction = -rotation[:, 2]
            if not np.allclose(np.cross(direction, up), right, atol=1e-6):
                logger.warning("Skipping inconsistent Leica axes at %s:%d", csv_path, row_number)
                continue

            image_name = row[0]
            image_path = sphere_dir / image_name
            if not image_path.is_file():
                logger.warning("Image referenced by Leica Sphere CSV does not exist: %s", image_path)
                continue

            timestamp, resolved_week, inferred_week = gps_sow_to_utc(
                gps_sow_seconds,
                job_name=job_name,
                gps_week=gps_week,
                gps_utc_offset_seconds=gps_utc_offset_seconds,
            )
            timestamp_iso = timestamp.isoformat() if timestamp is not None else f"GPS_SOW:{gps_sow_seconds:.6f}"
            tasks.append(
                {
                    "image_path": str(image_path.resolve()),
                    "image_name": image_name,
                    "image_stem": Path(image_name).stem,
                    "timestamp_iso": timestamp_iso,
                    "timestamp_source": "gps_sow",
                    "gps_sow_seconds": gps_sow_seconds,
                    "gps_week": resolved_week,
                    "gps_week_inferred": inferred_week,
                    "gps_utc_offset_seconds": gps_utc_offset_seconds,
                    "route_id": job_name,
                    "job_name": job_name,
                    "track_name": track_name,
                    "record_name": record_name,
                    "pose_csv_path": str(csv_path.resolve()),
                    "pose_row_number": row_number,
                    "pose_format": "leica-sphere",
                    "origin": origin,
                    "direction": direction.tolist(),
                    "up": up.tolist(),
                    "right": right.tolist(),
                    "rotation_local_to_world": rotation.tolist(),
                    "omega_gon": omega_gon,
                    "phi_gon": phi_gon,
                    "kappa_gon": kappa_gon,
                    "panorama": panorama,
                }
            )

    return tasks


def _scan_legacy_tasks(data_root: Path, logger) -> list[dict[str, Any]]:
    cam_root = data_root / "CAM"
    if not cam_root.exists():
        raise FileNotFoundError(f"Camera root not found: {cam_root}")

    tasks: list[dict[str, Any]] = []
    csv_files = sorted(cam_root.rglob("*.csv"))
    logger.info("Found %d legacy pose CSV files under %s", len(csv_files), cam_root)

    for csv_path in csv_files:
        folder_name = csv_path.parent.parent.name
        route_id = folder_name.split("_")[-1]
        for row_number, row in _iter_rows(csv_path):
            if len(row) < POSE_COLUMN_COUNT or not _looks_like_image(row[1] if len(row) > 1 else ""):
                continue
            row = row[:POSE_COLUMN_COUNT]
            image_name = row[1]
            image_path = csv_path.parent / image_name
            if not image_path.is_file():
                logger.warning("Image referenced by pose CSV does not exist: %s", image_path)
                continue
            try:
                timestamp = parse_image_timestamp(image_name)
                task = {
                    "image_path": str(image_path.resolve()),
                    "image_name": image_name,
                    "image_stem": Path(image_name).stem,
                    "timestamp_iso": timestamp.isoformat(),
                    "timestamp_source": "filename",
                    "route_id": route_id,
                    "job_name": folder_name,
                    "track_name": None,
                    "record_name": folder_name,
                    "pose_csv_path": str(csv_path.resolve()),
                    "pose_row_number": row_number,
                    "pose_format": "legacy",
                    "origin": [float(row[2]), float(row[3]), float(row[4])],
                    "direction": [float(row[5]), float(row[6]), float(row[7])],
                    "up": [float(row[8]), float(row[9]), float(row[10])],
                    "roll_deg": float(row[11]),
                    "pitch_deg": float(row[12]),
                    "yaw_deg": float(row[13]),
                    "omega_deg": float(row[14]),
                    "phi_deg": float(row[15]),
                    "kappa_deg": float(row[16]),
                    "panorama": {
                        "projection": "equirectangular",
                        "sidecar_path": None,
                        "image_width": None,
                        "image_height": None,
                        "longitude_limits_deg": [-180.0, 180.0],
                        "latitude_limits_deg": [-90.0, 90.0],
                        "panorama_hotspot": None,
                        "sphere_radius_m": None,
                    },
                }
            except ValueError as exc:
                logger.warning("Skipping invalid legacy pose row at %s:%d: %s", csv_path, row_number, exc)
                continue
            tasks.append(task)
    return tasks


def scan_image_tasks(
    data_root: Path,
    logger,
    *,
    pose_format: str = "auto",
    gps_week: int | None = None,
    gps_utc_offset_seconds: int = 18,
) -> list[dict[str, Any]]:
    """Discover legacy MMS or Leica TRK500Neo Sphere image/pose records."""
    data_root = data_root.resolve()
    if pose_format not in SUPPORTED_POSE_FORMATS:
        raise ValueError(f"Unsupported pose format {pose_format!r}; choose from {SUPPORTED_POSE_FORMATS}")

    resolved_format = pose_format
    if resolved_format == "auto":
        if (data_root / "CAM").is_dir():
            resolved_format = "legacy"
        elif any(data_root.rglob("*_Sphere.csv")):
            resolved_format = "leica-sphere"
        else:
            raise FileNotFoundError(
                f"No legacy CAM folder or Leica *_Sphere.csv was found under {data_root}"
            )

    if resolved_format == "legacy":
        tasks = _scan_legacy_tasks(data_root, logger)
    else:
        tasks = _scan_leica_sphere_tasks(
            data_root,
            logger,
            gps_week=gps_week,
            gps_utc_offset_seconds=gps_utc_offset_seconds,
        )

    tasks.sort(key=lambda item: (item["timestamp_iso"], item["image_path"]))
    if not tasks:
        raise ValueError(f"No valid {resolved_format} image pose records were found under {data_root}")
    logger.info("Prepared %d image tasks using %s pose format.", len(tasks), resolved_format)
    return tasks
