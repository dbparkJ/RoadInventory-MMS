from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


LEICA_CALIBRATION_XOR_KEY = b"BFqjcI26rmlNV70EXD7Oh+Y8VDn"
CALIBRATION_SCHEMA_VERSION = 2


def _readonly_connection(path: Path) -> sqlite3.Connection:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _number(text: str | None) -> int | float | str | None:
    if text is None or not text.strip():
        return None
    value = text.strip()
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _child_values(element: ET.Element | None) -> dict[str, Any] | None:
    if element is None:
        return None
    return {child.tag: _number(child.text) for child in element}


def _xml_text(value: str | bytes | None, field_name: str) -> str:
    if value is None:
        raise ValueError(f"Missing {field_name} in SCAN.IMAGING_SENSORS")
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _camera_elements(root: ET.Element) -> list[ET.Element]:
    return [item for item in root.iter("Camera") if item.find("Name") is not None]


def parse_camera_calibration(scan_db_path: Path) -> dict[str, Any]:
    """Read fixed camera intrinsics and boresight extrinsics from a Leica scan.db."""
    with _readonly_connection(scan_db_path) as connection:
        rows = connection.execute(
            """
            SELECT ID, FRIENDLY_NAME, INNER_ORIENTATION, OUTER_ORIENTATION
            FROM [SCAN.IMAGING_SENSORS]
            ORDER BY ID
            """
        ).fetchall()

    sensors: list[dict[str, Any]] = []
    for sensor_id, friendly_name, inner_value, outer_value in rows:
        inner_root = ET.fromstring(_xml_text(inner_value, "INNER_ORIENTATION"))
        outer_root = ET.fromstring(_xml_text(outer_value, "OUTER_ORIENTATION"))
        outer_by_serial = {
            item.findtext("SN"): item for item in _camera_elements(outer_root)
        }

        cameras: list[dict[str, Any]] = []
        for camera in _camera_elements(inner_root):
            serial = camera.findtext("SN")
            calib = camera.find("Calib")
            outer_camera = outer_by_serial.get(serial)
            outer_calib = outer_camera.find("Calib") if outer_camera is not None else None
            cameras.append(
                {
                    "id": _number(camera.findtext("ID")),
                    "name": camera.findtext("Name"),
                    "serial": serial,
                    "width": _number(camera.findtext("Width")),
                    "height": _number(camera.findtext("Height")),
                    "status": camera.findtext("Status"),
                    "intrinsic": {
                        "created_at": calib.findtext("CreatedAt") if calib is not None else None,
                        "calibration_type": calib.findtext("Type") if calib is not None else None,
                        "images_processed": _number(calib.findtext("ImagesProcessed"))
                        if calib is not None
                        else None,
                        "model": _child_values(calib.find("Model")) if calib is not None else None,
                        "distortion": _child_values(calib.find("Distortion"))
                        if calib is not None
                        else None,
                        "boresight_internal": _child_values(calib.find("BoresightInternal"))
                        if calib is not None
                        else None,
                    },
                    "extrinsic": _child_values(outer_calib.find("BoresightExternal"))
                    if outer_calib is not None
                    else None,
                }
            )

        sensors.append(
            {
                "sensor_id": int(sensor_id),
                "friendly_name": friendly_name,
                "output_width": _number(inner_root.findtext("Width")),
                "output_height": _number(inner_root.findtext("Height")),
                "output_model": inner_root.findtext("./Calib/Model/Type"),
                "cameras": cameras,
            }
        )

    return {"scan_db": str(scan_db_path.resolve()), "imaging_sensors": sensors}


def _decode_leica_calibration_value(value: bytes) -> str | None:
    """Decode Leica's obfuscated numeric calibration text.

    Pegasus replaces encrypted NUL bytes with 0xff. The original raw bytes are
    always retained by :func:`_serialize_leica_value` for auditability.
    """
    decoded = bytes(
        (0 if byte == 0xFF else byte) ^ LEICA_CALIBRATION_XOR_KEY[index % len(LEICA_CALIBRATION_XOR_KEY)]
        for index, byte in enumerate(value)
    )
    try:
        text = decoded.decode("ascii")
        [float(item) for item in text.split(",")]
    except (UnicodeDecodeError, ValueError):
        return None
    return text


def _serialize_leica_value(value: bytes | str | None, key_name: str | None = None) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        decoded = None
    if decoded is not None and all(char.isprintable() or char.isspace() for char in decoded):
        return decoded
    payload: dict[str, Any] = {
        "encoding": "base64",
        "value": base64.b64encode(value).decode("ascii"),
        "hex": value.hex(),
    }
    decoded = _decode_leica_calibration_value(value)
    if decoded is not None:
        payload["decoded"] = decoded
        payload["numeric_values"] = [float(item) for item in decoded.split(",")]
        if key_name == "Distance":
            payload["unit"] = "m"
        elif key_name in {"Angles", "Mounting"}:
            payload["unit"] = "deg"
        if "unit" in payload:
            payload["unit_source"] = "LeicaField log cross-check; DB has no unit metadata"
    return payload


def parse_lidar_calibration(job_db_path: Path) -> dict[str, Any]:
    """Read Laser-to-IMU calibration records without losing Leica binary values."""
    with _readonly_connection(job_db_path) as connection:
        connection.text_factory = bytes
        rows = connection.execute(
            """
            SELECT SECTION, KEYNAME, KEYVALUE
            FROM [JOB.SENSORCALIBRATION]
            ORDER BY SECTION, KEYNAME
            """
        ).fetchall()

    sections: dict[str, dict[str, Any]] = {}
    for section_value, key_value, calibration_value in rows:
        section = section_value.decode("utf-8")
        key = key_value.decode("utf-8")
        sections.setdefault(section, {})[key] = _serialize_leica_value(calibration_value, key)
    return {"job_db": str(job_db_path.resolve()), "laser_to_imu": sections}


def parse_job_metadata(job_db_path: Path) -> dict[str, Any]:
    """Read timing metadata needed to interpret Leica GPS week-time values."""
    with _readonly_connection(job_db_path) as connection:
        row = connection.execute(
            """
            SELECT KEYVALUE
            FROM [JOB.SETTINGS]
            WHERE SECTION = 'User Data' AND KEYNAME = 'GPSWeek'
            """
        ).fetchone()
    return {
        "gps_week": int(row[0]) if row is not None and row[0] not in (None, "") else None,
        "time_scale": "GPS",
        "time_value": "seconds_of_week",
    }


def extract_project_calibrations(root: Path) -> dict[str, Any]:
    """Extract calibration snapshots for every Track*.scan found below root."""
    root = root.resolve()
    scan_paths = sorted(root.rglob("scan.db"))
    tracks: list[dict[str, Any]] = []
    for scan_path in scan_paths:
        if not scan_path.parent.name.lower().endswith(".scan"):
            continue
        job_dir = scan_path.parent.parent
        job_db_path = job_dir / "job.db"
        tracks.append(
            {
                "job": job_dir.name,
                "track": scan_path.parent.name,
                "camera": parse_camera_calibration(scan_path),
                "lidar": parse_lidar_calibration(job_db_path) if job_db_path.is_file() else None,
                "time": parse_job_metadata(job_db_path) if job_db_path.is_file() else None,
            }
        )
    if not tracks:
        raise FileNotFoundError(f"No Track*.scan/scan.db found under {root}")
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "source_root": str(root),
        "sphere_processing": {
            "projection": "equirectangular",
            "raw_camera_intrinsic_extrinsic_application": "already_applied_by_leica_export",
            "pose_source": "Sphere CSV frame XYZ and local-to-world rotation",
        },
        "tracks": tracks,
    }


def calibration_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_calibration_bundle(path: Path) -> dict[str, Any]:
    path = path.resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid calibration JSON at {path}: {exc}") from exc
    if not isinstance(payload.get("tracks"), list):
        raise ValueError(f"Calibration JSON has no tracks list: {path}")
    payload["calibration_path"] = str(path)
    payload["sha256"] = calibration_sha256(path)
    return payload


def _normalized_container_name(value: str | None, suffix: str) -> str:
    normalized = (value or "").lower()
    return normalized[: -len(suffix)] if normalized.endswith(suffix) else normalized


def match_task_calibration(task: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any] | None:
    task_job = _normalized_container_name(task.get("job_name"), ".job")
    task_track = _normalized_container_name(task.get("track_name"), ".scan")
    candidates: list[dict[str, Any]] = []
    for item in bundle.get("tracks", []):
        item_job = _normalized_container_name(str(item.get("job", "")), ".job")
        item_track = _normalized_container_name(str(item.get("track", "")), ".scan")
        if item_job == task_job and (not task_track or item_track == task_track):
            candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def attach_calibration_metadata(
    tasks: list[dict[str, Any]],
    calibration_path: Path | None,
    logger,
    *,
    require_calibration: bool = False,
) -> dict[str, Any] | None:
    """Attach matched calibration provenance and validate Sphere dimensions.

    Raw Front/Rear EUCM and boresight values are already consumed by Leica when
    exporting a stitched Sphere. They are intentionally *not* applied again.
    """
    if calibration_path is None or not calibration_path.is_file():
        if require_calibration:
            raise FileNotFoundError(f"Calibration JSON not found: {calibration_path}")
        logger.warning("Calibration JSON was not supplied; continuing with exported frame poses only.")
        return None

    bundle = load_calibration_bundle(calibration_path)
    unmatched: set[tuple[str | None, str | None]] = set()
    matched_count = 0
    for task in tasks:
        match = match_task_calibration(task, bundle)
        if match is None:
            unmatched.add((task.get("job_name"), task.get("track_name")))
            task["calibration"] = None
            continue

        sensors = match.get("camera", {}).get("imaging_sensors", [])
        sphere_sensor = next(
            (
                sensor
                for sensor in sensors
                if str(sensor.get("output_model", "")).lower() == "sphere"
                or str(sensor.get("friendly_name", "")).lower() == "sphere"
            ),
            None,
        )
        panorama = task.get("panorama", {})
        if sphere_sensor is not None:
            expected_width = sphere_sensor.get("output_width")
            expected_height = sphere_sensor.get("output_height")
            actual_width = panorama.get("image_width")
            actual_height = panorama.get("image_height")
            if actual_width and expected_width and int(actual_width) != int(expected_width):
                raise ValueError(
                    f"Sphere width mismatch for {task['image_name']}: sidecar={actual_width}, "
                    f"calibration={expected_width}"
                )
            if actual_height and expected_height and int(actual_height) != int(expected_height):
                raise ValueError(
                    f"Sphere height mismatch for {task['image_name']}: sidecar={actual_height}, "
                    f"calibration={expected_height}"
                )

        time_metadata = match.get("time") or {}
        calibrated_gps_week = time_metadata.get("gps_week")
        if calibrated_gps_week is not None and task.get("gps_week") not in (None, calibrated_gps_week):
            if task.get("gps_week_inferred") and task.get("gps_sow_seconds") is not None:
                gps_epoch = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)
                corrected_timestamp = gps_epoch + dt.timedelta(
                    weeks=int(calibrated_gps_week),
                    seconds=float(task["gps_sow_seconds"])
                    - int(task.get("gps_utc_offset_seconds", 18)),
                )
                task["timestamp_iso"] = corrected_timestamp.isoformat()
            else:
                raise ValueError(
                    f"GPS week mismatch for {task['image_name']}: "
                    f"pose={task.get('gps_week')}, calibration={calibrated_gps_week}"
                )
        if calibrated_gps_week is not None:
            task["gps_week"] = int(calibrated_gps_week)
            task["gps_week_source"] = "job.db"
        task["calibration"] = {
            "calibration_path": bundle["calibration_path"],
            "calibration_sha256": bundle["sha256"],
            "job": match.get("job"),
            "track": match.get("track"),
            "imaging_sensor_id": sphere_sensor.get("sensor_id") if sphere_sensor else None,
            "imaging_sensor_name": sphere_sensor.get("friendly_name") if sphere_sensor else None,
            "raw_camera_serials": [camera.get("serial") for camera in (sphere_sensor or {}).get("cameras", [])],
            "gps_week": time_metadata.get("gps_week"),
            "application": "validated_only_already_applied_to_leica_sphere",
        }
        matched_count += 1

    if unmatched:
        message = ", ".join(f"{job}/{track}" for job, track in sorted(unmatched, key=str))
        if require_calibration:
            raise ValueError(f"No matching calibration for: {message}")
        logger.warning("No matching calibration for %s", message)
    logger.info(
        "Matched calibration %s to %d/%d image tasks (raw camera calibration is not double-applied).",
        bundle["sha256"][:12],
        matched_count,
        len(tasks),
    )
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Leica Pegasus camera and LiDAR calibration values to JSON."
    )
    parser.add_argument("source", type=Path, help="Pegasus project, job, or parent directory")
    parser.add_argument("--output", type=Path, help="JSON output path; stdout when omitted")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = extract_project_calibrations(args.source)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote calibration JSON: {args.output.resolve()}")


if __name__ == "__main__":
    main()
