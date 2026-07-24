from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import sys
import uuid
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable

import shapefile
from pyproj import CRS
from pyproj.exceptions import CRSError

EPSG_5179_ESRI_WKT = (
    'PROJCS["KGD2002_Unified_Coordinate_System",'
    'GEOGCS["GCS_KGD2002",'
    'DATUM["D_Korea_Geodetic_Datum_2002",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",1000000.0],'
    'PARAMETER["False_Northing",2000000.0],'
    'PARAMETER["Central_Meridian",127.5],'
    'PARAMETER["Scale_Factor",0.9996],'
    'PARAMETER["Latitude_Of_Origin",38.0],'
    'UNIT["Meter",1.0]]'
)

SHAPEFILE_COMPONENT_SUFFIXES = (
    ".dbf",
    ".shx",
    ".prj",
    ".cpg",
    ".qpj",
    ".wkt2",
    ".shp",
)
STALE_SPATIAL_INDEX_SUFFIXES = (
    ".qix",
    ".sbn",
    ".sbx",
    ".fbn",
    ".fbx",
    ".ain",
    ".aih",
    ".ixs",
    ".mxs",
    ".atx",
)


def make_detection_id(
    record_name: Any,
    image_name: Any,
    detection_index: Any,
) -> str:
    """Return a stable cross-layer key for one image detection."""

    identity = (
        f"{record_name or ''}|{image_name or ''}|"
        f"{int(detection_index) if detection_index is not None else 0}"
    )
    return "D" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:19]


def _temporary_shapefile_path(target_path: Path, role: str) -> Path:
    token = uuid.uuid4().hex
    return target_path.with_name(f".{target_path.stem}.{token}.{role}.shp")


def _remove_shapefile_bundle(path: Path) -> None:
    for suffix in (*SHAPEFILE_COMPONENT_SUFFIXES, *STALE_SPATIAL_INDEX_SUFFIXES):
        try:
            path.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            # A caller-visible exception remains authoritative. A locked hidden
            # temporary component can be removed after the external lock clears.
            pass


@contextmanager
def _exclusive_publish_lock(target_paths: list[Path]):
    """Prevent concurrent pipeline writers from interleaving final components."""

    parent_directories = sorted(
        {path.parent.resolve() for path in target_paths},
        key=lambda path: str(path).casefold(),
    )
    locked_handles: list[Any] = []
    try:
        for directory in parent_directories:
            directory.mkdir(parents=True, exist_ok=True)
            # Keep the lock on the output filesystem so separate users/hosts
            # addressing the same shared directory contend on the same inode.
            lock_path = directory / ".mms_shp_publish.lock"
            handle = lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise RuntimeError(
                    f"Another Shapefile publication is active for {directory}"
                ) from exc
            locked_handles.append(handle)
        yield
    finally:
        active_exception = sys.exc_info()[1]
        unlock_errors: list[str] = []
        for handle in reversed(locked_handles):
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                unlock_errors.append(f"{handle.name}: {exc}")
            finally:
                try:
                    handle.close()
                except OSError as exc:
                    unlock_errors.append(f"{handle.name}: close failed: {exc}")
        if unlock_errors:
            message = "Shapefile publish lock release failed: " + "; ".join(unlock_errors)
            if active_exception is not None:
                active_exception.add_note(message)
            # After a successful commit, close() still asks the operating
            # system to release the byte-range lock. Do not turn a committed
            # result into an ambiguous pipeline failure solely because the
            # explicit unlock call reported an error.


def _validate_staged_shapefile_bundle(source_path: Path) -> None:
    """Reopen a completed bundle before it is allowed to replace final data."""

    with shapefile.Reader(str(source_path), encoding="utf-8") as reader:
        if reader.shapeType != shapefile.POINTZ:
            raise ValueError(
                f"Staged Shapefile must be POINTZ, not type {reader.shapeType}: {source_path}"
            )
        shx_size = source_path.with_suffix(".shx").stat().st_size
        if shx_size < 100 or (shx_size - 100) % 8:
            raise ValueError(f"Staged Shapefile has an invalid SHX length: {source_path}")
        shape_count = (shx_size - 100) // 8
        if shape_count != reader.numRecords:
            raise ValueError(
                "Staged Shapefile geometry/attribute count mismatch: "
                f"shapes={shape_count}, records={reader.numRecords}, path={source_path}"
            )
    for suffix in (".prj", ".cpg", ".qpj", ".wkt2"):
        component = source_path.with_suffix(suffix)
        if not component.read_text(encoding="utf-8").strip():
            raise ValueError(f"Staged Shapefile sidecar is empty: {component}")
    if source_path.with_suffix(".qpj").read_bytes() != source_path.with_suffix(
        ".wkt2"
    ).read_bytes():
        raise ValueError(f"Staged .qpj and .wkt2 CRS definitions differ: {source_path}")


def _paths_refer_to_same_file(first: Path, second: Path) -> bool:
    first_key = os.path.normcase(str(first.resolve(strict=False)))
    second_key = os.path.normcase(str(second.resolve(strict=False)))
    if first_key == second_key:
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _stale_spatial_index_components(target_path: Path) -> list[Path]:
    components = [
        target_path.with_suffix(suffix)
        for suffix in STALE_SPATIAL_INDEX_SUFFIXES
        if target_path.with_suffix(suffix).exists()
    ]
    dbf_path = target_path.with_suffix(".dbf")
    if target_path.parent.is_dir() and dbf_path.is_file():
        with shapefile.Reader(dbf=str(dbf_path), encoding="utf-8") as reader:
            field_names = [str(field[0]) for field in reader.fields[1:]]
        expected_field_indexes = {
            f"{target_path.stem}.{field_name}.atx".casefold()
            for field_name in field_names
        }
        for candidate in target_path.parent.iterdir():
            if (
                candidate.is_file()
                and candidate.name.casefold() in expected_field_indexes
                and candidate not in components
            ):
                components.append(candidate)
    return components


def publish_shapefile_bundles(
    source_target_pairs: list[tuple[Path, Path]],
) -> None:
    """Publish one or more complete Shapefile bundles as one transaction.

    A Shapefile is seven sibling files in this project, so the filesystem cannot
    replace it with one atomic rename. Every component is first completed under
    a staging basename. Existing targets are backed up, replacements are
    attempted with ``os.replace``, and every already-published component is
    rolled back if a GIS lock or other I/O error occurs. Supplying both the sign
    and pole bundles prevents a new sign result from being paired with an old
    pole result when the second publication fails.
    """

    raw_pairs = [(Path(source), Path(target)) for source, target in source_target_pairs]
    for source_path, target_path in raw_pairs:
        if source_path.suffix.casefold() != ".shp" or target_path.suffix.casefold() != ".shp":
            raise ValueError("Shapefile staging and target paths must both end in .shp")
    pairs = [
        (source.resolve(strict=False), target.resolve(strict=False))
        for source, target in raw_pairs
    ]
    if not pairs:
        return
    all_sources = [source for source, _ in pairs]
    all_targets = [target for _, target in pairs]
    for paths, label in ((all_sources, "staging source"), (all_targets, "target")):
        for index, first in enumerate(paths):
            for second in paths[index + 1 :]:
                if _paths_refer_to_same_file(first, second):
                    raise ValueError(f"Each Shapefile publication {label} must be unique")
    for source_path in all_sources:
        for target_path in all_targets:
            if _paths_refer_to_same_file(source_path, target_path):
                raise ValueError(
                    "Shapefile staging paths must not alias any publication target"
                )
    source_components = [
        source_path.with_suffix(suffix)
        for source_path in all_sources
        for suffix in SHAPEFILE_COMPONENT_SUFFIXES
    ]
    target_components = [
        target_path.with_suffix(suffix)
        for target_path in all_targets
        for suffix in SHAPEFILE_COMPONENT_SUFFIXES
    ]
    for source_component in source_components:
        for target_component in target_components:
            if _paths_refer_to_same_file(source_component, target_component):
                raise ValueError(
                    "A staged Shapefile component aliases a publication target component"
                )
    for source_path, target_path in pairs:
        missing = [
            str(source_path.with_suffix(suffix))
            for suffix in SHAPEFILE_COMPONENT_SUFFIXES
            if not source_path.with_suffix(suffix).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "Cannot publish incomplete Shapefile bundle: " + ", ".join(missing)
            )
        _validate_staged_shapefile_bundle(source_path)

    with _exclusive_publish_lock([target for _, target in pairs]):
        backup_paths = {
            target_path: _temporary_shapefile_path(target_path, "backup")
            for _, target_path in pairs
        }
        backed_up: set[tuple[Path, str]] = set()
        published: list[tuple[Path, str]] = []
        removed_spatial_indexes: list[tuple[Path, Path]] = []
        rollback_incomplete = False
        try:
            for _, target_path in pairs:
                for suffix in SHAPEFILE_COMPONENT_SUFFIXES:
                    target_component = target_path.with_suffix(suffix)
                    if target_component.exists():
                        shutil.copy2(
                            target_component,
                            backup_paths[target_path].with_suffix(suffix),
                        )
                        backed_up.add((target_path, suffix))
                # Spatial indexes generated by external GIS refer to the old
                # geometry and must not survive a successful replacement.
                for index_component in _stale_spatial_index_components(target_path):
                    backup_component = backup_paths[target_path].with_name(
                        f"{backup_paths[target_path].stem}.{index_component.name}.indexbak"
                    )
                    shutil.copy2(index_component, backup_component)
                    removed_spatial_indexes.append((index_component, backup_component))
                    index_component.unlink()

            # The primary .shp file is deliberately last, after every index,
            # attribute and CRS sidecar has reached its target basename.
            for suffix in SHAPEFILE_COMPONENT_SUFFIXES:
                for source_path, target_path in pairs:
                    # Register the target before entering the replace syscall.
                    # A signal can be delivered after the OS completed the
                    # rename but before Python executes the next statement;
                    # conservative rollback is safe even when replace failed
                    # before changing the target.
                    published.append((target_path, suffix))
                    os.replace(
                        source_path.with_suffix(suffix),
                        target_path.with_suffix(suffix),
                    )
        except BaseException as exc:
            # Preserve all recovery material unless every rollback operation
            # below completes. This remains true even if rollback itself is
            # interrupted by another BaseException.
            rollback_incomplete = True
            rollback_errors: list[str] = []
            for target_path, suffix in reversed(published):
                target_component = target_path.with_suffix(suffix)
                try:
                    if (target_path, suffix) in backed_up:
                        os.replace(
                            backup_paths[target_path].with_suffix(suffix),
                            target_component,
                        )
                        backed_up.remove((target_path, suffix))
                    else:
                        target_component.unlink(missing_ok=True)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{target_component}: {rollback_exc}")
            for target_component, backup_component in reversed(removed_spatial_indexes):
                try:
                    os.replace(backup_component, target_component)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{target_component}: {rollback_exc}")
            if rollback_errors:
                exc.add_note(
                    "Shapefile rollback also failed for: " + "; ".join(rollback_errors)
                )
                exc.add_note(
                    "Recovery components were preserved under staging/backup basenames: "
                    + ", ".join(
                        str(path)
                        for source_path, target_path in pairs
                        for path in (source_path, backup_paths[target_path])
                    )
                    + ", spatial index backups: "
                    + ", ".join(str(path) for _, path in removed_spatial_indexes)
                )
            else:
                rollback_incomplete = False
            raise
        finally:
            if not rollback_incomplete:
                for source_path, target_path in pairs:
                    _remove_shapefile_bundle(source_path)
                    _remove_shapefile_bundle(backup_paths[target_path])
                for _, backup_component in removed_spatial_indexes:
                    try:
                        backup_component.unlink(missing_ok=True)
                    except OSError:
                        pass


def _publish_shapefile_bundle(source_path: Path, target_path: Path) -> None:
    publish_shapefile_bundles([(source_path, target_path)])


def _atomic_shapefile_writer(
    writer_function: Callable[..., None],
) -> Callable[..., None]:
    """Write to a hidden basename and publish only after every sidecar succeeds."""

    @wraps(writer_function)
    def wrapped(
        records: list[dict[str, Any]],
        shp_path: Path,
        *,
        crs_wkt: str | None = None,
    ) -> None:
        final_shp_path = Path(shp_path)
        final_shp_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = _temporary_shapefile_path(final_shp_path, "writing")
        preserve_for_recovery = False
        try:
            writer_function(records, temporary_path, crs_wkt=crs_wkt)
            _publish_shapefile_bundle(temporary_path, final_shp_path)
        except BaseException as exc:
            preserve_for_recovery = any(
                "Recovery components were preserved" in note
                for note in getattr(exc, "__notes__", [])
            )
            raise
        finally:
            if not preserve_for_recovery:
                _remove_shapefile_bundle(temporary_path)

    return wrapped


def write_crs_sidecars(shp_path: Path, crs_wkt: str | None) -> None:
    """Write a GIS-compatible horizontal .prj and retain the authoritative WKT2.

    The Shapefile format has no CRS field inside ``.shp``.  Desktop GIS reads
    the sibling ``.prj`` and many ArcGIS/XDROAD-era readers only understand
    ESRI WKT1 there.  A compound WKT2 copied verbatim is therefore often shown
    as "unknown".  The full horizontal+vertical definition is retained in
    ``.wkt2``/``.qpj`` while ``.prj`` receives the projected horizontal CRS.
    """

    supplied = (crs_wkt or EPSG_5179_ESRI_WKT).replace("\x00", "").strip()
    try:
        full_crs = CRS.from_wkt(supplied)
        horizontal = full_crs
        if full_crs.is_compound:
            projected = [item for item in full_crs.sub_crs_list if item.is_projected]
            if projected:
                horizontal = projected[0]
        epsg = horizontal.to_epsg(min_confidence=70)
        if epsg is not None:
            epsg_crs = CRS.from_epsg(epsg)
            if horizontal.equals(epsg_crs, ignore_axis_order=True):
                horizontal = epsg_crs
        esri_wkt = horizontal.to_wkt(version="WKT1_ESRI")
        full_wkt2 = full_crs.to_wkt(version="WKT2_2019")
    except CRSError:
        # Preserve legacy/custom local WKT rather than silently inventing a CRS.
        esri_wkt = supplied
        full_wkt2 = supplied

    shp_path.with_suffix(".prj").write_text(esri_wkt, encoding="utf-8")
    shp_path.with_suffix(".wkt2").write_text(full_wkt2, encoding="utf-8")
    shp_path.with_suffix(".qpj").write_text(full_wkt2, encoding="utf-8")
    shp_path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


def collect_detection_records(
    txt_root: Path,
    logger=None,
    *,
    run_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for txt_path in sorted(txt_root.rglob("*.txt")):
        try:
            payload = json.loads(txt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            if logger is not None:
                logger.warning("Skipping unreadable detection txt %s: %s", txt_path, exc)
            continue
        if run_fingerprint is not None and payload.get("run_fingerprint") != run_fingerprint:
            if logger is not None:
                logger.info("Ignoring result from a different run configuration: %s", txt_path)
            continue
        for detection in payload.get("detections", []):
            if detection.get("x") is None or detection.get("accepted_for_shp") is False:
                continue
            record = dict(detection)
            record["record_name"] = payload.get("record_name")
            record["detection_id"] = make_detection_id(
                payload.get("record_name"),
                detection.get("image_name") or payload.get("image_name"),
                detection.get("detection_index"),
            )
            record["support_id"] = None
            record["run_fingerprint"] = payload.get("run_fingerprint")
            record["model_name"] = payload.get("model_name")
            record["model_key"] = payload.get("model_key")
            record["model_profile"] = payload.get("model_profile")
            record["model_object_type"] = payload.get("model_object_type")
            record["pose_format"] = payload.get("pose_format")
            record["gps_week"] = payload.get("gps_week")
            record["pointcloud_source"] = payload.get("pointcloud_source")
            calibration = payload.get("calibration") or {}
            record["calibration_sha256"] = calibration.get("calibration_sha256")
            records.append(record)
    return records


def collect_pole_records(
    txt_root: Path,
    logger=None,
    *,
    run_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    """Collect per-frame pole observations from the normal result JSON files."""

    records: list[dict[str, Any]] = []
    for txt_path in sorted(txt_root.rglob("*.txt")):
        try:
            payload = json.loads(txt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            if logger is not None:
                logger.warning("Skipping unreadable detection txt %s: %s", txt_path, exc)
            continue
        if run_fingerprint is not None and payload.get("run_fingerprint") != run_fingerprint:
            continue
        for detection in payload.get("detections", []):
            if not isinstance(detection, dict):
                continue
            if detection.get("accepted_for_shp") is False or detection.get("x") is None:
                continue
            pole = detection.get("pole") or {}
            if not pole.get("found") or pole.get("x") is None:
                continue
            record = {
                "record_name": payload.get("record_name"),
                "detection_index": detection.get("detection_index"),
                "detection_id": make_detection_id(
                    payload.get("record_name"),
                    detection.get("image_name") or payload.get("image_name"),
                    detection.get("detection_index"),
                ),
                "class_id": detection.get("class_id"),
                "class_name": detection.get("class_name"),
                "confidence": detection.get("confidence"),
                "image_name": detection.get("image_name") or payload.get("image_name"),
                "timestamp_iso": detection.get("timestamp_iso") or payload.get("timestamp_iso"),
                "run_fingerprint": payload.get("run_fingerprint"),
                "model_name": payload.get("model_name"),
                "model_key": payload.get("model_key"),
                "model_profile": payload.get("model_profile"),
                "model_object_type": payload.get("model_object_type"),
                "pose_format": payload.get("pose_format"),
                "gps_week": payload.get("gps_week"),
                "pointcloud_source": payload.get("pointcloud_source"),
                "sign_x": detection.get("x"),
                "sign_y": detection.get("y"),
                "sign_z": detection.get("z"),
                "pole_x": pole.get("x"),
                "pole_y": pole.get("y"),
                "pole_z": pole.get("z"),
                "pole_type": pole.get("type"),
                "pole_method": pole.get("method"),
                "pole_status": pole.get("status"),
                "pole_occluded": pole.get("occluded_bottom"),
                "pole_occlusion_status": pole.get("occlusion_status")
                or (
                    "OCCLUDED"
                    if pole.get("occluded_bottom") is True
                    else "VISIBLE"
                    if pole.get("occluded_bottom") is False
                    else "UNKNOWN"
                ),
                "pole_count": pole.get("pole_count"),
                "pole_point_count": pole.get("point_count"),
                "pole_quality": pole.get("quality"),
                "axis_rmse_m": pole.get("axis_rmse_m"),
                "ground_rmse_m": pole.get("ground_rmse_m"),
                "axis_stabilized": pole.get("axis_stabilized"),
                "bottom_gap_m": pole.get("bottom_gap_m"),
                "ground_support_distance_m": pole.get(
                    "ground_support_distance_m"
                ),
                "association_distance_m": pole.get("association_distance_m"),
                "horizontal_connection_coverage_ratio": pole.get(
                    "horizontal_connection_coverage_ratio"
                ),
                "completeness_ratio": pole.get("completeness_ratio"),
                "dominant_class_id": pole.get("dominant_class_id"),
                "dominant_class_fraction": pole.get("dominant_class_fraction"),
                "classification_mode_requested": pole.get(
                    "classification_mode_requested"
                ),
                "classification_mode": pole.get("classification_mode"),
                "classification_reason": pole.get("classification_reason"),
                "pole_search_mode": pole.get("corridor_mode"),
                "pole_fallback_attempted": pole.get("pole_fallback_attempted"),
                "pole_fallback_used": pole.get("pole_fallback_used"),
                "pole_point_crop_path": pole.get("point_crop_path"),
                "pole_debug_image_path": pole.get("debug_image_path"),
            }
            records.append(record)
    return records


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _detection_identity(record: dict[str, Any]) -> str:
    detection_id = str(record.get("detection_id") or "").strip()
    if detection_id:
        return detection_id
    try:
        detection_index = int(record.get("detection_index") or 0)
    except (TypeError, ValueError, OverflowError):
        detection_index = 0
    return make_detection_id(
        record.get("record_name"),
        record.get("image_name"),
        detection_index,
    )


def _detection_sort_key(record: dict[str, Any], detection_id: str) -> tuple[Any, ...]:
    try:
        detection_index = int(record.get("detection_index") or 0)
    except (TypeError, ValueError, OverflowError):
        detection_index = 0
    return (
        str(record.get("record_name") or ""),
        str(record.get("image_name") or "").casefold(),
        detection_index,
        detection_id,
    )


def _normalised_class_id(value: Any) -> tuple[str, Any]:
    try:
        if value is not None and not isinstance(value, bool):
            return ("number", int(value))
    except (TypeError, ValueError, OverflowError):
        pass
    return ("text", str(value or ""))


def _complete_link_score(
    left: list[int],
    right: list[int],
    *,
    records: list[dict[str, Any]],
    xyz_by_index: dict[int, tuple[float, float, float] | None],
    supported_xy_radius_m: float,
    supported_z_radius_m: float,
    unsupported_xy_radius_m: float,
    unsupported_z_radius_m: float,
) -> float | None:
    """Return the complete-link distance when two clusters may be joined."""

    left_images = {
        str(records[index].get("image_name") or "").casefold()
        for index in left
        if str(records[index].get("image_name") or "").strip()
    }
    right_images = {
        str(records[index].get("image_name") or "").casefold()
        for index in right
        if str(records[index].get("image_name") or "").strip()
    }
    # Two boxes from one source image can be two real signs.  Never collapse
    # them merely because their reconstructed coordinates are close.
    if left_images.intersection(right_images):
        return None

    maximum_distance = 0.0
    for left_index in left:
        left_xyz = xyz_by_index[left_index]
        if left_xyz is None:
            return None
        for right_index in right:
            right_xyz = xyz_by_index[right_index]
            if right_xyz is None:
                return None
            left_support_id = str(records[left_index].get("support_id") or "").strip()
            right_support_id = str(records[right_index].get("support_id") or "").strip()
            if left_support_id and right_support_id:
                if left_support_id != right_support_id:
                    return None
                xy_radius_m = supported_xy_radius_m
                z_radius_m = supported_z_radius_m
            else:
                # A pole-less observation may join a supported observation,
                # but only with the tighter fallback tolerance.  This recovers
                # signs whose pole is occluded/cropped in one of the frames.
                xy_radius_m = unsupported_xy_radius_m
                z_radius_m = unsupported_z_radius_m
            xy_distance = math.hypot(
                left_xyz[0] - right_xyz[0],
                left_xyz[1] - right_xyz[1],
            )
            z_distance = abs(left_xyz[2] - right_xyz[2])
            if xy_distance > xy_radius_m + 1e-12 or z_distance > z_radius_m + 1e-12:
                return None
            maximum_distance = max(
                maximum_distance,
                xy_distance / xy_radius_m,
                z_distance / z_radius_m,
            )
    return maximum_distance


def _cluster_observations_complete_link(
    indices: list[int],
    *,
    records: list[dict[str, Any]],
    xyz_by_index: dict[int, tuple[float, float, float] | None],
    stable_keys: dict[int, tuple[Any, ...]],
    supported_xy_radius_m: float,
    supported_z_radius_m: float,
    unsupported_xy_radius_m: float,
    unsupported_z_radius_m: float,
) -> list[list[int]]:
    """Agglomerate deterministically without connected-component chaining."""

    clusters = [[index] for index in sorted(indices, key=stable_keys.__getitem__)]
    while True:
        best: tuple[float, tuple[tuple[Any, ...], ...], int, int] | None = None
        for left_position in range(len(clusters)):
            for right_position in range(left_position + 1, len(clusters)):
                left = clusters[left_position]
                right = clusters[right_position]
                score = _complete_link_score(
                    left,
                    right,
                    records=records,
                    xyz_by_index=xyz_by_index,
                    supported_xy_radius_m=supported_xy_radius_m,
                    supported_z_radius_m=supported_z_radius_m,
                    unsupported_xy_radius_m=unsupported_xy_radius_m,
                    unsupported_z_radius_m=unsupported_z_radius_m,
                )
                if score is None:
                    continue
                merged_keys = tuple(
                    sorted(
                        (stable_keys[index] for index in (*left, *right)),
                    )
                )
                candidate = (score, merged_keys, left_position, right_position)
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            break
        _, _, left_position, right_position = best
        merged = sorted(
            (*clusters[left_position], *clusters[right_position]),
            key=stable_keys.__getitem__,
        )
        clusters = [
            cluster
            for position, cluster in enumerate(clusters)
            if position not in {left_position, right_position}
        ]
        clusters.append(merged)
        clusters.sort(key=lambda cluster: tuple(stable_keys[index] for index in cluster))
    return clusters


def _representative_index(
    members: list[int],
    *,
    records: list[dict[str, Any]],
    xyz_by_index: dict[int, tuple[float, float, float] | None],
    stable_keys: dict[int, tuple[Any, ...]],
    preferred_detection_ids: set[str],
    identities: dict[int, str],
    supported_xy_radius_m: float,
    supported_z_radius_m: float,
    unsupported_xy_radius_m: float,
    unsupported_z_radius_m: float,
) -> int:
    def quality(value: Any) -> float:
        number = _finite_float(value)
        return number if number is not None else float("-inf")

    def medoid_distance(index: int) -> float:
        xyz = xyz_by_index[index]
        if xyz is None:
            return float("inf")
        total = 0.0
        for other_index in members:
            other_xyz = xyz_by_index[other_index]
            if other_xyz is None:
                return float("inf")
            support_id = str(records[index].get("support_id") or "").strip()
            other_support_id = str(records[other_index].get("support_id") or "").strip()
            if support_id and other_support_id:
                xy_radius_m = supported_xy_radius_m
                z_radius_m = supported_z_radius_m
            else:
                xy_radius_m = unsupported_xy_radius_m
                z_radius_m = unsupported_z_radius_m
            total += max(
                math.hypot(xyz[0] - other_xyz[0], xyz[1] - other_xyz[1])
                / xy_radius_m,
                abs(xyz[2] - other_xyz[2]) / z_radius_m,
            )
        return total

    # A usable pole relation is more valuable than a visually stronger frame
    # whose pole was cropped or occluded.  Spatial medoid, point support, and
    # detector confidence then choose the most representative retained row.
    return min(
        members,
        key=lambda index: (
            identities[index] not in preferred_detection_ids,
            medoid_distance(index),
            -quality(records[index].get("point_count")),
            -quality(records[index].get("confidence")),
            stable_keys[index],
        ),
    )


def deduplicate_sign_and_pole_observations(
    detection_records: list[dict[str, Any]],
    pole_relations: list[dict[str, Any]],
    *,
    supported_xy_radius_m: float = 0.25,
    supported_z_radius_m: float = 0.25,
    unsupported_xy_radius_m: float = 0.15,
    unsupported_z_radius_m: float = 0.20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse repeat sign observations and keep pole relations joinable.

    Signs are compared only within the same record and class.  Two populated
    ``support_id`` values must match; a sign without a support may be absorbed
    by a supported sign only under the tighter fallback thresholds.  Clusters
    use complete-link bounds, so a chain of pairwise-near observations cannot
    bridge two separate signs.  Records from the same source image are always
    kept separate.

    The returned objects are copies; the two input lists and their dictionaries
    are not mutated.  Every pole relation belonging to a collapsed sign is
    rewritten to the retained detection ID and reduced to one relation row.
    """

    radii = (
        supported_xy_radius_m,
        supported_z_radius_m,
        unsupported_xy_radius_m,
        unsupported_z_radius_m,
    )
    if any(_finite_float(radius) is None or float(radius) <= 0.0 for radius in radii):
        raise ValueError("Sign deduplication radii must be finite and positive")

    records = [dict(item) for item in detection_records]
    identities = {index: _detection_identity(item) for index, item in enumerate(records)}
    stable_keys = {
        index: _detection_sort_key(item, identities[index])
        for index, item in enumerate(records)
    }
    xyz_by_index: dict[int, tuple[float, float, float] | None] = {}
    for index, item in enumerate(records):
        xyz = tuple(_finite_float(item.get(axis)) for axis in ("x", "y", "z"))
        xyz_by_index[index] = (
            (xyz[0], xyz[1], xyz[2])
            if all(value is not None for value in xyz)
            else None
        )

    buckets: dict[tuple[Any, ...], list[int]] = {}
    for index, item in enumerate(records):
        bucket_key = (
            str(item.get("record_name") or ""),
            _normalised_class_id(item.get("class_id")),
        )
        buckets.setdefault(bucket_key, []).append(index)

    clusters: list[list[int]] = []
    for bucket_key in sorted(buckets, key=repr):
        bucket_clusters = _cluster_observations_complete_link(
            buckets[bucket_key],
            records=records,
            xyz_by_index=xyz_by_index,
            stable_keys=stable_keys,
            supported_xy_radius_m=float(supported_xy_radius_m),
            supported_z_radius_m=float(supported_z_radius_m),
            unsupported_xy_radius_m=float(unsupported_xy_radius_m),
            unsupported_z_radius_m=float(unsupported_z_radius_m),
        )
        clusters.extend(bucket_clusters)

    clusters.sort(key=lambda cluster: tuple(stable_keys[index] for index in cluster))
    valid_pole_detection_ids = {
        str(item.get("detection_id") or "").strip()
        for item in pole_relations
        if str(item.get("detection_id") or "").strip()
        and all(
            _finite_float(item.get(axis)) is not None
            for axis in ("pole_x", "pole_y", "pole_z")
        )
    }
    preferred_detection_ids = valid_pole_detection_ids.union(
        identities[index]
        for index, item in enumerate(records)
        if str(item.get("support_id") or "").strip()
    )
    canonical_by_detection_id: dict[str, str] = {}
    source_ids_by_canonical: dict[str, list[str]] = {}
    deduplicated_records: list[dict[str, Any]] = []
    for members in clusters:
        representative_index = _representative_index(
            members,
            records=records,
            xyz_by_index=xyz_by_index,
            stable_keys=stable_keys,
            preferred_detection_ids=preferred_detection_ids,
            identities=identities,
            supported_xy_radius_m=float(supported_xy_radius_m),
            supported_z_radius_m=float(supported_z_radius_m),
            unsupported_xy_radius_m=float(unsupported_xy_radius_m),
            unsupported_z_radius_m=float(unsupported_z_radius_m),
        )
        canonical_id = identities[representative_index]
        source_ids = sorted({identities[index] for index in members})
        representative = dict(records[representative_index])
        representative["detection_id"] = canonical_id
        representative["observation_count"] = len(members)
        representative["source_detection_ids"] = source_ids
        deduplicated_records.append(representative)
        source_ids_by_canonical[canonical_id] = source_ids
        for index in members:
            canonical_by_detection_id[identities[index]] = canonical_id

    deduplicated_records.sort(
        key=lambda item: _detection_sort_key(item, str(item.get("detection_id") or ""))
    )

    relation_groups: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    anonymous_relations: list[dict[str, Any]] = []
    for relation_index, item in enumerate(pole_relations):
        relation = dict(item)
        original_id = str(relation.get("detection_id") or "").strip()
        if not original_id:
            anonymous_relations.append(relation)
            continue
        canonical_id = canonical_by_detection_id.get(original_id, original_id)
        relation_groups.setdefault(canonical_id, []).append(
            (relation_index, original_id, relation)
        )

    def relation_quality(value: Any) -> float:
        number = _finite_float(value)
        return number if number is not None else float("-inf")

    deduplicated_relations: list[dict[str, Any]] = []
    for canonical_id in sorted(relation_groups):
        candidates = relation_groups[canonical_id]
        _, _, selected = min(
            candidates,
            key=lambda candidate: (
                candidate[1] != canonical_id,
                -relation_quality(candidate[2].get("pole_point_count")),
                -relation_quality(candidate[2].get("pole_quality")),
                -relation_quality(candidate[2].get("confidence")),
                str(candidate[2].get("image_name") or "").casefold(),
                candidate[0],
            ),
        )
        selected = dict(selected)
        selected["detection_id"] = canonical_id
        source_ids = source_ids_by_canonical.get(canonical_id, [canonical_id])
        selected["sign_observation_count"] = len(source_ids)
        selected["source_detection_ids"] = list(source_ids)
        deduplicated_relations.append(selected)

    # A malformed relation without a join key cannot safely be associated with
    # another sign.  Preserve it as-is instead of silently losing user data.
    deduplicated_relations.extend(anonymous_relations)
    deduplicated_relations.sort(
        key=lambda item: (
            str(item.get("record_name") or ""),
            str(item.get("support_id") or ""),
            str(item.get("detection_id") or ""),
            str(item.get("image_name") or "").casefold(),
        )
    )
    return deduplicated_records, deduplicated_relations


def _write_pointz_table(
    shp_path: Path,
    fields: tuple[tuple[str, str, int, int], ...],
    rows: Iterable[tuple[float, float, float, tuple[Any, ...]]],
) -> None:
    """Write one pyshp table and guarantee handle closure on every exception."""

    def close_writer() -> None:
        try:
            writer.close()
        except BaseException as close_exc:
            emergency_errors: list[str] = []
            # pyshp exposes its three owned binary streams. If its high-level
            # close fails, close the raw handles so Windows does not retain a
            # lock that prevents temporary-file cleanup.
            for attribute in ("shp", "shx", "dbf"):
                handle = getattr(writer, attribute, None)
                if handle is None or getattr(handle, "closed", False):
                    continue
                try:
                    handle.close()
                except BaseException as emergency_exc:
                    emergency_errors.append(f"{attribute}: {emergency_exc}")
            if emergency_errors:
                close_exc.add_note(
                    "Emergency raw Shapefile handle close also failed: "
                    + "; ".join(emergency_errors)
                )
            raise

    # Open all three streams under our own ExitStack before constructing
    # pyshp.Writer. This also closes shp/shx if the Writer constructor itself
    # fails while initializing the third (dbf) stream.
    with ExitStack() as stack:
        shp_stream = stack.enter_context(shp_path.with_suffix(".shp").open("w+b"))
        shx_stream = stack.enter_context(shp_path.with_suffix(".shx").open("w+b"))
        dbf_stream = stack.enter_context(shp_path.with_suffix(".dbf").open("w+b"))
        writer = shapefile.Writer(
            shp=shp_stream,
            shx=shx_stream,
            dbf=dbf_stream,
            shapeType=shapefile.POINTZ,
            encoding="utf-8",
        )
        writer.autoBalance = 1
        try:
            for name, field_type, size, decimal in fields:
                writer.field(name, field_type, size=size, decimal=decimal)
            for x, y, z, attributes in rows:
                writer.pointz(x, y, z)
                writer.record(*attributes)
        except BaseException as exc:
            try:
                close_writer()
            except BaseException as close_exc:
                exc.add_note(f"Closing failed Shapefile writer also failed: {close_exc}")
            raise
        close_writer()


@_atomic_shapefile_writer
def write_shapefile(
    records: list[dict[str, Any]],
    shp_path: Path,
    *,
    crs_wkt: str | None = None,
) -> None:
    shp_path.parent.mkdir(parents=True, exist_ok=True)
    def prepared_rows() -> Iterable[tuple[float, float, float, tuple[Any, ...]]]:
        for item in records:
            x = float(item["x"])
            y = float(item["y"])
            z = float(item["z"])
            yield (
                x,
                y,
                z,
                (
                    int(item["class_id"]),
                    str(item["class_name"])[:40],
                    str(item.get("model_name") or "")[:80],
                    str(item.get("model_object_type") or "")[:16],
                    float(item["confidence"]),
                    x,
                    y,
                    z,
                    str(item.get("detection_id") or "")[:20],
                    str(item.get("support_id") or "")[:20],
                    str(item["image_name"])[:80],
                    str(item["timestamp_iso"])[:26],
                    int(item["point_count"]),
                    Path(item["point_crop_path"]).name[:100]
                    if item.get("point_crop_path")
                    else "",
                    str(item.get("pose_format") or "")[:16],
                    int(item["gps_week"])
                    if item.get("gps_week") is not None
                    else None,
                    str(item.get("pointcloud_source") or "")[:8],
                    str(item.get("calibration_sha256") or "")[:12],
                    str(item.get("run_fingerprint") or "")[:12],
                ),
            )
    fields = (
        ("class_id", "N", 50, 0),
        ("class_nm", "C", 40, 0),
        ("model_nm", "C", 80, 0),
        ("obj_type", "C", 16, 0),
        ("conf", "F", 10, 4),
        ("x", "F", 18, 4),
        ("y", "F", 18, 4),
        ("z", "F", 18, 4),
        ("det_id", "C", 20, 0),
        ("support_id", "C", 20, 0),
        ("img_name", "C", 80, 0),
        ("img_time", "C", 26, 0),
        ("pt_count", "N", 50, 0),
        ("las_file", "C", 100, 0),
        ("pose_fmt", "C", 16, 0),
        ("gps_week", "N", 8, 0),
        ("pc_src", "C", 8, 0),
        ("calib_id", "C", 12, 0),
        ("run_id", "C", 12, 0),
    )
    _write_pointz_table(shp_path, fields, prepared_rows())
    write_crs_sidecars(shp_path, crs_wkt)


@_atomic_shapefile_writer
def write_pole_shapefile(
    records: list[dict[str, Any]],
    shp_path: Path,
    *,
    crs_wkt: str | None = None,
) -> None:
    """Write aggregated pole axis-ground intersections as a separate PointZ SHP."""

    shp_path.parent.mkdir(parents=True, exist_ok=True)
    def prepared_rows() -> Iterable[tuple[float, float, float, tuple[Any, ...]]]:
        for item in records:
            x = float(item["pole_x"])
            y = float(item["pole_y"])
            z = float(item["pole_z"])
            occluded_value = item.get("pole_occluded")
            if not isinstance(occluded_value, bool):
                occluded_value = None
            occlusion_status = str(
                item.get("pole_occlusion_status")
                or (
                    "OCCLUDED"
                    if occluded_value is True
                    else "VISIBLE"
                    if occluded_value is False
                    else "UNKNOWN"
                )
            )
            observation_count = int(item.get("obs_count") or 1)
            detection_count = int(item.get("detection_count") or observation_count)
            occluded_count_value = item.get("occluded_count")
            occluded_count = (
                int(occluded_count_value)
                if occluded_count_value is not None
                else int(occluded_value is True)
            )
            unknown_count_value = item.get("unknown_occlusion_count")
            unknown_occlusion_count = (
                int(unknown_count_value)
                if unknown_count_value is not None
                else int(occluded_value is None)
            )
            yield (
                x,
                y,
                z,
                (
                    int(item["class_id"])
                    if item.get("class_id") is not None
                    else -1,
                    str(item.get("class_name") or "")[:40],
                    str(item.get("model_name") or "")[:80],
                    str(item.get("model_object_type") or "")[:16],
                    float(item.get("confidence") or 0.0),
                    x,
                    y,
                    z,
                    str(item.get("detection_id") or "")[:20],
                    str(item.get("support_id") or "")[:20],
                    str(item.get("pole_type") or "")[:12],
                    str(item.get("pole_method") or "")[:16],
                    str(item.get("pole_status") or "")[:12],
                    occluded_value,
                    occlusion_status[:16],
                    int(item.get("pole_count") or 0),
                    observation_count,
                    detection_count,
                    occluded_count,
                    unknown_occlusion_count,
                    int(item.get("pole_point_count") or 0),
                    float(item["axis_rmse_m"])
                    if item.get("axis_rmse_m") is not None
                    else None,
                    float(item["ground_rmse_m"])
                    if item.get("ground_rmse_m") is not None
                    else None,
                    (
                        bool(item.get("axis_stabilized"))
                        if item.get("axis_stabilized") is not None
                        else None
                    ),
                    float(item["bottom_gap_m"])
                    if item.get("bottom_gap_m") is not None
                    else None,
                    float(item["ground_support_distance_m"])
                    if item.get("ground_support_distance_m") is not None
                    else None,
                    float(item["association_distance_m"])
                    if item.get("association_distance_m") is not None
                    else None,
                    float(item["horizontal_connection_coverage_ratio"])
                    if item.get("horizontal_connection_coverage_ratio") is not None
                    else None,
                    float(item["completeness_ratio"])
                    if item.get("completeness_ratio") is not None
                    else None,
                    int(item["dominant_class_id"])
                    if item.get("dominant_class_id") is not None
                    else None,
                    float(item["dominant_class_fraction"])
                    if item.get("dominant_class_fraction") is not None
                    else None,
                    str(item.get("classification_mode_requested") or "")[:8],
                    str(item.get("classification_mode") or "")[:10],
                    str(item.get("pole_search_mode") or "")[:40],
                    (
                        bool(item.get("pole_fallback_used"))
                        if item.get("pole_fallback_used") is not None
                        else None
                    ),
                    float(item["xy_spread_m"])
                    if item.get("xy_spread_m") is not None
                    else None,
                    float(item["z_spread_m"])
                    if item.get("z_spread_m") is not None
                    else None,
                    int(item.get("consensus_outlier_count") or 0),
                    str(item.get("image_name") or "")[:80],
                    str(item.get("timestamp_iso") or "")[:26],
                    Path(str(item["pole_point_crop_path"])).name[:100]
                    if item.get("pole_point_crop_path")
                    else "",
                    str(item.get("run_fingerprint") or "")[:12],
                ),
            )
    fields = (
        ("class_id", "N", 50, 0),
        ("class_nm", "C", 40, 0),
        ("model_nm", "C", 80, 0),
        ("obj_type", "C", 16, 0),
        ("conf", "F", 10, 4),
        ("x", "F", 18, 4),
        ("y", "F", 18, 4),
        ("z", "F", 18, 4),
        ("det_id", "C", 20, 0),
        ("support_id", "C", 20, 0),
        ("pole_type", "C", 12, 0),
        ("method", "C", 16, 0),
        ("status", "C", 12, 0),
        ("occluded", "L", 1, 0),
        ("occ_state", "C", 16, 0),
        ("pole_cnt", "N", 50, 0),
        ("obs_count", "N", 50, 0),
        ("det_count", "N", 50, 0),
        ("occl_cnt", "N", 50, 0),
        ("unk_occ", "N", 50, 0),
        ("pt_count", "N", 50, 0),
        ("axis_rmse", "F", 10, 4),
        ("grnd_rmse", "F", 10, 4),
        ("axis_stab", "L", 1, 0),
        ("btm_gap", "F", 10, 4),
        ("grnd_dist", "F", 10, 4),
        ("assoc_m", "F", 10, 4),
        ("arm_cov", "F", 10, 4),
        ("complete", "F", 10, 4),
        ("dom_class", "N", 5, 0),
        ("cls_purity", "F", 10, 4),
        ("class_req", "C", 8, 0),
        ("class_mode", "C", 10, 0),
        ("search_md", "C", 40, 0),
        ("fallback", "L", 1, 0),
        ("xy_spread", "F", 10, 4),
        ("z_spread", "F", 10, 4),
        ("outlier_n", "N", 10, 0),
        ("img_name", "C", 80, 0),
        ("img_time", "C", 26, 0),
        ("las_file", "C", 100, 0),
        ("run_id", "C", 12, 0),
    )
    _write_pointz_table(shp_path, fields, prepared_rows())
    write_crs_sidecars(shp_path, crs_wkt)
