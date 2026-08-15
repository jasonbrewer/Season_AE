"""FastAPI application: settings, native folder picking, and the UI shell.

Every endpoint here is a thin wrapper. No pipeline work lives in this layer.
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
from ..config import Settings, default_settings_path, load_settings, save_settings
from ..logging_setup import configure_app_logging, get_logger

STATIC_DIR = Path(__file__).parent / "static"

_logger = get_logger("web")

app = FastAPI(title="Season Assistant Editor", version=__version__)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class PickFolderRequest(BaseModel):
    prompt: str = "Choose a folder"
    default_path: str | None = None


class SettingsRequest(BaseModel):
    backup_root: str


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
    """Current persisted settings."""
    return _settings_payload(load_settings())


@app.post("/api/settings")
def put_settings(request: SettingsRequest) -> dict[str, Any]:
    """Persist ``backup_root``. Rejects a path that is not an existing folder."""
    candidate = Path(request.backup_root).expanduser()
    if not candidate.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Not an existing folder: {candidate}",
        )
    settings = load_settings()
    settings.backup_root = str(candidate.resolve())
    save_settings(settings)
    _logger.info("Root set to %s", settings.backup_root)
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
# Frontend
# --------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    """Console entry point: ``footage-pipeline-web``."""
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Season Assistant Editor web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    configure_app_logging()
    _logger.info("Settings file: %s", default_settings_path())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
