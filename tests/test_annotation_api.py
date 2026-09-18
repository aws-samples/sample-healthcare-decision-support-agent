"""Annotation API integration, with storage outside the source checkout."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from medical_nudging.api.routes import eval as routes


def test_annotations_round_trip_outside_repo(tmp_path, monkeypatch):
    path = tmp_path / "annotations.json"
    monkeypatch.setattr(routes, "TAXONOMY_FILE", path)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/eval/annotations").json()["total"] == 0
        response = client.post(
            "/eval/annotations",
            json={
                "sample_id": "synthetic",
                "nudge_index": 0,
                "annotation": "Synthetic review note",
                "category": "other",
            },
        )
        assert response.status_code == 200
        saved = response.json()
        listing = client.get("/eval/annotations", params={"sample_id": "synthetic"}).json()
        assert listing["total"] == 1
        assert listing["annotations"][0]["id"] == saved["annotation_id"]
        assert (
            client.get("/eval/annotations", params={"sample_id": "different"}).json()["total"] == 0
        )
        assert client.get("/eval/taxonomy").json()["failure_categories"] == {"other": 1}
    assert json.loads(path.read_text())["observations"][0]["annotation"] == "Synthetic review note"
