from __future__ import annotations

import argparse
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laspy
import numpy as np
from PIL import Image
from pyproj import CRS

from mms_shp_detection.pipeline import (
    POINT_CROP_SEMANTICS,
    POLE_CROP_SEMANTICS,
    build_arg_parser,
    build_dataset_signature,
    build_forward_detection_mapping,
    build_pole_debug_axis_segments,
    build_pole_debug_overview_view,
    build_pole_search_corridor_masks,
    build_rectified_detection_view,
    build_run_fingerprint,
    circular_bbox_iou_xyxy,
    collect_detection_points_at_range,
    create_forward_detection_qa_image,
    evaluate_point_range_fallback_quality,
    find_pole_bases_with_corridor_fallback,
    missing_result_artifacts,
    pole_classifications_for_policy,
    resolve_matched_crs_wkt,
    resolve_num_workers,
    resolve_pole_classification_policy,
    robust_front_surface_distance,
    run_panorama_alignment_qa,
    run_pipeline,
    safely_refresh_shapefile_from_txt,
    save_debug_crop,
    unwrap_panorama_x_coordinates,
    validate_crs_wkt,
    validate_panorama_image,
    validate_point_range_fallback_arguments,
    validate_pose_pointcloud_proximity,
    write_las,
    write_pole_las,
)
from mms_shp_detection.geometry import (
    pixel_to_world_ray,
    perspective_pixel_to_world_ray,
    world_ray_to_equirectangular_pixel,
    world_ray_to_perspective_pixel,
)


def _classification_catalog(
    counts: dict[int, int],
    *,
    source_type: str = "las",
) -> dict[str, object]:
    summary = {
        "dimension_present": source_type == "las",
        "point_count": sum(counts.values()),
        "nonzero_point_count": sum(
            count for class_id, count in counts.items() if class_id != 0
        ),
        "class_counts": {str(class_id): count for class_id, count in counts.items()},
    }
    return {
        "selected_source_type": source_type,
        "classification_summary": summary,
        "files": [
            {
                "path": f"sample.{source_type}",
                "classification_summary": summary,
            }
        ],
    }


class PoleClassificationPolicyTests(unittest.TestCase):
    def resolve(
        self,
        counts: dict[int, int],
        mode: str = "auto",
        *,
        pole_ids: tuple[int, ...] = (),
        source_type: str = "las",
    ) -> dict[str, object]:
        return resolve_pole_classification_policy(
            _classification_catalog(counts, source_type=source_type),
            requested_mode=mode,
            ground_class_ids=(2, 11),
            pole_class_ids=pole_ids,
            excluded_pole_class_ids=(3, 4, 5),
        )

    def test_auto_uses_geometry_for_unclassified_or_unmapped_custom_values(self) -> None:
        for counts, reason in (
            ({0: 100}, "unclassified_values_only"),
            ({0: 20, 84: 80}, "observed_classes_are_not_mapped"),
        ):
            with self.subTest(counts=counts):
                policy = self.resolve(counts)
                self.assertEqual(policy["effective_mode"], "GEOMETRY")
                self.assertFalse(policy["uses_classification"])
                self.assertEqual(policy["reason"], reason)

    def test_auto_uses_hybrid_for_configured_standard_or_custom_classes(self) -> None:
        standard = self.resolve({0: 20, 2: 50, 3: 30})
        self.assertEqual(standard["effective_mode"], "HYBRID")
        self.assertEqual(standard["matched_class_ids"], [2, 3])

        custom = self.resolve({84: 100}, pole_ids=(84,))
        self.assertEqual(custom["effective_mode"], "HYBRID")
        self.assertEqual(custom["matched_class_ids"], [84])

    def test_off_forces_geometry_even_when_semantic_classes_exist(self) -> None:
        policy = self.resolve({2: 50, 84: 50}, mode="off", pole_ids=(84,))
        self.assertEqual(policy["requested_mode"], "off")
        self.assertEqual(policy["effective_mode"], "GEOMETRY")
        self.assertFalse(policy["uses_classification"])
        self.assertEqual(policy["reason"], "forced_off")
        source_classes = np.asarray([2, 3, 84], dtype=np.int16)
        self.assertIsNone(pole_classifications_for_policy(source_classes, policy))
        np.testing.assert_array_equal(source_classes, [2, 3, 84])

        hybrid = self.resolve({2: 50, 84: 50}, pole_ids=(84,))
        self.assertIs(
            pole_classifications_for_policy(source_classes, hybrid),
            source_classes,
        )

    def test_require_rejects_unusable_las_and_pcdb(self) -> None:
        for counts, source_type in (({0: 100}, "las"), ({}, "pcdb")):
            with self.subTest(counts=counts, source_type=source_type):
                with self.assertRaisesRegex(ValueError, "classification_mode=require"):
                    self.resolve(counts, mode="require", source_type=source_type)

        required = self.resolve({2: 100}, mode="require")
        self.assertEqual(required["effective_mode"], "HYBRID")


class PanoramaAlignmentQaTests(unittest.TestCase):
    def test_zero_pixel_mad_is_a_stable_recommendation(self) -> None:
        args = SimpleNamespace(
            alignment_qa_enabled=True,
            panorama_yaw_offset_deg=0.0,
            panorama_pitch_offset_deg=0.0,
            pointcloud_neighbor_count=6,
            alignment_qa_sample_images=3,
            alignment_qa_max_points_per_image=1000,
            alignment_qa_search_radius_px=6,
            alignment_qa_trim_fraction=0.8,
            alignment_qa_min_range_m=2.0,
            alignment_qa_max_range_m=15.0,
            alignment_qa_min_valid_samples=3,
            alignment_qa_max_mad_px=2.0,
        )
        estimate = {
            "status": "ok",
            "valid_sample_count": 3,
            "estimated_yaw_residual_deg": 0.0,
            "estimated_pitch_residual_deg": 0.0,
            "dx_mad_px": 0.0,
            "dy_mad_px": 0.0,
            "samples": [
                {"dx_px": 0, "dy_px": 0},
                {"dx_px": 0, "dy_px": 0},
                {"dx_px": 0, "dy_px": 0},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "mms_shp_detection.pipeline.estimate_panorama_alignment",
            return_value=estimate,
        ):
            report = run_panorama_alignment_qa(
                [],
                {},
                args,
                Path(temp_dir) / "alignment.json",
                mock.Mock(),
            )

        self.assertEqual(report["status"], "recommendation")
        self.assertTrue(report["stable_recommendation"])


class PanoramaSeamTests(unittest.TestCase):
    def test_forward_detection_view_is_centered_on_panorama_forward(self) -> None:
        mapping = build_forward_detection_mapping(
            7040,
            3520,
            view_size=1280,
            hfov_deg=70.0,
            vfov_deg=70.0,
        )
        center_ray = perspective_pixel_to_world_ray(
            640.0,
            640.0,
            1280,
            1280,
            mapping["view_forward_vec"],
            mapping["view_right_vec"],
            mapping["view_up_vec"],
            70.0,
            70.0,
        )
        pano_u, pano_v = world_ray_to_equirectangular_pixel(
            center_ray,
            mapping["pano_forward_vec"],
            mapping["pano_right_vec"],
            mapping["pano_up_vec"],
            7040,
            3520,
        )
        self.assertAlmostEqual(pano_u, 3520.0)
        self.assertAlmostEqual(pano_v, 1760.0)
        edge_ray = perspective_pixel_to_world_ray(
            1280.0,
            640.0,
            1280,
            1280,
            mapping["view_forward_vec"],
            mapping["view_right_vec"],
            mapping["view_up_vec"],
            70.0,
            70.0,
        )
        edge_angle = np.degrees(
            np.arccos(np.clip(np.dot(edge_ray, mapping["view_forward_vec"]), -1.0, 1.0))
        )
        self.assertAlmostEqual(edge_angle, 35.0, places=5)

    def test_forward_qa_annotation_does_not_modify_yolo_source(self) -> None:
        source = np.full((128, 128, 3), 80, dtype=np.uint8)
        original = source.copy()
        qa_image = create_forward_detection_qa_image(
            source,
            hfov_deg=50.0,
            vfov_deg=50.0,
            max_center_ray_angle_deg=25.0,
        )
        annotated = np.asarray(qa_image)
        np.testing.assert_array_equal(source, original)
        self.assertEqual(annotated.shape, source.shape)
        self.assertTrue(np.any(annotated != source))

    def test_pole_debug_axis_separates_observed_and_extrapolated_ranges(self) -> None:
        candidate = SimpleNamespace(
            base_xyz=np.asarray([1.0, 2.0, 10.0]),
            axis_direction=np.asarray([0.1, 0.0, 1.0]),
            observed_z_min=12.0,
            observed_z_max=15.0,
            vertical_span_m=3.0,
        )
        segments = build_pole_debug_axis_segments(candidate)
        observed = segments["observed"]
        extrapolated = segments["ground_extrapolated"]
        self.assertIsNotNone(observed)
        self.assertIsNotNone(extrapolated)
        np.testing.assert_allclose(observed[0], [1.2, 2.0, 12.0])
        np.testing.assert_allclose(observed[1], [1.5, 2.0, 15.0])
        np.testing.assert_allclose(extrapolated[0], [1.0, 2.0, 10.0])
        np.testing.assert_allclose(extrapolated[1], observed[0])

    def test_pole_debug_axis_supports_legacy_lowest_observation_field(self) -> None:
        candidate = SimpleNamespace(
            base_xyz=np.asarray([0.0, 0.0, 1.0]),
            axis_direction=np.asarray([0.0, 0.0, 1.0]),
            lowest_observed_z=2.0,
            vertical_span_m=2.5,
        )
        segments = build_pole_debug_axis_segments(candidate)
        np.testing.assert_allclose(segments["observed"][0], [0.0, 0.0, 2.0])
        np.testing.assert_allclose(segments["observed"][1], [0.0, 0.0, 4.5])

    def test_remote_pole_corridor_expands_only_below_sign_and_beyond_direct_range(
        self,
    ) -> None:
        points = np.asarray(
            [
                [0.30, 0.0, 2.0],
                [6.75, 0.0, 2.0],
                [6.75, 0.0, 5.5],
                [6.75, 0.0, 2.0],
                [0.30, 0.0, 2.0],
            ],
            dtype=np.float64,
        )
        pixels = np.asarray(
            [
                [500.0, 700.0],
                [927.0, 823.0],
                [927.0, 450.0],
                [927.0, 823.0],
                [927.0, 823.0],
            ],
            dtype=np.float64,
        )
        valid = np.asarray([True, True, True, False, True])
        parameters = SimpleNamespace(direct_max_axis_sign_distance_m=0.75)

        strict, expanded = build_pole_search_corridor_masks(
            points,
            pixels,
            valid,
            np.asarray([0.0, 0.0, 5.0]),
            (324.0, 481.0, 700.0, 1023.0),
            parameters,
        )

        np.testing.assert_array_equal(strict, [True, False, False, False, False])
        np.testing.assert_array_equal(expanded, [True, True, False, False, False])

    def test_pole_search_expands_for_missing_or_remote_but_not_direct_result(self) -> None:
        points = np.asarray([[0.3, 0.0, 2.0], [6.75, 0.0, 2.0]])
        pixels = np.asarray([[500.0, 700.0], [927.0, 823.0]])
        valid = np.asarray([True, True])
        classifications = np.asarray([84, 84], dtype=np.int16)
        parameters = SimpleNamespace(direct_max_axis_sign_distance_m=0.75)
        recovered = SimpleNamespace(representative_xyz=np.asarray([6.75, 0.0, 0.0]))

        with mock.patch(
            "mms_shp_detection.pipeline.find_pole_bases",
            side_effect=[None, recovered],
        ) as finder:
            result, searched, mode, strict_count, expanded_count = (
                find_pole_bases_with_corridor_fallback(
                    points,
                    pixels,
                    valid,
                    np.asarray([0.0, 0.0, 5.0]),
                    (324.0, 481.0, 700.0, 1023.0),
                    parameters,
                    classifications,
                )
            )

        self.assertIs(result, recovered)
        self.assertEqual(mode, "remote_expanded")
        self.assertEqual((strict_count, expanded_count), (1, 2))
        np.testing.assert_array_equal(searched, [True, True])
        self.assertEqual(finder.call_count, 2)
        np.testing.assert_array_equal(finder.call_args_list[0].args[1], [True, False])
        np.testing.assert_array_equal(finder.call_args_list[1].args[1], [True, True])

        strict_result = SimpleNamespace(
            representative_xyz=np.zeros(3),
            candidates=(SimpleNamespace(association_distance_m=0.3),),
        )
        with mock.patch(
            "mms_shp_detection.pipeline.find_pole_bases",
            return_value=strict_result,
        ) as finder:
            result, _searched, mode, _strict_count, _expanded_count = (
                find_pole_bases_with_corridor_fallback(
                    points,
                    pixels,
                    valid,
                    np.asarray([0.0, 0.0, 5.0]),
                    (324.0, 481.0, 700.0, 1023.0),
                    parameters,
                    classifications,
                )
            )
        self.assertIs(result, strict_result)
        self.assertEqual(mode, "strict")
        finder.assert_called_once()

        strict_remote = SimpleNamespace(
            representative_xyz=np.asarray([7.2, 0.0, 0.0]),
            candidates=(SimpleNamespace(association_distance_m=7.2),),
        )
        better_remote = SimpleNamespace(
            representative_xyz=np.asarray([3.9, 0.0, 0.0]),
            candidates=(SimpleNamespace(association_distance_m=3.9),),
        )
        with mock.patch(
            "mms_shp_detection.pipeline.find_pole_bases",
            side_effect=[strict_remote, better_remote],
        ) as finder:
            result, searched, mode, _strict_count, _expanded_count = (
                find_pole_bases_with_corridor_fallback(
                    points,
                    pixels,
                    valid,
                    np.asarray([0.0, 0.0, 5.0]),
                    (324.0, 481.0, 700.0, 1023.0),
                    parameters,
                    classifications,
                )
            )
        self.assertIs(result, better_remote)
        self.assertEqual(mode, "remote_expanded")
        np.testing.assert_array_equal(searched, [True, True])
        self.assertEqual(finder.call_count, 2)

        strict_better = SimpleNamespace(
            representative_xyz=np.asarray([3.9, 0.0, 0.0]),
            candidates=(SimpleNamespace(association_distance_m=3.9),),
        )
        expanded_worse = SimpleNamespace(
            representative_xyz=np.asarray([7.2, 0.0, 0.0]),
            candidates=(SimpleNamespace(association_distance_m=7.2),),
        )
        with mock.patch(
            "mms_shp_detection.pipeline.find_pole_bases",
            side_effect=[strict_better, expanded_worse],
        ):
            result, _searched, mode, _strict_count, _expanded_count = (
                find_pole_bases_with_corridor_fallback(
                    points,
                    pixels,
                    valid,
                    np.asarray([0.0, 0.0, 5.0]),
                    (324.0, 481.0, 700.0, 1023.0),
                    parameters,
                    classifications,
                )
            )
        self.assertIs(result, strict_better)
        self.assertEqual(mode, "remote_expanded")

    def test_adaptive_pole_debug_overview_contains_clipped_base_and_ground(self) -> None:
        pano_height, pano_width = 360, 720
        image_rgb = np.zeros((pano_height, pano_width, 3), dtype=np.uint8)
        image_task = {
            "direction": [1.0, 0.0, 0.0],
            "up": [0.0, 0.0, 1.0],
        }
        detection_payload = {
            "bbox_xyxy": [350.0, 130.0, 370.0, 150.0],
            "mask_polygon": [
                [350.0, 130.0],
                [370.0, 130.0],
                [370.0, 150.0],
                [350.0, 150.0],
            ],
        }
        runtime = {
            "panorama_yaw_offset_deg": 0.0,
            "panorama_pitch_offset_deg": 0.0,
            "perspective_margin_deg": 2.0,
            "perspective_min_fov_deg": 55.0,
            "perspective_max_fov_deg": 110.0,
            "perspective_view_size": 1024,
            "pole_min_fov_deg": 55.0,
        }
        fallback_view = build_rectified_detection_view(
            image_task=image_task,
            image_rgb=image_rgb,
            detection_payload=detection_payload,
            runtime=runtime,
        )
        origin_xyz = np.zeros(3, dtype=np.float64)
        base_xyz = np.asarray([10.0, 0.0, -8.0], dtype=np.float64)
        ground_xyz = np.asarray(
            [
                [9.5, -0.5, -8.0],
                [10.0, 0.0, -8.1],
                [10.5, 0.5, -7.9],
            ],
            dtype=np.float64,
        )
        candidate = SimpleNamespace(
            base_xyz=base_xyz,
            axis_direction=np.asarray([0.0, 0.0, 1.0], dtype=np.float64),
            observed_z_min=-3.0,
            observed_z_max=2.0,
            vertical_span_m=5.0,
            ground_estimate=SimpleNamespace(support_xyz=ground_xyz),
        )
        result = SimpleNamespace(
            representative_xyz=base_xyz,
            candidates=(candidate,),
        )

        base_ray = base_xyz / np.linalg.norm(base_xyz)
        _fallback_x, fallback_y, fallback_depth = world_ray_to_perspective_pixel(
            base_ray,
            fallback_view["view_forward_vec"],
            fallback_view["view_right_vec"],
            fallback_view["view_up_vec"],
            1024,
            1024,
            fallback_view["hfov_deg"],
            fallback_view["vfov_deg"],
        )
        self.assertGreater(fallback_depth, 0.0)
        self.assertGreaterEqual(fallback_y, 1024.0)

        overview = build_pole_debug_overview_view(
            image_rgb=image_rgb,
            detection_payload=detection_payload,
            pole_result=result,
            origin_xyz=origin_xyz,
            fallback_view=fallback_view,
            runtime=runtime,
        )
        self.assertTrue(overview["debug_overview_adaptive"])
        self.assertEqual(overview["rectified_rgb"].shape, (1024, 1024, 3))
        self.assertAlmostEqual(overview["hfov_deg"], overview["vfov_deg"])
        self.assertGreaterEqual(overview["hfov_deg"], 55.0)
        self.assertLessEqual(overview["hfov_deg"], 110.0)
        self.assertGreater(
            float(np.dot(overview["view_up_vec"], overview["pano_up_vec"])),
            0.0,
        )

        sign_rays = [
            pixel_to_world_ray(
                pixel_x,
                pixel_y,
                pano_width,
                pano_height,
                overview["pano_forward_vec"],
                overview["pano_right_vec"],
                overview["pano_up_vec"],
            )
            for pixel_x, pixel_y in (
                (350.0, 130.0),
                (370.0, 130.0),
                (350.0, 150.0),
                (370.0, 150.0),
            )
        ]
        observed_xyz = np.asarray([[10.0, 0.0, -3.0], [10.0, 0.0, 2.0]])
        world_rays = [
            *(point / np.linalg.norm(point) for point in observed_xyz),
            base_ray,
            *(point / np.linalg.norm(point) for point in ground_xyz),
        ]
        for ray in [*sign_rays, *world_rays]:
            pixel_x, pixel_y, depth = world_ray_to_perspective_pixel(
                ray,
                overview["view_forward_vec"],
                overview["view_right_vec"],
                overview["view_up_vec"],
                1024,
                1024,
                overview["hfov_deg"],
                overview["vfov_deg"],
            )
            self.assertGreater(depth, 0.0)
            self.assertGreater(pixel_x, 0.0)
            self.assertLess(pixel_x, 1024.0)
            self.assertGreater(pixel_y, 0.0)
            self.assertLess(pixel_y, 1024.0)

    def test_unwrap_keeps_seam_detection_narrow(self) -> None:
        unwrapped = unwrap_panorama_x_coordinates([7020.0, 20.0, 40.0], 7040.0)
        self.assertLess(max(unwrapped) - min(unwrapped), 100.0)

    def test_circular_iou_compares_equivalent_wrappings(self) -> None:
        iou = circular_bbox_iou_xyxy(
            (7000.0, 100.0, 7080.0, 200.0),
            (-40.0, 100.0, 40.0, 200.0),
            7040.0,
        )
        self.assertAlmostEqual(iou, 1.0)

    def test_sphere_sidecar_dimensions_are_enforced(self) -> None:
        task = {
            "image_name": "fixture.jpg",
            "panorama": {
                "projection": "equirectangular",
                "image_width": 8,
                "image_height": 4,
                "longitude_limits_deg": [-180.0, 180.0],
                "latitude_limits_deg": [-90.0, 90.0],
                "panorama_hotspot": [0.0, 0.0],
            },
        }
        validate_panorama_image(task, np.zeros((4, 8, 3), dtype=np.uint8))
        with self.assertRaisesRegex(ValueError, "width mismatch"):
            validate_panorama_image(task, np.zeros((4, 7, 3), dtype=np.uint8))


class FrontSurfaceTests(unittest.TestCase):
    def test_single_near_outlier_does_not_define_surface(self) -> None:
        distances = np.asarray([1.0] + [10.0] * 99, dtype=np.float64)
        anchor = robust_front_surface_distance(distances, quantile=0.02, min_support=6)
        self.assertEqual(anchor, 10.0)


class PointRangeFallbackTests(unittest.TestCase):
    @staticmethod
    def _runtime() -> dict[str, object]:
        return {
            "point_range_fallback_min_point_count": 60,
            "point_range_fallback_min_cluster_fraction": 0.80,
            "point_range_fallback_min_core_mask_fraction": 0.45,
            "point_range_fallback_max_depth_span_m": 0.50,
        }

    @staticmethod
    def _view() -> dict[str, object]:
        return {
            "rectified_polygon": None,
            "rectified_bbox": (10.0, 10.0, 30.0, 30.0),
            "view_width": 40,
            "view_height": 40,
        }

    @staticmethod
    def _cluster(
        *,
        point_count: int = 77,
        raw_point_count: int = 77,
        cluster_count: int = 1,
        pixel_xy: tuple[int, int] = (20, 20),
        distance_stop: float = 12.8,
    ) -> dict[str, object]:
        return {
            "pixels_xy": np.tile(
                np.asarray(pixel_xy, dtype=np.int32),
                (point_count, 1),
            ),
            "distances": np.linspace(12.6, distance_stop, point_count),
            "cluster_point_count": point_count,
            "raw_point_count": raw_point_count,
            "cluster_count": cluster_count,
        }

    def test_sparse_single_cluster_inside_core_mask_is_accepted(self) -> None:
        result = evaluate_point_range_fallback_quality(
            self._cluster(),
            (20.0, 20.0),
            self._view(),
            self._runtime(),
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "accepted")
        self.assertEqual(result["cluster_fraction"], 1.0)
        self.assertEqual(result["core_mask_fraction"], 1.0)
        self.assertLess(result["depth_span_m"], 0.50)

    def test_00224_sparse_core_profile_passes_repository_gate(self) -> None:
        cluster = self._cluster()
        cluster["pixels_xy"] = np.vstack(
            (
                np.tile(np.asarray([20, 20]), (38, 1)),
                np.tile(np.asarray([35, 35]), (39, 1)),
            )
        )
        result = evaluate_point_range_fallback_quality(
            cluster,
            (20.0, 20.0),
            self._view(),
            self._runtime(),
        )
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["core_mask_fraction"], 38 / 77)

    def test_sparse_fallback_quality_gates_reject_unsafe_clusters(self) -> None:
        cases = {
            "few": (self._cluster(point_count=59, raw_point_count=59), (20.0, 20.0), "point_count_lt_60"),
            "multiple": (self._cluster(cluster_count=2), (20.0, 20.0), "cluster_count_ne_1"),
            "dilute": (self._cluster(raw_point_count=100), (20.0, 20.0), "cluster_fraction_lt_0.80"),
            "outside": (self._cluster(pixel_xy=(35, 35)), (35.0, 35.0), "representative_outside_core_mask"),
            "deep": (self._cluster(distance_stop=13.4), (20.0, 20.0), "depth_span_gt_0.50m"),
        }
        for name, (cluster, representative, reason) in cases.items():
            with self.subTest(name=name):
                result = evaluate_point_range_fallback_quality(
                    cluster,
                    representative,
                    self._view(),
                    self._runtime(),
                )
                self.assertFalse(result["accepted"])
                self.assertIn(reason, result["reason"])

    def test_collection_applies_the_requested_range_to_blocks_and_points(self) -> None:
        pointcloud_file = {"path": "fixture.las", "blocks": []}
        cache = mock.Mock()
        cache.read_block_points.return_value = (
            np.asarray([[0.0, 11.0, 0.0], [0.0, 13.0, 0.0]]),
            np.zeros((2, 3), dtype=np.uint8),
            np.zeros((2,), dtype=np.uint16),
        )
        view = {
            "center_ray": np.asarray([0.0, 1.0, 0.0]),
            "detection_angle": 0.1,
            "view_forward_vec": np.asarray([0.0, 1.0, 0.0]),
            "view_right_vec": np.asarray([1.0, 0.0, 0.0]),
            "view_up_vec": np.asarray([0.0, 0.0, 1.0]),
            "view_width": 40,
            "view_height": 40,
            "hfov_deg": 40.0,
            "vfov_deg": 40.0,
        }
        with mock.patch(
            "mms_shp_detection.pipeline.select_candidate_blocks",
            return_value=[{"name": "block"}],
        ) as selector:
            strict = collect_detection_points_at_range(
                [pointcloud_file],
                cache,
                np.zeros(3),
                view,
                np.full((40, 40), 255, dtype=np.uint8),
                (0, 0, 40, 40),
                maximum_range_m=12.0,
                angle_margin_rad=0.0,
            )
        self.assertEqual(strict["points_xyz"].shape[0], 1)
        self.assertEqual(float(strict["distances"][0]), 11.0)
        self.assertEqual(selector.call_args.args[4], 12.0)

    def test_cross_field_validation_rejects_invalid_cli_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be greater"):
            validate_point_range_fallback_arguments(
                argparse.Namespace(
                    point_range_fallback_enabled=True,
                    max_range_m=12.0,
                    point_range_fallback_max_range_m=12.0,
                    min_point_count=100,
                    point_range_fallback_min_point_count=60,
                )
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            validate_point_range_fallback_arguments(
                argparse.Namespace(
                    point_range_fallback_enabled=True,
                    max_range_m=12.0,
                    point_range_fallback_max_range_m=15.0,
                    min_point_count=100,
                    point_range_fallback_min_point_count=101,
                )
            )


class WorkerSafetyTests(unittest.TestCase):
    def test_cuda_workers_are_capped_to_one_configured_device(self) -> None:
        args = argparse.Namespace(num_workers=4, allow_unsafe_cuda_multiprocessing=False)
        self.assertEqual(resolve_num_workers(args, "cuda:0", mock.Mock()), 1)
        args.allow_unsafe_cuda_multiprocessing = True
        self.assertEqual(resolve_num_workers(args, "cuda:0", mock.Mock()), 4)

    def test_intermediate_shapefile_failure_is_logged_not_raised(self) -> None:
        logger = mock.Mock()
        with mock.patch(
            "mms_shp_detection.pipeline.refresh_shapefile_from_txt",
            side_effect=PermissionError("locked by GIS"),
        ):
            result = safely_refresh_shapefile_from_txt(
                Path("txt"),
                Path("detected_signs.in_progress.shp"),
                logger,
                reason="fixture",
                run_fingerprint="x",
                crs_wkt=None,
            )
        self.assertIsNone(result)
        logger.exception.assert_called_once()


class CrsAndDerivedArtifactTests(unittest.TestCase):
    def _pointcloud_fixture(self, wkt: str) -> dict:
        return {
            "path": "fixture.las",
            "job_name": "Job_A",
            "track_name": "Track01",
            "file_min": [329000.0, 4153000.0, 0.0],
            "file_max": [330000.0, 4154000.0, 100.0],
            "crs_wkt": wkt,
        }

    def test_semantically_equivalent_crs_wkts_are_accepted_and_normalized(self) -> None:
        crs = CRS.from_epsg(32652)
        first = self._pointcloud_fixture(crs.to_wkt(version="WKT2_2019"))
        second = self._pointcloud_fixture(crs.to_wkt(version="WKT1_GDAL"))
        second["path"] = "fixture_2.las"
        task = {
            "image_name": "fixture.jpg",
            "job_name": "Job_A",
            "track_name": "Track01",
            "origin": [329500.0, 4153500.0, 50.0],
        }
        resolved = resolve_matched_crs_wkt([task], {"files": [first, second]}, 1)
        self.assertTrue(CRS.from_wkt(resolved).equals(crs))
        with self.assertRaisesRegex(ValueError, "Invalid CRS WKT"):
            validate_crs_wkt("not a WKT", label="fixture")

    def test_pose_pointcloud_proximity_catches_gross_mismatch(self) -> None:
        crs_wkt = CRS.from_epsg(32652).to_wkt()
        catalog = {"files": [self._pointcloud_fixture(crs_wkt)]}
        near_task = {
            "image_name": "near.jpg",
            "job_name": "Job_A",
            "track_name": "Track01",
            "origin": [329500.0, 4153500.0, 50.0],
        }
        self.assertEqual(validate_pose_pointcloud_proximity([near_task], catalog, 1, 1000.0), 0.0)
        far_task = dict(near_task, image_name="far.jpg", origin=[127.0, 37.0, 50.0])
        with self.assertRaisesRegex(ValueError, "CRS/job mismatch"):
            validate_pose_pointcloud_proximity([far_task], catalog, 1, 1000.0)

    def test_missing_referenced_artifact_invalidates_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "crop.jpg"
            artifact.write_bytes(b"fixture")
            payload = {
                "detections": [
                    {
                        "image_crop_path": str(artifact),
                        "point_crop_path": None,
                        "point_preview_path": None,
                    }
                ]
            }
            self.assertEqual(missing_result_artifacts(payload), [])
            artifact.unlink()
            self.assertEqual(missing_result_artifacts(payload), [str(artifact)])

    def test_missing_forward_qa_view_invalidates_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "forward.jpg"
            artifact.write_bytes(b"fixture")
            payload = {
                "panorama_detection": {"forward_view_path": str(artifact)},
                "detections": [],
            }
            self.assertEqual(missing_result_artifacts(payload), [])
            artifact.unlink()
            self.assertEqual(missing_result_artifacts(payload), [str(artifact)])

    def test_missing_pole_artifact_invalidates_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "pole.las"
            artifact.write_bytes(b"fixture")
            payload = {
                "detections": [
                    {
                        "image_crop_path": None,
                        "point_crop_path": None,
                        "point_preview_path": None,
                        "pole": {"point_crop_path": str(artifact), "debug_image_path": None},
                    }
                ]
            }
            self.assertEqual(missing_result_artifacts(payload), [])
            artifact.unlink()
            self.assertEqual(missing_result_artifacts(payload), [str(artifact)])

    def test_pole_processing_error_is_never_reused_by_skip_existing(self) -> None:
        payload = {
            "detections": [
                {
                    "image_crop_path": None,
                    "point_crop_path": None,
                    "point_preview_path": None,
                    "pole": {
                        "enabled": True,
                        "found": False,
                        "reason": "processing_error",
                    },
                }
            ]
        }
        self.assertEqual(missing_result_artifacts(payload), ["<pole processing error>"])

    def test_derived_las_declares_non_record_preserving_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "derived.las"
            write_las(
                np.asarray([[329500.001, 4153500.002, 50.003]], dtype=np.float64),
                np.asarray([[10, 20, 30]], dtype=np.uint8),
                output,
                crs_wkt=CRS.from_epsg(32652).to_wkt(),
            )
            with laspy.open(output) as reader:
                self.assertEqual(reader.header.point_format.id, 2)
                self.assertEqual(reader.header.system_identifier, "MMS_SIGN_DERIVED")
                self.assertTrue(reader.header.parse_crs().equals(CRS.from_epsg(32652)))
                self.assertNotIn("gps_time", reader.header.point_format.dimension_names)
            self.assertFalse(POINT_CROP_SEMANTICS["source_point_attributes_preserved"])

    def test_pole_las_preserves_available_core_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "pole.las"
            records = {
                "xyz": np.asarray(
                    [[329500.001, 4153500.002, 50.003], [329500.101, 4153500.102, 51.003]],
                    dtype=np.float64,
                ),
                "rgb": np.asarray([[10, 20, 30], [40, 50, 60]], dtype=np.uint8),
                "intensity": np.asarray([100, 200], dtype=np.uint16),
                "classification": np.asarray([2, 84], dtype=np.int16),
                "gps_time": np.asarray([123.0, 456.0], dtype=np.float64),
                "gps_time_type": np.asarray([1, 1], dtype=np.int8),
                "return_number": np.asarray([1, 2], dtype=np.uint8),
                "number_of_returns": np.asarray([1, 2], dtype=np.uint8),
                "source_index": np.asarray([10, 11], dtype=np.int64),
            }
            write_pole_las(
                records,
                np.asarray([1], dtype=np.int64),
                output,
                crs_wkt=CRS.from_epsg(32652).to_wkt(),
            )
            with laspy.open(output) as reader:
                data = reader.read()
                self.assertEqual(reader.header.point_format.id, 7)
                self.assertEqual(reader.header.system_identifier, "MMS_POLE_DERIVED")
                self.assertEqual(int(data.classification[0]), 84)
                self.assertEqual(int(data.intensity[0]), 200)
                self.assertEqual(float(data.gps_time[0]), 456.0)
                self.assertEqual(int(reader.header.global_encoding.gps_time_type), 1)
            self.assertIn(
                "classification",
                POLE_CROP_SEMANTICS["source_point_attributes_preserved"],
            )
            self.assertIn(
                "GPS time encoding (LAS global encoding bit 0)",
                POLE_CROP_SEMANTICS["source_point_attributes_preserved"],
            )

    def test_pole_las_rejects_mixed_source_gps_time_encodings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "mixed-time.las"
            records = {
                "xyz": np.asarray([[1.0, 2.0, 3.0], [1.1, 2.1, 4.0]], dtype=np.float64),
                "rgb": np.asarray([[10, 20, 30], [40, 50, 60]], dtype=np.uint8),
                "intensity": np.asarray([100, 200], dtype=np.uint16),
                "classification": np.asarray([84, 84], dtype=np.int16),
                "gps_time": np.asarray([123.0, 1_000_000_456.0], dtype=np.float64),
                "gps_time_type": np.asarray([0, 1], dtype=np.int8),
                "return_number": np.asarray([1, 1], dtype=np.uint8),
                "number_of_returns": np.asarray([1, 1], dtype=np.uint8),
                "source_index": np.asarray([10, 11], dtype=np.int64),
            }

            with self.assertRaisesRegex(ValueError, "different LAS GPS time encodings"):
                write_pole_las(records, np.asarray([0, 1]), output)
            self.assertFalse(output.exists())

            records["gps_time_type"] = np.asarray([-1, 1], dtype=np.int8)
            missing_output = Path(temp_dir) / "missing-time.las"
            with self.assertRaisesRegex(ValueError, "known and unknown LAS GPS time"):
                write_pole_las(records, np.asarray([0, 1]), missing_output)
            self.assertFalse(missing_output.exists())

    def test_debug_mask_alpha_is_composited_instead_of_replacing_image_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "debug.jpg"
            source = np.full((80, 80, 3), 100, dtype=np.uint8)
            save_debug_crop(
                source,
                [[20.0, 20.0], [60.0, 20.0], [60.0, 60.0], [20.0, 60.0]],
                (20.0, 20.0, 60.0, 60.0),
                output,
                padding_px=10,
                mask_alpha=8,
                label="fixture",
            )
            rendered = np.asarray(Image.open(output).convert("RGB"))
            # Alpha 8 should produce only a subtle yellow tint, not an opaque fill.
            center = rendered[30, 30].astype(np.int16)
            self.assertLess(int(center.max() - center.min()), 15)


class DatasetFingerprintTests(unittest.TestCase):
    def _task_fixture(self, root: Path, image_index: int) -> dict:
        job_name = "Job_20250311_1043"
        image_path = root / f"image_{image_index:05d}.jpg"
        image_path.write_bytes(f"jpeg-{image_index}".encode("ascii"))
        pose_path = root / "poses.csv"
        if not pose_path.exists():
            pose_path.write_text("pose fixture\n", encoding="utf-8")
        sidecar_path = root / "Sphere.txt"
        if not sidecar_path.exists():
            sidecar_path.write_text("ImageSize=7040,3520\n", encoding="utf-8")
        return {
            "image_path": str(image_path.resolve()),
            "image_name": image_path.name,
            "image_stem": image_path.stem,
            "record_name": f"{job_name}_Track01",
            "route_id": job_name,
            "job_name": job_name,
            "track_name": "Track01",
            "pose_csv_path": str(pose_path.resolve()),
            "pose_format": "leica-sphere",
            "pose_row_number": image_index,
            "timestamp_iso": f"2025-03-11T02:00:{image_index:02d}+00:00",
            "timestamp_source": "gps_sow",
            "gps_sow_seconds": 180000.0 + image_index,
            "gps_week": 2357,
            "gps_week_source": "job.db",
            "gps_week_inferred": False,
            "gps_utc_offset_seconds": 18,
            "origin": [329700.0 + image_index, 4153500.0, 42.0],
            "direction": [0.0, 1.0, 0.0],
            "up": [0.0, 0.0, 1.0],
            "right": [1.0, 0.0, 0.0],
            "rotation_local_to_world": [
                [1.0, 0.0, 0.0],
                [0.0, 0.0, -1.0],
                [0.0, 1.0, 0.0],
            ],
            "omega_gon": 0.0,
            "phi_gon": 0.0,
            "kappa_gon": 0.0,
            "panorama": {
                "projection": "equirectangular",
                "sidecar_path": str(sidecar_path.resolve()),
                "image_width": 7040,
                "image_height": 3520,
                "longitude_limits_deg": [-180.0, 180.0],
                "latitude_limits_deg": [-90.0, 90.0],
                "panorama_hotspot": [0.0, 0.0],
                "sphere_radius_m": 100.0,
            },
            "calibration": {
                "calibration_sha256": "a" * 64,
                "job": f"{job_name}.job",
                "track": "Track01.scan",
                "imaging_sensor_id": 1,
                "imaging_sensor_name": "Sphere",
                "raw_camera_serials": ["front", "rear"],
                "gps_week": 2357,
                "application": "validated_only_already_applied_to_leica_sphere",
            },
        }

    def test_dataset_signature_invalidates_pose_sidecar_image_and_calibration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tasks = [self._task_fixture(root, 1), self._task_fixture(root, 2)]
            baseline = build_dataset_signature(tasks)

            self.assertEqual(build_dataset_signature(list(reversed(tasks))), baseline)

            changed_pose = copy.deepcopy(tasks)
            changed_pose[0]["origin"][0] += 0.001
            self.assertNotEqual(build_dataset_signature(changed_pose)["sha256"], baseline["sha256"])

            changed_metadata = copy.deepcopy(tasks)
            changed_metadata[0]["panorama"]["sphere_radius_m"] = 101.0
            self.assertNotEqual(
                build_dataset_signature(changed_metadata)["sha256"], baseline["sha256"]
            )

            changed_calibration = copy.deepcopy(tasks)
            changed_calibration[0]["calibration"]["calibration_sha256"] = "b" * 64
            self.assertNotEqual(
                build_dataset_signature(changed_calibration)["sha256"], baseline["sha256"]
            )

            before_image_change = build_dataset_signature(tasks)
            Path(tasks[0]["image_path"]).write_bytes(b"replacement-jpeg-with-new-size")
            after_image_change = build_dataset_signature(tasks)
            self.assertNotEqual(after_image_change["sha256"], before_image_change["sha256"])

            before_sidecar_change = build_dataset_signature(tasks)
            Path(tasks[0]["panorama"]["sidecar_path"]).write_text(
                "ImageSize=7040,3520\nSphereRadius=101\n",
                encoding="utf-8",
            )
            after_sidecar_change = build_dataset_signature(tasks)
            self.assertNotEqual(after_sidecar_change["sha256"], before_sidecar_change["sha256"])

    def test_run_fingerprint_is_slice_independent_but_tracks_dataset_and_pointcloud(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model fixture")
            common_args = {
                "data_root": root,
                "model_path": model_path,
                "output_dir": root / "outputs",
                "pointcloud_cache_path": root / "catalog.json",
                "point_source": "las",
                "skip_existing": True,
                "num_workers": 1,
                "allow_unsafe_cuda_multiprocessing": False,
                "worker_progress_every": 10,
                "progress_log_interval_sec": 60,
                "disable_intermediate_shp": True,
            }
            first_slice = argparse.Namespace(
                **common_args,
                start_index=0,
                limit_images=443,
            )
            second_slice = argparse.Namespace(
                **common_args,
                start_index=443,
                limit_images=359,
            )
            pointcloud_catalog = {
                "selected_source_type": "las",
                "resolved_crs_wkt": 'PROJCS["fixture"]',
                "signature": {
                    "include_job_keys": ["joba", "jobb"],
                    "source_files": [
                        {"path": "a.las", "file_size": 10, "mtime_ns": 1},
                        {"path": "b.las", "file_size": 20, "mtime_ns": 2},
                    ],
                },
            }
            dataset_signature = {
                "signature_version": 1,
                "task_count": 802,
                "image_file_count": 802,
                "pose_file_count": 2,
                "sidecar_file_count": 2,
                "sha256": "c" * 64,
            }
            calibration_bundle = {"sha256": "d" * 64}

            first_fingerprint = build_run_fingerprint(
                first_slice,
                pointcloud_catalog,
                calibration_bundle,
                dataset_signature,
            )
            second_fingerprint = build_run_fingerprint(
                second_slice,
                pointcloud_catalog,
                calibration_bundle,
                dataset_signature,
            )
            self.assertEqual(first_fingerprint, second_fingerprint)

            changed_dataset = dict(dataset_signature, sha256="e" * 64)
            self.assertNotEqual(
                build_run_fingerprint(
                    first_slice,
                    pointcloud_catalog,
                    calibration_bundle,
                    changed_dataset,
                ),
                first_fingerprint,
            )
            changed_catalog = copy.deepcopy(pointcloud_catalog)
            changed_catalog["signature"]["source_files"][1]["mtime_ns"] = 3
            self.assertNotEqual(
                build_run_fingerprint(
                    first_slice,
                    changed_catalog,
                    calibration_bundle,
                    dataset_signature,
                ),
                first_fingerprint,
            )


class PipelineInputScopeTests(unittest.TestCase):
    def test_catalog_and_dataset_signature_use_all_jobs_before_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = build_arg_parser().parse_args(
                [
                    "--data-root",
                    str(root),
                    "--output-dir",
                    str(root / "outputs"),
                    "--pointcloud-cache-path",
                    str(root / "catalog.json"),
                    "--device",
                    "cpu",
                    "--start-index",
                    "1",
                    "--limit-images",
                    "1",
                    "--disable-intermediate-shp",
                ]
            )
            tasks = [
                {
                    "timestamp_iso": "2025-03-11T00:00:01+00:00",
                    "image_path": str(root / "a.jpg"),
                    "job_name": "Job_A",
                    "pose_format": "leica-sphere",
                },
                {
                    "timestamp_iso": "2025-03-11T00:00:02+00:00",
                    "image_path": str(root / "b.jpg"),
                    "job_name": "Job_B",
                    "pose_format": "leica-sphere",
                },
            ]
            dataset_signature = {
                "signature_version": 1,
                "task_count": 2,
                "image_file_count": 2,
                "pose_file_count": 2,
                "sidecar_file_count": 2,
                "sha256": "a" * 64,
            }
            catalog = {
                "selected_source_type": "las",
                "signature": {"source_files": []},
                "files": [],
            }

            with (
                mock.patch(
                    "mms_shp_detection.pipeline.setup_logging",
                    return_value=mock.Mock(),
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.scan_image_tasks",
                    return_value=tasks,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.attach_calibration_metadata",
                    return_value={"sha256": "b" * 64},
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.build_dataset_signature",
                    return_value=dataset_signature,
                ) as signature_mock,
                mock.patch(
                    "mms_shp_detection.pipeline.build_pointcloud_catalog",
                    return_value=catalog,
                ) as catalog_mock,
                mock.patch(
                    "mms_shp_detection.pipeline.resolve_matched_crs_wkt",
                    return_value='PROJCS["fixture"]',
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.validate_pose_pointcloud_proximity",
                    return_value=0.0,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.build_run_fingerprint",
                    return_value="c" * 64,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.worker_process",
                    return_value={"images": 1, "detections": 0, "points": 0, "failures": 0},
                ) as worker_mock,
                mock.patch(
                    "mms_shp_detection.pipeline.collect_detection_records",
                    return_value=[],
                ),
                mock.patch("mms_shp_detection.pipeline.write_shapefile"),
                mock.patch("mms_shp_detection.pipeline.publish_shapefile_bundles"),
            ):
                run_pipeline(args)

            self.assertEqual(len(signature_mock.call_args.args[0]), 2)
            self.assertEqual(catalog_mock.call_args.kwargs["include_jobs"], {"Job_A", "Job_B"})
            processed_tasks = worker_mock.call_args.args[0]
            self.assertEqual(len(processed_tasks), 1)
            self.assertEqual(processed_tasks[0]["job_name"], "Job_B")


if __name__ == "__main__":
    unittest.main()
