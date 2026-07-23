from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml


DEFAULT_CONFIG_NAME = "config.yaml"


class _Yaml12SafeLoader(yaml.SafeLoader):
    """Safe loader whose booleans follow YAML 1.2 (true/false only).

    PyYAML's default YAML 1.1 resolver treats words such as ``off``, ``on``,
    ``yes`` and ``no`` as booleans.  Those words are legitimate string values
    for enum-style pipeline options, so keep only the YAML 1.2 boolean forms.
    """


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


class ConfigError(ValueError):
    """Raised when a pipeline YAML file cannot be mapped to CLI options."""


_RANGES: dict[str, tuple[float | None, float | None]] = {
    "gps_week": (0, None),
    "gps_utc_offset_seconds": (0, None),
    "las_index_chunk_points": (1, None),
    "max_pose_pointcloud_separation_m": (0, None),
    "imgsz": (1, None),
    "forward_view_size": (256, None),
    "forward_view_hfov_deg": (1, 179),
    "forward_view_vfov_deg": (1, 179),
    "panorama_yaw_offset_deg": (-5, 5),
    "panorama_pitch_offset_deg": (-5, 5),
    "alignment_qa_sample_images": (1, None),
    "alignment_qa_max_points_per_image": (500, None),
    "alignment_qa_search_radius_px": (0, 50),
    "alignment_qa_trim_fraction": (0.01, 1),
    "alignment_qa_min_range_m": (0, None),
    "alignment_qa_max_range_m": (0, None),
    "alignment_qa_min_valid_samples": (1, None),
    "alignment_qa_max_mad_px": (0, None),
    "tile_width_px": (0, None),
    "tile_height_px": (0, None),
    "tile_overlap_px": (0, None),
    "tile_batch_size": (1, None),
    "tile_merge_iou": (0, 1),
    "conf": (0, 1),
    "iou": (0, 1),
    "max_det": (1, None),
    "num_workers": (1, None),
    "pointcloud_neighbor_count": (1, None),
    "point_padding_px": (0, None),
    "debug_crop_padding_px": (0, None),
    "debug_mask_alpha": (0, 255),
    "max_range_m": (0, None),
    "point_range_fallback_max_range_m": (0, None),
    "point_range_fallback_min_point_count": (1, None),
    "point_range_fallback_min_cluster_fraction": (0, 1),
    "point_range_fallback_min_core_mask_fraction": (0, 1),
    "point_range_fallback_max_depth_span_m": (0, None),
    "depth_window_m": (0, None),
    "front_surface_quantile": (0, 1),
    "front_surface_min_support": (1, None),
    "block_angle_margin_deg": (0, None),
    "max_center_ray_angle_deg": (0, 180),
    "min_point_count": (1, None),
    "perspective_view_size": (1, None),
    "perspective_margin_deg": (0, None),
    "perspective_min_fov_deg": (0, 180),
    "perspective_max_fov_deg": (0, 180),
    "cluster_radius_m": (0, None),
    "cluster_min_neighbors": (1, None),
    "cluster_trim_radius_multiplier": (1, None),
    "point_preview_size": (1, None),
    "worker_progress_every": (1, None),
    "progress_log_interval_sec": (1, None),
    "start_index": (0, None),
    "limit_images": (0, None),
    "pole_min_fov_deg": (1, 180),
    "pole_corridor_side_expand_ratio": (0, None),
    "pole_corridor_top_margin_ratio": (0, None),
    "pole_search_radius_m": (0, None),
    "pole_max_drop_m": (0, None),
    "pole_top_margin_m": (0, None),
    "pole_xy_voxel_m": (0, None),
    "pole_z_bin_m": (0, None),
    "pole_axis_cluster_radius_m": (0, None),
    "pole_axis_inlier_radius_m": (0, None),
    "pole_min_vertical_span_m": (0, None),
    "pole_min_vertical_bins": (2, None),
    "pole_min_consecutive_vertical_bins": (2, None),
    "pole_max_observed_z_gap_m": (0, None),
    "pole_min_vertical_occupancy_ratio": (0, 1),
    "pole_middle_support_start_fraction": (0, 1),
    "pole_min_middle_support_coverage_ratio": (0, 1),
    "pole_preferred_min_completeness_ratio": (0, 1),
    "pole_geometry_ground_clearance_m": (0, None),
    "pole_geometry_remote_min_completeness_ratio": (0, 1),
    "pole_geometry_remote_max_axis_rmse_m": (0, None),
    "pole_geometry_remote_max_ground_rmse_m": (0, None),
    "pole_min_points": (3, None),
    "pole_max_axis_tilt_deg": (0, 90),
    "pole_direct_max_axis_sign_distance_m": (0, None),
    "pole_max_axis_sign_distance_m": (0, None),
    "pole_horizontal_connection_radius_m": (0, None),
    "pole_horizontal_connection_z_tolerance_m": (0, None),
    "pole_horizontal_connection_bin_m": (0, None),
    "pole_horizontal_connection_min_points_per_bin": (1, None),
    "pole_min_horizontal_connection_coverage": (0, 1),
    "pole_max_ground_class_fraction": (0, 1),
    "pole_min_ground_drop_m": (0, None),
    "pole_ground_search_radius_m": (0, None),
    "pole_ground_core_radius_m": (0, None),
    "pole_ground_exclusion_radius_m": (0, None),
    "pole_ground_cell_size_m": (0, None),
    "pole_ground_cell_quantile": (0, 1),
    "pole_ground_min_cells": (3, None),
    "pole_ground_max_rmse_m": (0, None),
    "pole_ground_geometry_preference_margin_m": (0, None),
    "pole_occlusion_gap_m": (0, None),
    "pole_observation_merge_radius_m": (0, None),
    "pole_min_observations": (1, None),
    "sign_observation_merge_xy_radius_m": (0, None),
    "sign_observation_merge_z_radius_m": (0, None),
    "sign_observation_fallback_xy_radius_m": (0, None),
    "sign_observation_fallback_z_radius_m": (0, None),
}


def _flatten_config(
    value: dict[str, Any],
    *,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    leaves: list[tuple[tuple[str, ...], Any]] = []
    for raw_key, item in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ConfigError("YAML configuration keys must be non-empty strings.")
        key = raw_key.strip().replace("-", "_")
        path = (*prefix, key)
        if isinstance(item, dict):
            leaves.extend(_flatten_config(item, prefix=path))
        else:
            leaves.append((path, item))
    return leaves


def _action_map(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {
        action.dest: action
        for action in parser._actions
        if action.dest not in {argparse.SUPPRESS, "help"}
    }


def _convert_scalar(
    action: argparse.Action,
    value: Any,
    *,
    config_dir: Path,
    dotted_key: str,
) -> Any:
    if value is None:
        if action.required:
            raise ConfigError(f"'{dotted_key}' cannot be null.")
        return None

    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if not isinstance(value, bool):
            raise ConfigError(
                f"'{dotted_key}' must be a YAML boolean (true or false), not {value!r}."
            )
        converted = value
    elif action.type is Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ConfigError(f"'{dotted_key}' must be a file-system path.")
        expanded = Path(os.path.expandvars(os.path.expanduser(os.fspath(value))))
        converted = expanded if expanded.is_absolute() else (config_dir / expanded).resolve()
    elif action.type is not None:
        if action.type is int and (
            isinstance(value, bool)
            or (isinstance(value, float) and not value.is_integer())
        ):
            raise ConfigError(f"'{dotted_key}' must be an integer, not {value!r}.")
        if action.type is float and isinstance(value, bool):
            raise ConfigError(f"'{dotted_key}' must be a number, not a boolean.")
        if action.type is str and not isinstance(value, str):
            raise ConfigError(f"'{dotted_key}' must be a string, not {value!r}.")
        try:
            converted = action.type(value)
        except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
            type_name = getattr(action.type, "__name__", str(action.type))
            raise ConfigError(
                f"'{dotted_key}' must be convertible to {type_name}; received {value!r}."
            ) from exc
    else:
        converted = value

    if action.choices is not None and converted not in action.choices:
        choices = ", ".join(map(str, action.choices))
        raise ConfigError(
            f"'{dotted_key}' must be one of [{choices}]; received {converted!r}."
        )
    return converted


def _validate_range(dest: str, value: Any, dotted_key: str) -> None:
    if value is None or dest not in _RANGES:
        return
    lower, upper = _RANGES[dest]
    if lower is not None and value < lower:
        raise ConfigError(f"'{dotted_key}' must be at least {lower}; received {value!r}.")
    if upper is not None and value > upper:
        raise ConfigError(f"'{dotted_key}' must be at most {upper}; received {value!r}.")


def load_config_defaults(
    parser: argparse.ArgumentParser,
    config_path: Path,
) -> dict[str, Any]:
    """Load nested YAML and return validated defaults keyed by argparse destination.

    YAML section names are organizational only.  Every leaf name maps to the
    corresponding argparse ``dest`` so parser types and choices remain the one
    source of truth.  This also makes newly-added parser options available to
    YAML without changing this loader.
    """

    resolved_path = config_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {resolved_path}")
    try:
        document = yaml.load(
            resolved_path.read_text(encoding="utf-8-sig"),
            Loader=_Yaml12SafeLoader,
        )
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read YAML configuration {resolved_path}: {exc}") from exc

    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConfigError("The YAML document root must be a mapping.")

    actions = _action_map(parser)
    defaults: dict[str, Any] = {}
    source_keys: dict[str, str] = {}
    for path, raw_value in _flatten_config(document):
        dotted_key = ".".join(path)
        dest = path[-1]
        if dest == "config_version":
            if raw_value != 1:
                raise ConfigError(
                    f"Unsupported config_version {raw_value!r}; this program expects 1."
                )
            continue
        if dest not in actions:
            available = ", ".join(sorted(actions))
            raise ConfigError(
                f"Unknown YAML option '{dotted_key}'. Available leaf keys: {available}"
            )
        if dest in defaults:
            raise ConfigError(
                f"YAML option '{dest}' is defined more than once: "
                f"'{source_keys[dest]}' and '{dotted_key}'."
            )
        converted = _convert_scalar(
            actions[dest],
            raw_value,
            config_dir=resolved_path.parent,
            dotted_key=dotted_key,
        )
        _validate_range(dest, converted, dotted_key)
        defaults[dest] = converted
        source_keys[dest] = dotted_key

    minimum_fov = defaults.get("perspective_min_fov_deg")
    maximum_fov = defaults.get("perspective_max_fov_deg")
    if minimum_fov is not None and maximum_fov is not None and minimum_fov > maximum_fov:
        raise ConfigError(
            "perspective_min_fov_deg cannot be greater than perspective_max_fov_deg."
        )
    minimum_alignment_range = defaults.get("alignment_qa_min_range_m")
    maximum_alignment_range = defaults.get("alignment_qa_max_range_m")
    if (
        minimum_alignment_range is not None
        and maximum_alignment_range is not None
        and minimum_alignment_range >= maximum_alignment_range
    ):
        raise ConfigError(
            "alignment_qa_min_range_m must be smaller than alignment_qa_max_range_m."
        )
    if defaults.get("point_range_fallback_enabled"):
        strict_range = defaults.get("max_range_m", actions["max_range_m"].default)
        fallback_range = defaults.get(
            "point_range_fallback_max_range_m",
            actions["point_range_fallback_max_range_m"].default,
        )
        if fallback_range <= strict_range:
            raise ConfigError(
                "point_range_fallback_max_range_m must be greater than max_range_m "
                "when point_range_fallback_enabled is true."
            )
        standard_minimum = defaults.get(
            "min_point_count",
            actions["min_point_count"].default,
        )
        fallback_minimum = defaults.get(
            "point_range_fallback_min_point_count",
            actions["point_range_fallback_min_point_count"].default,
        )
        if fallback_minimum > standard_minimum:
            raise ConfigError(
                "point_range_fallback_min_point_count cannot exceed min_point_count."
            )
    if (
        defaults.get("detection_view_mode", "panorama") == "panorama"
        and defaults.get("disable_full_panorama_detection")
        and defaults.get("disable_tiled_detection")
    ):
        raise ConfigError(
            "Full-panorama and tiled detection cannot both be disabled."
        )
    return defaults


def _extract_config_argument(
    argv: list[str],
    *,
    default_config_path: Path,
) -> tuple[Path | None, list[str], bool]:
    """Extract the optional config selector without consuming legacy option values."""

    remaining = list(argv)
    explicit = False
    disabled = False
    selected: Path | None = None

    if remaining and not remaining[0].startswith("-"):
        selected = Path(remaining.pop(0))
        explicit = True

    index = 0
    while index < len(remaining):
        token = remaining[index]
        if token == "--no-config":
            disabled = True
            remaining.pop(index)
            continue
        if token == "--config":
            if index + 1 >= len(remaining):
                raise ConfigError("--config requires a YAML file path.")
            if explicit:
                raise ConfigError("Specify the configuration path only once.")
            selected = Path(remaining[index + 1])
            explicit = True
            del remaining[index : index + 2]
            continue
        if token.startswith("--config="):
            if explicit:
                raise ConfigError("Specify the configuration path only once.")
            selected = Path(token.split("=", 1)[1])
            explicit = True
            remaining.pop(index)
            continue
        index += 1

    if disabled:
        if explicit:
            raise ConfigError("--no-config cannot be combined with a config path.")
        return None, remaining, False

    if selected is None:
        selected = default_config_path
    if explicit or selected.is_file():
        return selected, remaining, explicit
    return None, remaining, False


def parse_args_with_config(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None = None,
    *,
    default_config_path: Path = Path(DEFAULT_CONFIG_NAME),
) -> argparse.Namespace:
    """Parse YAML-first pipeline arguments while preserving legacy CLI overrides.

    Supported normal invocations are ``run_pipeline.py`` (loads config.yaml),
    ``run_pipeline.py custom.yaml``, and ``run_pipeline.py --config custom.yaml``.
    Remaining legacy CLI flags override YAML values and ``--no-config`` opts out.
    """

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        config_path, remaining, _explicit = _extract_config_argument(
            raw_argv,
            default_config_path=default_config_path,
        )
        if config_path is not None:
            defaults = load_config_defaults(parser, config_path)
            parser.set_defaults(**defaults)
    except ConfigError as exc:
        parser.error(str(exc))

    args = parser.parse_args(remaining)
    # Private metadata is excluded from processing fingerprints and is useful
    # only for the run log/effective configuration snapshot.
    args._config_path = str(config_path.resolve()) if config_path is not None else None
    return args


def serializable_config(args: argparse.Namespace) -> dict[str, Any]:
    """Return the effective public configuration for provenance/logging."""

    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in sorted(vars(args).items())
        if not key.startswith("_")
    }
