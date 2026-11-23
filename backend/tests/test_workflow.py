import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Keep persistence tests away from the user's real preference database.
_test_data = tempfile.TemporaryDirectory(prefix="wishcraft-tests-")
os.environ["WISHCRAFT_DB_PATH"] = str(Path(_test_data.name) / "preferences.sqlite3")

from app.main import app, provider

client = TestClient(app)


def test_discovery_request_rejects_long_period():
    response = client.post("/api/v1/occasions/discover", json={"country":"Canada","region":"Ontario","timezone":"America/Toronto","start_date":"2026-01-01","end_date":"2026-05-01","demo_mode":True})
    assert response.status_code == 422


def test_discovery_demo_is_labeled():
    response = client.post("/api/v1/occasions/discover", json={"country":"Canada","region":"Ontario","timezone":"America/Toronto","start_date":"2026-01-01","end_date":"2026-01-07","demo_mode":True})
    assert response.status_code == 200
    assert response.json()["meta"]["demo"] is True
    assert response.json()["occasions"] == []


def test_discovery_rejects_out_of_range_model_items(monkeypatch):
    monkeypatch.setattr(provider, "complete", lambda *args, **kwargs: ('{"occasions":[{"name":"Outside","date":"2026-02-01","category":"Social","description":"Outside range","relevance":"high","date_type":"fixed","confidence":0.9,"verification_status":"unverified"},{"name":"Inside","date":"2026-01-04","category":"Social","description":"Within range","relevance":"medium","date_type":"fixed","confidence":0.9,"verification_status":"unverified"}]}', {"provider":"mock","model":"mock","duration_ms":1,"tokens":4}))
    response = client.post("/api/v1/occasions/discover", json={"country":"Canada","region":"Ontario","timezone":"America/Toronto","start_date":"2026-01-01","end_date":"2026-01-07"})
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["occasions"]] == ["Inside"]
    assert response.json()["meta"]["rejected_count"] == 1


@pytest.mark.parametrize("tone,language", [("Funny and cheerful","French"),("Formal and professional","Arabic")])
def test_wish_generation_passes_tone_language_to_provider(monkeypatch, tone, language):
    seen = {}
    def fake(system, prompt, **kwargs):
        seen["prompt"] = prompt
        return "A lovely greeting.", {"provider":"mock","model":"mock","duration_ms":1,"tokens":2}
    monkeypatch.setattr(provider, "complete", fake)
    response = client.post("/api/v1/wishes/generate", json={"occasion":"Custom day","tone":tone,"language":language})
    assert response.status_code == 200
    assert response.json()["wish"] == "A lovely greeting."
    assert tone in seen["prompt"] and language in seen["prompt"]


def test_preferences_round_trip():
    client.put("/api/v1/preferences", json={"language":"Hindi","tone":"Short and simple","country":"India","region":"Punjab","timezone":"Asia/Kolkata"})
    assert client.get("/api/v1/preferences").json()["language"] == "Hindi"
