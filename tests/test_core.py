from __future__ import annotations

import json
import logging
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from pyproj import CRS

from mms_shp_detection.calibration import (
    _decode_leica_calibration_value,
    attach_calibration_metadata,
)
from mms_shp_detection.dataset import gps_sow_to_utc, scan_image_tasks
from mms_shp_detection.geometry import (
    apply_panorama_angular_offsets,
    build_camera_axes,
    build_view_axes,
    fit_perspective_overview,
    pixel_to_world_ray,
    project_points_equirectangular,
    world_ray_to_equirectangular_pixel,
    world_ray_to_perspective_pixel,
)
from mms_shp_detection.pcdb import PcdbConnectionCache
from mms_shp_detection.shp_writer import (
    publish_shapefile_bundles,
    write_pole_shapefile,
    write_shapefile,
)


LOGGER = logging.getLogger("tests")
LOGGER.addHandler(logging.NullHandler())


class LeicaDatasetTests(unittest.TestCase):
    def test_headerless_sphere_csv_uses_full_rotation_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sphere_dir = (
                root
                / "Export"
                / "JPEG"
                / "Job_20250311_1043"
                / "Track01"
                / "Sphere"
            )
            sphere_dir.mkdir(parents=True)
            image_name = "Job_20250311_1043_Track01_Sphere_00001.jpg"
            (sphere_dir / image_name).write_bytes(b"fixture")
            (sphere_dir / "Job_20250311_1043_Track01_Sphere.txt").write_text(
                "ImageSize=7040,3520\n"
                "SphereRadius=100.0000\n"
                "HeightLimits=-90.0000,90.0000\n"
                "WidthLimits=-180.0000,180.0000\n"
                "PanoramaHotSpot=0,0\n",
                encoding="utf-8",
            )
            (sphere_dir / "Job_20250311_1043_Track01_Sphere.csv").write_text(
                f"{image_name};180096.447723;329703.430;4153507.556;42.345;"
                "0;0;0;1;0;0;0;1;0;0;0;1\n",
                encoding="utf-8",
            )

            tasks = scan_image_tasks(root, LOGGER, pose_format="leica-sphere", gps_week=2357)

            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task["pose_row_number"], 1)
            self.assertEqual(task["origin"], [329703.43, 4153507.556, 42.345])
            np.testing.assert_allclose(task["right"], [1.0, 0.0, 0.0])
            np.testing.assert_allclose(task["up"], [0.0, 1.0, 0.0])
            np.testing.assert_allclose(task["direction"], [0.0, 0.0, -1.0])
            self.assertEqual(task["gps_week"], 2357)
            self.assertEqual(task["timestamp_iso"], "2025-03-11T02:01:18.447723+00:00")

    def test_gps_week_inference(self) -> None:
        timestamp, week, inferred = gps_sow_to_utc(
            180096.447723,
            job_name="Job_20250311_1043",
        )
        self.assertEqual(week, 2357)
        self.assertTrue(inferred)
        self.assertEqual(timestamp.isoformat(), "2025-03-11T02:01:18.447723+00:00")

    def test_auto_recursively_combines_pegasus_and_standard_delivery_spheres(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pegasus_dir = (
                root
                / "nested"
                / "MultiJob.PegasusProject"
                / "Export"
                / "JPEG"
                / "Job_20250311_1043"
                / "Track01"
                / "Sphere"
            )
            pegasus_dir.mkdir(parents=True)
            pegasus_image = "Job_20250311_1043_Track01_Sphere_00001.jpg"
            (pegasus_dir / pegasus_image).write_bytes(b"fixture")
            (pegasus_dir / "Job_20250311_1043_Track01_Sphere.csv").write_text(
                f"{pegasus_image};180096.0;300000;4100000;100;0;0;0;"
                "1;0;0;0;1;0;0;0;1\n",
                encoding="utf-8",
            )

            track_dir = root / "SEC006_sample_250903" / "SURV01" / "TRACK01"
            camera_dir = track_dir / "Camera05"
            camera_dir.mkdir(parents=True)
            delivery_image = "Track01-Sphere-17.jpg"
            (camera_dir / delivery_image).write_bytes(b"fixture")
            (camera_dir / "Internal Orientation.txt").write_text(
                "PanoramaHotSpot=180,90\n"
                "WidthLimits=0,360\n"
                "HeightLimits=0,180\n"
                "SphereRadius=100\n"
                "ImageSize=7040,3520\n",
                encoding="utf-8",
            )
            (camera_dir / "External Orientation.csv").write_text(
                f"{delivery_image};281430.869;465216.066;3911273.445;47.495;"
                "0;0;0;1;0;0;0;1;0;0;0;1\n",
                encoding="utf-8",
            )
            (track_dir / "MMS_Leica_PegasusTRK700Neo_291112.ini").write_text(
                "[MMSIdentification]\n"
                "Manufacturer=Leica\n"
                "ModelName=PegasusTRK700Neo\n"
                "SerialNumber=291112\n",
                encoding="utf-8",
            )

            tasks = scan_image_tasks(root, LOGGER, pose_format="auto")

            self.assertEqual(
                {task["pose_format"] for task in tasks},
                {"leica-sphere", "leica-delivery"},
            )
            delivery = next(
                task for task in tasks if task["pose_format"] == "leica-delivery"
            )
            self.assertEqual(delivery["panorama"]["image_width"], 7040)
            self.assertEqual(
                delivery["panorama"]["longitude_limits_deg"],
                [-180.0, 180.0],
            )
            self.assertEqual(delivery["panorama"]["panorama_hotspot"], [0.0, 0.0])
            self.assertEqual(delivery["gps_week"], 2382)
            self.assertEqual(
                delivery["timestamp_iso"],
                "2025-09-03T06:10:12.869000+00:00",
            )
            self.assertEqual(
                delivery["delivery_calibration"]["model_name"],
                "PegasusTRK700Neo",
            )
            self.assertEqual(delivery["pointcloud_scope"], str(track_dir.resolve()))

            bundle = attach_calibration_metadata(
                [delivery],
                None,
                LOGGER,
                require_calibration=True,
            )
            self.assertIsNone(bundle)
            self.assertEqual(
                delivery["calibration"]["application"],
                "validated_vendor_delivery_sphere_metadata",
            )
            self.assertEqual(delivery["calibration"]["manufacturer"], "Leica")


class GeometryTests(unittest.TestCase):
    def test_equirectangular_pixel_ray_round_trip(self) -> None:
        forward, right, up = build_camera_axes((0.1, 0.98, 0.05), (0.0, 0.0, 1.0))
        for pixel_x, pixel_y in ((0.0, 1760.0), (3520.0, 1760.0), (7039.0, 100.0)):
            ray = pixel_to_world_ray(pixel_x, pixel_y, 7040, 3520, forward, right, up)
            roundtrip_x, roundtrip_y = world_ray_to_equirectangular_pixel(
                ray, forward, right, up, 7040, 3520
            )
            wrapped_error = min(abs(pixel_x - roundtrip_x), 7040 - abs(pixel_x - roundtrip_x))
            self.assertLess(wrapped_error, 1e-8)
            self.assertAlmostEqual(pixel_y, roundtrip_y, places=8)

    def test_panorama_angular_offset_signs_and_round_trip(self) -> None:
        forward, right, up = build_camera_axes((0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        corrected = apply_panorama_angular_offsets(
            forward,
            right,
            up,
            yaw_offset_deg=1.0,
            pitch_offset_deg=0.5,
        )
        origin = np.zeros(3, dtype=np.float64)
        point = forward[None, :] * 10.0
        x, y, _distance = project_points_equirectangular(
            point,
            origin,
            *corrected,
            3600,
            1800,
        )
        # 3600 px / 360 degrees and 1800 px / 180 degrees make the
        # configured offsets directly observable in pixels.
        self.assertAlmostEqual(float(x[0]), 1810.0, delta=0.01)
        self.assertAlmostEqual(float(y[0]), 905.0, delta=0.01)

        ray = pixel_to_world_ray(
            float(x[0]),
            float(y[0]),
            3600,
            1800,
            *corrected,
        )
        np.testing.assert_allclose(ray, forward, atol=1e-12)

    def test_pole_overview_contains_base_clipped_by_sign_centered_view(self) -> None:
        reference_forward, reference_right, reference_up = build_camera_axes(
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )

        def ray(horizontal_deg: float, vertical_deg: float) -> np.ndarray:
            value = (
                reference_forward
                + (reference_right * np.tan(np.radians(horizontal_deg)))
                + (reference_up * np.tan(np.radians(vertical_deg)))
            )
            return value / np.linalg.norm(value)

        # This emulates C00233: a compact sign is high in the frame while its
        # base and local ground fall below a symmetric 55-degree sign view.
        sign_rays = np.asarray(
            [ray(-3.0, 12.0), ray(3.0, 12.0), ray(-3.0, 18.0), ray(3.0, 18.0)]
        )
        pole_base_ray = ray(1.0, -40.0)
        pole_axis_rays = np.asarray([ray(1.0, -33.0), ray(0.0, 11.0)])
        ground_rays = np.asarray(
            [ray(-4.0, -42.0), ray(0.0, -41.0), ray(5.0, -39.0)]
        )

        sign_center = np.sum(sign_rays, axis=0)
        sign_center /= np.linalg.norm(sign_center)
        sign_forward, sign_right, sign_up = build_view_axes(
            sign_center,
            reference_up,
            reference_right,
        )
        _base_x, base_y, base_depth = world_ray_to_perspective_pixel(
            pole_base_ray,
            sign_forward,
            sign_right,
            sign_up,
            1024,
            1024,
            55.0,
            55.0,
        )
        self.assertGreater(base_depth, 0.0)
        self.assertGreaterEqual(base_y, 1024.0)

        forward, right, up, hfov_deg, vfov_deg = fit_perspective_overview(
            sign_rays,
            pole_base_ray,
            pole_axis_rays,
            ground_rays,
            reference_up,
            padding_deg=2.0,
            max_fov_deg=110.0,
            output_aspect_ratio=1.0,
            reference_right_vec=reference_right,
        )
        all_rays = np.vstack((sign_rays, pole_base_ray, pole_axis_rays, ground_rays))
        for overview_ray in all_rays:
            pixel_x, pixel_y, depth = world_ray_to_perspective_pixel(
                overview_ray,
                forward,
                right,
                up,
                1024,
                1024,
                hfov_deg,
                vfov_deg,
            )
            self.assertGreater(depth, 0.0)
            self.assertGreater(pixel_x, 0.0)
            self.assertLess(pixel_x, 1024.0)
            self.assertGreater(pixel_y, 0.0)
            self.assertLess(pixel_y, 1024.0)

        self.assertGreater(vfov_deg, 55.0)
        self.assertLessEqual(max(hfov_deg, vfov_deg), 110.0)
        self.assertLess(float(np.dot(forward, reference_up)), 0.0)
        np.testing.assert_allclose(np.linalg.norm([forward, right, up], axis=1), 1.0)
        self.assertAlmostEqual(float(np.dot(forward, right)), 0.0, places=12)
        self.assertAlmostEqual(float(np.dot(forward, up)), 0.0, places=12)
        self.assertAlmostEqual(float(np.dot(right, up)), 0.0, places=12)
        self.assertGreater(float(np.dot(np.cross(forward, up), right)), 0.999999)
        self.assertGreater(float(np.dot(up, reference_up)), 0.0)
        self.assertGreater(float(np.dot(right, reference_right)), 0.0)

    def test_pole_overview_respects_aspect_and_preserves_roll_near_up_axis(self) -> None:
        reference_up = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
        reference_right = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
        near_up_forward = reference_up.copy()
        near_up_view_up = np.cross(reference_right, near_up_forward)

        def ray(horizontal_deg: float, vertical_deg: float) -> np.ndarray:
            value = (
                near_up_forward
                + (reference_right * np.tan(np.radians(horizontal_deg)))
                + (near_up_view_up * np.tan(np.radians(vertical_deg)))
            )
            return value / np.linalg.norm(value)

        sign_rays = np.asarray(
            [ray(-6.0, -3.0), ray(6.0, -3.0), ray(-6.0, 3.0), ray(6.0, 3.0)]
        )
        forward, right, up, hfov_deg, vfov_deg = fit_perspective_overview(
            sign_rays,
            ray(0.0, -12.0),
            np.asarray([ray(0.0, -8.0), ray(0.0, 2.0)]),
            np.asarray([ray(-3.0, -13.0), ray(3.0, -13.0)]),
            reference_up,
            padding_deg=1.5,
            max_fov_deg=80.0,
            output_aspect_ratio=16.0 / 9.0,
            reference_right_vec=reference_right,
        )
        self.assertAlmostEqual(
            np.tan(np.radians(hfov_deg) * 0.5)
            / np.tan(np.radians(vfov_deg) * 0.5),
            16.0 / 9.0,
            places=12,
        )
        self.assertGreater(float(np.dot(right, reference_right)), 0.999)
        self.assertGreater(float(np.dot(np.cross(forward, up), right)), 0.999999)

        with self.assertRaisesRegex(ValueError, "exceeding max_fov_deg"):
            fit_perspective_overview(
                sign_rays,
                ray(0.0, -45.0),
                None,
                None,
                reference_up,
                padding_deg=3.0,
                max_fov_deg=30.0,
                output_aspect_ratio=16.0 / 9.0,
                reference_right_vec=reference_right,
            )


class CalibrationDecodeTests(unittest.TestCase):
    def test_decodes_lidar_calibration_value(self) -> None:
        encoded = bytes.fromhex("726841595a650218405955626619ff706f")
        self.assertEqual(_decode_leica_calibration_value(encoded), "0.039,0.249,0.057")


class PcdbPrecisionTests(unittest.TestCase):
    def test_world_coordinates_remain_float64(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fixture.pcdb"
            center = np.asarray([329703.0, 4153507.5, 42.0], dtype=np.float64)
            minimum = center - 1.0
            maximum = center + 1.0
            offsets = ([0.01, 0.01, 0.01], [0.02, 0.02, 0.02])
            records = b"".join(
                struct.pack("<3f3BH", *offset, 1, 2, 3, 4) for offset in offsets
            )
            blob = struct.pack("<6dI", *minimum, *maximum, len(offsets)) + records
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE CRYSTAL_CUBE (NAME TEXT, DATA BLOB)")
            connection.execute(
                "INSERT INTO CRYSTAL_CUBE (NAME, DATA) VALUES (?, ?)",
                ("fixture.bpc", blob),
            )
            connection.commit()
            connection.close()

            cache = PcdbConnectionCache()
            try:
                points, _colors, _intensity = cache.read_block_points(str(path), "fixture.bpc")
            finally:
                cache.close()

            self.assertEqual(points.dtype, np.float64)
            self.assertAlmostEqual(float(points[1, 1] - points[0, 1]), 0.01, places=6)


class CrsPropagationTests(unittest.TestCase):
    def test_shapefile_uses_supplied_wkt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "result.shp"
            custom_wkt = 'PROJCS["fixture",UNIT["metre",1]]'
            write_shapefile([], shp_path, crs_wkt=custom_wkt)
            self.assertEqual(shp_path.with_suffix(".prj").read_text(encoding="utf-8"), custom_wkt)

    def test_shapefile_records_run_provenance(self) -> None:
        import shapefile

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "result.shp"
            write_shapefile(
                [
                    {
                        "class_id": 1,
                        "class_name": "sign",
                        "confidence": 0.9,
                        "x": 329000.0,
                        "y": 4153000.0,
                        "z": 40.0,
                        "image_name": "image.jpg",
                        "timestamp_iso": "2025-03-11T00:00:00+00:00",
                        "point_count": 100,
                        "point_crop_path": "crop.las",
                        "pose_format": "leica-sphere",
                        "gps_week": 2357,
                        "pointcloud_source": "las",
                        "calibration_sha256": "a" * 64,
                        "run_fingerprint": "b" * 64,
                    }
                ],
                shp_path,
            )
            reader = shapefile.Reader(str(shp_path))
            record = reader.record(0).as_dict()
            self.assertEqual(record["gps_week"], 2357)
            self.assertEqual(record["pose_fmt"], "leica-sphere")
            self.assertEqual(record["calib_id"], "a" * 12)
            self.assertEqual(record["run_id"], "b" * 12)
            reader.close()

    def test_failed_sidecar_write_does_not_replace_existing_bundle(self) -> None:
        import shapefile

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "result.shp"
            original = {
                "class_id": 1,
                "class_name": "old",
                "confidence": 0.9,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "image_name": "old.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }
            write_shapefile([original], shp_path)

            replacement = dict(original, class_name="new", image_name="new.jpg")
            with mock.patch(
                "mms_shp_detection.shp_writer.write_crs_sidecars",
                side_effect=PermissionError("locked sidecar"),
            ):
                with self.assertRaises(PermissionError):
                    write_shapefile([replacement], shp_path)

            reader = shapefile.Reader(str(shp_path))
            self.assertEqual(reader.record(0).as_dict()["class_nm"], "old")
            reader.close()
            self.assertFalse(
                [path for path in Path(temp_dir).iterdir() if ".writing." in path.name]
            )

    def test_invalid_record_closes_writer_and_removes_temporary_components(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shp_path = root / "result.shp"
            with self.assertRaises((TypeError, ValueError)):
                write_shapefile([{"x": "not-a-number"}], shp_path)
            self.assertFalse(shp_path.exists())
            self.assertFalse(
                [path for path in root.iterdir() if ".writing." in path.name]
            )

    def test_writer_close_failure_force_closes_raw_handles_and_cleans_temp(self) -> None:
        import shapefile as pyshp

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shp_path = root / "result.shp"

            real_writer_class = pyshp.Writer

            def writer_with_one_failed_close(*args, **kwargs):
                writer = real_writer_class(*args, **kwargs)
                real_close = writer.close

                def fail_once():
                    writer.close = real_close
                    raise OSError("injected close failure")

                writer.close = fail_once
                return writer

            with mock.patch(
                "mms_shp_detection.shp_writer.shapefile.Writer",
                side_effect=writer_with_one_failed_close,
            ):
                with self.assertRaisesRegex(OSError, "injected close failure"):
                    write_shapefile([], shp_path)
            self.assertFalse(shp_path.exists())
            self.assertFalse(
                [path for path in root.iterdir() if ".writing." in path.name]
            )

    def test_writer_constructor_failure_closes_all_preopened_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shp_path = root / "result.shp"
            opened_handles = []

            def fail_constructor(*args, **kwargs):
                opened_handles.extend(
                    [kwargs["shp"], kwargs["shx"], kwargs["dbf"]]
                )
                raise OSError("injected constructor failure")

            with mock.patch(
                "mms_shp_detection.shp_writer.shapefile.Writer",
                side_effect=fail_constructor,
            ):
                with self.assertRaisesRegex(OSError, "injected constructor failure"):
                    write_shapefile([], shp_path)

            self.assertEqual(len(opened_handles), 3)
            self.assertTrue(all(handle.closed for handle in opened_handles))
            self.assertFalse(shp_path.exists())
            self.assertFalse(
                [path for path in root.iterdir() if ".writing." in path.name]
            )

    def test_source_target_path_alias_is_rejected_without_deleting_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "alias").mkdir()
            target = root / "result.shp"
            record = {
                "class_id": 1,
                "class_name": "existing",
                "confidence": 0.9,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "image_name": "existing.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }
            write_shapefile([record], target)
            aliased_source = root / "alias" / ".." / "result.shp"

            with self.assertRaisesRegex(ValueError, "must not alias"):
                publish_shapefile_bundles([(aliased_source, target.resolve())])

            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2"):
                self.assertTrue(target.with_suffix(suffix).is_file())

    def test_non_shp_basename_alias_is_rejected_without_deleting_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "same.shp"
            write_shapefile([], target)

            with self.assertRaisesRegex(ValueError, "must both end in .shp"):
                publish_shapefile_bundles([(target.with_suffix(".dbf"), target)])

            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2"):
                self.assertTrue(target.with_suffix(suffix).is_file())

    def test_two_bundle_publish_rolls_back_both_targets(self) -> None:
        import os
        import shapefile

        def record(name: str, x: float) -> dict:
            return {
                "class_id": 1,
                "class_name": name,
                "confidence": 0.9,
                "x": x,
                "y": 2.0,
                "z": 3.0,
                "image_name": f"{name}.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sign_target = root / "detected_signs.shp"
            pole_target = root / "pole_bottoms.shp"
            sign_stage = root / "detected_signs.ready.shp"
            pole_stage = root / "pole_bottoms.ready.shp"
            write_shapefile([record("old_sign", 1.0)], sign_target)
            write_shapefile([record("old_pole", 2.0)], pole_target)
            write_shapefile([record("new_sign", 3.0)], sign_stage)
            write_shapefile([record("new_pole", 4.0)], pole_stage)
            sign_target.with_suffix(".qix").write_bytes(b"old spatial index")

            real_replace = os.replace
            failure_raised = False

            def fail_on_second_bundle(source, target):
                nonlocal failure_raised
                if Path(source) == pole_stage.with_suffix(".dbf") and not failure_raised:
                    failure_raised = True
                    raise PermissionError("pole bundle is locked")
                return real_replace(source, target)

            with mock.patch(
                "mms_shp_detection.shp_writer.os.replace",
                side_effect=fail_on_second_bundle,
            ):
                with self.assertRaises(PermissionError):
                    publish_shapefile_bundles(
                        [(sign_stage, sign_target), (pole_stage, pole_target)]
                    )

            for target, expected in (
                (sign_target, "old_sign"),
                (pole_target, "old_pole"),
            ):
                reader = shapefile.Reader(str(target))
                self.assertEqual(reader.record(0).as_dict()["class_nm"], expected)
                reader.close()
            self.assertEqual(
                sign_target.with_suffix(".qix").read_bytes(),
                b"old spatial index",
            )
            self.assertFalse(any(root.glob("*.ready.*")))

    def test_failed_rollback_preserves_recovery_components(self) -> None:
        import os

        def record(name: str) -> dict:
            return {
                "class_id": 1,
                "class_name": name,
                "confidence": 0.9,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "image_name": f"{name}.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sign_target = root / "detected_signs.shp"
            pole_target = root / "pole_bottoms.shp"
            sign_stage = root / "detected_signs.ready.shp"
            pole_stage = root / "pole_bottoms.ready.shp"
            for path, name in (
                (sign_target, "old_sign"),
                (pole_target, "old_pole"),
                (sign_stage, "new_sign"),
                (pole_stage, "new_pole"),
            ):
                write_shapefile([record(name)], path)

            real_replace = os.replace

            def fail_publish_and_restore(source, target):
                source_path = Path(source)
                target_path = Path(target)
                if source_path == pole_stage.with_suffix(".dbf"):
                    raise PermissionError("pole publish locked")
                if ".backup." in source_path.name and target_path == sign_target.with_suffix(
                    ".dbf"
                ):
                    raise KeyboardInterrupt("sign restore interrupted")
                return real_replace(source, target)

            with mock.patch(
                "mms_shp_detection.shp_writer.os.replace",
                side_effect=fail_publish_and_restore,
            ):
                with self.assertRaises(PermissionError) as raised:
                    publish_shapefile_bundles(
                        [(sign_stage, sign_target), (pole_stage, pole_target)]
                    )

            notes = "\n".join(getattr(raised.exception, "__notes__", []))
            self.assertIn("Recovery components were preserved", notes)
            self.assertTrue(
                [path for path in root.iterdir() if ".backup." in path.name]
            )
            self.assertTrue(
                [path for path in root.iterdir() if ".ready." in path.name]
            )

    def test_keyboard_interrupt_during_publish_rolls_back_both_bundles(self) -> None:
        import os
        import shapefile

        def record(name: str, x: float) -> dict:
            return {
                "class_id": 1,
                "class_name": name,
                "confidence": 0.9,
                "x": x,
                "y": 2.0,
                "z": 3.0,
                "image_name": f"{name}.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sign_target = root / "detected_signs.shp"
            pole_target = root / "pole_bottoms.shp"
            sign_stage = root / "detected_signs.ready.shp"
            pole_stage = root / "pole_bottoms.ready.shp"
            for path, item in (
                (sign_target, record("old_sign", 1.0)),
                (pole_target, record("old_pole", 2.0)),
                (sign_stage, record("new_sign", 3.0)),
                (pole_stage, record("new_pole", 4.0)),
            ):
                write_shapefile([item], path)

            real_replace = os.replace

            def interrupt_second_bundle(source, target):
                if Path(source) == pole_stage.with_suffix(".dbf"):
                    raise KeyboardInterrupt()
                return real_replace(source, target)

            with mock.patch(
                "mms_shp_detection.shp_writer.os.replace",
                side_effect=interrupt_second_bundle,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    publish_shapefile_bundles(
                        [(sign_stage, sign_target), (pole_stage, pole_target)]
                    )

            for target, expected in (
                (sign_target, "old_sign"),
                (pole_target, "old_pole"),
            ):
                reader = shapefile.Reader(str(target))
                self.assertEqual(reader.record(0).as_dict()["class_nm"], expected)
                reader.close()
            self.assertFalse(any(root.glob("*.ready.*")))

    def test_interrupt_after_replace_syscall_rolls_back_completed_component(self) -> None:
        import os
        import shapefile

        def record(name: str, x: float) -> dict:
            return {
                "class_id": 1,
                "class_name": name,
                "confidence": 0.9,
                "x": x,
                "y": 2.0,
                "z": 3.0,
                "image_name": f"{name}.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "detected_signs.shp"
            stage = root / "detected_signs.ready.shp"
            write_shapefile([record("old_sign", 1.0)], target)
            write_shapefile([record("new_sign", 3.0)], stage)

            real_replace = os.replace
            interrupted = False

            def interrupt_after_dbf_replace(source, destination):
                nonlocal interrupted
                result = real_replace(source, destination)
                if Path(source) == stage.with_suffix(".dbf") and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("injected after completed replace")
                return result

            with mock.patch(
                "mms_shp_detection.shp_writer.os.replace",
                side_effect=interrupt_after_dbf_replace,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    publish_shapefile_bundles([(stage, target)])

            reader = shapefile.Reader(str(target))
            try:
                self.assertEqual(reader.shape(0).points[0][0], 1.0)
                self.assertEqual(
                    reader.record(0).as_dict()["class_nm"], "old_sign"
                )
            finally:
                reader.close()
            self.assertFalse(any(root.glob("*.ready.*")))
            self.assertFalse(any(root.glob("*.backup.*")))

    def test_successful_publish_removes_stale_spatial_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "result.shp"
            stage = root / "result.ready.shp"
            base_record = {
                "class_id": 1,
                "class_name": "old",
                "confidence": 0.9,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "image_name": "old.jpg",
                "timestamp_iso": "",
                "point_count": 1,
            }
            write_shapefile([base_record], target)
            write_shapefile([dict(base_record, class_name="new")], stage)
            target.with_suffix(".qix").write_bytes(b"stale index")
            target.with_suffix(".fbn").write_bytes(b"stale fixed-bin index")
            field_index = root / "result.class_nm.atx"
            field_index.write_bytes(b"stale field index")
            unrelated_field_index = root / "result.v2.class_nm.atx"
            unrelated_field_index.write_bytes(b"different shapefile index")

            publish_shapefile_bundles([(stage, target)])

            self.assertFalse(target.with_suffix(".qix").exists())
            self.assertFalse(target.with_suffix(".fbn").exists())
            self.assertFalse(field_index.exists())
            self.assertEqual(
                unrelated_field_index.read_bytes(),
                b"different shapefile index",
            )

    def test_compound_crs_writes_esri_horizontal_prj_and_full_wkt2(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "result.shp"
            compound = CRS.from_user_input("EPSG:32652+3855")
            write_shapefile([], shp_path, crs_wkt=compound.to_wkt())
            prj = shp_path.with_suffix(".prj").read_text(encoding="utf-8")
            full = shp_path.with_suffix(".wkt2").read_text(encoding="utf-8")
            self.assertTrue(prj.startswith("PROJCS["))
            self.assertNotIn("COMPOUNDCRS", prj)
            self.assertEqual(CRS.from_wkt(prj).to_epsg(), 32652)
            self.assertIn("COMPOUNDCRS", full)
            self.assertEqual(
                shp_path.with_suffix(".qpj").read_text(encoding="utf-8"),
                full,
            )

    def test_separate_pole_shapefile_contains_quality_and_occlusion_fields(self) -> None:
        import shapefile

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "pole_bottoms.shp"
            write_pole_shapefile(
                [
                    {
                        "class_id": 65,
                        "class_name": "22000",
                        "confidence": 0.92,
                        "pole_x": 329435.8,
                        "pole_y": 4153753.7,
                        "pole_z": 44.4,
                        "detection_id": "Dfixture000000000001",
                        "support_id": "Pfixture000000000001",
                        "pole_type": "SINGLE",
                        "pole_method": "GROUND_EXTR",
                        "pole_status": "REVIEW",
                        "support_reconciled": True,
                        "support_reconciled_replaced_remote": True,
                        "support_hypothesis_distance_m": 0.04,
                        "pole_occluded": True,
                        "pole_occlusion_status": "OCCLUDED",
                        "pole_count": 1,
                        "obs_count": 2,
                        "detection_count": 3,
                        "occluded_count": 1,
                        "unknown_occlusion_count": 1,
                        "pole_point_count": 500,
                        "axis_rmse_m": 0.03,
                        "ground_rmse_m": 0.04,
                        "association_distance_m": 2.75,
                        "horizontal_connection_coverage_ratio": 0.80,
                        "horizontal_connection_coherent_coverage_ratio": 0.70,
                        "horizontal_connection_coherent_ratio": 0.875,
                        "horizontal_connection_coherent_point_fraction": 0.45,
                        "horizontal_connection_endpoint_anchored": True,
                        "completeness_ratio": 0.96,
                        "dominant_class_id": 84,
                        "dominant_class_fraction": 0.95,
                        "classification_mode_requested": "auto",
                        "classification_mode": "HYBRID",
                        "image_name": "image.jpg",
                        "timestamp_iso": "2025-03-11T00:00:00+00:00",
                        "pole_point_crop_path": "pole.las",
                        "run_fingerprint": "b" * 64,
                    }
                ],
                shp_path,
            )
            reader = shapefile.Reader(str(shp_path))
            record = reader.record(0).as_dict()
            self.assertEqual(record["pole_type"], "SINGLE")
            self.assertEqual(record["det_id"], "Dfixture000000000001")
            self.assertEqual(record["support_id"], "Pfixture000000000001")
            self.assertTrue(record["reconciled"])
            self.assertTrue(record["repl_rem"])
            self.assertAlmostEqual(record["hyp_dist"], 0.04)
            self.assertTrue(record["occluded"])
            self.assertEqual(record["occ_state"], "OCCLUDED")
            self.assertEqual(record["obs_count"], 2)
            self.assertEqual(record["det_count"], 3)
            self.assertEqual(record["occl_cnt"], 1)
            self.assertEqual(record["unk_occ"], 1)
            self.assertAlmostEqual(record["assoc_m"], 2.75)
            self.assertAlmostEqual(record["arm_cov"], 0.80)
            self.assertAlmostEqual(record["arm_3d"], 0.70)
            self.assertAlmostEqual(record["arm_ratio"], 0.875)
            self.assertAlmostEqual(record["arm_pts"], 0.45)
            self.assertTrue(record["arm_end"])
            self.assertAlmostEqual(record["complete"], 0.96)
            self.assertEqual(record["dom_class"], 84)
            self.assertEqual(record["class_req"], "auto")
            self.assertEqual(record["class_mode"], "HYBRID")
            reader.close()

    def test_pole_shapefile_keeps_duplicate_geometry_for_attached_signs(self) -> None:
        import shapefile

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "pole_bottoms.shp"
            common = {
                "pole_x": 10.0,
                "pole_y": 20.0,
                "pole_z": 1.0,
                "support_id": "Pshared0000000000001",
            }
            write_pole_shapefile(
                [
                    {**common, "detection_id": "Dsign000000000000001", "class_id": 65},
                    {**common, "detection_id": "Dsign000000000000002", "class_id": 72},
                ],
                shp_path,
            )
            reader = shapefile.Reader(str(shp_path))
            try:
                self.assertEqual(len(reader), 2)
                self.assertEqual(
                    {record.as_dict()["support_id"] for record in reader.records()},
                    {"Pshared0000000000001"},
                )
                self.assertEqual(
                    len({record.as_dict()["det_id"] for record in reader.records()}),
                    2,
                )
                self.assertEqual(
                    {tuple(shape.points[0]) for shape in reader.shapes()},
                    {(10.0, 20.0)},
                )
            finally:
                reader.close()

    def test_unknown_pole_occlusion_remains_null_in_shapefile(self) -> None:
        import shapefile

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = Path(temp_dir) / "pole_bottoms.shp"
            write_pole_shapefile(
                [
                    {
                        "pole_x": 1.0,
                        "pole_y": 2.0,
                        "pole_z": 3.0,
                        "pole_occluded": None,
                        "pole_occlusion_status": "UNKNOWN",
                    }
                ],
                shp_path,
            )
            reader = shapefile.Reader(str(shp_path))
            record = reader.record(0).as_dict()
            self.assertIsNone(record["occluded"])
            self.assertEqual(record["occ_state"], "UNKNOWN")
            self.assertEqual(record["unk_occ"], 1)
            reader.close()


if __name__ == "__main__":
    unittest.main()
