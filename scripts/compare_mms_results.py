"""Compare reviewed MMS result bundles without importing the numerical pipeline.

See docs/refactor/GOLDEN_DATASET.md for the deliberately explicit input contract.
PASS for synthetic data is a comparator test, never real MMS validation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import shapefile
from pyproj import CRS
from pyproj.exceptions import CRSError


COMPONENTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".wkt2")
FINGERPRINTS = ("input", "model", "calibration", "config")
EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED_VALIDATION": 2}


class BlockedValidation(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    value = json.loads(path.read_text(encoding="utf-8-sig"),
                       object_pairs_hook=_pairs, parse_constant=reject_constant)
    _require(isinstance(value, dict), "JSON document must be an object")
    _finite_tree(value)
    return value


def _finite_tree(value: Any) -> None:
    if isinstance(value, float):
        _require(math.isfinite(value), "non-finite number")
    elif isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)


def _path(root: Path, value: str) -> Path:
    _require(isinstance(value, str) and bool(value), "output path must be non-empty")
    relative = Path(value)
    _require(not relative.is_absolute() and ".." not in relative.parts,
             "output path must stay inside the bundle")
    result = root / relative
    _require(result.resolve().is_relative_to(root.resolve()), "output path escapes bundle")
    current = root
    for part in relative.parts:
        current = current / part
        _require(not current.is_symlink() and not getattr(current, "is_junction", lambda: False)(),
                 "linked bundle components are unsupported")
    return result


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "<" not in value


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _identity(record: str, image: str, index: int) -> str:
    # Existing durable identity contract (shp_writer.make_detection_id).
    return "D" + hashlib.sha256(f"{record}|{image}|{index}".encode()).hexdigest()[:19]


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _matches(pattern: str, path: str) -> bool:
    left, right = pattern.split("/"), path.split("/")
    return len(left) == len(right) and all(a == "*" or a == b for a, b in zip(left, right))


def _profile(path: Path) -> dict[str, Any]:
    profile = _json(path)
    for key in ("id", "reviewed_by", "rationale"):
        if not _text(profile.get(key)):
            raise BlockedValidation(f"profile.{key} needs review evidence")
    for key in ("xy_tolerance", "z_tolerance"):
        if not _number(profile.get(key)) or profile[key] < 0:
            raise BlockedValidation(f"profile.{key} must be explicitly reviewed (zero allowed)")
    _require(isinstance(profile.get("exclude"), list), "profile.exclude must be a list")
    _require(isinstance(profile.get("numeric_tolerances"), list),
             "profile.numeric_tolerances must be a list")
    paths: set[str] = set()
    for rule in profile["exclude"] + profile["numeric_tolerances"]:
        _require(isinstance(rule, dict) and _text(rule.get("reason")), "rule needs a reason")
        pattern = rule.get("path")
        _require(isinstance(pattern, str) and pattern.startswith("/")
                 and "**" not in pattern and pattern.split("/")[-1] != "*",
                 "rules must target named leaves; recursive/container exclusions are forbidden")
        _require(pattern not in paths, "duplicate comparison rule")
        paths.add(pattern)
        if rule in profile["numeric_tolerances"]:
            _require(_number(rule.get("absolute")) and rule["absolute"] >= 0,
                     "numeric tolerance must be finite and non-negative")
    return profile


def _crs(path: Path) -> CRS:
    return CRS.from_wkt(path.read_text(encoding="utf-8-sig"))


def _horizontal(crs: CRS) -> CRS:
    if crs.is_compound:
        return next(item for item in crs.sub_crs_list if item.is_projected or item.is_geographic)
    return crs


def _same_horizontal(first: CRS, second: CRS) -> bool:
    first, second = _horizontal(first), _horizontal(second)
    if first.equals(second, ignore_axis_order=True):
        return True
    # The existing writer intentionally serializes .prj as ESRI WKT1; PROJ
    # may not consider that round trip equal to WKT2 even for EPSG:5179.
    # Accept only a full-confidence authority match, never a fuzzy EPSG guess.
    authority = first.to_authority(min_confidence=100)
    return authority is not None and authority == second.to_authority(min_confidence=100)


def _load_bundle(root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    _require(not root.is_symlink() and not getattr(root, "is_junction", lambda: False)(), "bundle root must not be linked")
    manifest = _json(root / "comparison.json")
    _require(manifest.get("schema_version") == 1, "unsupported comparison schema")
    _require(_text(manifest.get("fixture_id")), "fixture_id is required")
    _require(manifest.get("source_kind") in ("synthetic", "real_mms"), "invalid source_kind")
    _require(manifest.get("comparison_profile") == profile["id"], "comparison profile mismatch")
    for key in FINGERPRINTS:
        _require(_sha(manifest.get(f"{key}_fingerprint")), f"invalid {key} fingerprint")
    _require(re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))) is not None,
             "source_commit must be a full commit SHA")
    contract = manifest.get("coordinate_contract")
    _require(isinstance(contract, dict), "coordinate_contract is required")
    for key in ("horizontal_crs", "vertical_reference", "units", "axis_order"):
        _require(_text(contract.get(key)), f"coordinate_contract.{key} is required")
    expected_crs = CRS.from_user_input(contract["horizontal_crs"])
    _require(not expected_crs.is_compound and (expected_crs.is_projected or expected_crs.is_geographic),
             "horizontal_crs must identify a horizontal coordinate system")
    _require(contract["axis_order"] == "x,y,z", "only existing x,y,z output order is supported")
    _require(all(axis.unit_name == contract["units"] for axis in expected_crs.axis_info[:2]),
             "coordinate units disagree with horizontal CRS")
    run = manifest.get("run")
    _require(isinstance(run, dict) and run.get("status") == "succeeded",
             "bundle must belong to a succeeded run")
    _require(_text(run.get("job_id")) and type(run.get("attempt")) is int and run["attempt"] > 0,
             "current job/attempt identity is required")
    _require(_sha(run.get("fingerprint")), "invalid run fingerprint")
    schema = manifest.get("frame_schema_version")
    _require(type(schema) is int and schema > 0, "frame_schema_version is required")
    frames_dir = _path(root, manifest["frames_directory"])
    if not frames_dir.is_dir():
        raise BlockedValidation("frame result directory is unavailable")
    frames: dict[str, Any] = {}
    observations: dict[str, Any] = {}
    files: set[str] = set()
    accepted: set[str] = set()
    for path in sorted(frames_dir.rglob("*.txt")):
        _path(root, path.relative_to(root).as_posix())
        payload = _json(path)
        _require(payload.get("schema_version") == schema, "frame JSON schema mismatch")
        _require(payload.get("run_fingerprint") == run["fingerprint"], "stale frame run fingerprint")
        _require(payload.get("model_sha256") == manifest["model_fingerprint"],
                 "frame model fingerprint mismatch")
        _require(isinstance(payload.get("calibration"), dict)
                 and payload["calibration"].get("calibration_sha256") == manifest["calibration_fingerprint"],
                 "frame calibration fingerprint mismatch")
        frame_crs = CRS.from_wkt(payload["crs_wkt"])
        _require(_same_horizontal(frame_crs, expected_crs),
                 "frame horizontal CRS mismatch")
        record, image = payload.get("record_name"), payload.get("image_name")
        _require(_text(record) and _text(image), "frame record/image identity is required")
        frame_key = f"{record}|{image}"
        _require(frame_key not in frames, f"ambiguous frame identity: {frame_key}")
        detections = payload.get("detections")
        _require(isinstance(detections, list), "frame detections must be a list")
        indexed: dict[str, Any] = {}
        for detection in detections:
            _require(isinstance(detection, dict), "invalid detection object")
            index = detection.get("detection_index")
            _require(type(index) is int and index > 0, "invalid detection index")
            _require(detection.get("image_name", image) == image, "detection frame identity mismatch")
            det_id = _identity(record, image, index)
            _require(det_id not in observations, f"ambiguous detection identity: {det_id}")
            _require(type(detection.get("class_id")) is int and _text(detection.get("class_name")),
                     "detection class contract missing")
            _require(_number(detection.get("confidence")), "invalid detection confidence")
            _require(detection.get("pole") is None or isinstance(detection["pole"], dict), "invalid pole object")
            xyz = [detection.get(axis) for axis in "xyz"]
            _require(all(value is None for value in xyz) or all(_number(value) for value in xyz),
                     "partial/invalid detection coordinates")
            if detection.get("accepted_for_shp") is not False and xyz[0] is not None:
                accepted.add(det_id)
            indexed[det_id] = detection
            observations[det_id] = detection
        frames[frame_key] = {**payload, "crs_wkt": frame_crs, "detections": indexed}
        files.add(path.relative_to(root).as_posix())
    _require(bool(frames), "no frame JSON results (empty detections in a frame are supported)")
    declared = manifest.get("layers")
    _require(isinstance(declared, list) and bool(declared), "layers are required")
    layers: dict[str, Any] = {}
    signs: dict[str, Any] = {}
    poles: dict[str, Any] = {}
    for layer in declared:
        name, kind = layer["name"], layer["kind"]
        _require(_text(name) and name not in layers, "ambiguous layer name")
        _require(kind in ("sign", "pole"), "unsupported layer kind")
        target = _path(root, layer["path"])
        _require(target.suffix == ".shp" and ".in_progress" not in target.name,
                 "only final SHP bundles are comparable")
        for suffix in COMPONENTS:
            component = _path(root, target.with_suffix(suffix).relative_to(root).as_posix())
            _require(component.stat().st_size > 0, "empty SHP component")
            files.add(component.relative_to(root).as_posix())
        encoding = target.with_suffix(".cpg").read_text(encoding="ascii").strip()
        _require(encoding.upper().replace("-", "") == "UTF8", "SHP encoding must be UTF-8")
        sidecars = {suffix: _crs(target.with_suffix(suffix)) for suffix in (".prj", ".qpj", ".wkt2")}
        _require(sidecars[".qpj"].equals(sidecars[".wkt2"]), "Q PJ/WKT2 CRS mismatch")
        for crs in sidecars.values():
            _require(_same_horizontal(crs, expected_crs), "SHP horizontal CRS mismatch")
        features: dict[str, Any] = {}
        with shapefile.Reader(str(target), encoding="utf-8") as reader:
            _require(reader.shapeType == shapefile.POINTZ, "SHP must be PointZ")
            fields = [list(field) for field in reader.fields[1:]]
            field_map = {field[0]: field for field in fields}
            _require(len(field_map) == len(fields), "duplicate DBF field name")
            _require({"det_id", "class_id", "class_nm", "support_id", "x", "y", "z", "run_id"} <= field_map.keys(),
                     "required DBF fields missing")
            shapes, records = list(reader.iterShapes()), list(reader.iterRecords())
            _require(len(shapes) == len(records) == reader.numRecords, "SHP geometry/record count mismatch")
            _require(target.with_suffix(".shx").stat().st_size == 100 + 8 * len(records), "SHX count mismatch")
            for shape, row in zip(shapes, records):
                _require(shape.shapeType == shapefile.POINTZ and len(shape.points) == 1 and len(shape.z) == 1,
                         "feature must contain exactly one XYZ point")
                xyz = [*shape.points[0], shape.z[0]]
                _require(all(_number(value) for value in xyz), "invalid PointZ coordinate")
                attrs = row.as_dict()
                _finite_tree(attrs)
                det_id = attrs["det_id"]
                _require(_text(det_id) and det_id not in features, f"ambiguous feature identity: {det_id}")
                for axis, value in zip("xyz", xyz):
                    field = field_map[axis]
                    _require(field[1] in ("F", "N") and attrs[axis] == float(f"{value:.{field[3]}f}"),
                             "PointZ/DBF coordinate mismatch at declared DBF precision")
                _require(attrs["run_id"] == run["fingerprint"][:12], "stale SHP run fingerprint")
                features[det_id] = {"geometry": dict(zip("xyz", xyz)), "attributes": attrs}
                combined = signs if kind == "sign" else poles
                _require(det_id not in combined, "ambiguous feature identity across layers of the same kind")
                combined[det_id] = features[det_id]
        layers[name] = {"kind": kind, "path": layer["path"], "fields": fields,
                        "encoding": encoding, "crs": sidecars, "features": features}
    groups = manifest.get("observation_groups")
    _require(isinstance(groups, dict) and set(groups) == set(signs), "observation groups must identify every exported sign")
    mapped: set[str] = set()
    for canonical, members in groups.items():
        _require(isinstance(members, list) and bool(members) and canonical in members,
                 "observation group must contain its canonical detection")
        _require(all(isinstance(member, str) for member in members) and len(set(members)) == len(members),
                 "ambiguous observation group members")
        for member in members:
            _require(member in accepted and member not in mapped, "unmatched/ambiguous accepted observation")
            mapped.add(member)
            _require(observations[member]["class_id"] == signs[canonical]["attributes"]["class_id"],
                     "merged observation class mismatch")
        source, exported = observations[canonical], signs[canonical]
        _require(exported["geometry"] == {axis: source[axis] for axis in "xyz"},
                 "canonical frame/SHP sign geometry mismatch")
        _require(exported["attributes"]["class_nm"] == source["class_name"][:40],
                 "frame/SHP class name mismatch")
    _require(mapped == accepted, "accepted observations missing from exported groups")
    supports: dict[str, Any] = {}
    for det_id, pole in poles.items():
        _require(det_id in signs, "pole relation references a missing sign")
        attrs, sign_attrs = pole["attributes"], signs[det_id]["attributes"]
        support = attrs["support_id"]
        _require(_text(support) and support == sign_attrs["support_id"]
                 and attrs["class_id"] == sign_attrs["class_id"], "sign/pole support relation mismatch")
        _require(support not in supports or supports[support] == pole["geometry"],
                 "shared support has inconsistent coordinates")
        supports[support] = pole["geometry"]
    _require(all(not sign["attributes"]["support_id"] or det_id in poles for det_id, sign in signs.items()),
             "supported sign is missing its pole relation")
    assets = manifest.get("assets")
    _require(isinstance(assets, list), "assets inventory must be explicit (empty allowed)")
    asset_data: dict[str, Any] = {}
    for asset in assets:
        _require(asset["path"] not in asset_data and _text(asset.get("semantics")), "invalid asset inventory")
        path = _path(root, asset["path"])
        _require(_sha(asset.get("sha256")) and _hash(path) == asset["sha256"], "asset hash mismatch")
        asset_data[asset["path"]] = asset
        files.add(asset["path"])
    distributions = {
        "classes": dict(Counter(str(item["class_id"]) for item in observations.values())),
        "accepted": dict(Counter(str(item.get("accepted_for_shp")) for item in observations.values())),
        "failure_review_reasons": dict(Counter(
            f"{scope}:{item[key]}" for detection in observations.values()
            for scope, item in (("sign", detection), ("pole", detection.get("pole") or {}))
            for key in ("reason", "status") if key in item)),
    }
    return {"manifest": manifest, "frames": frames, "layers": layers, "assets": asset_data,
            "distributions": distributions,
            "files": files, "counts": {"frames": len(frames), "observations": len(observations),
                                        "signs": len(signs), "poles": len(poles)}}


def _real_evidence(root: Path, bundle: dict[str, Any]) -> None:
    """Require review evidence bound to the exact compared output bytes.

    This verifies evidence consistency, not that an operator's real-data
    attestation is truthful. Source input/model bytes remain in private storage.
    """
    manifest = bundle["manifest"]
    evidence = manifest.get("real_evidence")
    if not isinstance(evidence, dict):
        raise BlockedValidation("reviewed real MMS execution evidence is unavailable")
    for key in ("reviewed_by", "input_inventory_reference", "execution_log_reference"):
        if not _text(evidence.get(key)):
            raise BlockedValidation(f"real_evidence.{key} is required")
    if not _sha(evidence.get("input_inventory_sha256")) or not _sha(evidence.get("execution_log_sha256")):
        raise BlockedValidation("private input inventory/execution log digests are required")
    expected = {key: manifest[key] for key in ("source_commit", *[f"{item}_fingerprint" for item in FINGERPRINTS])}
    expected["run"] = manifest["run"]
    _require(evidence.get("execution") == expected, "real execution evidence identity mismatch")
    if not _text(evidence.get("run_manifest")):
        raise BlockedValidation("real evidence must reference the actual run manifest")
    run_path = _path(root, evidence["run_manifest"])
    run_document = _json(run_path)
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from mms_shp_detection.infrastructure.manifest_writer import (
        validate_manifest_document, validate_published_outputs,
    )
    _require(not validate_manifest_document(run_document), "invalid real run manifest")
    _require(run_document["versions"].get("git_commit") == manifest["source_commit"],
             "real run manifest source commit mismatch")
    model_hashes = run_document["versions"].get("model_hashes")
    calibration_hash = run_document["versions"].get("calibration_hash")
    if not isinstance(model_hashes, dict) or len(model_hashes) != 1 or not isinstance(calibration_hash, str):
        raise BlockedValidation("real evidence requires a single-model/single-calibration run manifest")
    _require(list(model_hashes.values()) == [manifest["model_fingerprint"]],
             "real run manifest model fingerprint mismatch")
    _require(calibration_hash == manifest["calibration_fingerprint"],
             "real run manifest calibration fingerprint mismatch")
    _require(all(run_document.get(key) == manifest["run"][key] for key in ("job_id", "attempt", "status")),
             "real run manifest current-attempt identity mismatch")
    current_stages = [stage for stage in run_document["stages"]
                      if isinstance(stage, dict) and stage.get("attempt", 1) == manifest["run"]["attempt"]]
    _require(not run_document["errors"] and bool(current_stages)
             and all(stage.get("status") in ("succeeded", "skipped") for stage in current_stages)
             and any(stage.get("status") == "succeeded" for stage in current_stages)
             and not run_document["progress"].get("failed_stage"),
             "real run manifest contains failed/incomplete stages")
    _require(run_document.get("config", {}).get("effective_hash") == manifest["config_fingerprint"],
             "real run effective configuration fingerprint mismatch")
    _require(not validate_published_outputs(root, run_document["outputs"]), "invalid real published outputs")
    _require(set(run_document["outputs"]["shapefiles"]) == {layer["path"] for layer in manifest["layers"]},
             "compared SHP inventory differs from actual run publication")
    bundle["files"].add(evidence["run_manifest"])
    hashes = evidence.get("output_sha256")
    if not isinstance(hashes, dict) or set(hashes) != bundle["files"]:
        raise BlockedValidation("real execution evidence must cover every compared output component")
    for relative, digest in hashes.items():
        _require(_sha(digest) and _hash(_path(root, relative)) == digest, "real evidence output digest mismatch")


def compare(baseline: Path, candidate: Path, profile_path: Path, *, require_real: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "BLOCKED_VALIDATION", "real_data_verified": False,
                              "sources": {}, "counts": {}, "distributions": {}, "differences": [], "blockers": [],
                              "excluded_fields": [], "max_xy": {"error": 0.0, "case": None},
                              "max_z": {"error": 0.0, "case": None}}
    try:
        profile = _profile(profile_path)
        report["comparison_profile"] = profile["id"]
        report["excluded_fields"] = profile["exclude"]
        bundles = {}
        for label, root in (("baseline", baseline), ("candidate", candidate)):
            bundle = _load_bundle(root, profile)
            bundles[label] = bundle
            manifest = bundle["manifest"]
            report["sources"][label] = {key: manifest[key] for key in ("fixture_id", "source_kind", "source_commit")}
            report["counts"][label] = bundle["counts"]
            report["distributions"][label] = bundle["distributions"]
        old, new = bundles["baseline"], bundles["candidate"]
        for key in ("fixture_id", "source_kind", "coordinate_contract", "frame_schema_version",
                    "comparison_profile", "observation_groups", *[f"{item}_fingerprint" for item in FINGERPRINTS]):
            if old["manifest"].get(key) != new["manifest"].get(key):
                report["differences"].append({"kind": "manifest", "path": f"/manifest/{key}"})
        real = all(bundle["manifest"]["source_kind"] == "real_mms" for bundle in bundles.values())
        if real:
            for label, root in (("baseline", baseline), ("candidate", candidate)):
                try:
                    _real_evidence(root, bundles[label])
                except BlockedValidation as exc:
                    report["blockers"].append(f"{label}: {exc}")
        elif require_real:
            report["blockers"].append("require-real needs reviewed real MMS baseline and candidate")

        def difference(kind: str, path: str) -> None:
            report["differences"].append({"kind": kind, "path": path})

        def walk(a: Any, b: Any, path: str) -> None:
            if isinstance(a, (dict, list, CRS)):
                _require(not any(_matches(rule["path"], path) for rule in profile["exclude"] + profile["numeric_tolerances"]),
                         "comparison rules cannot exclude/tolerate entire containers or CRS")
            if isinstance(a, CRS) and isinstance(b, CRS):
                if not a.equals(b):
                    difference("crs", path)
                return
            if type(a) is not type(b) and not (_number(a) and _number(b)):
                difference("schema", path)
                return
            if isinstance(a, dict):
                for key in sorted(a.keys() - b.keys()):
                    difference("missing", f"{path}/{_pointer(key)}")
                for key in sorted(b.keys() - a.keys()):
                    difference("extra", f"{path}/{_pointer(key)}")
                coordinates = set("xyz") <= a.keys() and set("xyz") <= b.keys()
                if coordinates and all(_number(a[key]) and _number(b[key]) for key in "xyz"):
                    xy = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                    z = abs(a["z"] - b["z"])
                    for metric, delta, bound in (("xy", xy, profile["xy_tolerance"]), ("z", z, profile["z_tolerance"])):
                        if delta > report[f"max_{metric}"]["error"]:
                            report[f"max_{metric}"] = {"error": delta, "case": path}
                        if delta > bound:
                            difference(f"coordinate_{metric}", path)
                else:
                    coordinates = False
                for key in sorted(a.keys() & b.keys()):
                    if not coordinates or key not in ("x", "y", "z"):
                        walk(a[key], b[key], f"{path}/{_pointer(key)}")
            elif isinstance(a, list):
                if len(a) != len(b):
                    difference("count", path)
                for index, (left, right) in enumerate(zip(a, b)):
                    walk(left, right, f"{path}/{index}")
            else:
                if any(_matches(rule["path"], path) for rule in profile["exclude"]):
                    return
                rules = [rule for rule in profile["numeric_tolerances"] if _matches(rule["path"], path)]
                _require(len(rules) <= 1, "overlapping numeric tolerance rules")
                if rules:
                    _require(_number(a) and _number(b), "numeric tolerance targets a nonnumeric leaf")
                    if abs(a - b) > rules[0]["absolute"]:
                        difference("numeric", path)
                elif a != b:
                    difference("attribute", path)

        for key in ("frames", "layers", "assets"):
            walk(old[key], new[key], f"/{key}")
        report["real_data_verified"] = real and not report["blockers"] and not report["differences"]
    except BlockedValidation as exc:
        report["blockers"].append(str(exc))
    except (FileNotFoundError, PermissionError):
        report["blockers"].append("a required comparison file is unavailable or unreadable")
    except (ValueError, TypeError, KeyError, OSError, CRSError, shapefile.ShapefileException, StopIteration) as exc:
        # Never include OS exception text: it can contain private source paths.
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        report["differences"].append({"kind": "invalid_bundle", "detail": detail})
    report["status"] = "FAIL" if report["differences"] else "BLOCKED_VALIDATION" if report["blockers"] else "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--require-real", action="store_true")
    args = parser.parse_args(argv)
    report = compare(args.baseline, args.candidate, args.profile, require_real=args.require_real)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return EXIT_CODES[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
