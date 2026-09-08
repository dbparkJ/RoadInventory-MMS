"""Unified, cached access to legacy PCDB and exported LAS point clouds.

The public API in this module deliberately mirrors :mod:`mms_shp_detection.pcdb`
where possible.  Catalog file and block dictionaries therefore remain easy to
feed into the existing pipeline while LAS files gain a lightweight sequential
chunk index.  Building the index reads a LAS file once; subsequent runs reuse
the JSON cache while the source signature is unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import laspy
import numpy as np

from .pcdb import (
    PCDB_NAME_PATTERN,
    _index_single_pcdb as _legacy_index_single_pcdb,
    select_candidate_blocks as _legacy_select_candidate_blocks,
)


POINTCLOUD_CATALOG_VERSION = 6
DEFAULT_LAS_CHUNK_SIZE = 250_000
DEFAULT_DECODED_BLOCK_CACHE_MAX_ENTRIES = 64
DEFAULT_DECODED_BLOCK_CACHE_MAX_BYTES = 512 * 1024 * 1024
NEUTRAL_RGB = np.asarray([128, 128, 128], dtype=np.uint8)

# ``read_block_records`` deliberately remains an array-only mapping.  A
# structured, row-aligned availability array lets all of the existing generic
# mask/concatenate code continue to work while distinguishing a real zero from
# a field that the source format cannot provide.
POINT_RECORD_FIELD_AVAILABILITY_DTYPE = np.dtype(
    [
        ("xyz", np.bool_),
        ("rgb", np.bool_),
        ("rgb_raw", np.bool_),
        ("intensity", np.bool_),
        ("classification", np.bool_),
        ("gps_time", np.bool_),
        ("gps_time_type", np.bool_),
        ("return_number", np.bool_),
        ("number_of_returns", np.bool_),
        ("point_source_id", np.bool_),
        ("scan_angle", np.bool_),
        ("scan_angle_rank", np.bool_),
        ("user_data", np.bool_),
        ("edge_of_flight_line", np.bool_),
        ("scan_direction_flag", np.bool_),
        ("source_index", np.bool_),
    ]
)

_LAS_NAME_PATTERN = re.compile(
    r"^(?P<job>.+)_(?P<track>Track[_-]?\d+)(?:_(?P<split>[1-9]\d*))?$",
    re.IGNORECASE,
)
_DELIVERY_LAS_NAME_PATTERN = re.compile(
    r"^(?P<base>.+?\.zfs)_(?P<split>\d+)$",
    re.IGNORECASE,
)


def _canonical_name(value: Any) -> str | None:
    if value is None:
        return None
    canonical = re.sub(r"[^0-9a-z]+", "", str(value).casefold())
    return canonical or None


def _exact_scope_key(value: Any) -> str | None:
    """Match object-crop identities by normalized case only, not punctuation."""

    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value.strip()).casefold()
    return text or None


def _normalize_include_jobs(
    include_jobs: Iterable[str] | str | None,
) -> tuple[list[str] | None, list[str] | None]:
    if include_jobs is None:
        return None, None
    values = [include_jobs] if isinstance(include_jobs, str) else list(include_jobs)
    names_by_key: dict[str, str] = {}
    for value in values:
        name = str(value).strip()
        key = _canonical_name(name)
        if key is not None:
            names_by_key.setdefault(key, name)
    keys = sorted(names_by_key)
    names = [names_by_key[key] for key in keys]
    return names, keys


def _normalize_include_scope(
    include_scope: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, set[tuple[str, str]] | None]:
    """Normalize an exact Job/Track allowlist without substring matching.

    Both the configuration shape (``{"strict": true, "jobs": [...]}``) and
    the compact API shape (``[{"job_id": ..., "track_ids": [...]}]``) are
    accepted.  ``tracks`` is the canonical key; ``track_ids`` remains an alias
    for the API example in the P0 specification.
    """

    if include_scope is None:
        return None, None

    strict = True
    if isinstance(include_scope, Mapping):
        if "jobs" in include_scope:
            unknown_top_level = set(include_scope) - {"strict", "jobs"}
            if unknown_top_level:
                names = ", ".join(sorted(str(name) for name in unknown_top_level))
                raise ValueError(f"Unknown include_scope fields: {names}")
            strict_value = include_scope.get("strict", True)
            if not isinstance(strict_value, bool):
                raise ValueError("include_scope.strict must be a boolean")
            strict = strict_value
            entries_value = include_scope.get("jobs")
        else:
            # Also accept one compact job mapping directly.
            entries_value = [include_scope]
    else:
        if isinstance(include_scope, (str, bytes)):
            raise ValueError("include_scope must contain Job/Track mappings")
        entries_value = include_scope

    if isinstance(entries_value, Mapping) or isinstance(entries_value, (str, bytes)):
        raise ValueError("include_scope.jobs must be a list of mappings")
    try:
        entries = list(entries_value)
    except TypeError as exc:
        raise ValueError("include_scope.jobs must be a list of mappings") from exc
    if not entries:
        if strict:
            raise ValueError("strict include_scope must contain at least one job")
        # An explicitly non-strict empty scope is the feature-off/legacy
        # compatible form: retain it in the catalog identity but allow all
        # discovered sources.
        return {"strict": False, "jobs": []}, None

    tracks_by_job: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"include_scope.jobs[{index}] must be a mapping")
        unknown_fields = set(entry) - {"job_id", "tracks", "track_ids"}
        if unknown_fields:
            names = ", ".join(sorted(str(name) for name in unknown_fields))
            raise ValueError(
                f"Unknown include_scope.jobs[{index}] fields: {names}"
            )
        if "tracks" in entry and "track_ids" in entry:
            raise ValueError(
                f"include_scope.jobs[{index}] cannot define both tracks and track_ids"
            )
        job_key = _exact_scope_key(entry.get("job_id"))
        if job_key is None:
            raise ValueError(f"include_scope.jobs[{index}].job_id is required")
        track_values = entry.get("tracks", entry.get("track_ids"))
        if isinstance(track_values, (str, bytes)):
            track_values = [track_values]
        if track_values is None:
            raise ValueError(f"include_scope.jobs[{index}].tracks is required")
        try:
            raw_tracks = list(track_values)
        except TypeError as exc:
            raise ValueError(
                f"include_scope.jobs[{index}].tracks must be a list"
            ) from exc
        if not raw_tracks:
            raise ValueError(
                f"include_scope.jobs[{index}].tracks must not be empty"
            )
        job_tracks = tracks_by_job.setdefault(job_key, set())
        for track_index, raw_track in enumerate(raw_tracks):
            track_key = _exact_scope_key(raw_track)
            if track_key is None:
                raise ValueError(
                    f"include_scope.jobs[{index}].tracks[{track_index}] is invalid"
                )
            job_tracks.add(track_key)

    normalized_jobs = [
        {"job_id": job_key, "tracks": sorted(tracks_by_job[job_key])}
        for job_key in sorted(tracks_by_job)
    ]
    normalized = {"strict": strict, "jobs": normalized_jobs}
    pairs = {
        (job["job_id"], track_key)
        for job in normalized_jobs
        for track_key in job["tracks"]
    }
    return normalized, pairs


def _filter_las_paths_by_scope(
    paths: Iterable[Path],
    normalized_scope: dict[str, Any] | None,
    allowed_pairs: set[tuple[str, str]] | None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Filter LAS paths by parsed identity before any LAS file is opened."""

    if normalized_scope is None or allowed_pairs is None:
        return list(paths), []

    strict = bool(normalized_scope["strict"])
    selected: list[Path] = []
    filtered: list[dict[str, Any]] = []
    matched_pairs: set[tuple[str, str]] = set()
    for path in paths:
        identity = _parse_las_identity(path)
        job_key = _exact_scope_key(identity.get("job_name"))
        track_key = _exact_scope_key(identity.get("track_name"))
        if job_key is None or track_key is None:
            if strict:
                raise ValueError(
                    "Strict include_scope could not parse exact Job/Track identity "
                    f"for LAS source: {path.resolve()}"
                )
            filtered.append(
                {
                    "path": str(path.resolve()),
                    "job_name": identity.get("job_name"),
                    "track_name": identity.get("track_name"),
                    "reason": "scope_identity_unavailable",
                }
            )
            continue

        pair = (job_key, track_key)
        if pair not in allowed_pairs:
            filtered.append(
                {
                    "path": str(path.resolve()),
                    "job_name": identity.get("job_name"),
                    "track_name": identity.get("track_name"),
                    "reason": "job_track_not_included",
                }
            )
            continue
        selected.append(path)
        matched_pairs.add(pair)

    missing_pairs = sorted(allowed_pairs - matched_pairs)
    if strict and missing_pairs:
        missing = ", ".join(f"{job}/{track}" for job, track in missing_pairs)
        raise FileNotFoundError(
            f"No LAS files found for strict include_scope pairs: {missing}"
        )
    return selected, filtered


def _log(logger: Any, level: str, message: str, *args: Any) -> None:
    if logger is not None:
        getattr(logger, level)(message, *args)


def resolve_safe_pointcloud_source(path: Path | str, data_root: Path | str) -> Path:
    """Resolve one current source without permitting a link/root escape."""

    root = Path(data_root).resolve(strict=True)
    candidate = Path(path)
    if candidate.is_symlink() or bool(
        getattr(candidate, "is_junction", lambda: False)()
    ):
        raise ValueError(
            "Symbolic links and junctions are not allowed in point-cloud data."
        )
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "A point-cloud source escaped the configured data root."
        ) from exc
    return resolved


def _source_signature(
    paths: Iterable[Path],
    data_root: Path,
    *,
    reject_symlinks: bool = False,
) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: str(item.resolve()).casefold()):
        if reject_symlinks:
            path = resolve_safe_pointcloud_source(path, data_root)
        stat = path.stat()
        try:
            relative_path = str(path.resolve().relative_to(data_root.resolve()))
        except ValueError:
            if reject_symlinks:
                raise ValueError(
                    "A point-cloud source escaped the configured data root."
                )
            relative_path = path.name
        signature.append(
            {
                "path": str(path.resolve()),
                "relative_path": relative_path,
                "file_size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return signature


def _discover_sources(
    data_root: Path,
    *,
    reject_symlinks: bool = False,
) -> tuple[list[Path], list[Path]]:
    if not data_root.exists():
        raise FileNotFoundError(f"Point-cloud data root not found: {data_root}")
    if not data_root.is_dir():
        raise NotADirectoryError(f"Point-cloud data root is not a directory: {data_root}")

    resolved_root = data_root.resolve(strict=True)
    pcdb_paths: list[Path] = []
    las_paths: list[Path] = []
    if reject_symlinks:
        # ``Path.rglob`` plus ``resolve`` on every panorama/image made a cached
        # point catalog take tens of seconds to reopen on large deliveries.
        # Traverse with scandir instead: inspect every link boundary, but only
        # resolve directories and actual point-cloud sources.
        pending = [resolved_root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if entry.is_symlink():
                            raise ValueError(
                                "Symbolic links and junctions are not allowed "
                                "in point-cloud data."
                            )
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                            if is_directory:
                                is_junction = bool(
                                    getattr(path, "is_junction", lambda: False)()
                                )
                                if is_junction:
                                    raise ValueError(
                                        "Symbolic links and junctions are not allowed "
                                        "in point-cloud data."
                                    )
                                resolved_directory = path.resolve(strict=True)
                                try:
                                    resolved_directory.relative_to(resolved_root)
                                except ValueError as exc:
                                    raise ValueError(
                                        "A point-cloud source escaped the configured "
                                        "data root."
                                    ) from exc
                                pending.append(resolved_directory)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError as exc:
                            raise ValueError(
                                "A point-cloud source could not be inspected safely."
                            ) from exc
                        suffix = path.suffix.casefold()
                        if suffix not in {".pcdb", ".las"}:
                            continue
                        # Recheck the actual source through pathlib as a
                        # defense against a directory-entry race.
                        if path.is_symlink():
                            raise ValueError(
                                "Symbolic links and junctions are not allowed "
                                "in point-cloud data."
                            )
                        try:
                            path.resolve(strict=True).relative_to(resolved_root)
                        except ValueError as exc:
                            raise ValueError(
                                "A point-cloud source escaped the configured data root."
                            ) from exc
                        if suffix == ".pcdb":
                            pcdb_paths.append(path)
                        else:
                            las_paths.append(path)
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError(
                    "A point-cloud folder could not be inspected safely."
                ) from exc
    else:
        for path in data_root.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix == ".pcdb":
                pcdb_paths.append(path)
            elif suffix == ".las":
                las_paths.append(path)
    sort_key = lambda item: str(item.resolve()).casefold()
    return sorted(pcdb_paths, key=sort_key), sorted(las_paths, key=sort_key)


def _parse_las_identity(path: Path) -> dict[str, Any]:
    match = _LAS_NAME_PATTERN.match(path.stem)
    if match is not None:
        split_text = match.group("split")
        return {
            "job_name": match.group("job"),
            "track_name": match.group("track"),
            "split_index": int(split_text) if split_text is not None else None,
            "split_base_stem": f"{match.group('job')}_{match.group('track')}",
        }

    delivery_match = _DELIVERY_LAS_NAME_PATTERN.match(path.stem)
    track_dir = next(
        (
            parent
            for parent in path.parents
            if re.fullmatch(r"Track[_-]?\d+", parent.name, re.IGNORECASE)
        ),
        None,
    )
    if delivery_match is not None and track_dir is not None:
        return {
            "job_name": track_dir.parent.name,
            "track_name": track_dir.name,
            "split_index": int(delivery_match.group("split")),
            "split_base_stem": delivery_match.group("base"),
        }

    return {
        "job_name": None,
        "track_name": track_dir.name if track_dir is not None else None,
        "split_index": None,
        "split_base_stem": path.stem,
    }


def _las_header_summary(path: Path) -> dict[str, Any]:
    """Read only the LAS header fields needed to validate a split set."""

    with laspy.open(path) as reader:
        header = reader.header
        return {
            "path": str(path.resolve()),
            "point_count": int(header.point_count),
            "crs_wkt": (_las_crs_wkt(header) or "").replace("\x00", "").strip() or None,
            "scales": [float(value) for value in header.scales],
            "offsets": [float(value) for value in header.offsets],
            "point_format_id": int(header.point_format.id),
            "dimensions": [str(name) for name in header.point_format.dimension_names],
            "mins": [float(value) for value in header.mins],
            "maxs": [float(value) for value in header.maxs],
        }


def _header_scale_offset_grid_is_valid(summary: dict[str, Any]) -> bool:
    """Check that bounds are representable on a finite LAS scale/offset grid.

    Numbered Leica exports legitimately use a different integer origin for each
    split.  Offset equality is therefore not required; each offset must instead
    be finite and its declared bounds must lie on its own positive scale grid.
    """

    scales = np.asarray(summary["scales"], dtype=np.float64)
    offsets = np.asarray(summary["offsets"], dtype=np.float64)
    bounds = np.asarray([summary["mins"], summary["maxs"]], dtype=np.float64)
    if (
        scales.shape != (3,)
        or offsets.shape != (3,)
        or bounds.shape != (2, 3)
        or not np.all(np.isfinite(scales))
        or not np.all(np.isfinite(offsets))
        or not np.all(np.isfinite(bounds))
        or np.any(scales <= 0.0)
        or np.any(bounds[0] > bounds[1])
    ):
        return False

    grid_coordinates = (bounds - offsets[None, :]) / scales[None, :]
    grid_error = np.abs(grid_coordinates - np.rint(grid_coordinates))
    return bool(np.all(grid_error <= 1e-4))


def _validate_numbered_las_splits(
    full_path: Path,
    split_paths: list[Path],
) -> dict[str, Any]:
    """Validate that numbered LAS files are a complete header-level replacement."""

    split_paths = sorted(
        split_paths,
        key=lambda path: int(_parse_las_identity(path)["split_index"]),
    )
    split_indices = [int(_parse_las_identity(path)["split_index"]) for path in split_paths]
    expected_indices = list(range(1, len(split_paths) + 1))
    reasons: list[str] = []
    if split_indices != expected_indices:
        reasons.append("non_contiguous_split_indices")

    try:
        full_header = _las_header_summary(full_path)
        split_headers = [_las_header_summary(path) for path in split_paths]
    except Exception as exc:
        return {
            "status": "failed",
            "reasons": reasons
            + [f"header_read_error:{type(exc).__name__}:{exc}"],
            "split_indices": split_indices,
            "expected_split_indices": expected_indices,
            "offset_policy": "finite_per_file_scale_grid",
        }

    all_headers = [full_header, *split_headers]
    grid_valid = all(_header_scale_offset_grid_is_valid(item) for item in all_headers)
    if not grid_valid:
        reasons.append("invalid_scale_offset_grid")

    full_scales = np.asarray(full_header["scales"], dtype=np.float64)
    scales_match = all(
        np.allclose(
            np.asarray(item["scales"], dtype=np.float64),
            full_scales,
            rtol=0.0,
            atol=1e-15,
        )
        for item in split_headers
    )
    if not scales_match:
        reasons.append("scale_mismatch")

    crs_match = all(item["crs_wkt"] == full_header["crs_wkt"] for item in split_headers)
    if not crs_match:
        reasons.append("crs_mismatch")

    point_format_match = all(
        item["point_format_id"] == full_header["point_format_id"]
        and item["dimensions"] == full_header["dimensions"]
        for item in split_headers
    )
    if not point_format_match:
        reasons.append("point_format_mismatch")

    split_point_count = sum(int(item["point_count"]) for item in split_headers)
    point_count_match = split_point_count == int(full_header["point_count"])
    if not point_count_match:
        reasons.append("point_count_mismatch")

    split_union_min = np.min(
        np.asarray([item["mins"] for item in split_headers], dtype=np.float64), axis=0
    )
    split_union_max = np.max(
        np.asarray([item["maxs"] for item in split_headers], dtype=np.float64), axis=0
    )
    full_min = np.asarray(full_header["mins"], dtype=np.float64)
    full_max = np.asarray(full_header["maxs"], dtype=np.float64)
    # Independently rebased offsets can move a re-encoded coordinate by up to
    # one scale unit.  This sample's full/split extrema differ by 0.5 mm.
    bounds_tolerance = np.maximum(full_scales, 1e-12)
    bounds_match = bool(
        np.all(np.abs(split_union_min - full_min) <= bounds_tolerance)
        and np.all(np.abs(split_union_max - full_max) <= bounds_tolerance)
    )
    if not bounds_match:
        reasons.append("bounds_mismatch")

    return {
        "status": "passed" if not reasons else "failed",
        "reasons": reasons,
        "split_indices": split_indices,
        "expected_split_indices": expected_indices,
        "full_point_count": int(full_header["point_count"]),
        "split_point_count": split_point_count,
        "point_count_match": point_count_match,
        "crs_match": crs_match,
        "scales_match": scales_match,
        "point_format_match": point_format_match,
        "offsets_compatible": grid_valid,
        "offset_policy": "finite_per_file_scale_grid",
        "full_bounds": [full_header["mins"], full_header["maxs"]],
        "split_union_bounds": [split_union_min.tolist(), split_union_max.tolist()],
        "bounds_tolerance": bounds_tolerance.tolist(),
        "bounds_match": bounds_match,
    }


def _prefer_numbered_las_splits(
    paths: list[Path],
    logger: Any = None,
) -> tuple[list[Path], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Use numbered splits only when their headers prove a complete replacement."""

    identities = {str(path.resolve()): _parse_las_identity(path) for path in paths}
    groups: dict[tuple[str, str], list[Path]] = {}
    for path in paths:
        identity = identities[str(path.resolve())]
        group_key = (
            str(path.resolve().parent).casefold(),
            str(identity["split_base_stem"]).casefold(),
        )
        groups.setdefault(group_key, []).append(path)

    selected: list[Path] = []
    excluded: list[dict[str, Any]] = []
    provenance_by_path: dict[str, dict[str, Any]] = {}
    for group_paths in groups.values():
        full_paths = [
            path
            for path in group_paths
            if identities[str(path.resolve())]["split_index"] is None
        ]
        split_paths = [
            path
            for path in group_paths
            if identities[str(path.resolve())]["split_index"] is not None
        ]
        # A suffix such as ``_1`` is considered a split only when its unsuffixed
        # sibling is also present.  This avoids dropping ordinary numbered files.
        validation: dict[str, Any] | None = None
        use_numbered_splits = False
        if len(full_paths) == 1 and split_paths:
            validation = _validate_numbered_las_splits(full_paths[0], split_paths)
            use_numbered_splits = validation["status"] == "passed"
            if not use_numbered_splits:
                _log(
                    logger,
                    "warning",
                    "LAS split validation failed for %s; using full LAS (%s).",
                    identities[str(full_paths[0].resolve())]["split_base_stem"],
                    ", ".join(validation["reasons"]),
                )
        elif full_paths and split_paths:
            validation = {
                "status": "failed",
                "reasons": ["full_file_count_not_one"],
                "split_indices": sorted(
                    int(identities[str(path.resolve())]["split_index"])
                    for path in split_paths
                ),
                "offset_policy": "finite_per_file_scale_grid",
            }
            _log(
                logger,
                "warning",
                "LAS split validation found %d full companions; using full LAS files.",
                len(full_paths),
            )
        kept_paths = split_paths if use_numbered_splits else group_paths
        if full_paths and split_paths and not use_numbered_splits:
            kept_paths = full_paths
        selected.extend(kept_paths)

        full_companions = [str(path.resolve()) for path in full_paths]
        split_companions = [str(path.resolve()) for path in split_paths]
        for path in kept_paths:
            provenance_by_path[str(path.resolve())] = {
                "selection_policy": (
                    "numbered_splits_validated"
                    if use_numbered_splits
                    else "full_preferred_split_validation_failed"
                    if full_paths and split_paths
                    else "standalone"
                ),
                "full_companion_paths": full_companions if full_paths and split_paths else [],
                "split_companion_paths": split_companions if full_paths and split_paths else [],
                "split_validation": validation,
            }
        if use_numbered_splits:
            for path in full_paths:
                excluded.append(
                    {
                        "path": str(path.resolve()),
                        "reason": "numbered_splits_validated",
                        "replacement_paths": split_companions,
                        "split_validation": validation,
                    }
                )
        elif full_paths and split_paths:
            for path in split_paths:
                excluded.append(
                    {
                        "path": str(path.resolve()),
                        "reason": "split_validation_failed_full_preferred",
                        "replacement_paths": full_companions,
                        "split_validation": validation,
                    }
                )

    selected.sort(key=lambda item: str(item.resolve()).casefold())
    excluded.sort(key=lambda item: str(item["path"]).casefold())
    return selected, provenance_by_path, excluded


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("\x00")
    return text or None


def _las_crs_wkt(header: laspy.LasHeader) -> str | None:
    try:
        crs = header.parse_crs(prefer_wkt=True)
    except Exception:
        crs = None
    if crs is not None:
        try:
            return crs.to_wkt()
        except Exception:
            return str(crs)

    # ``parse_crs`` may be unavailable when pyproj is not installed.  Preserve
    # an original WKT VLR string when laspy has already decoded one for us.
    for vlr in list(header.vlrs) + list(header.evlrs or []):
        string = getattr(vlr, "string", None)
        if string:
            return str(string).rstrip("\x00")
    return None


def _associated_crs_sidecar(path: Path, data_root: Path) -> Path | None:
    """Find the nearest unambiguous PRJ supplied with a LAS delivery track."""

    resolved_root = data_root.resolve()
    current = path.resolve().parent
    while True:
        candidates = sorted(
            (candidate for candidate in current.glob("*.prj") if candidate.is_file()),
            key=lambda item: item.name.casefold(),
        )
        if candidates:
            texts: dict[str, Path] = {}
            for candidate in candidates:
                try:
                    text = candidate.read_text(encoding="utf-8-sig").replace("\x00", "").strip()
                except (OSError, UnicodeError):
                    continue
                if text:
                    texts.setdefault(text, candidate)
            if len(texts) == 1:
                return next(iter(texts.values()))
            # Multiple different CRS declarations at one level are ambiguous;
            # do not climb higher and silently choose a broader file.
            return None
        if current == resolved_root or resolved_root not in current.parents:
            return None
        current = current.parent


def _sidecar_crs_wkt(
    path: Path,
    data_root: Path,
    *,
    reject_symlinks: bool = False,
) -> tuple[str | None, Path | None]:
    sidecar = _associated_crs_sidecar(path, data_root)
    if sidecar is None:
        return None, None
    if reject_symlinks:
        sidecar = resolve_safe_pointcloud_source(sidecar, data_root)
    try:
        value = sidecar.read_text(encoding="utf-8-sig").replace("\x00", "").strip()
    except (OSError, UnicodeError):
        return None, None
    return (value or None), sidecar


def _xyz_from_las_points(points: Any) -> np.ndarray:
    if len(points) == 0:
        return np.empty((0, 3), dtype=np.float64)
    # ScaledArrayView performs integer * scale + offset using float64.  Creating
    # each column independently avoids ever quantizing large world coordinates
    # through float32.
    return np.column_stack(
        (
            np.asarray(points.x, dtype=np.float64),
            np.asarray(points.y, dtype=np.float64),
            np.asarray(points.z, dtype=np.float64),
        )
    )


def _las_record_field_metadata(
    dimension_names: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Describe raw fields whose LAS source semantics must remain explicit."""

    dimensions = {str(name).casefold() for name in dimension_names}
    metadata: dict[str, dict[str, Any]] = {}
    if {"red", "green", "blue"}.issubset(dimensions):
        metadata["rgb_raw"] = {
            "source_dimensions": ["red", "green", "blue"],
            "source_dtype": "uint16",
            "output_dtype": "uint16",
            "semantic": "las_rgb_channels_raw",
        }
    for name, dtype, semantic in (
        ("point_source_id", "uint16", "las_point_source_id"),
        ("user_data", "uint8", "las_user_data"),
        ("edge_of_flight_line", "uint8", "las_edge_of_flight_line_flag"),
        ("scan_direction_flag", "uint8", "las_scan_direction_flag"),
    ):
        if name in dimensions:
            metadata[name] = {
                "source_dimension": name,
                "source_dtype": dtype,
                "output_dtype": dtype,
                "semantic": semantic,
            }
    if "scan_angle" in dimensions:
        metadata["scan_angle"] = {
            "source_dimension": "scan_angle",
            "source_dtype": "int16",
            "output_dtype": "int16",
            "semantic": "las_1_4_scan_angle_raw_0_006_degree",
            "scale_to_degrees": 0.006,
        }
    if "scan_angle_rank" in dimensions:
        metadata["scan_angle_rank"] = {
            "source_dimension": "scan_angle_rank",
            "source_dtype": "int8",
            "output_dtype": "int8",
            "semantic": "legacy_las_scan_angle_rank_degrees",
            "scale_to_degrees": 1.0,
        }
    return metadata


def _index_single_las(
    path: Path,
    data_root: Path,
    chunk_size: int,
    selection_provenance: dict[str, Any],
    *,
    reject_symlinks: bool = False,
) -> dict[str, Any]:
    if reject_symlinks:
        path = resolve_safe_pointcloud_source(path, data_root)
    stat = path.stat()
    identity = _parse_las_identity(path)
    blocks: list[dict[str, Any]] = []

    with laspy.open(path) as reader:
        header = reader.header
        point_count = int(header.point_count)
        dimension_names = [str(name) for name in header.point_format.dimension_names]
        has_classification = "classification" in dimension_names
        classification_counts = np.zeros(256, dtype=np.uint64)
        start = 0
        while start < point_count:
            count = min(chunk_size, point_count - start)
            points = reader.read_points(count)
            actual_count = len(points)
            if actual_count == 0:
                break
            if has_classification:
                classes = np.asarray(points.classification, dtype=np.int16)
                valid_classes = classes[(classes >= 0) & (classes <= 255)]
                if valid_classes.size:
                    classification_counts += np.bincount(
                        valid_classes,
                        minlength=256,
                    ).astype(np.uint64, copy=False)
            xyz = _xyz_from_las_points(points)
            finite = np.all(np.isfinite(xyz), axis=1)
            if finite.any():
                finite_xyz = xyz[finite]
                minimum = finite_xyz.min(axis=0).tolist()
                maximum = finite_xyz.max(axis=0).tolist()
            else:
                minimum = [None, None, None]
                maximum = [None, None, None]
            blocks.append(
                {
                    "name": f"las:{start}:{actual_count}",
                    "source_type": "las",
                    "start": int(start),
                    "count": int(actual_count),
                    "point_count": int(actual_count),
                    "min": minimum,
                    "max": maximum,
                }
            )
            start += actual_count

        crs_wkt = _las_crs_wkt(header)
        crs_sidecar_path: Path | None = None
        crs_source = "las_header" if crs_wkt else None
        if not crs_wkt:
            crs_wkt, crs_sidecar_path = _sidecar_crs_wkt(
                path,
                data_root,
                reject_symlinks=reject_symlinks,
            )
            if crs_wkt:
                crs_source = "nearest_delivery_prj"
        scales = [float(value) for value in header.scales]
        offsets = [float(value) for value in header.offsets]
        point_format_id = int(header.point_format.id)
        record_field_metadata = _las_record_field_metadata(dimension_names)
        las_version = str(header.version)
        system_identifier = _safe_text(header.system_identifier)
        generating_software = _safe_text(header.generating_software)
        creation_date = (
            header.creation_date.isoformat() if header.creation_date is not None else None
        )

    valid_blocks = [
        block for block in blocks if all(value is not None for value in block["min"])
    ]
    if valid_blocks:
        file_min = [min(block["min"][axis] for block in valid_blocks) for axis in range(3)]
        file_max = [max(block["max"][axis] for block in valid_blocks) for axis in range(3)]
    else:
        file_min = [None, None, None]
        file_max = [None, None, None]

    try:
        relative_path = str(path.resolve().relative_to(data_root.resolve()))
    except ValueError:
        if reject_symlinks:
            raise ValueError(
                "A point-cloud source escaped the configured data root."
            )
        relative_path = path.name
    provenance = {
        "source_type": "las",
        "provenance_complete": True,
        "training": True,
        "source_path": str(path.resolve()),
        "relative_path": relative_path,
        "index_method": "sequential_point_chunks",
        "chunk_size": int(chunk_size),
        "las_version": las_version,
        "system_identifier": system_identifier,
        "generating_software": generating_software,
        "creation_date": creation_date,
        "crs_source": crs_source,
        "crs_sidecar_path": (
            str(crs_sidecar_path.resolve()) if crs_sidecar_path is not None else None
        ),
        "crs_sidecar_file_size": (
            int(crs_sidecar_path.stat().st_size) if crs_sidecar_path is not None else None
        ),
        "crs_sidecar_mtime_ns": (
            int(crs_sidecar_path.stat().st_mtime_ns) if crs_sidecar_path is not None else None
        ),
        **selection_provenance,
    }
    return {
        "path": str(path.resolve()),
        "source_type": "las",
        "format": "las",
        "provenance_complete": True,
        "training": True,
        "job_name": identity["job_name"],
        "track_name": identity["track_name"],
        "split_index": identity["split_index"],
        "route_id": None,
        "timestamp_iso": None,
        "file_size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "point_count": int(point_count),
        "file_min": file_min,
        "file_max": file_max,
        "crs_wkt": crs_wkt,
        "wkt": crs_wkt,
        "scales": scales,
        "scale": scales,
        "offsets": offsets,
        "point_format_id": point_format_id,
        "point_format": {
            "id": point_format_id,
            "dimensions": dimension_names,
        },
        "record_field_metadata": record_field_metadata,
        "classification_summary": {
            "dimension_present": bool(has_classification),
            "point_count": int(classification_counts.sum()),
            "nonzero_point_count": int(classification_counts[1:].sum()),
            "class_counts": {
                str(class_id): int(count)
                for class_id, count in enumerate(classification_counts)
                if count
            },
        },
        "provenance": provenance,
        "blocks": blocks,
    }


def _generic_pcdb_index(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            """
            SELECT NAME, LENGTH(DATA), SUBSTR(DATA, 1, 52)
            FROM CRYSTAL_CUBE
            WHERE NAME LIKE '%.bpc'
            ORDER BY NAME
            """
        ).fetchall()
    finally:
        connection.close()

    blocks: list[dict[str, Any]] = []
    for block_name, blob_length, header in rows:
        header_bytes = bytes(header)
        if len(header_bytes) < 52:
            continue
        bounds = struct.unpack("<6d", header_bytes[:48])
        point_count = struct.unpack("<I", header_bytes[48:52])[0]
        blocks.append(
            {
                "name": block_name,
                "source_type": "pcdb",
                "min": list(bounds[:3]),
                "max": list(bounds[3:]),
                "point_count": int(point_count),
                "blob_length": int(blob_length),
            }
        )
    if blocks:
        file_min = [min(block["min"][axis] for block in blocks) for axis in range(3)]
        file_max = [max(block["max"][axis] for block in blocks) for axis in range(3)]
    else:
        file_min = [None, None, None]
        file_max = [None, None, None]
    return {"file_min": file_min, "file_max": file_max, "blocks": blocks}


def _index_single_pcdb(
    path: Path,
    data_root: Path,
    *,
    reject_symlinks: bool = False,
) -> dict[str, Any]:
    if reject_symlinks:
        path = resolve_safe_pointcloud_source(path, data_root)
    try:
        indexed = _legacy_index_single_pcdb(path)
    except (ValueError, IndexError):
        indexed = _generic_pcdb_index(path)

    match = PCDB_NAME_PATTERN.search(path.name)
    route_id: str | None = None
    timestamp_iso: str | None = None
    scanner_id: int | None = None
    if match is not None:
        route_id = match.group("route")
        timestamp_iso = dt.datetime.strptime(
            match.group("stamp"), "%y%m%d_%H%M%S"
        ).isoformat()
        scanner_id = int(match.group("scanner"))

    stat = path.stat()
    try:
        relative_path = str(path.resolve().relative_to(data_root.resolve()))
    except ValueError:
        if reject_symlinks:
            raise ValueError(
                "A point-cloud source escaped the configured data root."
            )
        relative_path = path.name
    blocks = indexed.get("blocks", [])
    for block in blocks:
        block.setdefault("source_type", "pcdb")
    return {
        **indexed,
        "path": str(path.resolve()),
        "source_type": "pcdb",
        "format": "pcdb",
        "provenance_complete": False,
        "training": False,
        "route_id": indexed.get("route_id", route_id),
        "timestamp_iso": indexed.get("timestamp_iso", timestamp_iso),
        "scanner_id": indexed.get("scanner_id", scanner_id),
        "job_name": None,
        "track_name": None,
        "file_size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "point_count": int(sum(block.get("point_count", 0) for block in blocks)),
        "crs_wkt": None,
        "wkt": None,
        "scales": None,
        "scale": None,
        "offsets": None,
        "point_format_id": None,
        "point_format": {"id": None, "dimensions": ["x", "y", "z", "rgb", "intensity"]},
        "record_field_metadata": {},
        "classification_summary": {
            "dimension_present": False,
            "point_count": 0,
            "nonzero_point_count": 0,
            "class_counts": {},
        },
        "provenance": {
            "source_type": "pcdb",
            "provenance_complete": False,
            "training": False,
            "source_path": str(path.resolve()),
            "relative_path": relative_path,
            "index_method": "crystal_cube_bpc_headers",
        },
        "blocks": blocks,
    }


def _aggregate_classification_summary(files: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine cached per-file LAS class histograms without inferring their meaning."""

    class_counts: dict[int, int] = {}
    source_file_count = 0
    files_with_dimension = 0
    files_with_nonzero_classes = 0
    classification_point_count = 0
    for item in files:
        source_file_count += 1
        summary = item.get("classification_summary") or {}
        if summary.get("dimension_present"):
            files_with_dimension += 1
        if int(summary.get("nonzero_point_count") or 0) > 0:
            files_with_nonzero_classes += 1
        classification_point_count += int(summary.get("point_count") or 0)
        for raw_class_id, raw_count in (summary.get("class_counts") or {}).items():
            class_id = int(raw_class_id)
            class_counts[class_id] = class_counts.get(class_id, 0) + int(raw_count)
    return {
        "source_file_count": source_file_count,
        "files_with_dimension": files_with_dimension,
        "files_with_nonzero_classes": files_with_nonzero_classes,
        "point_count": classification_point_count,
        "nonzero_point_count": sum(
            count for class_id, count in class_counts.items() if class_id != 0
        ),
        "class_counts": {
            str(class_id): class_counts[class_id] for class_id in sorted(class_counts)
        },
    }


def _cache_matches(
    cached: Any,
    signature: dict[str, Any],
    source_mode: str,
    selected_source_type: str,
    chunk_size: int,
) -> bool:
    return bool(
        isinstance(cached, dict)
        and cached.get("catalog_version") == POINTCLOUD_CATALOG_VERSION
        and cached.get("signature") == signature
        and cached.get("source_mode") == source_mode
        and cached.get("selected_source_type") == selected_source_type
        and cached.get("las_chunk_size") == chunk_size
    )


def _cached_file_map(cached: Any, chunk_size: int) -> dict[str, dict[str, Any]]:
    if not isinstance(cached, dict):
        return {}
    if cached.get("catalog_version") != POINTCLOUD_CATALOG_VERSION:
        return {}
    if cached.get("las_chunk_size") != chunk_size:
        return {}
    return {
        str(item.get("path")): item
        for item in cached.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }


def _same_file_signature(
    cached_file: dict[str, Any],
    path: Path,
    data_root: Path,
    *,
    reject_symlinks: bool = False,
) -> bool:
    if reject_symlinks:
        path = resolve_safe_pointcloud_source(path, data_root)
    stat = path.stat()
    if not (
        cached_file.get("file_size") == int(stat.st_size)
        and cached_file.get("mtime_ns") == int(stat.st_mtime_ns)
        and cached_file.get("source_type") == path.suffix.casefold().lstrip(".")
    ):
        return False
    if path.suffix.casefold() != ".las":
        return True
    provenance = cached_file.get("provenance") or {}
    if provenance.get("crs_source") == "las_header":
        return True
    sidecar = _associated_crs_sidecar(path, data_root)
    if sidecar is None:
        return provenance.get("crs_sidecar_path") is None
    if reject_symlinks:
        sidecar = resolve_safe_pointcloud_source(sidecar, data_root)
    sidecar_stat = sidecar.stat()
    return (
        provenance.get("crs_sidecar_path") == str(sidecar.resolve())
        and provenance.get("crs_sidecar_file_size") == int(sidecar_stat.st_size)
        and provenance.get("crs_sidecar_mtime_ns") == int(sidecar_stat.st_mtime_ns)
    )


def _common_crs_wkt(files: list[dict[str, Any]]) -> str | None:
    if not files:
        return None
    values = [item.get("crs_wkt") for item in files]
    if any(not value for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values[1:]) else None


def build_pointcloud_catalog(
    data_root: Path,
    cache_path: Path,
    logger: Any = None,
    *,
    source: str = "auto",
    las_chunk_size: int = DEFAULT_LAS_CHUNK_SIZE,
    include_jobs: Iterable[str] | str | None = None,
    include_scope: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    reject_symlinks: bool = False,
) -> dict[str, Any]:
    """Discover point clouds and build or load their persistent spatial catalog.

    ``source='auto'`` catalogs both PCDB and LAS when a recursive parent contains
    independent legacy and Leica deliveries. Task identity and spatial scope keep
    duplicate backends from being mixed at projection time. ``include_jobs``
    limits LAS discovery to named Leica jobs before files are opened; it does not
    filter legacy PCDB files. ``include_scope`` adds an exact normalized
    Job/Track allowlist for LAS.  In strict mode an unparseable LAS identity,
    an allowlisted pair with no source, or an admitted PCDB source without an
    exact identity fails closed before any point-cloud file is opened.
    """

    data_root = Path(data_root)
    cache_path = Path(cache_path)
    source_mode = source.casefold()
    if source_mode not in {"auto", "pcdb", "las"}:
        raise ValueError("source must be one of: auto, pcdb, las")
    if las_chunk_size <= 0:
        raise ValueError("las_chunk_size must be greater than zero")
    las_chunk_size = int(las_chunk_size)
    include_job_names, include_job_keys = _normalize_include_jobs(include_jobs)
    normalized_include_scope, include_scope_pairs = _normalize_include_scope(
        include_scope
    )

    pcdb_paths, all_las_paths = _discover_sources(
        data_root,
        reject_symlinks=reject_symlinks,
    )
    if source_mode == "auto":
        selected_source_type = (
            "mixed"
            if pcdb_paths and all_las_paths
            else "pcdb"
            if pcdb_paths
            else "las"
        )
    else:
        selected_source_type = source_mode

    if (
        normalized_include_scope is not None
        and bool(normalized_include_scope["strict"])
        and pcdb_paths
        and selected_source_type in {"pcdb", "mixed"}
    ):
        raise ValueError(
            "Strict include_scope cannot admit a PCDB source without exact "
            "Job/Track identity; select source='las' or use a non-strict scope. "
            f"First unidentified source: {pcdb_paths[0].resolve()}"
        )

    if selected_source_type == "pcdb":
        selected_paths = pcdb_paths
        selection_provenance: dict[str, dict[str, Any]] = {}
        excluded_files: list[dict[str, Any]] = []
        job_filtered_files: list[dict[str, Any]] = []
        discovered_paths = pcdb_paths
        effective_include_job_names: list[str] | None = None
        effective_include_job_keys: list[str] | None = None
        effective_include_scope: dict[str, Any] | None = None
        scope_filtered_files: list[dict[str, Any]] = []
    else:
        effective_include_job_names = include_job_names
        effective_include_job_keys = include_job_keys
        effective_include_scope = normalized_include_scope
        scope_las_paths, scope_filtered_files = _filter_las_paths_by_scope(
            all_las_paths,
            normalized_include_scope,
            include_scope_pairs,
        )
        if include_job_keys is None:
            job_las_paths = scope_las_paths
            job_filtered_files = []
        else:
            include_job_key_set = set(include_job_keys)
            job_las_paths = []
            job_filtered_files = []
            for path in scope_las_paths:
                identity = _parse_las_identity(path)
                file_job_key = _canonical_name(identity.get("job_name"))
                if file_job_key in include_job_key_set:
                    job_las_paths.append(path)
                else:
                    job_filtered_files.append(
                        {
                            "path": str(path.resolve()),
                            "job_name": identity.get("job_name"),
                            "reason": "job_not_included",
                        }
                    )
            _log(
                logger,
                "info",
                "LAS job filter selected %d/%d files for jobs: %s",
                len(job_las_paths),
                len(all_las_paths),
                ", ".join(include_job_names or []),
            )
        selected_las_paths, selection_provenance, excluded_files = _prefer_numbered_las_splits(
            job_las_paths,
            logger=logger,
        )
        selected_paths = (
            [*pcdb_paths, *selected_las_paths]
            if selected_source_type == "mixed"
            else selected_las_paths
        )
        # Include excluded full companions in the signature so adding/removing a
        # split invalidates the selection decision, not only the point index.
        discovered_paths = (
            [*pcdb_paths, *job_las_paths]
            if selected_source_type == "mixed"
            else job_las_paths
        )

    if not selected_paths:
        if source_mode == "auto":
            raise FileNotFoundError(f"No .pcdb or .las files found under {data_root}")
        if selected_source_type in {"las", "mixed"} and include_job_keys is not None:
            requested = ", ".join(include_job_names or []) or "<empty>"
            raise FileNotFoundError(
                f"No .las files for include_jobs ({requested}) found under {data_root}"
            )
        raise FileNotFoundError(f"No .{selected_source_type} files found under {data_root}")

    crs_sidecars = sorted(
        {
            sidecar.resolve()
            for path in discovered_paths
            if path.suffix.casefold() == ".las"
            if (sidecar := _associated_crs_sidecar(path, data_root)) is not None
        },
        key=lambda item: str(item).casefold(),
    )
    signature = {
        "catalog_version": POINTCLOUD_CATALOG_VERSION,
        "source_mode": source_mode,
        "selected_source_type": selected_source_type,
        "las_chunk_size": las_chunk_size,
        "include_job_keys": effective_include_job_keys,
        "include_scope": effective_include_scope,
        "source_files": _source_signature(
            discovered_paths,
            data_root,
            reject_symlinks=reject_symlinks,
        ),
        "crs_sidecars": _source_signature(
            crs_sidecars,
            data_root,
            reject_symlinks=reject_symlinks,
        ),
    }
    cached: dict[str, Any] | None = None
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            cached = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            _log(logger, "warning", "Ignoring unreadable point-cloud catalog: %s", cache_path)
    if _cache_matches(
        cached,
        signature,
        source_mode,
        selected_source_type,
        las_chunk_size,
    ):
        _log(logger, "info", "Loaded cached point-cloud catalog from %s", cache_path)
        return cached  # type: ignore[return-value]

    cached_files = _cached_file_map(cached, las_chunk_size)
    files: list[dict[str, Any]] = []
    started_at = time.perf_counter()
    _log(
        logger,
        "info",
        "Building %s point-cloud catalog for %d files...",
        selected_source_type.upper(),
        len(selected_paths),
    )
    for index, path in enumerate(selected_paths, start=1):
        path_text = str(path.resolve())
        cached_file = cached_files.get(path_text)
        if cached_file is not None and _same_file_signature(
            cached_file,
            path,
            data_root,
            reject_symlinks=reject_symlinks,
        ):
            # Selection provenance can change without source bytes changing.
            if path.suffix.casefold() == ".las":
                cached_file = dict(cached_file)
                cached_file["provenance"] = {
                    **cached_file.get("provenance", {}),
                    **selection_provenance.get(path_text, {}),
                }
            files.append(cached_file)
            _log(logger, "info", "Reused point-cloud index %d/%d: %s", index, len(selected_paths), path.name)
            continue

        file_started_at = time.perf_counter()
        _log(logger, "info", "Indexing point cloud %d/%d: %s", index, len(selected_paths), path.name)
        if path.suffix.casefold() == ".pcdb":
            indexed = _index_single_pcdb(
                path,
                data_root,
                reject_symlinks=reject_symlinks,
            )
        else:
            indexed = _index_single_las(
                path,
                data_root,
                las_chunk_size,
                selection_provenance.get(path_text, {"selection_policy": "standalone"}),
                reject_symlinks=reject_symlinks,
            )
        files.append(indexed)
        _log(
            logger,
            "info",
            "Indexed point cloud %d/%d: %s in %.1fs",
            index,
            len(selected_paths),
            path.name,
            time.perf_counter() - file_started_at,
        )

    common_wkt = _common_crs_wkt(files)
    catalog = {
        "catalog_version": POINTCLOUD_CATALOG_VERSION,
        "catalog_type": "pointcloud",
        "source_mode": source_mode,
        "selected_source_type": selected_source_type,
        "las_chunk_size": las_chunk_size,
        "data_root": str(data_root.resolve()),
        "include_jobs": effective_include_job_names,
        "include_job_keys": effective_include_job_keys,
        "include_scope": effective_include_scope,
        "signature": signature,
        "crs_wkt": common_wkt,
        "wkt": common_wkt,
        "files": files,
        "provenance_complete": all(
            bool(item.get("provenance_complete", False)) for item in files
        ),
        "training": all(bool(item.get("training", False)) for item in files),
        "classification_summary": _aggregate_classification_summary(files),
        "excluded_files": excluded_files,
        "job_filtered_files": job_filtered_files,
        "scope_filtered_files": scope_filtered_files,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_name(
        f".{cache_path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(cache_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    _log(
        logger,
        "info",
        "Finished point-cloud catalog in %.1fs and saved %s",
        time.perf_counter() - started_at,
        cache_path,
    )
    return catalog


def _canonical_track(value: Any) -> str | None:
    canonical = _canonical_name(value)
    if canonical is None:
        return None
    match = re.search(r"(?:track)?0*(\d+)$", canonical)
    if match is not None:
        return str(int(match.group(1)))
    return canonical


def _task_job_track(image_task: dict[str, Any]) -> tuple[Any, Any]:
    job_name = image_task.get("job_name")
    track_name = image_task.get("track_name")
    if job_name is not None and track_name is not None:
        return job_name, track_name

    image_path_text = image_task.get("image_path")
    if image_path_text:
        # Exclude the filename: Leica image names themselves also begin with
        # ``Job_`` and would otherwise be mistaken for the job directory.
        parts = Path(str(image_path_text)).parts[:-1]
        if track_name is None:
            track_name = next(
                (part for part in reversed(parts) if re.fullmatch(r"Track[_-]?\d+", part, re.IGNORECASE)),
                None,
            )
        if job_name is None:
            job_name = next(
                (part.removesuffix(".job") for part in reversed(parts) if part.casefold().startswith("job_")),
                None,
            )
    return job_name, track_name


def _bbox_distance_xy(item: dict[str, Any], origin_x: float, origin_y: float) -> float:
    minimum = item.get("file_min")
    maximum = item.get("file_max")
    if not minimum or not maximum or minimum[0] is None or maximum[0] is None:
        return math.inf
    min_x, min_y = float(minimum[0]), float(minimum[1])
    max_x, max_y = float(maximum[0]), float(maximum[1])
    dx = min_x - origin_x if origin_x < min_x else origin_x - max_x if origin_x > max_x else 0.0
    dy = min_y - origin_y if origin_y < min_y else origin_y - max_y if origin_y > max_y else 0.0
    return math.hypot(dx, dy)


def match_nearest_pointcloud_files(
    image_task: dict[str, Any],
    catalog: dict[str, Any],
    neighbor_count: int,
) -> list[dict[str, Any]]:
    """Match job/track first, then rank files by XY distance to their bbox."""

    origin = image_task.get("origin")
    if origin is None or len(origin) < 2:
        raise ValueError("image_task origin must contain at least X and Y")
    origin_x, origin_y = float(origin[0]), float(origin[1])
    files = list(catalog.get("files", []))
    if not files:
        return []

    pointcloud_scope = image_task.get("pointcloud_scope")
    if pointcloud_scope:
        scope = Path(str(pointcloud_scope)).resolve()
        scoped_files = []
        for item in files:
            try:
                candidate = Path(str(item.get("path"))).resolve()
                candidate.relative_to(scope)
            except (TypeError, ValueError):
                continue
            scoped_files.append(item)
        if scoped_files:
            files = scoped_files

    job_name, track_name = _task_job_track(image_task)
    job_key = _canonical_name(job_name)
    track_key = _canonical_track(track_name)

    def job_matches(item: dict[str, Any]) -> bool:
        return job_key is not None and _canonical_name(item.get("job_name")) == job_key

    def track_matches(item: dict[str, Any]) -> bool:
        return track_key is not None and _canonical_track(item.get("track_name")) == track_key

    # Use the most specific non-empty tier.  This avoids contaminating an exact
    # Leica job/track with files from another export merely to fill neighbor_count.
    candidates = files
    if job_key is not None and track_key is not None:
        exact = [item for item in files if job_matches(item) and track_matches(item)]
        if exact:
            # A validated Leica split set is one logical point cloud.  Returning
            # only the first N splits would silently create spatial holes, so an
            # exact job/track match is never capped by neighbor_count.
            exact.sort(
                key=lambda item: (
                    _bbox_distance_xy(item, origin_x, origin_y),
                    int(item.get("split_index") or 0),
                    str(item.get("path", "")).casefold(),
                )
            )
            return exact
        else:
            same_job = [item for item in files if job_matches(item)]
            same_track = [item for item in files if track_matches(item)]
            candidates = same_job or same_track or files
    elif job_key is not None:
        same_job = [item for item in files if job_matches(item)]
        candidates = same_job or files
    elif track_key is not None:
        same_track = [item for item in files if track_matches(item)]
        candidates = same_track or files
    else:
        route_id = image_task.get("route_id")
        if route_id is not None:
            same_route = [
                item for item in files if str(item.get("route_id")) == str(route_id)
            ]
            candidates = same_route or files

    image_timestamp: dt.datetime | None = None
    try:
        if image_task.get("timestamp_iso"):
            image_timestamp = dt.datetime.fromisoformat(str(image_task["timestamp_iso"]))
    except ValueError:
        image_timestamp = None

    def timestamp_distance(item: dict[str, Any]) -> float:
        if image_timestamp is None or not item.get("timestamp_iso"):
            return math.inf
        try:
            return abs(
                (dt.datetime.fromisoformat(str(item["timestamp_iso"])) - image_timestamp).total_seconds()
            )
        except ValueError:
            return math.inf

    if neighbor_count <= 0:
        return []
    candidates.sort(
        key=lambda item: (
            _bbox_distance_xy(item, origin_x, origin_y),
            timestamp_distance(item),
            str(item.get("path", "")).casefold(),
        )
    )
    return candidates[: int(neighbor_count)]


def select_candidate_blocks(
    pointcloud_file: dict[str, Any],
    origin_xyz: np.ndarray,
    center_ray: np.ndarray,
    detection_angle_rad: float,
    max_range_m: float,
    angle_margin_rad: float,
) -> list[dict[str, Any]]:
    """Select PCDB or LAS blocks intersecting a camera-centred view cone."""

    # Both catalog backends use the legacy ``min``/``max`` block schema, so the
    # well-tested geometric selector can be reused without source-specific code.
    origin = np.asarray(origin_xyz, dtype=np.float64)
    ray = np.asarray(center_ray, dtype=np.float64)
    ray_norm = float(np.linalg.norm(ray))
    if ray_norm == 0.0 or not math.isfinite(ray_norm):
        return []
    ray = ray / ray_norm
    valid_blocks = [
        block
        for block in pointcloud_file.get("blocks", [])
        if block.get("min")
        and block.get("max")
        and all(value is not None for value in block["min"])
        and all(value is not None for value in block["max"])
    ]
    if len(valid_blocks) == len(pointcloud_file.get("blocks", [])):
        return _legacy_select_candidate_blocks(
            pointcloud_file,
            origin,
            ray,
            float(detection_angle_rad),
            float(max_range_m),
            float(angle_margin_rad),
        )
    normalized_file = dict(pointcloud_file)
    normalized_file["blocks"] = valid_blocks
    return _legacy_select_candidate_blocks(
        normalized_file,
        origin,
        ray,
        float(detection_angle_rad),
        float(max_range_m),
        float(angle_margin_rad),
    )


def _empty_points() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.empty((0, 3), dtype=np.float64),
        np.empty((0, 3), dtype=np.uint8),
        np.empty((0,), dtype=np.uint16),
    )


def _parse_las_block_name(block_name: str) -> tuple[int, int]:
    match = re.fullmatch(r"las:(\d+):(\d+)", block_name)
    if match is None:
        raise ValueError(f"Unsupported LAS block name: {block_name}")
    return int(match.group(1)), int(match.group(2))


def _rgb8_from_las(points: Any) -> np.ndarray:
    dimension_names = {str(name).casefold() for name in points.point_format.dimension_names}
    if not {"red", "green", "blue"}.issubset(dimension_names):
        return np.tile(NEUTRAL_RGB, (len(points), 1))
    rgb16 = np.column_stack(
        (
            np.asarray(points.red, dtype=np.uint16),
            np.asarray(points.green, dtype=np.uint16),
            np.asarray(points.blue, dtype=np.uint16),
        )
    )
    # LAS RGB channels are unsigned 16-bit.  Division by 257 maps both endpoints
    # exactly and rounds the midpoint correctly: 0 -> 0, 32768 -> 128, 65535 -> 255.
    return ((rgb16.astype(np.uint32) + 128) // 257).astype(np.uint8)


def _record_field_availability(
    count: int,
    present: Mapping[str, bool],
) -> np.ndarray:
    """Build a row-aligned availability array with one stable record dtype."""

    availability = np.zeros(
        (count,),
        dtype=POINT_RECORD_FIELD_AVAILABILITY_DTYPE,
    )
    for field_name in POINT_RECORD_FIELD_AVAILABILITY_DTYPE.names or ():
        availability[field_name] = bool(present.get(field_name, False))
    return availability


def _raw_las_dimension(
    points: Any,
    name: str,
    dtype: np.dtype[Any] | type[np.generic],
) -> np.ndarray:
    """Copy one LAS dimension without applying a scaled presentation view."""

    raw_names = points.array.dtype.names or ()
    if name in raw_names:
        return np.asarray(points.array[name], dtype=dtype).copy()
    return np.asarray(getattr(points, name), dtype=dtype).copy()


class PointCloudReaderCache:
    """Thread-safe cache for source handles and decoded point-cloud blocks.

    Decoded arrays are shared between :meth:`read_block_points` and
    :meth:`read_block_records`.  :meth:`read_preview_block` retains only the
    four display fields in the same bounded LRU, or reuses a cached full block.
    Returned arrays are read-only so a caller
    cannot accidentally corrupt a cached block.  The LRU is constrained by
    both entry count and decoded array bytes; a block larger than the byte
    budget is returned but not retained.
    """

    def __init__(
        self,
        *,
        decoded_cache_max_entries: int = DEFAULT_DECODED_BLOCK_CACHE_MAX_ENTRIES,
        decoded_cache_max_bytes: int = DEFAULT_DECODED_BLOCK_CACHE_MAX_BYTES,
    ) -> None:
        if decoded_cache_max_entries < 0:
            raise ValueError("decoded_cache_max_entries must be non-negative")
        if decoded_cache_max_bytes < 0:
            raise ValueError("decoded_cache_max_bytes must be non-negative")
        self._state_lock = threading.RLock()
        self._source_locks: dict[tuple[str, str], threading.RLock] = {}
        self._closed = False
        self._pcdb_connections: dict[
            str,
            tuple[tuple[int, int], sqlite3.Connection],
        ] = {}
        self._las_readers: dict[
            str,
            tuple[tuple[int, int], laspy.LasReader],
        ] = {}
        self._decoded_cache_max_entries = int(decoded_cache_max_entries)
        self._decoded_cache_max_bytes = int(decoded_cache_max_bytes)
        self._decoded_cache_bytes = 0
        self._decoded_blocks: OrderedDict[
            tuple[str, str, tuple[int, int], str | int, int | None, str],
            tuple[dict[str, np.ndarray], int],
        ] = OrderedDict()
        self._inflight_decodes: dict[
            tuple[str, str, tuple[int, int], str | int, int | None, str],
            Future[dict[str, np.ndarray]],
        ] = {}

    def _source_lock(self, source_type: str, resolved_path: str) -> threading.RLock:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PointCloudReaderCache is closed")
            key = (source_type, resolved_path)
            lock = self._source_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[key] = lock
            return lock

    @staticmethod
    def _source_version(
        pointcloud: str | Path | dict[str, Any],
        resolved_path: str,
    ) -> tuple[int, int]:
        """Return the catalog or filesystem version of one point-cloud source."""

        if isinstance(pointcloud, dict):
            file_size = pointcloud.get("file_size")
            mtime_ns = pointcloud.get("mtime_ns")
            if (
                isinstance(file_size, int)
                and not isinstance(file_size, bool)
                and file_size >= 0
                and isinstance(mtime_ns, int)
                and not isinstance(mtime_ns, bool)
                and mtime_ns >= 0
            ):
                return int(file_size), int(mtime_ns)
        stat = Path(resolved_path).stat()
        return int(stat.st_size), int(stat.st_mtime_ns)

    def _discard_stale_decoded_blocks(
        self,
        source_type: str,
        resolved_path: str,
        source_version: tuple[int, int],
    ) -> None:
        """Drop decoded blocks retained for older content at the same path."""

        with self._state_lock:
            stale_keys = [
                key
                for key in self._decoded_blocks
                if key[0] == source_type
                and key[1] == resolved_path
                and key[2] != source_version
            ]
            for key in stale_keys:
                _records, byte_size = self._decoded_blocks.pop(key)
                self._decoded_cache_bytes -= byte_size

    def _pcdb_connection(
        self,
        path: str,
        source_version: tuple[int, int],
    ) -> sqlite3.Connection:
        resolved = str(Path(path).resolve())
        cached = self._pcdb_connections.get(resolved)
        if cached is not None and cached[0] != source_version:
            cached[1].close()
            self._pcdb_connections.pop(resolved, None)
            self._discard_stale_decoded_blocks("pcdb", resolved, source_version)
            cached = None
        if cached is None:
            # The enclosing re-entrant lock serializes use of a connection.
            # Disabling SQLite's creator-thread check lets later worker threads
            # safely reuse that same serialized connection.
            connection = sqlite3.connect(resolved, check_same_thread=False)
            self._pcdb_connections[resolved] = (source_version, connection)
            return connection
        return cached[1]

    def _las_reader(
        self,
        path: str,
        source_version: tuple[int, int],
    ) -> laspy.LasReader:
        resolved = str(Path(path).resolve())
        cached = self._las_readers.get(resolved)
        if cached is not None and cached[0] != source_version:
            cached[1].close()
            self._las_readers.pop(resolved, None)
            self._discard_stale_decoded_blocks("las", resolved, source_version)
            cached = None
        if cached is None:
            reader = laspy.open(resolved)
            self._las_readers[resolved] = (source_version, reader)
            return reader
        return cached[1]

    def _read_pcdb(
        self,
        path: str,
        source_version: tuple[int, int],
        block_name: str,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        row = self._pcdb_connection(path, source_version).execute(
            "SELECT DATA FROM CRYSTAL_CUBE WHERE NAME = ?", (block_name,)
        ).fetchone()
        if row is None:
            return _empty_points()
        data = bytes(row[0])
        if len(data) < 52:
            return _empty_points()

        bounds = np.asarray(struct.unpack("<6d", data[:48]), dtype=np.float64)
        block_center = (bounds[:3] + bounds[3:]) * 0.5
        declared_count = int(struct.unpack("<I", data[48:52])[0])
        point_count = min(declared_count, max(0, len(data) - 52) // 17)
        if point_count == 0:
            return _empty_points()

        point_dtype = np.dtype(
            [
                ("offset", "<f4", (3,)),
                ("rgb", "u1", (3,)),
                ("intensity", "<u2"),
            ]
        )
        raw = np.frombuffer(data, dtype=point_dtype, count=point_count, offset=52)
        # The local offsets are float32 by format, but the block centre is not.
        # Promote before addition to retain centimetres at multi-million metre Y.
        xyz = raw["offset"].astype(np.float64) + block_center[None, :]
        return xyz, raw["rgb"].copy(), raw["intensity"].copy()

    def _read_las(
        self,
        path: str,
        source_version: tuple[int, int],
        start: int,
        count: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if start < 0 or count < 0:
            raise ValueError("LAS block start and count must be non-negative")
        if count == 0:
            return _empty_points()
        reader = self._las_reader(path, source_version)
        reader.seek(start)
        points = reader.read_points(count)
        xyz = _xyz_from_las_points(points)
        rgb = _rgb8_from_las(points)
        dimension_names = {
            str(name).casefold() for name in points.point_format.dimension_names
        }
        intensity = (
            np.asarray(points.intensity, dtype=np.uint16).copy()
            if "intensity" in dimension_names
            else np.zeros((len(points),), dtype=np.uint16)
        )
        return xyz, rgb, intensity

    def _read_las_preview(
        self,
        path: str,
        source_version: tuple[int, int],
        start: int,
        count: int,
    ) -> dict[str, np.ndarray]:
        """Decode display fields without allocating preservation attributes."""

        if start < 0 or count < 0:
            raise ValueError("LAS block start and count must be non-negative")
        if count == 0:
            xyz, rgb, intensity = _empty_points()
            return {
                "xyz": xyz,
                "rgb": rgb,
                "intensity": intensity,
                "classification": np.empty((0,), dtype=np.int16),
            }
        reader = self._las_reader(path, source_version)
        reader.seek(start)
        points = reader.read_points(count)
        dimension_names = {
            str(name).casefold() for name in points.point_format.dimension_names
        }
        return {
            "xyz": _xyz_from_las_points(points),
            "rgb": _rgb8_from_las(points),
            "intensity": (
                np.asarray(points.intensity, dtype=np.uint16).copy()
                if "intensity" in dimension_names
                else np.zeros((len(points),), dtype=np.uint16)
            ),
            "classification": (
                np.asarray(points.classification, dtype=np.int16).copy()
                if "classification" in dimension_names
                else np.full((len(points),), -1, dtype=np.int16)
            ),
        }

    def _read_pcdb_preview(
        self,
        path: str,
        source_version: tuple[int, int],
        block_name: str,
    ) -> dict[str, np.ndarray]:
        xyz, rgb, intensity = self._read_pcdb(path, source_version, block_name)
        return {
            "xyz": xyz,
            "rgb": rgb,
            "intensity": intensity,
            "classification": np.full((xyz.shape[0],), -1, dtype=np.int16),
        }

    def _read_las_records(
        self,
        path: str,
        source_version: tuple[int, int],
        start: int,
        count: int,
    ) -> dict[str, np.ndarray]:
        if start < 0 or count < 0:
            raise ValueError("LAS block start and count must be non-negative")
        if count == 0:
            xyz, rgb, intensity = _empty_points()
            return {
                "xyz": xyz,
                "rgb": rgb,
                "rgb_raw": np.empty((0, 3), dtype=np.uint16),
                "intensity": intensity,
                "classification": np.empty((0,), dtype=np.int16),
                "gps_time": np.empty((0,), dtype=np.float64),
                "gps_time_type": np.empty((0,), dtype=np.int8),
                "return_number": np.empty((0,), dtype=np.uint8),
                "number_of_returns": np.empty((0,), dtype=np.uint8),
                "point_source_id": np.empty((0,), dtype=np.uint16),
                "scan_angle": np.empty((0,), dtype=np.int16),
                "scan_angle_rank": np.empty((0,), dtype=np.int8),
                "user_data": np.empty((0,), dtype=np.uint8),
                "edge_of_flight_line": np.empty((0,), dtype=np.uint8),
                "scan_direction_flag": np.empty((0,), dtype=np.uint8),
                "source_index": np.empty((0,), dtype=np.int64),
                "field_availability": _record_field_availability(0, {}),
            }
        reader = self._las_reader(path, source_version)
        reader.seek(start)
        points = reader.read_points(count)
        xyz = _xyz_from_las_points(points)
        rgb = _rgb8_from_las(points)
        dimension_names = {str(name).casefold() for name in points.point_format.dimension_names}
        has_rgb_raw = {"red", "green", "blue"}.issubset(dimension_names)
        rgb_raw = (
            np.column_stack(
                (
                    _raw_las_dimension(points, "red", np.uint16),
                    _raw_las_dimension(points, "green", np.uint16),
                    _raw_las_dimension(points, "blue", np.uint16),
                )
            )
            if has_rgb_raw
            else np.zeros((len(points), 3), dtype=np.uint16)
        )
        if "intensity" in dimension_names:
            intensity = np.asarray(points.intensity, dtype=np.uint16).copy()
        else:
            intensity = np.zeros((len(points),), dtype=np.uint16)
        classification = (
            np.asarray(points.classification, dtype=np.int16).copy()
            if "classification" in dimension_names
            else np.full((len(points),), -1, dtype=np.int16)
        )
        gps_time = (
            np.asarray(points.gps_time, dtype=np.float64).copy()
            if "gps_time" in dimension_names
            else np.full((len(points),), np.nan, dtype=np.float64)
        )
        # GPS time type is a file-level LAS global-encoding flag.  Repeat it per
        # row so crops assembled from multiple blocks/files retain the encoding
        # associated with every selected source point.
        gps_time_type = np.full(
            (len(points),),
            int(reader.header.global_encoding.gps_time_type)
            if "gps_time" in dimension_names
            else -1,
            dtype=np.int8,
        )
        return_number = (
            np.asarray(points.return_number, dtype=np.uint8).copy()
            if "return_number" in dimension_names
            else np.zeros((len(points),), dtype=np.uint8)
        )
        number_of_returns = (
            np.asarray(points.number_of_returns, dtype=np.uint8).copy()
            if "number_of_returns" in dimension_names
            else np.zeros((len(points),), dtype=np.uint8)
        )
        point_source_id = (
            _raw_las_dimension(points, "point_source_id", np.uint16)
            if "point_source_id" in dimension_names
            else np.zeros((len(points),), dtype=np.uint16)
        )
        # Point formats 6+ store a signed int16 scan angle.  Older formats use
        # the distinct signed int8 scan_angle_rank.  Keep both arrays so their
        # source semantic and raw dtype can never be conflated.
        scan_angle = (
            _raw_las_dimension(points, "scan_angle", np.int16)
            if "scan_angle" in dimension_names
            else np.zeros((len(points),), dtype=np.int16)
        )
        scan_angle_rank = (
            _raw_las_dimension(points, "scan_angle_rank", np.int8)
            if "scan_angle_rank" in dimension_names
            else np.zeros((len(points),), dtype=np.int8)
        )
        user_data = (
            _raw_las_dimension(points, "user_data", np.uint8)
            if "user_data" in dimension_names
            else np.zeros((len(points),), dtype=np.uint8)
        )
        edge_of_flight_line = (
            np.asarray(points.edge_of_flight_line, dtype=np.uint8).copy()
            if "edge_of_flight_line" in dimension_names
            else np.zeros((len(points),), dtype=np.uint8)
        )
        scan_direction_flag = (
            np.asarray(points.scan_direction_flag, dtype=np.uint8).copy()
            if "scan_direction_flag" in dimension_names
            else np.zeros((len(points),), dtype=np.uint8)
        )
        availability = _record_field_availability(
            len(points),
            {
                "xyz": True,
                "rgb": has_rgb_raw,
                "rgb_raw": has_rgb_raw,
                "intensity": "intensity" in dimension_names,
                "classification": "classification" in dimension_names,
                "gps_time": "gps_time" in dimension_names,
                "gps_time_type": "gps_time" in dimension_names,
                "return_number": "return_number" in dimension_names,
                "number_of_returns": "number_of_returns" in dimension_names,
                "point_source_id": "point_source_id" in dimension_names,
                "scan_angle": "scan_angle" in dimension_names,
                "scan_angle_rank": "scan_angle_rank" in dimension_names,
                "user_data": "user_data" in dimension_names,
                "edge_of_flight_line": "edge_of_flight_line" in dimension_names,
                "scan_direction_flag": "scan_direction_flag" in dimension_names,
                "source_index": True,
            },
        )
        return {
            "xyz": xyz,
            "rgb": rgb,
            "rgb_raw": rgb_raw,
            "intensity": intensity,
            "classification": classification,
            "gps_time": gps_time,
            "gps_time_type": gps_time_type,
            "return_number": return_number,
            "number_of_returns": number_of_returns,
            "point_source_id": point_source_id,
            "scan_angle": scan_angle,
            "scan_angle_rank": scan_angle_rank,
            "user_data": user_data,
            "edge_of_flight_line": edge_of_flight_line,
            "scan_direction_flag": scan_direction_flag,
            "source_index": np.arange(start, start + len(points), dtype=np.int64),
            "field_availability": availability,
        }

    def _read_pcdb_records(
        self,
        path: str,
        source_version: tuple[int, int],
        block_name: str,
    ) -> dict[str, np.ndarray]:
        xyz, rgb, intensity = self._read_pcdb(path, source_version, block_name)
        count = xyz.shape[0]
        return {
            "xyz": xyz,
            "rgb": rgb,
            "rgb_raw": np.zeros((count, 3), dtype=np.uint16),
            "intensity": intensity,
            "classification": np.full((count,), -1, dtype=np.int16),
            "gps_time": np.full((count,), np.nan, dtype=np.float64),
            "gps_time_type": np.full((count,), -1, dtype=np.int8),
            "return_number": np.zeros((count,), dtype=np.uint8),
            "number_of_returns": np.zeros((count,), dtype=np.uint8),
            "point_source_id": np.zeros((count,), dtype=np.uint16),
            "scan_angle": np.zeros((count,), dtype=np.int16),
            "scan_angle_rank": np.zeros((count,), dtype=np.int8),
            "user_data": np.zeros((count,), dtype=np.uint8),
            "edge_of_flight_line": np.zeros((count,), dtype=np.uint8),
            "scan_direction_flag": np.zeros((count,), dtype=np.uint8),
            "source_index": np.full((count,), -1, dtype=np.int64),
            "field_availability": _record_field_availability(
                count,
                {
                    "xyz": True,
                    "rgb": True,
                    "intensity": True,
                },
            ),
        }

    @staticmethod
    def _freeze_records(
        records: dict[str, np.ndarray],
    ) -> tuple[dict[str, np.ndarray], int]:
        """Make decoded arrays immutable and return their unique byte size."""

        frozen: dict[str, np.ndarray] = {}
        byte_size = 0
        seen_arrays: set[int] = set()
        for name, value in records.items():
            array = np.asarray(value)
            array.setflags(write=False)
            frozen[name] = array
            array_identity = id(array)
            if array_identity not in seen_arrays:
                byte_size += int(array.nbytes)
                seen_arrays.add(array_identity)
        return frozen, byte_size

    def _cached_records(
        self,
        key: tuple[str, str, tuple[int, int], str | int, int | None, str],
        source_lock: threading.RLock,
        decode: Callable[[], dict[str, np.ndarray]],
    ) -> dict[str, np.ndarray]:
        """Return one immutable decoded block, updating the bounded LRU."""

        with self._state_lock:
            if self._closed:
                raise RuntimeError("PointCloudReaderCache is closed")
            if key[-1] == "preview":
                full_key = (*key[:-1], "records")
                full_cached = self._decoded_blocks.get(full_key)
                if full_cached is not None:
                    self._decoded_blocks.move_to_end(full_key)
                    return full_cached[0]
            cached = self._decoded_blocks.get(key)
            if cached is not None:
                self._decoded_blocks.move_to_end(key)
                return cached[0]

            future = self._inflight_decodes.get(key)
            decode_owner = future is None
            if future is None:
                future = Future()
                self._inflight_decodes[key] = future

        if not decode_owner:
            records = future.result()
            with self._state_lock:
                if key in self._decoded_blocks:
                    self._decoded_blocks.move_to_end(key)
            return records

        with source_lock:
            with self._state_lock:
                if self._closed:
                    error = RuntimeError("PointCloudReaderCache is closed")
                    pending = self._inflight_decodes.pop(key, None)
                    if pending is not None and not pending.done():
                        pending.set_exception(error)
                    raise error
            try:
                records, byte_size = self._freeze_records(decode())
            except BaseException as exc:
                with self._state_lock:
                    pending = self._inflight_decodes.pop(key, None)
                    if pending is not None and not pending.done():
                        pending.set_exception(exc)
                raise

            with self._state_lock:
                if (
                    not self._closed
                    and self._decoded_cache_max_entries > 0
                    and self._decoded_cache_max_bytes > 0
                    and byte_size <= self._decoded_cache_max_bytes
                ):
                    self._decoded_blocks[key] = (records, byte_size)
                    self._decoded_cache_bytes += byte_size
                    while (
                        len(self._decoded_blocks) > self._decoded_cache_max_entries
                        or self._decoded_cache_bytes > self._decoded_cache_max_bytes
                    ):
                        _evicted_key, (_evicted_records, evicted_bytes) = (
                            self._decoded_blocks.popitem(last=False)
                        )
                        self._decoded_cache_bytes -= evicted_bytes
                pending = self._inflight_decodes.pop(key, None)
                if pending is not None and not pending.done():
                    pending.set_result(records)
            return records

    def _cached_pcdb_records(
        self,
        path: str,
        source_version: tuple[int, int],
        block_name: str,
    ) -> dict[str, np.ndarray]:
        resolved = str(Path(path).resolve())
        source_lock = self._source_lock("pcdb", resolved)
        key = ("pcdb", resolved, source_version, block_name, None, "records")
        return self._cached_records(
            key,
            source_lock,
            lambda: self._read_pcdb_records(
                resolved,
                source_version,
                block_name,
            ),
        )

    def _cached_las_records(
        self,
        path: str,
        source_version: tuple[int, int],
        start: int,
        count: int,
    ) -> dict[str, np.ndarray]:
        resolved = str(Path(path).resolve())
        source_lock = self._source_lock("las", resolved)
        key = ("las", resolved, source_version, start, count, "records")
        return self._cached_records(
            key,
            source_lock,
            lambda: self._read_las_records(
                resolved,
                source_version,
                start,
                count,
            ),
        )

    def read_preview_block(
        self,
        pointcloud: str | Path | dict[str, Any],
        block: str | dict[str, Any],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Read immutable XYZ/RGB/intensity/classification for display only.

        Arrays have float64/uint8/uint16/int16 dtypes respectively.  The source
        handles, version checks, in-flight coalescing and LRU limits are shared
        with full records, whose preservation fields remain available through
        :meth:`read_block_records`.  PCDB classification is unknown (-1).
        """

        if isinstance(pointcloud, dict):
            path = str(pointcloud["path"])
            source_type = str(
                pointcloud.get("source_type") or pointcloud.get("format") or ""
            ).casefold()
        else:
            path = str(pointcloud)
            source_type = Path(path).suffix.casefold().lstrip(".")
        resolved_path = str(Path(path).resolve())
        if isinstance(block, dict):
            block_name = str(block.get("name", ""))
            source_type = str(block.get("source_type") or source_type).casefold()
            start = block.get("start")
            count = block.get("count", block.get("point_count"))
        else:
            block_name = str(block)
            start = None
            count = None

        if source_type == "pcdb" or Path(path).suffix.casefold() == ".pcdb":
            source_version = self._source_version(pointcloud, resolved_path)
            key = ("pcdb", resolved_path, source_version, block_name, None, "preview")
            records = self._cached_records(
                key,
                self._source_lock("pcdb", resolved_path),
                lambda: self._read_pcdb_preview(
                    resolved_path, source_version, block_name
                ),
            )
        elif source_type == "las" or Path(path).suffix.casefold() == ".las":
            if start is None or count is None:
                start, count = _parse_las_block_name(block_name)
            start, count = int(start), int(count)
            source_version = self._source_version(pointcloud, resolved_path)
            key = ("las", resolved_path, source_version, start, count, "preview")
            records = self._cached_records(
                key,
                self._source_lock("las", resolved_path),
                lambda: self._read_las_preview(
                    resolved_path, source_version, start, count
                ),
            )
        else:
            raise ValueError(f"Unsupported point-cloud source type for {path}")
        return (
            records["xyz"],
            records["rgb"],
            records["intensity"],
            records["classification"],
        )

    def read_block_points(
        self,
        pointcloud: str | Path | dict[str, Any],
        block: str | dict[str, Any],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Read a catalog block as float64 XYZ, uint8 RGB, and uint16 intensity.

        For easy migration, both ``(file_dict, block_dict)`` and the legacy
        ``(path, block_name)`` calling styles are accepted.
        """

        if isinstance(pointcloud, dict):
            path = str(pointcloud["path"])
            source_type = str(
                pointcloud.get("source_type") or pointcloud.get("format") or ""
            ).casefold()
        else:
            path = str(pointcloud)
            source_type = Path(path).suffix.casefold().lstrip(".")
        resolved_path = str(Path(path).resolve())

        if isinstance(block, dict):
            block_name = str(block.get("name", ""))
            source_type = str(block.get("source_type") or source_type).casefold()
            start = block.get("start")
            count = block.get("count", block.get("point_count"))
        else:
            block_name = str(block)
            start = None
            count = None

        if source_type == "pcdb" or Path(path).suffix.casefold() == ".pcdb":
            source_version = self._source_version(pointcloud, resolved_path)
            records = self._cached_pcdb_records(
                resolved_path,
                source_version,
                block_name,
            )
            return records["xyz"], records["rgb"], records["intensity"]
        if source_type == "las" or Path(path).suffix.casefold() == ".las":
            if start is None or count is None:
                start, count = _parse_las_block_name(block_name)
            source_version = self._source_version(pointcloud, resolved_path)
            records = self._cached_las_records(
                resolved_path,
                source_version,
                int(start),
                int(count),
            )
            return records["xyz"], records["rgb"], records["intensity"]
        raise ValueError(f"Unsupported point-cloud source type for {path}")

    def read_block_records(
        self,
        pointcloud: str | Path | dict[str, Any],
        block: str | dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """Read precise points plus LAS attributes used by pole/ground extraction.

        PCDB has no standard classification/GPS-return fields or LAS GPS time
        encoding flag, so those arrays use explicit unknown values (-1, NaN, or
        zero) while retaining the same row count as XYZ.
        """

        if isinstance(pointcloud, dict):
            path = str(pointcloud["path"])
            source_type = str(
                pointcloud.get("source_type") or pointcloud.get("format") or ""
            ).casefold()
        else:
            path = str(pointcloud)
            source_type = Path(path).suffix.casefold().lstrip(".")
        resolved_path = str(Path(path).resolve())

        if isinstance(block, dict):
            block_name = str(block.get("name", ""))
            source_type = str(block.get("source_type") or source_type).casefold()
            start = block.get("start")
            count = block.get("count", block.get("point_count"))
        else:
            block_name = str(block)
            start = None
            count = None

        if source_type == "pcdb" or Path(path).suffix.casefold() == ".pcdb":
            source_version = self._source_version(pointcloud, resolved_path)
            return dict(
                self._cached_pcdb_records(
                    resolved_path,
                    source_version,
                    block_name,
                )
            )
        if source_type == "las" or Path(path).suffix.casefold() == ".las":
            if start is None or count is None:
                start, count = _parse_las_block_name(block_name)
            source_version = self._source_version(pointcloud, resolved_path)
            return dict(
                self._cached_las_records(
                    resolved_path,
                    source_version,
                    int(start),
                    int(count),
                )
            )
        raise ValueError(f"Unsupported point-cloud source type for {path}")

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            source_locks = list(self._source_locks.values())

        for source_lock in source_locks:
            source_lock.acquire()
        try:
            for _source_version, connection in self._pcdb_connections.values():
                connection.close()
            self._pcdb_connections.clear()
            for _source_version, reader in self._las_readers.values():
                reader.close()
            self._las_readers.clear()
            with self._state_lock:
                self._decoded_blocks.clear()
                self._decoded_cache_bytes = 0
                error = RuntimeError("PointCloudReaderCache is closed")
                for future in self._inflight_decodes.values():
                    if not future.done():
                        future.set_exception(error)
                self._inflight_decodes.clear()
                self._source_locks.clear()
        finally:
            for source_lock in reversed(source_locks):
                source_lock.release()

    def __enter__(self) -> "PointCloudReaderCache":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_DECODED_BLOCK_CACHE_MAX_BYTES",
    "DEFAULT_DECODED_BLOCK_CACHE_MAX_ENTRIES",
    "DEFAULT_LAS_CHUNK_SIZE",
    "POINTCLOUD_CATALOG_VERSION",
    "POINT_RECORD_FIELD_AVAILABILITY_DTYPE",
    "PointCloudReaderCache",
    "build_pointcloud_catalog",
    "match_nearest_pointcloud_files",
    "select_candidate_blocks",
]
