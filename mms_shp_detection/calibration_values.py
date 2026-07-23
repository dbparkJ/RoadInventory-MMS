from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

import yaml
from pyproj import CRS
from pyproj.exceptions import CRSError

from .calibration import extract_project_calibrations


VALUES_SCHEMA_VERSION = 1
UNKNOWN_LEICA_UNIT = "not_declared_in_leica_db"


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _numbers(mapping: dict[str, Any] | None, keys: Iterable[str]) -> dict[str, int | float]:
    source = mapping or {}
    result: dict[str, int | float] = {}
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            result[key] = value
    return result


def _value_group(values: dict[str, int | float] | list[int | float], unit: str) -> dict[str, Any]:
    return {"values": values, "unit": unit}


def _lidar_numeric_values(value: Any) -> tuple[list[int | float], str] | None:
    if isinstance(value, dict):
        unit = str(value.get("unit") or UNKNOWN_LEICA_UNIT)
        numeric_values = value.get("numeric_values")
        if isinstance(numeric_values, list):
            values = [item for item in (_number(item) for item in numeric_values) if item is not None]
            if values:
                return values, unit
        decoded = value.get("decoded")
        if isinstance(decoded, str):
            try:
                return [float(item.strip()) for item in decoded.split(",")], unit
            except ValueError:
                return None
    if isinstance(value, str) and value.strip():
        try:
            return [float(item.strip()) for item in value.split(",")], UNKNOWN_LEICA_UNIT
        except ValueError:
            return None
    return None


def _camera_value(camera: dict[str, Any]) -> dict[str, Any]:
    intrinsic = camera.get("intrinsic") or {}
    model = intrinsic.get("model") or {}
    distortion = intrinsic.get("distortion") or {}
    boresight_internal = intrinsic.get("boresight_internal") or {}
    extrinsic = camera.get("extrinsic") or {}

    result: dict[str, Any] = {
        "id": camera.get("id"),
        "name": camera.get("name"),
        "serial": camera.get("serial"),
        "status": camera.get("status"),
        "image_size": _value_group(
            _numbers(camera, ("width", "height")),
            "pixel",
        ),
        "intrinsic": {
            "calibrated_at": intrinsic.get("created_at"),
            "calibration_type": intrinsic.get("calibration_type"),
            "images_processed": intrinsic.get("images_processed"),
            "model": model.get("Type"),
            "principal_point": _value_group(_numbers(model, ("cx", "cy")), "pixel"),
            "focal_length": _value_group(_numbers(model, ("fx", "fy")), "pixel"),
            "skew": _value_group(_numbers(model, ("s",)), "pixel"),
            "model_parameters": _value_group(
                _numbers(model, ("alpha", "beta")), "dimensionless"
            ),
            "distortion_model": distortion.get("Model"),
            "distortion_coefficients": _value_group(
                _numbers(distortion, ("p1", "p2", "k1", "k2", "k3")),
                "dimensionless",
            ),
            # Leica XML does not declare units or the transform direction for
            # BoresightInternal. Keep the numbers, but do not invent semantics.
            "boresight_internal_rotation": _value_group(
                _numbers(boresight_internal, ("r1", "r2", "r3")), UNKNOWN_LEICA_UNIT
            ),
            "boresight_internal_translation": _value_group(
                _numbers(boresight_internal, ("t1", "t2", "t3")), UNKNOWN_LEICA_UNIT
            ),
        },
        # The same limitation applies to BoresightExternal in scan.db.
        "extrinsic": {
            "rotation": _value_group(
                _numbers(extrinsic, ("r1", "r2", "r3")), UNKNOWN_LEICA_UNIT
            ),
            "translation": _value_group(
                _numbers(extrinsic, ("t1", "t2", "t3")), UNKNOWN_LEICA_UNIT
            ),
        },
    }
    return result


def _lidar_values(lidar: dict[str, Any] | None) -> list[dict[str, Any]]:
    sections = (lidar or {}).get("laser_to_imu") or {}
    result: list[dict[str, Any]] = []
    for section_name, fields in sorted(sections.items()):
        item: dict[str, Any] = {
            "sensor": str(section_name).removesuffix(".Laser to IMU"),
            "calibration_section": section_name,
        }
        if isinstance(fields, dict) and fields.get("Serial") not in (None, ""):
            item["serial"] = fields["Serial"]
        for source_key, output_key in (
            ("Angles", "angles"),
            ("Distance", "distance"),
            ("Mounting", "mounting"),
        ):
            parsed = _lidar_numeric_values((fields or {}).get(source_key))
            if parsed is not None:
                values, unit = parsed
                item[output_key] = {"values": values, "unit": unit}
        result.append(item)
    return result


def _track_value(track: dict[str, Any]) -> dict[str, Any]:
    sensors = (track.get("camera") or {}).get("imaging_sensors") or []
    sphere_sensors: list[dict[str, Any]] = []
    cameras: list[dict[str, Any]] = []
    for sensor in sensors:
        sphere_sensors.append(
            {
                "sensor_id": sensor.get("sensor_id"),
                "name": sensor.get("friendly_name"),
                "projection_model": sensor.get("output_model"),
                "output_size": _value_group(
                    {
                        "width": sensor.get("output_width"),
                        "height": sensor.get("output_height"),
                    },
                    "pixel",
                ),
            }
        )
        cameras.extend(_camera_value(camera) for camera in (sensor.get("cameras") or []))

    time = track.get("time") or {}
    time_values: dict[str, Any] = {
        "scale": time.get("time_scale"),
        "frame_time_value": time.get("time_value"),
    }
    gps_week = _number(time.get("gps_week"))
    if gps_week is not None:
        time_values["gps_week"] = {"value": gps_week, "unit": "GPS_week"}

    return {
        "job": track.get("job"),
        "track": track.get("track"),
        "sphere": sphere_sensors,
        "cameras": cameras,
        "lidar_to_imu": _lidar_values(track.get("lidar")),
        "time": time_values,
    }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _authority(crs: CRS) -> dict[str, Any]:
    authority = crs.to_authority()
    result: dict[str, Any] = {"name": crs.name, "type": crs.type_name}
    if authority:
        result["authority"] = authority[0]
        try:
            result["code"] = int(authority[1])
        except ValueError:
            result["code"] = authority[1]
    result["axes"] = [
        {
            "name": axis.name,
            "direction": axis.direction,
            "unit": axis.unit_name,
            "unit_to_si": axis.unit_conversion_factor,
        }
        for axis in crs.axis_info
    ]
    return result


def _projection_values(crs: CRS) -> dict[str, Any]:
    operation = crs.coordinate_operation
    if operation is None:
        return {}
    return {
        parameter.name: {"value": parameter.value, "unit": parameter.unit_name or "dimensionless"}
        for parameter in operation.params
        if _number(parameter.value) is not None
    }


def extract_coordinate_values(project_db_path: Path | None) -> dict[str, Any] | None:
    """Read numeric CRS values without exporting the large WKT payload."""
    if project_db_path is None or not project_db_path.is_file():
        return None
    with closing(_readonly_connection(project_db_path)) as connection:
        rows = connection.execute(
            """
            SELECT INFINITY_NAME, UNIT_NAME, UNIT_SCALEFACTOR, WKT, USER
            FROM [PROJECT.COORDSYS]
            ORDER BY USER DESC
            """
        ).fetchall()
    for source_name, unit_name, unit_scale, wkt, is_user in rows:
        if not wkt:
            continue
        try:
            crs = CRS.from_wkt(wkt)
        except CRSError:
            continue
        sub_crs = crs.sub_crs_list
        horizontal = next((item for item in sub_crs if item.is_projected), crs if crs.is_projected else None)
        vertical = next((item for item in sub_crs if item.is_vertical), crs if crs.is_vertical else None)
        result: dict[str, Any] = {
            "name": crs.name,
            "leica_name": source_name,
            "selected_user_definition": bool(is_user),
            "storage_unit": unit_name,
            "storage_unit_to_si": unit_scale,
        }
        if horizontal is not None:
            result["horizontal"] = _authority(horizontal)
            result["projection_parameters"] = _projection_values(horizontal)
            ellipsoid = horizontal.ellipsoid
            result["ellipsoid"] = {
                "name": ellipsoid.name,
                "semi_major_axis": {"value": ellipsoid.semi_major_metre, "unit": "metre"},
                "inverse_flattening": {
                    "value": ellipsoid.inverse_flattening,
                    "unit": "dimensionless",
                },
            }
        if vertical is not None:
            result["vertical"] = _authority(vertical)
        return result
    return None


def _find_project_db(project_root: Path | None) -> Path | None:
    if project_root is None or not project_root.exists():
        return None
    if project_root.is_file() and project_root.name.lower() == "project.db":
        return project_root.resolve()
    direct = project_root / "project.db"
    if direct.is_file():
        return direct.resolve()
    candidates = sorted(project_root.rglob("project.db"))
    return candidates[0].resolve() if candidates else None


def build_calibration_values(
    calibration_bundle: dict[str, Any],
    *,
    project_db_path: Path | None = None,
) -> dict[str, Any]:
    tracks = calibration_bundle.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("Calibration input has no tracks list")
    return {
        "schema_version": VALUES_SCHEMA_VERSION,
        "units_policy": {
            "known_units_are_embedded_with_each_value": True,
            "undeclared_vendor_unit": UNKNOWN_LEICA_UNIT,
        },
        "coordinate_system": extract_coordinate_values(project_db_path),
        "tracks": [_track_value(track) for track in tracks],
    }


def _load_bundle(source: Path) -> tuple[dict[str, Any], Path | None]:
    if source.is_file():
        if source.suffix.lower() != ".json":
            raise ValueError(f"Calibration source file must be JSON: {source}")
        try:
            bundle = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid calibration JSON at {source}: {exc}") from exc
        source_root = bundle.get("source_root")
        root = Path(source_root) if isinstance(source_root, str) and source_root else None
        return bundle, root
    if source.is_dir():
        return extract_project_calibrations(source), source
    raise FileNotFoundError(source)


def _iter_csv_rows(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    def walk(value: Any, path: list[str], context: dict[str, Any]) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict) and "unit" in value and ("value" in value or "values" in value):
            unit = value["unit"]
            values = value.get("values", value.get("value"))
            if isinstance(values, dict):
                for name, number in values.items():
                    if _number(number) is not None:
                        yield {**context, "parameter": ".".join(path + [name]), "value": number, "unit": unit}
            elif isinstance(values, list):
                for index, number in enumerate(values):
                    if _number(number) is not None:
                        yield {**context, "parameter": ".".join(path + [str(index)]), "value": number, "unit": unit}
            elif _number(values) is not None:
                yield {**context, "parameter": ".".join(path), "value": values, "unit": unit}
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"job", "track", "name", "sensor", "serial"}:
                    continue
                yield from walk(item, path + [key], context)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                item_context = dict(context)
                if isinstance(item, dict):
                    item_context["component"] = str(
                        item.get("name") or item.get("sensor") or item.get("serial") or index
                    )
                yield from walk(item, path + [str(index)], item_context)
        elif _number(value) is not None:
            leaf = path[-1].lower() if path else ""
            if leaf in {"id", "sensor_id", "code", "images_processed", "schema_version"}:
                unit = "identifier_or_count"
            elif "unit_to_si" in leaf or "scale" in leaf or "flattening" in leaf:
                unit = "dimensionless"
            else:
                unit = UNKNOWN_LEICA_UNIT
            yield {
                **context,
                "parameter": ".".join(path),
                "value": value,
                "unit": unit,
            }

    coordinate = payload.get("coordinate_system")
    if coordinate:
        yield from walk(coordinate, ["coordinate_system"], {"job": "", "track": "", "component": "CRS"})
    for track in payload.get("tracks", []):
        context = {
            "job": track.get("job") or "",
            "track": track.get("track") or "",
            "component": "track",
        }
        for key in ("sphere", "cameras", "lidar_to_imu", "time"):
            yield from walk(track.get(key), [key], context)


def write_calibration_values(
    payload: dict[str, Any],
    json_path: Path,
    csv_path: Path | None = None,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if csv_path is not None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=("job", "track", "component", "parameter", "value", "unit")
            )
            writer.writeheader()
            writer.writerows(_iter_csv_rows(payload))


def export_from_config(config_path: Path) -> tuple[Path, Path | None]:
    config_path = config_path.resolve()
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML at {config_path}: {exc}") from exc
    if config.get("config_version") != 1:
        raise ValueError("calibration_values.yaml config_version must be 1")
    base = config_path.parent
    input_config = config.get("input") or {}
    output_config = config.get("output") or {}
    source_value = input_config.get("source", "calibration.json")
    source = Path(source_value)
    if not source.is_absolute():
        source = base / source
    bundle, discovered_root = _load_bundle(source.resolve())

    configured_root = input_config.get("project_root")
    if configured_root:
        project_root = Path(configured_root)
        if not project_root.is_absolute():
            project_root = base / project_root
    else:
        project_root = discovered_root
    project_db = _find_project_db(project_root.resolve() if project_root else None)
    payload = build_calibration_values(bundle, project_db_path=project_db)

    json_value = output_config.get("json_path", "calibration_values.json")
    json_path = Path(json_value)
    if not json_path.is_absolute():
        json_path = base / json_path
    csv_value = output_config.get("csv_path", "calibration_values.csv")
    csv_path = Path(csv_value) if csv_value else None
    if csv_path is not None and not csv_path.is_absolute():
        csv_path = base / csv_path
    write_calibration_values(payload, json_path.resolve(), csv_path.resolve() if csv_path else None)
    return json_path.resolve(), csv_path.resolve() if csv_path else None


def main() -> None:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}):
        print("Usage: python export_calibration_values.py [calibration_values.yaml]")
        if len(sys.argv) > 2:
            raise SystemExit(2)
        return
    config_path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("calibration_values.yaml")
    json_path, csv_path = export_from_config(config_path)
    print(f"Calibration values JSON: {json_path}")
    if csv_path is not None:
        print(f"Calibration values CSV:  {csv_path}")


if __name__ == "__main__":
    main()
