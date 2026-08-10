"""Tests for the FastAPI layer.

These check the wiring only — that the endpoints persist settings, start the
engine, and surface its report. The copy/verify behaviour itself is covered by
``test_backup_core.py`` against the same engine entry point.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from footage_pipeline.backup import FileStatus
from footage_pipeline.config import SETTINGS_ENV_VAR
from footage_pipeline.web import app as web_app

POLL_TIMEOUT_SECONDS = 120


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose settings file is redirected into the test's tmp dir."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "settings.json"))
    # The runner is a process-wide singleton; give each test a clean one.
    monkeypatch.setattr(web_app, "runner", web_app.BackupRunner())
    return TestClient(web_app.app)


def wait_for_result(client: TestClient) -> dict:
    """Poll the status endpoint the way the frontend does, until the run ends."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = client.get("/api/backup/status").json()
        if not status["running"] and (status["result"] or status["error"]):
            return status
        time.sleep(0.05)
    raise AssertionError("backup did not finish within the timeout")


def test_settings_round_trip(client: TestClient, backup_root: Path) -> None:
    assert client.get("/api/settings").json()["backup_root"] is None

    response = client.post("/api/settings", json={"backup_root": str(backup_root)})
    assert response.status_code == 200
    assert response.json()["backup_root"] == str(backup_root.resolve())
    assert response.json()["backup_root_exists"] is True

    # Persisted, not just held in memory.
    assert client.get("/api/settings").json()["backup_root"] == str(backup_root.resolve())


def test_settings_rejects_a_path_that_is_not_a_folder(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post("/api/settings", json={"backup_root": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "Not an existing folder" in response.json()["detail"]


def test_start_requires_a_configured_backup_root(
    client: TestClient, source_tree: Path
) -> None:
    response = client.post("/api/backup/start", json={"source": str(source_tree)})
    assert response.status_code == 400
    assert "backup root" in response.json()["detail"].lower()


def test_full_run_through_the_api_reports_pass_and_remembers_the_source(
    client: TestClient, source_tree: Path, backup_root: Path
) -> None:
    client.post("/api/settings", json={"backup_root": str(backup_root)})

    plan = client.post("/api/backup/plan", json={"source": str(source_tree)}).json()
    assert plan["total_files"] > 0
    assert plan["fits"] is True

    assert client.post("/api/backup/start", json={"source": str(source_tree)}).status_code == 200

    status = wait_for_result(client)
    result = status["result"]
    assert status["error"] is None
    assert result["overall"] == "PASS"
    assert result["totals"]["copied"] == plan["total_files"]
    assert Path(result["manifest_path"]).is_file()
    assert Path(result["log_path"]).is_file()
    assert status["progress"]["phase"] == "done"

    # The source is remembered so the next run can pre-fill it.
    assert client.get("/api/settings").json()["last_source"] == str(source_tree.resolve())


def test_conflicts_are_surfaced_in_the_status_payload(
    client: TestClient, source_tree: Path, backup_root: Path
) -> None:
    client.post("/api/settings", json={"backup_root": str(backup_root)})
    client.post("/api/backup/start", json={"source": str(source_tree)})
    wait_for_result(client)

    (backup_root / "README.md").write_bytes(b"edited outside the app\n")
    client.post("/api/backup/start", json={"source": str(source_tree)})
    result = wait_for_result(client)["result"]

    assert result["overall"] == "FAIL"
    assert result["totals"]["conflicts"] == 1
    conflicts = [row for row in result["issues"] if row["status"] == FileStatus.CONFLICT]
    assert [row["relative_path"] for row in conflicts] == ["README.md"]
    # Full per-file rows stay in the manifest; the poll only carries problems.
    assert "files" not in result


def test_start_rejects_a_source_that_is_not_a_folder(
    client: TestClient, backup_root: Path, tmp_path: Path
) -> None:
    client.post("/api/settings", json={"backup_root": str(backup_root)})
    response = client.post("/api/backup/start", json={"source": str(tmp_path / "ghost")})
    assert response.status_code == 400


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
