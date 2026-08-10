"""FastAPI application: settings, native folder picking, and backup control.

Every endpoint here is a thin wrapper. The copy/verify/manifest logic lives in
:mod:`footage_pipeline.backup` and is not duplicated in this layer.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, native_picker
from ..backup import FileStatus, PreflightError, plan_backup
from ..config import Settings, default_settings_path, load_settings, save_settings
from ..logging_setup import configure_app_logging, get_logger
from .runner import BackupBusy, BackupRunner

STATIC_DIR = Path(__file__).parent / "static"
#: Problem rows are surfaced inline in the status payload; the rest of the run
#: is in the manifest. Capped so a huge failing run cannot bloat the poll.
MAX_INLINE_ISSUES = 200

_logger = get_logger("web")

app = FastAPI(title="Footage Pipeline — Backup", version=__version__)
runner = BackupRunner()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class PickFolderRequest(BaseModel):
    prompt: str = "Choose a folder"
    default_path: str | None = None


class SettingsRequest(BaseModel):
    backup_root: str


class StartBackupRequest(BaseModel):
    source: str


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def _settings_payload(settings: Settings) -> dict[str, Any]:
    backup_root = settings.backup_root
    return {
        "backup_root": backup_root,
        "backup_root_exists": bool(backup_root) and Path(backup_root).is_dir(),
        "last_source": settings.last_source,
        "settings_path": str(default_settings_path()),
        "picker_available": native_picker.is_available(),
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """Current persisted settings (backup root + last-used source)."""
    return _settings_payload(load_settings())


@app.post("/api/settings")
def put_settings(request: SettingsRequest) -> dict[str, Any]:
    """Persist the backup root. Rejects a path that is not an existing folder."""
    candidate = Path(request.backup_root).expanduser()
    if not candidate.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Not an existing folder: {candidate}",
        )
    settings = load_settings()
    settings.backup_root = str(candidate.resolve())
    save_settings(settings)
    _logger.info("Backup root set to %s", settings.backup_root)
    return _settings_payload(settings)


# --------------------------------------------------------------------------
# Native folder picker
# --------------------------------------------------------------------------


@app.post("/api/pick-folder")
def pick_folder(request: PickFolderRequest) -> dict[str, Any]:
    """Open the native macOS folder dialog and return the absolute POSIX path.

    Declared ``def`` (not ``async def``) on purpose: FastAPI runs it in a
    worker thread, so the modal dialog does not block the event loop.
    """
    try:
        result = native_picker.choose_folder(
            prompt=request.prompt, default_path=request.default_path
        )
    except native_picker.NativePickerUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except native_picker.NativePickerFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"path": result.path, "cancelled": result.cancelled}


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


@app.post("/api/backup/plan")
def preview_plan(request: StartBackupRequest) -> dict[str, Any]:
    """Scan + pre-flight without copying anything (what would this run do?)."""
    settings = load_settings()
    if not settings.backup_root:
        raise HTTPException(status_code=400, detail="No backup root configured.")
    try:
        plan = plan_backup(Path(request.source), Path(settings.backup_root))
    except PreflightError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return plan.to_dict()


@app.post("/api/backup/start")
def start_backup(request: StartBackupRequest) -> dict[str, Any]:
    """Start a backup on a background thread and return immediately."""
    settings = load_settings()
    if not settings.backup_root:
        raise HTTPException(
            status_code=400,
            detail="No backup root configured. Set one in Settings first.",
        )

    source = Path(request.source).expanduser()
    if not source.is_dir():
        raise HTTPException(status_code=400, detail=f"Source is not a folder: {source}")

    # Fail fast on unusable path combinations before spawning the thread, so
    # the user gets an HTTP error instead of having to poll for it.
    try:
        plan_backup(source, Path(settings.backup_root))
    except PreflightError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        runner.start(str(source), settings.backup_root)
    except BackupBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    settings.last_source = str(source.resolve())
    save_settings(settings)
    _logger.info("Backup started: %s -> %s", source, settings.backup_root)
    return {"started": True, "source": str(source), "backup_root": settings.backup_root}


@app.get("/api/backup/status")
def backup_status() -> dict[str, Any]:
    """Poll target for live progress and the final report card."""
    status = runner.status()
    result = status.get("result")
    if result:
        rows = result.pop("files", [])
        issues = [
            row
            for row in rows
            if row.get("status")
            in (FileStatus.CONFLICT, FileStatus.FAILED, FileStatus.SYMLINK_SKIPPED)
        ]
        result["issues"] = issues[:MAX_INLINE_ISSUES]
        result["issue_count"] = len(issues)
    return status


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    """Console entry point: ``footage-pipeline-web``."""
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the footage pipeline web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    configure_app_logging()
    _logger.info("Settings file: %s", default_settings_path())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
