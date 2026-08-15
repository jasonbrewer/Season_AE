"""Tests for the FastAPI layer.

These check the wiring only: that the endpoints persist settings, refuse a bad
path, report an unavailable picker, and serve the UI shell.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from footage_pipeline.config import SETTINGS_ENV_VAR
from footage_pipeline.web import app as web_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose settings file is redirected into the test's tmp dir."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "settings.json"))
    return TestClient(web_app.app)


def test_settings_round_trip(client: TestClient, tmp_path: Path) -> None:
    folder = tmp_path / "chosen"
    folder.mkdir()

    assert client.get("/api/settings").json()["backup_root"] is None

    response = client.post("/api/settings", json={"backup_root": str(folder)})
    assert response.status_code == 200
    assert response.json()["backup_root"] == str(folder.resolve())
    assert response.json()["backup_root_exists"] is True

    # Persisted, not just held in memory.
    assert client.get("/api/settings").json()["backup_root"] == str(folder.resolve())


def test_settings_rejects_a_path_that_is_not_a_folder(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post("/api/settings", json={"backup_root": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "Not an existing folder" in response.json()["detail"]


def test_picker_reports_unavailable_off_macos(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_app.native_picker, "is_available", lambda: False)
    response = client.post("/api/pick-folder", json={"prompt": "Choose"})
    assert response.status_code == 501
    assert "macOS" in response.json()["detail"]


def test_index_and_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
