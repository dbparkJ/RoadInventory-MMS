from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mms_shp_detection.webapp import create_app


class WebAppSurveySegmentTests(unittest.TestCase):
    def test_survey_segments_are_persistent_dataset_scoped_and_deletable(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            now = "2026-08-14T00:00:00+00:00"
            app.state.store.upsert_scanning_dataset(
                dataset_id="dataset-survey",
                name="survey delivery",
                root_id="root",
                relative_path="delivery",
                crs="EPSG:4326",
                now=now,
            )

            with TestClient(app) as client:
                created = client.post(
                    "/api/datasets/dataset-survey/survey-segments",
                    json={
                        "name": "현장조사 필요구간 1",
                        "color": "#F5C542",
                        "coordinates": [[127.0, 37.0], [127.001, 37.002]],
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                segment = created.json()["segment"]
                self.assertTrue(segment["id"].startswith("survey_"))
                self.assertEqual(segment["color"], "#f5c542")
                self.assertEqual(segment["geometry"]["type"], "LineString")

                listed = client.get("/api/datasets/dataset-survey/survey-segments")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(listed.json()["items"], [segment])

                deleted = client.delete(
                    f"/api/datasets/dataset-survey/survey-segments/{segment['id']}"
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json(), {"id": segment["id"], "deleted": True})
                self.assertEqual(
                    client.get(
                        "/api/datasets/dataset-survey/survey-segments"
                    ).json()["items"],
                    [],
                )

    def test_survey_segment_validation_rejects_unsafe_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            app.state.store.upsert_scanning_dataset(
                dataset_id="dataset-survey",
                name="survey delivery",
                root_id="root",
                relative_path="delivery",
                crs="EPSG:4326",
                now="2026-08-14T00:00:00+00:00",
            )

            with TestClient(app) as client:
                for coordinates in (
                    [[127.0, 37.0]],
                    [[127.0, 37.0], [181.0, 37.0]],
                ):
                    response = client.post(
                        "/api/datasets/dataset-survey/survey-segments",
                        json={"name": "invalid", "coordinates": coordinates},
                    )
                    self.assertEqual(response.status_code, 422, response.text)

                missing = client.get("/api/datasets/missing/survey-segments")
                self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
