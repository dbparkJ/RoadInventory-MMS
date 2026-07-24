from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mms_shp_detection.config import (
    ConfigError,
    load_config_defaults,
    parse_args_with_config,
)
from mms_shp_detection.pipeline import build_arg_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PipelineConfigTests(unittest.TestCase):
    def test_repository_config_documents_every_pipeline_option(self) -> None:
        parser = build_arg_parser()
        configured = load_config_defaults(parser, PROJECT_ROOT / "config.yaml")
        parser_destinations = {
            action.dest
            for action in parser._actions
            if action.dest not in {"help"}
        }
        self.assertEqual(set(configured), parser_destinations)
        self.assertGreaterEqual(configured["conf"], 0.0)
        self.assertLessEqual(configured["conf"], 1.0)
        self.assertEqual(configured["debug_mask_alpha"], 8)
        self.assertIsInstance(configured["pole_detection"], bool)
        self.assertIn(configured["pole_classification_mode"], {"auto", "off", "require"})
        self.assertEqual(
            build_arg_parser().parse_args([]).pole_classification_mode,
            "auto",
        )
        self.assertEqual(configured["pole_ground_class_ids"], (2, 11))
        self.assertEqual(configured["pole_min_fov_deg"], 90.0)
        self.assertEqual(configured["detection_view_mode"], "forward")
        self.assertEqual(configured["forward_view_hfov_deg"], 70.0)
        self.assertEqual(configured["forward_view_vfov_deg"], 70.0)
        self.assertEqual(configured["conf"], 0.8)
        self.assertEqual(configured["max_center_ray_angle_deg"], 45.0)
        self.assertTrue(configured["point_range_fallback_enabled"])
        self.assertEqual(configured["point_range_fallback_max_range_m"], 20.0)
        self.assertEqual(configured["point_range_fallback_min_point_count"], 50)
        self.assertEqual(
            configured["point_range_fallback_min_cluster_fraction"],
            0.80,
        )
        self.assertEqual(
            configured["point_range_fallback_min_core_mask_fraction"],
            0.45,
        )
        self.assertEqual(
            configured["point_range_fallback_max_depth_span_m"],
            0.50,
        )
        self.assertEqual(configured["panorama_yaw_offset_deg"], 0.1534)
        self.assertEqual(configured["panorama_pitch_offset_deg"], 0.0)
        self.assertTrue(configured["alignment_qa_enabled"])
        self.assertEqual(configured["pole_min_consecutive_vertical_bins"], 4)
        self.assertEqual(configured["pole_max_observed_z_gap_m"], 1.0)
        self.assertEqual(configured["pole_max_drop_m"], 8.0)
        self.assertTrue(configured["pole_range_fallback_enabled"])
        self.assertEqual(configured["pole_fallback_max_drop_m"], 12.0)
        self.assertEqual(configured["pole_max_axis_sign_distance_m"], 8.0)
        self.assertEqual(configured["pole_direct_max_axis_sign_distance_m"], 0.75)
        self.assertEqual(configured["pole_min_horizontal_connection_coverage"], 0.50)
        self.assertEqual(configured["pole_geometry_ground_clearance_m"], 0.20)
        self.assertEqual(
            configured["pole_geometry_remote_min_completeness_ratio"],
            0.75,
        )
        self.assertEqual(configured["pole_geometry_remote_max_axis_rmse_m"], 0.095)
        self.assertEqual(configured["pole_geometry_remote_max_ground_rmse_m"], 0.15)
        self.assertEqual(configured["pole_min_ground_drop_m"], 1.8)
        self.assertEqual(configured["sign_observation_merge_xy_radius_m"], 0.25)
        self.assertTrue(configured["pole_require_ground"])
        self.assertEqual(
            configured["data_root"],
            (
                PROJECT_ROOT
                / "data"
                / "SEC006_마산교차로_하천리155-17_250903"
            ).resolve(),
        )
        self.assertIsNone(configured["model_path"])
        self.assertEqual(configured["model_dir"], (PROJECT_ROOT / "models").resolve())
        self.assertEqual(
            set(configured["model_filters"]),
            {"traffic_sign_best.pt", "traffic_light_best.pt"},
        )

    def test_no_argument_loads_default_yaml_and_cli_can_override_it(self) -> None:
        parser = build_arg_parser()
        args = parse_args_with_config(
            parser,
            ["--conf", "0.61", "--limit-images", "2"],
            default_config_path=PROJECT_ROOT / "config.yaml",
        )
        self.assertEqual(args.conf, 0.61)
        self.assertEqual(args.limit_images, 2)
        self.assertTrue(args.require_calibration)
        self.assertIsNone(args.model_path)
        self.assertEqual(args.model_dir, (PROJECT_ROOT / "models").resolve())
        self.assertEqual(args._config_path, str((PROJECT_ROOT / "config.yaml").resolve()))
        self.assertEqual(
            set(args._cli_override_dests),
            {"conf", "limit_images"},
        )

    def test_positional_config_path_is_supported_and_paths_are_config_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "custom.yaml"
            config_path.write_text(
                "config_version: 1\n"
                "paths:\n"
                "  data_root: sample_data\n"
                "yolo:\n"
                "  conf: 0.4\n",
                encoding="utf-8",
            )
            args = parse_args_with_config(build_arg_parser(), [str(config_path)])
            self.assertEqual(args.data_root, (root / "sample_data").resolve())
            self.assertEqual(args.conf, 0.4)

    def test_unknown_duplicate_and_invalid_values_are_rejected(self) -> None:
        fixtures = {
            "unknown.yaml": "config_version: 1\nyolo:\n  confidence: 0.5\n",
            "duplicate.yaml": "config_version: 1\na:\n  conf: 0.2\nb:\n  conf: 0.3\n",
            "range.yaml": "config_version: 1\nyolo:\n  conf: 1.1\n",
            "boolean.yaml": "config_version: 1\ninput:\n  require_calibration: 'false'\n",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, content in fixtures.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ConfigError):
                    load_config_defaults(build_arg_parser(), path)

    def test_no_config_preserves_legacy_parser_defaults(self) -> None:
        args = parse_args_with_config(
            build_arg_parser(),
            ["--no-config", "--conf", "0.7"],
            default_config_path=PROJECT_ROOT / "config.yaml",
        )
        self.assertEqual(args.conf, 0.7)
        self.assertFalse(args.require_calibration)
        self.assertIsNone(args._config_path)

    def test_pole_classification_mode_choices_and_cli_override(self) -> None:
        args = parse_args_with_config(
            build_arg_parser(),
            ["--pole-classification-mode", "off"],
            default_config_path=PROJECT_ROOT / "config.yaml",
        )
        self.assertEqual(args.pole_classification_mode, "off")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unquoted_off = root / "off.yaml"
            unquoted_off.write_text(
                "config_version: 1\npole_detection:\n"
                "  pole_detection: true\n"
                "  pole_classification_mode: off\n",
                encoding="utf-8",
            )
            configured = load_config_defaults(build_arg_parser(), unquoted_off)
            self.assertEqual(configured["pole_classification_mode"], "off")
            self.assertTrue(configured["pole_detection"])

            invalid = root / "invalid.yaml"
            invalid.write_text(
                "config_version: 1\npole_detection:\n  pole_classification_mode: magic\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config_defaults(build_arg_parser(), invalid)

    def test_point_range_fallback_cross_field_constraints(self) -> None:
        fixtures = {
            "range.yaml": (
                "config_version: 1\npoint_matching:\n"
                "  max_range_m: 12\n"
                "  point_range_fallback_enabled: true\n"
                "  point_range_fallback_max_range_m: 12\n"
            ),
            "count.yaml": (
                "config_version: 1\npoint_matching:\n"
                "  max_range_m: 12\n"
                "  min_point_count: 100\n"
                "  point_range_fallback_enabled: true\n"
                "  point_range_fallback_max_range_m: 15\n"
                "  point_range_fallback_min_point_count: 101\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, content in fixtures.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ConfigError):
                    load_config_defaults(build_arg_parser(), path)


if __name__ == "__main__":
    unittest.main()
