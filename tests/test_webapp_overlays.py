from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

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


if __name__ == "__main__":
    unittest.main()
