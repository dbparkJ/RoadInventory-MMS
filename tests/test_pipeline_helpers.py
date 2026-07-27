from __future__ import annotations

import argparse
import copy
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laspy
import numpy as np
from PIL import Image
from pyproj import CRS

from mms_shp_detection.config import ConfigError, parse_args_with_config
from mms_shp_detection.pole import PoleSearchParameters, cluster_pole_observations
from mms_shp_detection.shp_writer import collect_detection_records
from mms_shp_detection.pipeline import (
    POINT_CROP_SEMANTICS,
    POLE_CROP_SEMANTICS,
    MultiModelCoordinator,
    PersistentCudaOutOfMemoryError,
    apply_model_filter,
    build_arg_parser,
    build_dataset_signature,
    build_forward_detection_mapping,
    build_panorama_alignment_qa_fingerprint,
    build_pole_fallback_parameters,
    build_pole_debug_axis_segments,
    build_pole_debug_overview_view,
    build_pole_search_parameters,
    build_pole_search_corridor_masks,
    build_rectified_detection_view,
    build_run_fingerprint,
    circular_bbox_iou_xyxy,
    collect_detection_points_at_range,
    create_forward_detection_qa_image,
    discover_model_paths,
    evaluate_point_range_fallback_quality,
    ensure_output_dirs,
    find_pole_bases_with_corridor_fallback,
    load_panorama_rgb,
    missing_result_artifacts,
    pole_cross_profile_candidate_key,
    pole_classifications_for_policy,
    reconcile_remote_supports_from_direct_anchors,
    resolve_matched_crs_wkt,
    resolve_num_workers,
    resolve_pole_classification_policy,
    render_forward_detection_view,
    robust_front_surface_distance,
    run_panorama_alignment_qa,
    run_parallel_multi_model_pipeline,
    run_pipeline,
    run_yolo_prediction,
    safely_refresh_shapefile_from_txt,
    save_debug_crop,
    select_cross_profile_pole_candidate,
    unwrap_panorama_x_coordinates,
    validate_crs_wkt,
    validate_panorama_image,
    validate_point_range_fallback_arguments,
    validate_pose_pointcloud_proximity,
    write_las,
    write_pole_las,
)
from mms_shp_detection.geometry import (
    build_perspective_panorama_remap,
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
    @staticmethod
    def _args() -> SimpleNamespace:
        return SimpleNamespace(
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

    @staticmethod
    def _estimate() -> dict[str, object]:
        return {
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

    def test_zero_pixel_mad_is_a_stable_recommendation(self) -> None:
        args = self._args()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "mms_shp_detection.pipeline.estimate_panorama_alignment",
            return_value=self._estimate(),
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
        self.assertFalse(report["cache_hit"])

    def test_matching_complete_report_skips_estimator(self) -> None:
        args = self._args()
        dataset_signature = {"signature_version": 1, "sha256": "dataset-a"}
        catalog = {
            "selected_source_type": "las",
            "signature": {"source_files": [{"path": "cloud.las", "mtime_ns": 1}]},
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "mms_shp_detection.pipeline.estimate_panorama_alignment",
            return_value=self._estimate(),
        ) as estimator:
            report_path = Path(temp_dir) / "alignment.json"
            first = run_panorama_alignment_qa(
                [],
                catalog,
                args,
                report_path,
                mock.Mock(),
                dataset_signature=dataset_signature,
            )
            second = run_panorama_alignment_qa(
                [],
                catalog,
                args,
                report_path,
                mock.Mock(),
                dataset_signature=dataset_signature,
            )

            stored = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(estimator.call_count, 1)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertTrue(stored["cache_hit"])
        self.assertEqual(
            first["cache_fingerprint"],
            second["cache_fingerprint"],
        )

    def test_changed_catalog_signature_invalidates_cached_report(self) -> None:
        args = self._args()
        dataset_signature = {"signature_version": 1, "sha256": "dataset-a"}
        first_catalog = {
            "selected_source_type": "las",
            "signature": {"source_files": [{"path": "cloud.las", "mtime_ns": 1}]},
        }
        changed_catalog = copy.deepcopy(first_catalog)
        changed_catalog["signature"]["source_files"][0]["mtime_ns"] = 2
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "mms_shp_detection.pipeline.estimate_panorama_alignment",
            return_value=self._estimate(),
        ) as estimator:
            report_path = Path(temp_dir) / "alignment.json"
            first = run_panorama_alignment_qa(
                [],
                first_catalog,
                args,
                report_path,
                mock.Mock(),
                dataset_signature=dataset_signature,
            )
            second = run_panorama_alignment_qa(
                [],
                changed_catalog,
                args,
                report_path,
                mock.Mock(),
                dataset_signature=dataset_signature,
            )

        self.assertEqual(estimator.call_count, 2)
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(
            first["cache_fingerprint"],
            second["cache_fingerprint"],
        )

    def test_fingerprint_covers_dataset_calibration_catalog_and_qa_config(self) -> None:
        args = self._args()
        catalog = {
            "selected_source_type": "las",
            "signature": {"source_files": [{"path": "cloud.las", "mtime_ns": 1}]},
        }
        dataset = {
            "signature_version": 1,
            "sha256": "image-pose-and-calibration-a",
        }
        baseline = build_panorama_alignment_qa_fingerprint(
            [],
            catalog,
            args,
            dataset_signature=dataset,
        )
        self.assertEqual(
            baseline,
            build_panorama_alignment_qa_fingerprint(
                [],
                copy.deepcopy(catalog),
                copy.deepcopy(args),
                dataset_signature=copy.deepcopy(dataset),
            ),
        )

        changed_dataset = dict(dataset, sha256="image-pose-and-calibration-b")
        changed_catalog = copy.deepcopy(catalog)
        changed_catalog["signature"]["source_files"][0]["mtime_ns"] = 2
        changed_args = copy.deepcopy(args)
        changed_args.alignment_qa_trim_fraction = 0.7
        for candidate_args, candidate_catalog, candidate_dataset in (
            (args, catalog, changed_dataset),
            (args, changed_catalog, dataset),
            (changed_args, catalog, dataset),
        ):
            with self.subTest(
                dataset=candidate_dataset["sha256"],
                catalog=candidate_catalog["signature"],
                trim=candidate_args.alignment_qa_trim_fraction,
            ):
                self.assertNotEqual(
                    baseline,
                    build_panorama_alignment_qa_fingerprint(
                        [],
                        candidate_catalog,
                        candidate_args,
                        dataset_signature=candidate_dataset,
                    ),
                )


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

    def test_forward_remap_grid_is_reused_across_frames(self) -> None:
        runtime = {
            "forward_view_size": 320,
            "forward_view_hfov_deg": 61.25,
            "forward_view_vfov_deg": 61.25,
            "panorama_yaw_offset_deg": 0.125,
            "panorama_pitch_offset_deg": -0.125,
        }
        first = np.zeros((180, 360, 3), dtype=np.uint8)
        second = np.full_like(first, 255)
        with mock.patch(
            "mms_shp_detection.pipeline.build_perspective_panorama_remap",
            wraps=build_perspective_panorama_remap,
        ) as remap_builder:
            first_view, first_mapping = render_forward_detection_view(first, runtime)
            second_view, second_mapping = render_forward_detection_view(second, runtime)

        self.assertEqual(remap_builder.call_count, 1)
        self.assertEqual(first_view.shape, (320, 320, 3))
        self.assertEqual(second_view.shape, (320, 320, 3))
        self.assertEqual(first_mapping["hfov_deg"], second_mapping["hfov_deg"])
        self.assertEqual(int(first_view.max()), 0)
        self.assertEqual(int(second_view.min()), 255)

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
            "perspective_min_fov_deg": 18.0,
            "perspective_max_fov_deg": 150.0,
            "perspective_view_size": 1024,
            "pole_min_fov_deg": 140.0,
            "pole_debug_min_fov_deg": 18.0,
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
        self.assertGreaterEqual(overview["hfov_deg"], 18.0)
        self.assertLess(overview["hfov_deg"], 140.0)
        self.assertLessEqual(overview["hfov_deg"], 150.0)
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

    def test_truncated_jpeg_is_recovered_without_leaking_global_pillow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "truncated.jpg"
            source = np.full((64, 128, 3), 127, dtype=np.uint8)
            Image.fromarray(source).save(image_path, format="JPEG", quality=90)
            encoded = image_path.read_bytes()
            image_path.write_bytes(encoded[:-2])

            logger = mock.Mock()
            recovered = load_panorama_rgb(image_path, logger)

            self.assertEqual(recovered.shape, source.shape)
            logger.warning.assert_called_once()

    def test_invalid_image_still_fails_after_truncated_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "invalid.jpg"
            image_path.write_bytes(b"not an image")

            with self.assertRaises(OSError):
                load_panorama_rgb(image_path, mock.Mock())


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
                "model_profile": "profile-a",
                "model_object_type": "traffic_sign",
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
            changed_model_metadata = argparse.Namespace(**vars(first_slice))
            changed_model_metadata.model_object_type = "traffic_signal"
            self.assertNotEqual(
                build_run_fingerprint(
                    changed_model_metadata,
                    pointcloud_catalog,
                    calibration_bundle,
                    dataset_signature,
                ),
                first_fingerprint,
            )


class MultiModelExecutionTests(unittest.TestCase):
    def _parallel_runner_fixture(
        self,
        root: Path,
        *,
        frame_count: int,
    ) -> dict[str, object]:
        args = build_arg_parser().parse_args(["--disable-console-progress"])
        args.multi_model_inference_workers = 2
        args.multi_model_pole_workers = 1
        args.multi_model_queue_depth = 1

        tasks = [
            {
                "image_path": str(root / f"frame_{index}.jpg"),
                "image_stem": f"frame_{index}",
                "record_name": "track",
                "timestamp_iso": f"2025-01-01T00:00:0{index}+00:00",
            }
            for index in range(frame_count)
        ]
        prepared = []
        states = []
        manifest_models = []
        for model_key in ("a", "b"):
            model_path = root / f"{model_key}.pt"
            model_path.write_bytes(model_key.encode("ascii"))
            effective = copy.deepcopy(args)
            effective.model_path = model_path
            effective.output_dir = root / model_key
            prepared.append(
                (model_path, effective, model_key, model_key, "traffic_sign")
            )
            runtime = {
                "model_path": str(model_path),
                "detection_view_mode": "forward",
                "forward_view_size": 32,
                "forward_view_hfov_deg": 70.0,
                "forward_view_vfov_deg": 70.0,
                "panorama_yaw_offset_deg": 0.0,
                "panorama_pitch_offset_deg": 0.0,
                "max_center_ray_angle_deg": 45.0,
            }
            states.append(
                {
                    "args": effective,
                    "runtime": runtime,
                    "image_tasks": tasks,
                    "pointcloud_catalog": {},
                    "logger": mock.Mock(),
                    "run_fingerprint": model_key * 64,
                    "log_path": root / model_key / "logs" / "run.log",
                    "output_dirs": {"shp": root / model_key / "shp"},
                    "crs_wkt": None,
                }
            )
            manifest_models.append(
                {
                    "model_key": model_key,
                    "status": "pending",
                    "error": None,
                    "failure_log": None,
                    "published_current_run": False,
                    "run_fingerprint": None,
                    "expected_final_shapefiles": {
                        "detections": str(
                            root / model_key / "shp" / "detected_signs.shp"
                        ),
                        "poles": str(
                            root / model_key / "shp" / "pole_bottoms.shp"
                        ),
                    },
                }
            )

        return {
            "prepared": prepared,
            "states": states,
            "manifest": {
                "schema_version": 2,
                "execution_mode": "sequential",
                "models": manifest_models,
            },
            "manifest_path": root / "models_manifest.json",
        }

    def _run_shared_stage_failure_fixture(
        self,
        root: Path,
        *,
        failing_stage: str,
    ) -> dict[str, object]:
        fixture = self._parallel_runner_fixture(root, frame_count=2)
        image_rgb = np.zeros((8, 16, 3), dtype=np.uint8)
        forward_rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        mapping = {"hfov_deg": 70.0, "vfov_deg": 70.0}
        finalized: dict[str, dict[str, int]] = {}

        def finalize(state, summary):
            finalized[state["model_key"]] = dict(summary)
            return {
                "run_fingerprint": state["run_fingerprint"],
                "final_shapefiles": {},
                "feature_counts": {},
            }

        decode_side_effect = (
            [RuntimeError("shared decode fixture"), image_rgb]
            if failing_stage == "decode"
            else [image_rgb, image_rgb]
        )
        render_side_effect = (
            [RuntimeError("shared render fixture"), (forward_rgb, mapping)]
            if failing_stage == "render"
            else [(forward_rgb, mapping)]
        )
        progress = mock.Mock()
        pointcloud_cache = mock.Mock()
        with (
            mock.patch(
                "mms_shp_detection.pipeline.setup_logging",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "mms_shp_detection.pipeline.prepare_shared_pipeline_context",
                return_value={},
            ),
            mock.patch(
                "mms_shp_detection.pipeline._run_single_model_pipeline",
                side_effect=fixture["states"],
            ),
            mock.patch("mms_shp_detection.pipeline.YOLO", side_effect=[object(), object()]),
            mock.patch(
                "mms_shp_detection.pipeline.compatible_existing_result_summary",
                return_value=None,
            ),
            mock.patch(
                "mms_shp_detection.pipeline.load_panorama_rgb",
                side_effect=decode_side_effect,
            ) as decode,
            mock.patch("mms_shp_detection.pipeline.validate_panorama_image"),
            mock.patch(
                "mms_shp_detection.pipeline.render_forward_detection_view",
                side_effect=render_side_effect,
            ) as render,
            mock.patch("mms_shp_detection.pipeline.save_forward_detection_qa_image"),
            mock.patch(
                "mms_shp_detection.pipeline.run_forward_detection_on_view",
                return_value=[],
            ) as inference,
            mock.patch(
                "mms_shp_detection.pipeline.process_image_task",
                return_value={
                    "images": 1,
                    "detections": 0,
                    "points": 0,
                    "failures": 0,
                },
            ) as postprocess,
            mock.patch(
                "mms_shp_detection.pipeline.PointCloudReaderCache",
                return_value=pointcloud_cache,
            ),
            mock.patch(
                "mms_shp_detection.pipeline.finalize_prepared_model_run",
                side_effect=finalize,
            ),
            mock.patch("mms_shp_detection.pipeline.tqdm", return_value=progress),
        ):
            run_parallel_multi_model_pipeline(
                fixture["prepared"],
                base_output_dir=root,
                manifest=fixture["manifest"],
                manifest_path=fixture["manifest_path"],
            )

        return {
            "decode_calls": decode.call_count,
            "render_calls": render.call_count,
            "inference_calls": inference.call_count,
            "postprocess_calls": postprocess.call_count,
            "finalized": finalized,
        }

    def test_cross_profile_pole_ranking_ignores_profile_bin_count(self) -> None:
        common = {
            "completeness_ratio": 0.90,
            "association_distance_m": 9.0,
            "horizontal_connection_coverage_ratio": 0.80,
            "radial_rmse_m": 0.08,
            "ground_z": 1.0,
            "ground_rmse_m": 0.05,
            "status": "AUTO",
            "point_count": 120,
        }
        strict_candidate = SimpleNamespace(
            **common,
            horizontal_connection_expected_bin_count=36,
        )
        fallback_candidate = SimpleNamespace(
            **common,
            horizontal_connection_expected_bin_count=26,
        )

        strict_key = pole_cross_profile_candidate_key(
            strict_candidate,
            preferred_min_completeness_ratio=0.75,
            direct_max_axis_sign_distance_m=0.75,
        )
        fallback_key = pole_cross_profile_candidate_key(
            fallback_candidate,
            preferred_min_completeness_ratio=0.75,
            direct_max_axis_sign_distance_m=0.75,
        )

        self.assertEqual(strict_key, fallback_key)

    def test_cross_profile_pole_ranking_prefers_nearest_valid_junction(self) -> None:
        common = {
            "completeness_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "ground_z": 1.0,
            "ground_rmse_m": 0.05,
            "status": "AUTO",
            "point_count": 120,
        }
        near_support = SimpleNamespace(
            **common,
            association_distance_m=5.38,
            horizontal_connection_coverage_ratio=0.75,
        )
        far_structure = SimpleNamespace(
            **common,
            association_distance_m=13.07,
            horizontal_connection_coverage_ratio=1.0,
        )

        self.assertLess(
            pole_cross_profile_candidate_key(
                near_support,
                preferred_min_completeness_ratio=0.75,
                direct_max_axis_sign_distance_m=0.75,
            ),
            pole_cross_profile_candidate_key(
                far_structure,
                preferred_min_completeness_ratio=0.75,
                direct_max_axis_sign_distance_m=0.75,
            ),
        )

    def test_cross_profile_pole_ranking_uses_quality_within_one_metre(self) -> None:
        common = {
            "ground_z": 1.0,
            "ground_rmse_m": 0.05,
            "status": "AUTO",
            "point_count": 120,
        }
        near_noise = SimpleNamespace(
            **common,
            completeness_ratio=0.75,
            association_distance_m=5.38,
            horizontal_connection_coverage_ratio=0.50,
            radial_rmse_m=0.13,
            multi_return_fraction=0.20,
        )
        clean_support = SimpleNamespace(
            **common,
            completeness_ratio=1.0,
            association_distance_m=5.39,
            horizontal_connection_coverage_ratio=1.0,
            radial_rmse_m=0.06,
            multi_return_fraction=0.0,
        )

        self.assertLess(
            pole_cross_profile_candidate_key(
                clean_support,
                preferred_min_completeness_ratio=0.75,
                direct_max_axis_sign_distance_m=0.75,
            ),
            pole_cross_profile_candidate_key(
                near_noise,
                preferred_min_completeness_ratio=0.75,
                direct_max_axis_sign_distance_m=0.75,
            ),
        )

    def test_cross_profile_selection_keeps_opposite_side_density_tie_break(
        self,
    ) -> None:
        common = {
            "completeness_ratio": 1.0,
            "horizontal_connection_coverage_ratio": 1.0,
            "horizontal_connection_coherent_coverage_ratio": 1.0,
            "radial_rmse_m": 0.09,
            "multi_return_fraction": 0.0,
            "ground_z": 1.0,
            "ground_rmse_m": 0.05,
            "status": "AUTO",
        }
        strict_wrong = SimpleNamespace(
            **common,
            association_distance_m=4.323,
            horizontal_connection_point_count=1816,
            horizontal_connection_ridge_density_points_per_m=115.0,
            point_count=4632,
            support_side="LEFT_OF_TRAVEL",
        )
        fallback_actual = SimpleNamespace(
            **common,
            association_distance_m=4.754,
            horizontal_connection_point_count=5611,
            horizontal_connection_ridge_density_points_per_m=484.0,
            point_count=7653,
            support_side="RIGHT_OF_TRAVEL",
        )

        self.assertIs(
            select_cross_profile_pole_candidate(
                (strict_wrong, fallback_actual),
                PoleSearchParameters(),
            ),
            fallback_actual,
        )

    def test_two_direct_frames_reconcile_one_missing_remote_support(self) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "detection_index": 1,
            "model_object_type": "traffic_signal",
            "class_id": 1,
            "class_name": "vehicular_signal",
            "confidence": 0.9,
            "image_name": "frame634.jpg",
            "timestamp_iso": "634",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.219512,
                        "horizontal_connection_coherent_coverage_ratio": 0.121951,
                        "horizontal_connection_coherent_ratio": 0.5556,
                        "horizontal_connection_coherent_point_fraction": 0.0449,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "detection_index": 1,
                "class_id": 0,
                "class_name": "invisible_signal",
                "confidence": 0.9,
                "image_name": f"frame{row}.jpg",
                "timestamp_iso": str(row),
                "pose_row_number": row,
                "sign_x": 464356.4,
                "sign_y": 3911651.1,
                "sign_z": 41.4,
                "pole_x": 464356.375 + (0.002 * offset),
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_type": "SINGLE",
                "pole_method": "GROUND_SNAP",
                "pole_status": "AUTO",
                "pole_occluded": False,
                "pole_occlusion_status": "VISIBLE",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
                "pole_point_crop_path": f"anchor-{row}.las",
                "pole_debug_image_path": f"anchor-{row}.jpg",
            }
            for offset, row in enumerate((635, 636))
        ]

        reconciled = reconcile_remote_supports_from_direct_anchors(
            [target],
            direct_observations,
        )

        self.assertEqual(len(reconciled), 3)
        relation = next(item for item in reconciled if item["detection_id"] == "target")
        self.assertEqual(relation["pole_method"], "MULTI_FRAME_DIRECT_ANCHOR")
        self.assertEqual(relation["pole_status"], "REVIEW")
        self.assertAlmostEqual(relation["pole_x"], 464356.376, delta=0.01)
        self.assertEqual(
            relation["horizontal_connection_coverage_ratio"],
            0.219512,
        )
        self.assertEqual(
            relation["horizontal_connection_coherent_coverage_ratio"],
            0.121951,
        )
        self.assertEqual(
            relation["horizontal_connection_coherent_ratio"],
            0.5556,
        )
        self.assertEqual(
            relation["horizontal_connection_coherent_point_fraction"],
            0.0449,
        )
        self.assertIs(
            relation["horizontal_connection_endpoint_anchored"],
            True,
        )
        self.assertEqual(
            relation["support_hypothesis_rejection_reason"],
            "raw_coverage",
        )
        self.assertEqual(relation["support_anchor_pose_rows"], [635, 636])
        self.assertEqual(
            relation["support_anchor_source_detection_ids"],
            ["direct-635", "direct-636"],
        )
        self.assertAlmostEqual(
            relation["support_anchor_xy_spread_m"],
            0.001,
            delta=0.001,
        )
        self.assertAlmostEqual(
            relation["support_anchor_z_spread_m"],
            0.0,
        )
        self.assertIsNone(relation["pole_point_crop_path"])
        self.assertIsNone(relation["pole_debug_image_path"])
        clustered = cluster_pole_observations(reconciled, radius_m=0.75)
        clustered_target = next(
            item for item in clustered if item["detection_id"] == "target"
        )
        self.assertEqual(
            clustered_target["pole_method"],
            "MULTI_FRAME_DIRECT_ANCHOR",
        )
        self.assertTrue(clustered_target["support_reconciled"])
        self.assertTrue(
            all(
                item["pole_status"] == "AUTO"
                for item in clustered
                if item["detection_id"].startswith("direct-")
            )
        )

    def test_repeated_anchor_replaces_review_relation_only_with_shaft_hypothesis(
        self,
    ) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "detection_index": 1,
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375 + (0.002 * offset),
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for offset, row in enumerate((635, 636))
        ]
        wrong_remote = {
            "record_name": "route_a",
            "detection_id": "target",
            "image_name": "frame634.jpg",
            "pose_row_number": 634,
            "pole_x": 464344.90,
            "pole_y": 3911656.04,
            "pole_z": 38.40,
            "pole_status": "REVIEW",
            "association_distance_m": 6.40,
        }

        reconciled = reconcile_remote_supports_from_direct_anchors(
            [target],
            [*direct_observations, wrong_remote],
        )

        target_relations = [
            item for item in reconciled if item["detection_id"] == "target"
        ]
        self.assertEqual(len(target_relations), 1)
        self.assertEqual(
            target_relations[0]["pole_method"],
            "MULTI_FRAME_DIRECT_ANCHOR",
        )
        self.assertTrue(
            target_relations[0]["support_reconciled_replaced_remote"]
        )

        protected_auto = {**wrong_remote, "pole_status": "AUTO"}
        protected = reconcile_remote_supports_from_direct_anchors(
            [target],
            [*direct_observations, protected_auto],
        )
        protected_relation = next(
            item for item in protected if item["detection_id"] == "target"
        )
        self.assertEqual(protected_relation["pole_status"], "AUTO")
        self.assertEqual(protected_relation["pole_x"], 464344.90)
        self.assertNotIn("support_reconciled", protected_relation)

    def test_repeated_anchor_rejects_unanchored_or_sub_twenty_percent_hypotheses(
        self,
    ) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.375,
                        "axis_y": 3911650.758,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.0,
                        "horizontal_connection_coherent_coverage_ratio": 0.0,
                        "horizontal_connection_endpoint_anchored": False,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375,
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for row in (635, 636)
        ]

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors(
                [target],
                direct_observations,
            ),
            direct_observations,
        )
        hypothesis = target["pole"]["support_hypotheses"][0]
        hypothesis.update(
            {
                "horizontal_connection_coherent_coverage_ratio": 0.148148,
                "horizontal_connection_coherent_ratio": 0.8,
                "horizontal_connection_coherent_point_fraction": 0.6667,
                "horizontal_connection_endpoint_anchored": True,
            }
        )
        for raw_coverage in (0.185185, 0.15, 0.157895):
            with self.subTest(raw_coverage=raw_coverage):
                hypothesis[
                    "horizontal_connection_coverage_ratio"
                ] = raw_coverage
                self.assertEqual(
                    reconcile_remote_supports_from_direct_anchors(
                        [target],
                        direct_observations,
                    ),
                    direct_observations,
                )

    def test_one_direct_frame_cannot_reconcile_remote_support(self) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct = {
            "record_name": "route_a",
            "detection_id": "direct-635",
            "image_name": "frame635.jpg",
            "pose_row_number": 635,
            "pole_x": 464356.375,
            "pole_y": 3911650.758,
            "pole_z": 38.461,
            "pole_status": "AUTO",
            "association_distance_m": 0.394,
        }

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors([target], [direct]),
            [direct],
        )

    def test_two_anchors_must_both_be_near_the_target_frame(self) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375 + (0.002 * offset),
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for offset, row in enumerate((600, 635))
        ]

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors(
                [target],
                direct_observations,
            ),
            direct_observations,
        )

    def test_direct_anchor_cluster_requires_stable_base_height(self) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375,
                "pole_y": 3911650.758,
                "pole_z": pole_z,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for row, pole_z in ((635, 38.0), (636, 39.0))
        ]

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors(
                [target],
                direct_observations,
            ),
            direct_observations,
        )

    def test_anchor_consensus_outliers_cannot_reconcile_from_outlier_frames(
        self,
    ) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.375,
                        "axis_y": 3911650.758,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375,
                "pole_y": 3911650.758,
                "pole_z": pole_z,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for row, pole_z in (
                (600, 38.0),
                (601, 38.0),
                (602, 38.0),
                (603, 38.0),
                (635, 39.0),
                (636, 39.0),
            )
        ]
        clustered = cluster_pole_observations(
            [dict(item) for item in direct_observations],
            radius_m=0.15,
        )
        self.assertEqual(clustered[0]["pole_status"], "REVIEW")
        self.assertEqual(clustered[0]["consensus_outlier_count"], 2)

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors(
                [target],
                direct_observations,
            ),
            direct_observations,
        )

    def test_reconciliation_filters_malformed_numeric_observations(self) -> None:
        target = {
            "record_name": "route_a",
            "detection_id": "target",
            "detection_index": 1,
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 464350.625,
            "y": 3911658.990,
            "z": 43.602,
            "pole": {
                "support_hypotheses": [
                    {
                        "axis_x": 464356.39,
                        "axis_y": 3911650.76,
                        "rejection_reason": "raw_coverage",
                        "horizontal_connection_coverage_ratio": 0.22,
                        "horizontal_connection_coherent_coverage_ratio": 0.12,
                        "horizontal_connection_endpoint_anchored": True,
                    }
                ]
            },
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "image_name": f"frame{row}.jpg",
                "pose_row_number": row,
                "pole_x": 464356.375 + (0.002 * offset),
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_status": "AUTO",
                "pole_quality": 0.8,
                "association_distance_m": 0.394,
            }
            for offset, row in enumerate((635, 636))
        ]
        malformed = [
            {
                "record_name": "route_a",
                "detection_id": "target",
                "pose_row_number": 634,
                "pole_x": 464344.9,
                "pole_y": 3911656.0,
                "pole_z": 38.4,
                "pole_status": "AUTO",
                "association_distance_m": "not-a-number",
            },
            {
                "record_name": "route_a",
                "detection_id": "nan-coordinate",
                "pose_row_number": 635,
                "pole_x": float("nan"),
                "pole_y": 3911650.758,
                "pole_z": 38.461,
                "pole_status": "AUTO",
                "association_distance_m": 0.2,
            },
        ]

        reconciled = reconcile_remote_supports_from_direct_anchors(
            [None, target],
            [*malformed, *direct_observations],
        )

        self.assertEqual(
            {item["detection_id"] for item in reconciled},
            {"target", "direct-635", "direct-636"},
        )
        relation = next(
            item for item in reconciled if item["detection_id"] == "target"
        )
        self.assertEqual(
            relation["pole_method"],
            "MULTI_FRAME_DIRECT_ANCHOR",
        )
        cluster_pole_observations(reconciled, radius_m=0.75)

    def test_reconciliation_skips_non_mapping_pole_payload(self) -> None:
        detection = {
            "record_name": "route_a",
            "detection_id": "target",
            "model_object_type": "traffic_signal",
            "pose_row_number": 634,
            "x": 1.0,
            "y": 2.0,
            "z": 5.0,
            "pole": ["malformed"],
        }
        direct_observations = [
            {
                "record_name": "route_a",
                "detection_id": f"direct-{row}",
                "pose_row_number": row,
                "pole_x": 1.0,
                "pole_y": 2.0,
                "pole_z": 0.0,
                "pole_status": "AUTO",
                "association_distance_m": 0.2,
            }
            for row in (635, 636)
        ]

        self.assertEqual(
            reconcile_remote_supports_from_direct_anchors(
                [detection],
                direct_observations,
            ),
            direct_observations,
        )

    def test_detection_collection_skips_non_mapping_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            txt_root = Path(temp_dir)
            (txt_root / "frame.txt").write_text(
                json.dumps(
                    {
                        "record_name": "route_a",
                        "detections": [
                            None,
                            "malformed",
                            {
                                "detection_index": 1,
                                "image_name": "frame.jpg",
                                "x": 1.0,
                                "y": 2.0,
                                "z": 3.0,
                                "accepted_for_shp": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            records = collect_detection_records(txt_root)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["detection_index"], 1)

    def test_explicit_cli_filter_overrides_each_model_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "traffic_light_best.pt"
            model_path.write_bytes(b"fixture")
            args = parse_args_with_config(
                build_arg_parser(),
                ["--max-range-m", "18"],
                default_config_path=Path(__file__).resolve().parents[1] / "config.yaml",
            )

            effective, _, object_type = apply_model_filter(
                args,
                model_path,
                require_profile=True,
            )

            self.assertEqual(effective.max_range_m, 18.0)
            self.assertEqual(object_type, "traffic_signal")
            self.assertEqual(effective.pole_search_radius_m, 12.0)
            self.assertEqual(effective.pole_max_axis_sign_distance_m, 12.0)
            self.assertEqual(effective.pole_min_fov_deg, 140.0)
            self.assertEqual(effective.perspective_max_fov_deg, 150.0)

    def test_models_are_discovered_in_stable_order_and_require_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "zeta.pt").write_bytes(b"z")
            (root / "Alpha.PT").write_bytes(b"a")
            (root / "ignore.onnx").write_bytes(b"x")

            paths = discover_model_paths(root, None)
            self.assertEqual([path.name for path in paths], ["Alpha.PT", "zeta.pt"])

            args = build_arg_parser().parse_args([])
            args.model_filters = {
                "Alpha.PT": {
                    "object_type": "traffic_sign",
                    "point_matching": {"max_range_m": 15.0},
                }
            }
            effective, profile_name, object_type = apply_model_filter(
                args,
                paths[0],
                require_profile=True,
            )
            self.assertEqual(profile_name, "Alpha.PT")
            self.assertEqual(object_type, "traffic_sign")
            self.assertEqual(effective.max_range_m, 15.0)
            self.assertEqual(effective.model_path, paths[0])

            with self.assertRaises(ConfigError):
                apply_model_filter(args, paths[1], require_profile=True)

    def test_model_filter_rejects_zero_for_new_positive_pole_parameters(self) -> None:
        strictly_positive = (
            "pole_axis_plumb_endpoint_fraction",
            "pole_horizontal_connection_coherence_radius_m",
            "pole_remote_max_endpoint_tilt_deg",
            "pole_long_remote_distance_m",
            "pole_long_remote_transition_m",
            "pole_long_remote_min_vertical_span_m",
        )
        args = build_arg_parser().parse_args([])
        model_path = Path("fixture.pt")
        for key in strictly_positive:
            args.model_filters = {
                model_path.name: {
                    "pole_detection": {
                        key: 0,
                    }
                }
            }
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ConfigError, "must be greater than 0"),
            ):
                apply_model_filter(args, model_path, require_profile=True)

    def test_multi_model_wrapper_isolates_outputs_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "models"
            model_dir.mkdir()
            (model_dir / "b.pt").write_bytes(b"b")
            (model_dir / "a.pt").write_bytes(b"a")
            output_dir = root / "outputs"

            args = build_arg_parser().parse_args(
                [
                    "--model-dir",
                    str(model_dir),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            args.model_filters = {
                "a.pt": {"object_type": "traffic_sign"},
                "b.pt": {"object_type": "traffic_signal"},
            }

            with mock.patch(
                "mms_shp_detection.pipeline._run_single_model_pipeline"
            ) as single_model:
                run_pipeline(args)

            self.assertEqual(single_model.call_count, 2)
            effective_args = [
                call.args[0] for call in single_model.call_args_list
            ]
            self.assertEqual(
                [item.model_path.name for item in effective_args],
                ["a.pt", "b.pt"],
            )
            self.assertEqual(
                [item.output_dir for item in effective_args],
                [output_dir / "a", output_dir / "b"],
            )
            shared_forward_views = (output_dir / "forward_views").resolve()
            self.assertEqual(
                [
                    getattr(item, "_shared_forward_views_dir", None)
                    for item in effective_args
                ],
                [shared_forward_views, shared_forward_views],
            )
            manifest = json.loads(
                (output_dir / "models_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["status"] for item in manifest["models"]],
                ["completed", "completed"],
            )
            self.assertTrue(
                all(item["published_current_run"] for item in manifest["models"])
            )

    def test_parallel_wrapper_uses_one_shared_forward_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "models"
            model_dir.mkdir()
            (model_dir / "a.pt").write_bytes(b"a")
            (model_dir / "b.pt").write_bytes(b"b")
            output_dir = root / "outputs"
            args = build_arg_parser().parse_args(
                [
                    "--model-dir",
                    str(model_dir),
                    "--output-dir",
                    str(output_dir),
                    "--multi-model-parallel",
                ]
            )
            args.model_filters = {
                "a.pt": {"object_type": "traffic_sign"},
                "b.pt": {"object_type": "traffic_signal"},
            }

            with (
                mock.patch(
                    "mms_shp_detection.pipeline.run_parallel_multi_model_pipeline"
                ) as parallel_run,
                mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline"
                ) as single_run,
            ):
                run_pipeline(args)

            parallel_run.assert_called_once()
            single_run.assert_not_called()
            manifest = json.loads(
                (output_dir / "models_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema_version"], 2)

            shared = root / "shared_forward"
            model_output = root / "one_model"
            dirs = ensure_output_dirs(
                model_output,
                shared_forward_views_dir=shared,
            )
            self.assertEqual(dirs["forward_views"], shared)
            self.assertTrue(shared.is_dir())
            self.assertFalse((model_output / "forward_views").exists())

    def test_pole_queue_bounds_memory_heavy_work_to_one_slot(self) -> None:
        coordinator = MultiModelCoordinator(
            inference_workers=2,
            pole_workers=1,
            queue_depth=1,
        )
        barrier = threading.Barrier(3)
        active = 0
        max_active = 0
        active_lock = threading.Lock()

        def work() -> None:
            nonlocal active, max_active
            barrier.wait()
            with coordinator.pole_gate.slot():
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.02)
                with active_lock:
                    active -= 1

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active, 1)
        self.assertEqual(coordinator.snapshot()["pole_max_active"], 1)

    def test_serial_cuda_oom_retry_circuit_breaks_model(self) -> None:
        coordinator = MultiModelCoordinator(
            inference_workers=2,
            pole_workers=1,
            queue_depth=1,
        )
        model = mock.Mock()
        model.predict.side_effect = [
            RuntimeError("CUDA out of memory"),
            RuntimeError("CUDA out of memory"),
        ]

        with mock.patch("torch.cuda.is_available", return_value=False):
            with self.assertRaises(PersistentCudaOutOfMemoryError):
                run_yolo_prediction(
                    model,
                    {"model_key": "traffic_light"},
                    mock.Mock(),
                    coordinator=coordinator,
                    source=object(),
                )

        self.assertEqual(model.predict.call_count, 2)
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["inference_workers_effective"], 1)
        self.assertEqual(snapshot["cuda_oom_sequential_fallbacks"], 1)

    def test_shared_decode_or_render_failure_marks_all_models_and_continues(self) -> None:
        for failing_stage in ("decode", "render"):
            with self.subTest(failing_stage=failing_stage):
                with tempfile.TemporaryDirectory() as temp_dir:
                    result = self._run_shared_stage_failure_fixture(
                        Path(temp_dir),
                        failing_stage=failing_stage,
                    )

                self.assertEqual(result["decode_calls"], 2)
                self.assertEqual(
                    result["render_calls"],
                    1 if failing_stage == "decode" else 2,
                )
                self.assertEqual(result["inference_calls"], 2)
                self.assertEqual(result["postprocess_calls"], 2)
                finalized = result["finalized"]
                self.assertEqual(set(finalized), {"a", "b"})
                for summary in finalized.values():
                    self.assertEqual(summary["images"], 2)
                    self.assertEqual(summary["failures"], 1)

    def test_consumer_startup_failure_does_not_hang_bounded_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self._parallel_runner_fixture(root, frame_count=1)
            image_rgb = np.zeros((8, 16, 3), dtype=np.uint8)
            forward_rgb = np.zeros((32, 32, 3), dtype=np.uint8)
            mapping = {"hfov_deg": 70.0, "vfov_deg": 70.0}
            outcome: dict[str, BaseException] = {}

            def invoke_runner() -> None:
                try:
                    run_parallel_multi_model_pipeline(
                        fixture["prepared"],
                        base_output_dir=root,
                        manifest=fixture["manifest"],
                        manifest_path=fixture["manifest_path"],
                    )
                except BaseException as exc:
                    outcome["error"] = exc

            with (
                mock.patch(
                    "mms_shp_detection.pipeline.setup_logging",
                    return_value=mock.Mock(),
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.prepare_shared_pipeline_context",
                    return_value={},
                ),
                mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline",
                    side_effect=fixture["states"],
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.YOLO",
                    side_effect=[object(), object()],
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.compatible_existing_result_summary",
                    return_value=None,
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.load_panorama_rgb",
                    return_value=image_rgb,
                ),
                mock.patch("mms_shp_detection.pipeline.validate_panorama_image"),
                mock.patch(
                    "mms_shp_detection.pipeline.render_forward_detection_view",
                    return_value=(forward_rgb, mapping),
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.save_forward_detection_qa_image"
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.run_forward_detection_on_view",
                    return_value=[],
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.PointCloudReaderCache",
                    side_effect=RuntimeError("consumer startup fixture"),
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.finalize_prepared_model_run"
                ),
                mock.patch(
                    "mms_shp_detection.pipeline.tqdm",
                    return_value=mock.Mock(),
                ),
            ):
                runner = threading.Thread(target=invoke_runner, daemon=True)
                runner.start()
                runner.join(timeout=2.0)
                still_running = runner.is_alive()

        self.assertFalse(
            still_running,
            "parallel runner hung while cleaning up a full bounded queue",
        )
        self.assertIn("error", outcome)
        self.assertIn("consumer startup fixture", str(outcome["error"]))

    def test_second_consumer_thread_start_failure_cancels_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self._parallel_runner_fixture(root, frame_count=0)
            outcome: dict[str, BaseException] = {}
            first_consumer: dict[str, threading.Thread] = {}
            start_count = 0
            original_start = threading.Thread.start

            def flaky_start(thread: threading.Thread) -> None:
                nonlocal start_count
                if thread.name.startswith("postprocess-"):
                    start_count += 1
                    if start_count == 2:
                        raise RuntimeError("second consumer start fixture")
                    # Keep a broken implementation from pinning the test process.
                    thread.daemon = True
                    first_consumer["thread"] = thread
                original_start(thread)

            def invoke_runner() -> None:
                try:
                    with (
                        mock.patch(
                            "mms_shp_detection.pipeline.setup_logging",
                            return_value=mock.Mock(),
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline.prepare_shared_pipeline_context",
                            return_value={},
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline._run_single_model_pipeline",
                            side_effect=fixture["states"],
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline.YOLO",
                            side_effect=[object(), object()],
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline.PointCloudReaderCache",
                            return_value=mock.Mock(),
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline.finalize_prepared_model_run"
                        ),
                        mock.patch(
                            "mms_shp_detection.pipeline.tqdm",
                            return_value=mock.Mock(),
                        ),
                        mock.patch.object(
                            threading.Thread,
                            "start",
                            autospec=True,
                            side_effect=flaky_start,
                        ),
                    ):
                        run_parallel_multi_model_pipeline(
                            fixture["prepared"],
                            base_output_dir=root,
                            manifest=fixture["manifest"],
                            manifest_path=fixture["manifest_path"],
                        )
                except BaseException as exc:
                    outcome["error"] = exc

            runner = threading.Thread(target=invoke_runner, daemon=True)
            runner.start()
            runner.join(timeout=2.0)
            consumer = first_consumer.get("thread")
            if consumer is not None:
                consumer.join(timeout=1.0)

        self.assertFalse(runner.is_alive())
        self.assertIsNotNone(consumer)
        self.assertFalse(consumer.is_alive())
        self.assertIn("second consumer start fixture", str(outcome.get("error")))

    def test_multi_model_failure_keeps_running_and_records_model_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "models"
            model_dir.mkdir()
            (model_dir / "a.pt").write_bytes(b"a")
            (model_dir / "b.pt").write_bytes(b"b")
            output_dir = root / "outputs"
            args = build_arg_parser().parse_args(
                [
                    "--model-dir",
                    str(model_dir),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            args.model_filters = {
                "a.pt": {"object_type": "traffic_sign"},
                "b.pt": {"object_type": "traffic_signal"},
            }

            try:
                with mock.patch(
                    "mms_shp_detection.pipeline._run_single_model_pipeline",
                    side_effect=[RuntimeError("fixture failure"), {}],
                ) as single_model:
                    with self.assertRaisesRegex(RuntimeError, "a: RuntimeError"):
                        run_pipeline(args)

                self.assertEqual(single_model.call_count, 2)
                manifest = json.loads(
                    (output_dir / "models_manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [item["status"] for item in manifest["models"]],
                    ["failed", "completed"],
                )
                failure_log = Path(manifest["models"][0]["failure_log"])
                self.assertTrue(failure_log.is_file())
                failure_text = failure_log.read_text(encoding="utf-8")
                self.assertIn("fixture failure", failure_text)
                self.assertIn("Traceback", failure_text)
            finally:
                for logger in (
                    logging.getLogger("mms_shp_detection_main"),
                    logging.getLogger(),
                ):
                    for handler in list(logger.handlers):
                        if (
                            logger.name == "mms_shp_detection_main"
                            or getattr(handler, "_mms_file_handler", False)
                        ):
                            logger.removeHandler(handler)
                            handler.close()

    def test_pole_physical_fallback_expands_bounds_and_arm_tolerance(self) -> None:
        args = build_arg_parser().parse_args([])
        runtime = vars(args).copy()
        runtime.update(
            {
                "pole_range_fallback_enabled": True,
                "pole_fallback_search_radius_m": 16.0,
                "pole_fallback_max_drop_m": 14.0,
                "pole_fallback_top_margin_m": 5.0,
                "pole_fallback_max_axis_sign_distance_m": 16.0,
                "pole_fallback_min_vertical_span_m": 1.2,
                "pole_fallback_horizontal_connection_radius_m": 0.35,
                "pole_fallback_horizontal_connection_z_tolerance_m": 0.55,
                "pole_fallback_horizontal_connection_above_tolerance_m": 1.5,
                "pole_fallback_horizontal_connection_bin_m": 0.35,
                "pole_fallback_min_horizontal_connection_coverage": 0.45,
            }
        )
        strict = build_pole_search_parameters(runtime)
        fallback = build_pole_fallback_parameters(runtime, strict)
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback.search_radius_m, 16.0)
        self.assertEqual(fallback.max_drop_m, 14.0)
        self.assertEqual(fallback.top_margin_m, 5.0)
        self.assertEqual(fallback.max_axis_sign_distance_m, 16.0)
        self.assertEqual(fallback.horizontal_connection_radius_m, 0.35)
        self.assertEqual(fallback.horizontal_connection_above_tolerance_m, 1.5)
        self.assertEqual(fallback.min_horizontal_connection_coverage, 0.45)


class PipelineInputScopeTests(unittest.TestCase):
    def test_catalog_and_dataset_signature_use_all_jobs_before_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model fixture")
            args = build_arg_parser().parse_args(
                [
                    "--data-root",
                    str(root),
                    "--model-path",
                    str(model_path),
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
    apply_model_filter,
    discover_model_paths,
