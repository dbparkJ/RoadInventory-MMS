from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml
from pyproj import CRS

from mms_shp_detection.calibration_values import (
    UNKNOWN_LEICA_UNIT,
    _iter_csv_rows,
    build_calibration_values,
    export_from_config,
    extract_coordinate_values,
)


def _sample_bundle() -> dict:
    return {
        "source_root": "/source/that/must/not/be/exported",
        "tracks": [
            {
                "job": "sample.job",
                "track": "Track01.scan",
                "camera": {
                    "scan_db": "/private/scan.db",
                    "imaging_sensors": [
                        {
                            "sensor_id": 1,
                            "friendly_name": "Sphere",
                            "output_width": 7040,
                            "output_height": 3520,
                            "output_model": "sphere",
                            "cameras": [
                                {
                                    "id": 1,
                                    "name": "Pano Front",
                                    "serial": "CAM1",
                                    "width": 4096,
                                    "height": 3008,
                                    "status": "Passed",
                                    "intrinsic": {
                                        "model": {
                                            "Type": "eucm",
                                            "cx": 2050.0,
                                            "cy": 1690.0,
                                            "fx": 950.0,
                                            "fy": 951.0,
                                            "alpha": 0.60,
                                            "beta": 1.08,
                                        },
                                        "distortion": {"Model": "tan", "p1": 0.01, "k1": 0},
                                        "boresight_internal": {"r1": 1.0, "t1": 2.0},
                                    },
                                    "extrinsic": {"r1": 90.0, "t1": 0.1},
                                }
                            ],
                        }
                    ],
                },
                "lidar": {
                    "job_db": "/private/job.db",
                    "laser_to_imu": {
                        "Scanner1.Laser to IMU": {
                            "Angles": {
                                "encoding": "base64",
                                "value": "secret",
                                "hex": "deadbeef",
                                "numeric_values": [1.0, 2.0, 3.0],
                                "unit": "deg",
                            },
                            "Distance": {"decoded": "0.1,0.2,0.3", "unit": "m"},
                            "Serial": "L1",
                        }
                    },
                },
                "time": {"gps_week": 2357, "time_scale": "GPS", "time_value": "seconds_of_week"},
            }
        ],
    }


class CalibrationValuesTests(unittest.TestCase):
    def test_builds_compact_values_without_raw_binary_or_paths(self) -> None:
        result = build_calibration_values(_sample_bundle())
        rendered = json.dumps(result)
        self.assertNotIn("base64", rendered)
        self.assertNotIn("deadbeef", rendered)
        self.assertNotIn("/private/", rendered)
        camera = result["tracks"][0]["cameras"][0]
        self.assertEqual(camera["intrinsic"]["focal_length"]["unit"], "pixel")
        self.assertEqual(camera["extrinsic"]["rotation"]["unit"], UNKNOWN_LEICA_UNIT)
        lidar = result["tracks"][0]["lidar_to_imu"][0]
        self.assertEqual(lidar["angles"], {"values": [1.0, 2.0, 3.0], "unit": "deg"})
        self.assertEqual(lidar["distance"]["values"], [0.1, 0.2, 0.3])

    def test_reads_projected_coordinate_values_without_wkt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "project.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE [PROJECT.COORDSYS] "
                "(INFINITY_NAME TEXT, UNIT_NAME TEXT, UNIT_SCALEFACTOR DOUBLE, WKT TEXT, USER BOOLEAN)"
            )
            connection.execute(
                "INSERT INTO [PROJECT.COORDSYS] VALUES (?, ?, ?, ?, ?)",
                ("UTM52", "metre", 1.0, CRS.from_epsg(32652).to_wkt(), 1),
            )
            connection.commit()
            connection.close()
            result = extract_coordinate_values(db_path)
        self.assertIsNotNone(result)
        self.assertEqual(result["horizontal"]["code"], 32652)
        self.assertEqual(result["projection_parameters"]["Longitude of natural origin"]["value"], 129.0)
        self.assertNotIn("WKT", json.dumps(result))

    def test_csv_contains_one_numeric_value_per_row_with_units(self) -> None:
        rows = list(_iter_csv_rows(build_calibration_values(_sample_bundle())))
        focal_rows = [row for row in rows if "focal_length" in row["parameter"]]
        self.assertEqual(len(focal_rows), 2)
        self.assertTrue(all(row["unit"] == "pixel" for row in focal_rows))
        self.assertTrue(all(isinstance(row["value"], (int, float)) for row in rows))

    def test_yaml_config_exports_json_and_optional_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calibration.json").write_text(json.dumps(_sample_bundle()), encoding="utf-8")
            config = {
                "config_version": 1,
                "input": {"source": "calibration.json"},
                "output": {"json_path": "values.json", "csv_path": None},
            }
            config_path = root / "values.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            json_path, csv_path = export_from_config(config_path)
            self.assertTrue(json_path.is_file())
            self.assertIsNone(csv_path)
            self.assertEqual(json.loads(json_path.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
