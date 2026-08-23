from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

from mms_shp_detection.shp_writer import make_detection_id
from mms_shp_detection.webapp import create_app
from mms_shp_detection.webapp.overlays import _validate_point_geometry

NOW = "2026-08-03T00:00:00+00:00"


def _seed_dataset(app, dataset_id: str = "dataset-a") -> None:
    app.state.store.upsert_scanning_dataset(
        dataset_id=dataset_id,
        name="Dataset A",
        root_id="root-a",
        relative_path="",
        crs="EPSG:32652",
        now=NOW,
    )
    app.state.store.finish_dataset_scan(
        dataset_id,
        frames=[
            {
                "id": "frame-a",
                "ordinal": 0,
                "track_id": "track-a",
                "task": {
                    "image_name": "frame-a.jpg",
                    "origin": [300_000.0, 4_100_000.0, 10.0],
                    "direction": [1.0, 0.0, 0.0],
                    "up": [0.0, 0.0, 1.0],
                },
                "longitude": 126.75,
                "latitude": 37.03,
                "altitude": 10.0,
                "heading": 90.0,
            }
        ],
        tracks=[{"id": "track-a", "name": "Track A", "frame_count": 1}],
        bbox=[126.75, 37.03, 126.75, 37.03],
        warnings=[],
        now=NOW,
    )


def _write_bundle(
    directory: Path,
    stem: str = "poles",
    *,
    encoding: str = "utf-8",
    include_cpg: bool = True,
    names: tuple[str, str] = ("first", "second"),
    name_field_size: int = 40,
) -> list[Path]:
    primary = directory / f"{stem}.shp"
    writer = shapefile.Writer(str(primary), shapeType=shapefile.POINTZ, encoding=encoding)
    writer.field("NAME", "C", size=name_field_size)
    writer.field("VALUE", "N", size=10, decimal=0)
    writer.pointz(300_010.0, 4_100_000.0, 10.0)
    writer.record(names[0], 1)
    writer.pointz(300_030.0, 4_100_000.0, 12.0)
    writer.record(names[1], 2)
    writer.close()
    primary.with_suffix(".prj").write_text(
        CRS.from_epsg(32652).to_wkt(version="WKT1_ESRI"), encoding="utf-8"
    )
    if include_cpg:
        label = {"utf-8": "UTF-8", "cp949": "949", "euc-kr": "EUC-KR"}[encoding]
        primary.with_suffix(".cpg").write_text(label, encoding="ascii")
    return sorted(directory.glob(f"{stem}.*"))


def _write_2d_point_bundle(directory: Path, stem: str = "poles_2d") -> list[Path]:
    primary = directory / f"{stem}.shp"
    writer = shapefile.Writer(str(primary), shapeType=shapefile.POINT, encoding="utf-8")
    writer.field("NAME", "C", size=40)
    writer.field("VALUE", "N", size=10, decimal=0)
    writer.point(300_010.0, 4_100_000.0)
    writer.record("existing", 1)
    writer.close()
    primary.with_suffix(".prj").write_text(
        CRS.from_epsg(32652).to_wkt(version="WKT1_ESRI"), encoding="utf-8"
    )
    primary.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    return sorted(directory.glob(f"{stem}.*"))


def _write_id_bundle(directory: Path, stem: str = "assets") -> list[Path]:
    primary = directory / f"{stem}.shp"
    writer = shapefile.Writer(str(primary), shapeType=shapefile.POINTZ, encoding="utf-8")
    writer.field("id", "N", size=10, decimal=0)
    writer.field("NAME", "C", size=40)
    writer.pointz(300_010.0, 4_100_000.0, 10.0)
    writer.record(4, "first")
    writer.pointz(300_030.0, 4_100_000.0, 12.0)
    writer.record(9, "second")
    writer.close()
    primary.with_suffix(".prj").write_text(
        CRS.from_epsg(32652).to_wkt(version="WKT1_ESRI"), encoding="utf-8"
    )
    primary.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    return sorted(directory.glob(f"{stem}.*"))


def _write_detection_bundle(directory: Path, stem: str = "detected_signs") -> list[Path]:
    primary = directory / f"{stem}.shp"
    writer = shapefile.Writer(str(primary), shapeType=shapefile.POINTZ, encoding="utf-8")
    writer.field("det_id", "C", size=20)
    writer.field("img_name", "C", size=80)
    writer.field("bbox_l", "F", size=18, decimal=4)
    writer.field("bbox_t", "F", size=18, decimal=4)
    writer.field("bbox_r", "F", size=18, decimal=4)
    writer.field("bbox_b", "F", size=18, decimal=4)
    writer.field("pano_w", "N", size=10)
    writer.field("pano_h", "N", size=10)
    writer.pointz(300_010.0, 4_100_000.0, 10.0)
    writer.record(
        make_detection_id("record-a", "frame-a.jpg", 1),
        "frame-a.jpg",
        400,
        150,
        500,
        250,
        1000,
        500,
    )
    writer.close()
    primary.with_suffix(".prj").write_text(
        CRS.from_epsg(32652).to_wkt(version="WKT1_ESRI"), encoding="utf-8"
    )
    primary.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    return sorted(directory.glob(f"{stem}.*"))


def _multipart(paths: list[Path]):
    return [
        ("files", (path.name, path.read_bytes(), "application/octet-stream"))
        for path in paths
    ]


class WebAppOverlayTests(unittest.TestCase):
    def test_geometry_edit_cannot_convert_a_non_point_feature(self) -> None:
        with self.assertRaisesRegex(ValueError, "existing Point"):
            _validate_point_geometry(
                {"type": "Point", "coordinates": [1.0, 2.0]},
                old_geometry={
                    "type": "LineString",
                    "coordinates": [[0.0, 0.0], [1.0, 1.0]],
                },
            )

    def test_delete_attribute_field_updates_edit_store_export_and_audit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
            tempfile.TemporaryDirectory() as id_bundle_text,
        ):
            state = Path(state_text)
            source_bundle = _write_bundle(Path(bundle_text))
            source_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_bundle
            }
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=state,
                start_runner=False,
            )
            _seed_dataset(app)

            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(source_bundle),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]
                field_url = (
                    f"/api/datasets/dataset-a/overlays/{layer_id}/fields/NAME"
                )

                from mms_shp_detection.webapp import overlays as overlays_module

                with patch.object(
                    overlays_module,
                    "atomic_replace_bytes",
                    side_effect=OSError("simulated manifest failure"),
                ):
                    deleted = client.delete(
                        field_url, params={"expected_revision": 1}
                    )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                payload = deleted.json()
                self.assertEqual(payload["deleted_field"], "NAME")
                self.assertEqual(payload["revision"], 2)
                self.assertTrue(payload["source_preserved"])
                self.assertEqual(
                    [field["name"] for field in payload["fields"]], ["VALUE"]
                )
                recovered = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "dataset"},
                ).json()
                self.assertEqual(recovered["revision"], 2)
                self.assertEqual(
                    [field["name"] for field in recovered["fields"]], ["VALUE"]
                )
                self.assertEqual(
                    recovered["features"][0]["properties"], {"VALUE": 1}
                )

                features = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "dataset"},
                ).json()
                self.assertEqual(features["revision"], 2)
                self.assertEqual(features["features"][0]["properties"], {"VALUE": 1})
                stale = client.delete(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/fields/VALUE",
                    params={"expected_revision": 1},
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                final_field = client.delete(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/fields/VALUE",
                    params={"expected_revision": 2},
                )
                self.assertEqual(final_field.status_code, 422, final_field.text)

                exported = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/download"
                )
                self.assertEqual(exported.status_code, 200, exported.text)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    shp_name = next(
                        name for name in archive.namelist() if name.endswith(".shp")
                    )
                    stem = Path(shp_name).stem
                    reader = shapefile.Reader(
                        shp=io.BytesIO(archive.read(f"{stem}.shp")),
                        shx=io.BytesIO(archive.read(f"{stem}.shx")),
                        dbf=io.BytesIO(archive.read(f"{stem}.dbf")),
                        encoding="utf-8",
                    )
                    try:
                        self.assertEqual(
                            [field[0] for field in reader.fields[1:]], ["VALUE"]
                        )
                        self.assertEqual(list(reader.record(0)), [1])
                    finally:
                        reader.close()

                id_uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(_write_id_bundle(Path(id_bundle_text))),
                )
                self.assertEqual(id_uploaded.status_code, 201, id_uploaded.text)
                id_layer = id_uploaded.json()["layer"]
                protected = client.delete(
                    f"/api/datasets/dataset-a/overlays/{id_layer['id']}/fields/id",
                    params={"expected_revision": id_layer["revision"]},
                )
                self.assertEqual(protected.status_code, 422, protected.text)

            overlay_root = next((state / "overlays").iterdir())
            layer_dir = overlay_root / layer_id
            connection = sqlite3.connect(layer_dir / "features.sqlite3")
            try:
                audit = connection.execute(
                    "SELECT revision,action,feature_id,before_json,after_json "
                    "FROM audit ORDER BY revision"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                [(row[0], row[1], row[2]) for row in audit],
                [(2, "delete_field", "field:NAME")],
            )
            self.assertEqual(json.loads(audit[0][3])["field"]["name"], "NAME")
            self.assertEqual(
                [field["name"] for field in json.loads(audit[0][4])["fields"]],
                ["VALUE"],
            )
            self.assertEqual(
                source_hashes,
                {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in source_bundle
                },
            )

    def test_layer_name_and_color_update_persist_with_metadata_revision(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            bundle = _write_bundle(Path(bundle_text))
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            _seed_dataset(app)

            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(bundle),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer = uploaded.json()["layer"]
                self.assertIsNone(layer["color"])
                self.assertEqual(layer["metadata_revision"], 1)
                layer_url = f"/api/datasets/dataset-a/overlays/{layer['id']}"

                updated = client.patch(
                    layer_url,
                    json={
                        "name": "도로 안전시설",
                        "color": "#A1B2C3",
                        "expected_metadata_revision": 1,
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                updated_layer = updated.json()["layer"]
                self.assertEqual(updated_layer["name"], "도로 안전시설")
                self.assertEqual(updated_layer["color"], "#a1b2c3")
                self.assertEqual(updated_layer["metadata_revision"], 2)
                self.assertEqual(updated_layer["revision"], 1)

                stale = client.patch(
                    layer_url,
                    json={
                        "name": "뒤늦은 수정",
                        "expected_metadata_revision": 1,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                invalid_name = client.patch(
                    layer_url,
                    json={
                        "name": "폴더/이름",
                        "expected_metadata_revision": 2,
                    },
                )
                self.assertEqual(invalid_name.status_code, 422, invalid_name.text)
                invalid_color = client.patch(
                    layer_url,
                    json={
                        "color": "red",
                        "expected_metadata_revision": 2,
                    },
                )
                self.assertEqual(invalid_color.status_code, 422, invalid_color.text)

            reopened = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with TestClient(reopened) as client:
                listed = client.get("/api/datasets/dataset-a/overlays")
                self.assertEqual(listed.status_code, 200, listed.text)
                persisted = listed.json()["items"][0]
                self.assertEqual(persisted["name"], "도로 안전시설")
                self.assertEqual(persisted["color"], "#a1b2c3")
                self.assertEqual(persisted["metadata_revision"], 2)

    def test_layer_metadata_revision_is_serialized_across_app_workers(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            root = Path(root_text)
            state = Path(state_text)
            bundle = _write_bundle(Path(bundle_text))
            first_app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            _seed_dataset(first_app)

            with TestClient(first_app) as first_client:
                uploaded = first_client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(bundle),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]

            second_app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            layer_url = f"/api/datasets/dataset-a/overlays/{layer_id}"
            first_write_entered = threading.Event()
            second_write_entered = threading.Event()
            release_first_write = threading.Event()
            call_guard = threading.Lock()
            manifest_write_count = 0

            from mms_shp_detection.webapp import overlays as overlays_module

            original_atomic_replace = overlays_module.atomic_replace_bytes

            def delayed_manifest_replace(path: Path, data: bytes) -> None:
                nonlocal manifest_write_count
                if path.name == "manifest.json":
                    with call_guard:
                        manifest_write_count += 1
                        call_number = manifest_write_count
                    if call_number == 1:
                        first_write_entered.set()
                        self.assertTrue(release_first_write.wait(timeout=5.0))
                    elif call_number == 2:
                        second_write_entered.set()
                original_atomic_replace(path, data)

            with (
                TestClient(first_app) as first_client,
                TestClient(second_app) as second_client,
                patch.object(
                    overlays_module,
                    "atomic_replace_bytes",
                    side_effect=delayed_manifest_replace,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first_future = executor.submit(
                    first_client.patch,
                    layer_url,
                    json={
                        "name": "worker-one",
                        "expected_metadata_revision": 1,
                    },
                )
                self.assertTrue(first_write_entered.wait(timeout=5.0))
                second_future = executor.submit(
                    second_client.patch,
                    layer_url,
                    json={
                        "color": "#123456",
                        "expected_metadata_revision": 1,
                    },
                )
                # A second process-local lock would allow both workers to read
                # revision 1.  The SQLite write transaction keeps this request
                # outside the manifest replacement until the first commits.
                self.assertFalse(second_write_entered.wait(timeout=0.2))
                release_first_write.set()
                responses = [first_future.result(), second_future.result()]

                self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
                persisted = first_client.get(layer_url)
                self.assertEqual(persisted.status_code, 200, persisted.text)
                self.assertEqual(persisted.json()["metadata_revision"], 2)
                self.assertEqual(persisted.json()["name"], "worker-one")

    def test_create_point_and_copy_geometry_assign_monotonic_ids(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            bundle = _write_id_bundle(Path(bundle_text))

            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(bundle),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]
                feature_url = f"/api/datasets/dataset-a/overlays/{layer_id}/features"

                created = client.post(
                    feature_url,
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_050.0, 4_100_020.0, 14.0],
                        },
                        "coordinate_space": "dataset",
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                created_payload = created.json()
                self.assertEqual(created_payload["feature"]["id"], "f_000000003")
                self.assertEqual(created_payload["feature"]["properties"], {"id": 10, "NAME": None})
                self.assertEqual(created_payload["revision"], 2)

                first_wgs84 = client.get(
                    f"{feature_url}/f_000000001",
                    params={"coordinate_space": "wgs84"},
                ).json()["feature"]["geometry"]["coordinates"]
                created_wgs84 = client.post(
                    feature_url,
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [first_wgs84[0] + 0.0001, first_wgs84[1]],
                        },
                        "coordinate_space": "wgs84",
                        "expected_revision": 2,
                    },
                )
                self.assertEqual(created_wgs84.status_code, 201, created_wgs84.text)
                self.assertAlmostEqual(
                    created_wgs84.json()["feature"]["geometry"]["coordinates"][0],
                    first_wgs84[0] + 0.0001,
                    places=7,
                )
                self.assertEqual(created_wgs84.json()["feature"]["properties"]["id"], 11)

                copied = client.post(
                    feature_url,
                    json={
                        "copy_geometry_from": "f_000000001",
                        "expected_revision": 3,
                    },
                )
                self.assertEqual(copied.status_code, 201, copied.text)
                copied_feature = copied.json()["feature"]
                self.assertEqual(copied_feature["id"], "f_000000005")
                self.assertEqual(
                    copied_feature["geometry"]["coordinates"],
                    [300_010.0, 4_100_000.0, 10.0],
                )
                self.assertEqual(copied_feature["properties"], {"id": 12, "NAME": None})

                stale = client.post(
                    feature_url,
                    json={
                        "copy_geometry_from": "f_000000001",
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)

                deleted = client.delete(
                    f"{feature_url}/f_000000005",
                    params={"expected_revision": 4},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                after_delete = client.post(
                    feature_url,
                    json={
                        "copy_geometry_from": "f_000000001",
                        "expected_revision": 5,
                    },
                )
                self.assertEqual(after_delete.status_code, 201, after_delete.text)
                self.assertEqual(after_delete.json()["feature"]["id"], "f_000000006")
                self.assertEqual(after_delete.json()["feature"]["properties"]["id"], 13)

                invalid_wgs84 = client.post(
                    feature_url,
                    json={
                        "geometry": {"type": "Point", "coordinates": [200, 37]},
                        "coordinate_space": "wgs84",
                        "expected_revision": 6,
                    },
                )
                self.assertEqual(invalid_wgs84.status_code, 422, invalid_wgs84.text)

                exported = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/download"
                )
                self.assertEqual(exported.status_code, 200, exported.text)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    shp_name = next(name for name in archive.namelist() if name.endswith(".shp"))
                    stem = Path(shp_name).stem
                    reader = shapefile.Reader(
                        shp=io.BytesIO(archive.read(f"{stem}.shp")),
                        shx=io.BytesIO(archive.read(f"{stem}.shx")),
                        dbf=io.BytesIO(archive.read(f"{stem}.dbf")),
                        encoding="utf-8",
                    )
                    try:
                        self.assertEqual(reader.numRecords, 5)
                        self.assertEqual(list(reader.record(4)), [13, ""])
                    finally:
                        reader.close()

            dataset_overlay_root = next((Path(state_text) / "overlays").iterdir())
            connection = sqlite3.connect(dataset_overlay_root / layer_id / "features.sqlite3")
            try:
                audit = connection.execute(
                    "SELECT revision,action,feature_id,before_json FROM audit ORDER BY revision"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                [(revision, action, feature_id) for revision, action, feature_id, _ in audit],
                [
                    (2, "create", "f_000000003"),
                    (3, "create", "f_000000004"),
                    (4, "create", "f_000000005"),
                    (5, "delete", "f_000000005"),
                    (6, "create", "f_000000006"),
                ],
            )
            self.assertTrue(all(row[3] is None for row in (*audit[:3], audit[4])))
            self.assertIsNotNone(audit[3][3])

    def test_upload_edit_project_delete_and_download_preserve_source(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            bundle = _write_bundle(Path(bundle_text))
            original_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in bundle
            }

            with TestClient(app) as client:
                response = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(bundle),
                    data={"name": "Pole review"},
                )
                self.assertEqual(response.status_code, 201, response.text)
                layer = response.json()["layer"]
                layer_id = layer["id"]
                self.assertEqual(layer["feature_count"], 2)
                self.assertTrue(layer["source_preserved"])

                active_root = next((Path(state_text) / "overlays").iterdir())
                for index in range(1_001):
                    (active_root / f".stale-upload-{index:04d}").mkdir()
                listed = client.get("/api/datasets/dataset-a/overlays").json()
                self.assertEqual([item["id"] for item in listed["items"]], [layer_id])

                dataset_features = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "dataset"},
                ).json()
                self.assertEqual(dataset_features["total"], 2)
                first = dataset_features["features"][0]
                self.assertEqual(first["geometry"]["coordinates"], [300010.0, 4100000.0, 10.0])

                nearby = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={
                        "coordinate_space": "dataset",
                        "center_x": 300000,
                        "center_y": 4100000,
                        "radius": 20,
                    },
                )
                self.assertEqual(nearby.status_code, 200, nearby.text)
                self.assertEqual(nearby.json()["total"], 1)
                self.assertEqual(
                    nearby.json()["features"][0]["id"], "f_000000001"
                )
                self.assertEqual(
                    nearby.json()["spatial_filter"]["coordinate_space"], "dataset"
                )
                partial_filter = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"center_x": 300000},
                )
                self.assertEqual(partial_filter.status_code, 422)

                single = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000002",
                    params={"coordinate_space": "dataset"},
                )
                self.assertEqual(single.status_code, 200, single.text)
                self.assertEqual(single.json()["feature"]["properties"]["NAME"], "second")
                self.assertEqual(single.json()["revision"], 1)

                map_features = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "wgs84"},
                ).json()
                lon, lat, altitude = map_features["features"][0]["geometry"]["coordinates"]
                self.assertTrue(120.0 < lon < 130.0)
                self.assertTrue(30.0 < lat < 40.0)
                self.assertEqual(altitude, 10.0)

                frames = client.get("/api/datasets/dataset-a/frames").json()
                self.assertEqual(
                    frames["items"][0]["dataset_position"],
                    [300000.0, 4100000.0, 10.0],
                )
                projection = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/project/frame-a",
                    params={"max_distance": 100, "yaw_offset_deg": 0, "pitch_offset_deg": 0},
                )
                self.assertEqual(projection.status_code, 200, projection.text)
                projected = projection.json()["items"]
                self.assertEqual(len(projected), 2)
                self.assertAlmostEqual(projected[0]["u"], 0.5, places=5)
                self.assertAlmostEqual(projected[0]["v"], 0.5, places=5)
                self.assertAlmostEqual(projected[0]["depth"], 10.0, places=5)

                picked = client.post(
                    "/api/datasets/dataset-a/frames/frame-a/panorama-pick",
                    json={
                        "u": 0.5,
                        "v": 0.5,
                        "depth": 10.0,
                        "yaw_offset_deg": 0,
                        "pitch_offset_deg": 0,
                    },
                )
                self.assertEqual(picked.status_code, 200, picked.text)
                self.assertEqual(
                    picked.json()["dataset_position"],
                    [300010.0, 4100000.0, 10.0],
                )

                updated = client.patch(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000001",
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300020.0, 4100001.0],
                        },
                        "coordinate_space": "dataset",
                        "properties": {"NAME": "corrected"},
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()["revision"], 2)
                self.assertEqual(
                    updated.json()["feature"]["geometry"]["coordinates"],
                    [300020.0, 4100001.0, 10.0],
                )
                conflict = client.patch(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000001",
                    json={"properties": {"NAME": "stale"}, "expected_revision": 1},
                )
                self.assertEqual(conflict.status_code, 409)

                deleted = client.delete(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000002",
                    params={"expected_revision": 2},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()["revision"], 3)
                self.assertEqual(
                    client.get(
                        f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000002"
                    ).status_code,
                    404,
                )
                remaining = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "dataset"},
                ).json()
                self.assertEqual(remaining["total"], 1)

                download = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/download"
                )
                self.assertEqual(download.status_code, 200, download.text)
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    names = archive.namelist()
                    self.assertTrue(any(name.endswith(".shp") for name in names))
                    self.assertTrue(any(name.endswith(".dbf") for name in names))
                    self.assertTrue(any(name.endswith(".prj") for name in names))

                removed = client.delete(
                    f"/api/datasets/dataset-a/overlays/{layer_id}"
                )
                self.assertEqual(removed.status_code, 200, removed.text)
                self.assertFalse(removed.json()["source_deleted"])
                self.assertTrue(removed.json()["source_preserved"])
                self.assertEqual(
                    client.get("/api/datasets/dataset-a/overlays").json()["items"],
                    [],
                )
                self.assertEqual(
                    client.get(
                        f"/api/datasets/dataset-a/overlays/{layer_id}/features"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    list((Path(state_text) / "overlays").rglob("source/*.shp")),
                    [],
                )
                preserved_sources = list(
                    (Path(state_text) / "overlay_archive").rglob("source/*.shp")
                )
                self.assertEqual(len(preserved_sources), 1)

            self.assertEqual(
                original_hashes,
                {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in bundle},
            )

    def test_missing_cpg_auto_detects_cp949_and_preserves_dbf_byte_width(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
            tempfile.TemporaryDirectory() as extracted_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            bundle = _write_bundle(
                Path(bundle_text),
                encoding="cp949",
                include_cpg=False,
                names=("가나다", "라마바"),
                name_field_size=8,
            )
            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(bundle),
                    data={"encoding": "auto"},
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer = uploaded.json()["layer"]
                self.assertEqual(layer["source_encoding"], "cp949")
                self.assertTrue(any("inferred" in warning for warning in layer["warnings"]))
                layer_id = layer["id"]
                features = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features",
                    params={"coordinate_space": "dataset"},
                ).json()
                self.assertEqual(features["features"][0]["properties"]["NAME"], "가나다")

                edited = client.patch(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000001",
                    json={
                        "properties": {"NAME": "가나다라"},
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(edited.status_code, 200, edited.text)
                unencodable = client.patch(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000001",
                    json={"properties": {"NAME": "표지판😀"}, "expected_revision": 2},
                )
                self.assertEqual(unencodable.status_code, 422)
                too_wide = client.patch(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features/f_000000001",
                    json={"properties": {"NAME": "가나다라마"}, "expected_revision": 2},
                )
                self.assertEqual(too_wide.status_code, 422)

                download = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/download"
                )
                self.assertEqual(download.status_code, 200, download.text)
            archive_path = Path(extracted_text) / "edited.zip"
            archive_path.write_bytes(download.content)
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extracted_text)
            self.assertEqual(
                next(Path(extracted_text).glob("*.cpg")).read_text(encoding="ascii"),
                "949",
            )
            exported = shapefile.Reader(
                str(next(Path(extracted_text).glob("*.shp"))),
                encoding="cp949",
                encodingErrors="strict",
            )
            try:
                self.assertEqual(exported.record(0)[0], "가나다라")
            finally:
                exported.close()

    def test_uppercase_sidecar_extensions_are_imported(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            uppercase_bundle: list[Path] = []
            for path in _write_bundle(Path(bundle_text)):
                target = path.with_suffix(path.suffix.upper())
                path.rename(target)
                uppercase_bundle.append(target)
            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(uppercase_bundle),
                )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            self.assertEqual(uploaded.json()["layer"]["feature_count"], 2)

    def test_zip_slip_and_zip_bomb_are_rejected_without_escape(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("../escape.shp", b"unsafe")
            with TestClient(app) as client:
                response = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files={"files": ("unsafe.zip", archive_bytes.getvalue(), "application/zip")},
                )
                self.assertEqual(response.status_code, 422)
            self.assertFalse((Path(state_text) / "escape.shp").exists())

            bomb_bytes = io.BytesIO()
            with zipfile.ZipFile(bomb_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bomb.shp", b"0" * 2_000_000)
            with TestClient(app) as client:
                response = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files={"files": ("bomb.zip", bomb_bytes.getvalue(), "application/zip")},
                )
                self.assertEqual(response.status_code, 413)

    def test_run_results_publish_location_and_shapefile_bundle_actions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            app.state.store.create_run(
                {
                    "id": "run-complete",
                    "dataset_id": "dataset-a",
                    "request": {},
                    "resolved": {},
                    "work_relative": "run-complete",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )
            output = Path(state_text) / "runs" / "run-complete" / "output" / "shp"
            output.mkdir(parents=True)
            for source in _write_bundle(Path(bundle_text), "detected_signs"):
                (output / source.name).write_bytes(source.read_bytes())
            app.state.store.update_run(
                "run-complete", NOW, status="completed", return_code=0, finished_at=NOW
            )

            with TestClient(app) as client:
                result = client.get("/api/runs/run-complete/results")
                self.assertEqual(result.status_code, 200, result.text)
                payload = result.json()
                self.assertEqual(
                    payload["output_location"]["relative_path"],
                    "runs/run-complete/output",
                )
                self.assertEqual(len(payload["shapefiles"]), 1)
                shape = payload["shapefiles"][0]
                download = client.get(shape["download_url"])
                self.assertEqual(download.status_code, 200, download.text)
                self.assertEqual(download.headers["content-type"], "application/zip")

                imported = client.post(
                    shape["import_url"],
                    json={"path": shape["path"], "name": "Detected signs"},
                )
                self.assertEqual(imported.status_code, 201, imported.text)
                self.assertEqual(imported.json()["layer"]["feature_count"], 2)

    def test_imported_detection_projection_includes_current_frame_raw_yolo_boxes(self) -> None:
        """Cover pipeline result -> SHP import -> projection -> exact frame box flow."""

        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            state = Path(state_text)
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=state,
                start_runner=False,
            )
            _seed_dataset(app)
            app.state.store.create_run(
                {
                    "id": "run-detections",
                    "dataset_id": "dataset-a",
                    "request": {},
                    "resolved": {},
                    "work_relative": "run-detections",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )
            model_root = state / "runs" / "run-detections" / "output" / "model-a"
            shp_root = model_root / "shp"
            shp_root.mkdir(parents=True)
            for source in _write_detection_bundle(Path(bundle_text)):
                (shp_root / source.name).write_bytes(source.read_bytes())
            result_root = model_root / "txt" / "record-a"
            result_root.mkdir(parents=True)
            # The frame registry uses record-a for the safe, exact per-frame lookup.
            with app.state.store.connection(write=True) as connection:
                task = json.loads(
                    connection.execute(
                        "SELECT task_json FROM frames WHERE id='frame-a'"
                    ).fetchone()[0]
                )
                task["record_name"] = "record-a"
                task["image_stem"] = "frame-a"
                connection.execute(
                    "UPDATE frames SET task_json=? WHERE id='frame-a'",
                    (json.dumps(task),),
                )
            (result_root / "frame-a.txt").write_text(
                json.dumps(
                    {
                        "record_name": "record-a",
                        "image_name": "frame-a.jpg",
                        "detections": [
                            {
                                "detection_index": 1,
                                "image_name": "frame-a.jpg",
                                "class_id": 4,
                                "class_name": "traffic_sign",
                                "confidence": 0.91,
                                "bbox_xyxy": [400, 150, 500, 250],
                                "panorama_width": 1000,
                                "panorama_height": 500,
                                "accepted_for_shp": True,
                            },
                            {
                                "detection_index": 2,
                                "image_name": "frame-a.jpg",
                                "class_id": 7,
                                "class_name": "rejected_candidate",
                                "confidence": 0.45,
                                "bbox_xyxy": [600, 160, 650, 230],
                                "panorama_width": 1000,
                                "panorama_height": 500,
                                "accepted_for_shp": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app.state.store.update_run(
                "run-detections", NOW, status="completed", return_code=0, finished_at=NOW
            )

            with TestClient(app) as client:
                imported = client.post(
                    "/api/runs/run-detections/shapefile/import",
                    json={
                        "path": "model-a/shp/detected_signs.shp",
                        "name": "Detected signs",
                    },
                )
                self.assertEqual(imported.status_code, 201, imported.text)
                layer_id = imported.json()["layer"]["id"]
                projection = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/project/frame-a",
                    params={"max_distance": 100, "yaw_offset_deg": 0, "pitch_offset_deg": 0},
                )
                self.assertEqual(projection.status_code, 200, projection.text)
                boxes = projection.json()["detection_boxes"]
                self.assertEqual(len(boxes), 2)
                self.assertRegex(boxes[0]["source_id"], r"^det-src_[0-9a-f]{32}$")
                self.assertEqual(boxes[0]["source_id"], boxes[1]["source_id"])
                self.assertEqual(boxes[0]["properties"]["img_name"], "frame-a.jpg")
                self.assertEqual(boxes[0]["properties"]["bbox_l"], 400.0)
                self.assertEqual(boxes[0]["properties"]["pano_w"], 1000.0)
                self.assertTrue(boxes[0]["properties"]["accepted"])
                self.assertEqual(boxes[0]["feature_id"], "f_000000001")
                self.assertFalse(boxes[1]["properties"]["accepted"])

                layer_dir = next((state / "overlays").iterdir()) / layer_id
                manifest_path = layer_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                original_reference = manifest["source_reference"]
                manifest["source_reference"] = (
                    "run:run-detections:../outside/shp/detected_signs.shp"
                )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                unsafe = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/project/frame-a",
                    params={"max_distance": 100},
                )
                self.assertEqual(unsafe.status_code, 200, unsafe.text)
                self.assertEqual(unsafe.json()["detection_boxes"], [])
                manifest["source_reference"] = original_reference
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                # A mismatched frame payload cannot leak another panorama's boxes.
                (result_root / "frame-a.txt").write_text(
                    json.dumps({"image_name": "other-frame.jpg", "detections": []}),
                    encoding="utf-8",
                )
                mismatch = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/project/frame-a",
                    params={"max_distance": 100},
                )
                self.assertEqual(mismatch.json()["detection_boxes"], [])

    def test_feature_create_commits_geometry_and_validated_properties_atomically(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(_write_bundle(Path(bundle_text))),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]
                feature_url = (
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features"
                )
                created = client.post(
                    feature_url,
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_015.0, 4_100_001.0, 9.75],
                        },
                        "coordinate_space": "dataset",
                        "properties": {"NAME": "manual pole", "VALUE": 42},
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(created.json()["revision"], 2)
                self.assertEqual(
                    created.json()["feature"]["geometry"]["coordinates"],
                    [300015.0, 4100001.0, 9.75],
                )
                self.assertEqual(
                    created.json()["feature"]["properties"],
                    {"NAME": "manual pole", "VALUE": 42},
                )

                invalid = client.post(
                    feature_url,
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_016.0, 4_100_001.0, 9.5],
                        },
                        "properties": {"NOT_A_FIELD": "must roll back"},
                        "expected_revision": 2,
                    },
                )
                self.assertEqual(invalid.status_code, 422, invalid.text)
                after = client.get(
                    feature_url, params={"coordinate_space": "dataset"}
                )
                self.assertEqual(after.status_code, 200, after.text)
                self.assertEqual(after.json()["revision"], 2)
                self.assertEqual(after.json()["total"], 3)

    def test_xyz_edit_promotes_a_2d_point_download_to_pointz(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_text,
            tempfile.TemporaryDirectory() as state_text,
            tempfile.TemporaryDirectory() as bundle_text,
        ):
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            _seed_dataset(app)
            with TestClient(app) as client:
                uploaded = client.post(
                    "/api/datasets/dataset-a/overlays",
                    files=_multipart(_write_2d_point_bundle(Path(bundle_text))),
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                layer_id = uploaded.json()["layer"]["id"]
                feature_url = (
                    f"/api/datasets/dataset-a/overlays/{layer_id}/features"
                )
                created = client.post(
                    feature_url,
                    json={
                        "geometry": {
                            "type": "Point",
                            "coordinates": [300_015.0, 4_100_001.0, 9.75],
                        },
                        "coordinate_space": "dataset",
                        "expected_revision": 1,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)

                exported = client.get(
                    f"/api/datasets/dataset-a/overlays/{layer_id}/download"
                )
                self.assertEqual(exported.status_code, 200, exported.text)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    shp_name = next(
                        name for name in archive.namelist() if name.endswith(".shp")
                    )
                    stem = Path(shp_name).stem
                    reader = shapefile.Reader(
                        shp=io.BytesIO(archive.read(f"{stem}.shp")),
                        shx=io.BytesIO(archive.read(f"{stem}.shx")),
                        dbf=io.BytesIO(archive.read(f"{stem}.dbf")),
                        encoding="utf-8",
                    )
                    try:
                        self.assertEqual(reader.shapeType, shapefile.POINTZ)
                        self.assertEqual(tuple(reader.shape(0).z), (0.0,))
                        self.assertEqual(tuple(reader.shape(1).z), (9.75,))
                    finally:
                        reader.close()


if __name__ == "__main__":
    unittest.main()
