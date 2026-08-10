"""Persisted application settings.

Persistence is a single local JSON file. There is no database anywhere in this
project, by design.

The file lives in the platform's per-user application-support directory unless
``FOOTAGE_PIPELINE_SETTINGS`` overrides it (the tests use that override so they
never touch the real user's settings).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

APP_NAME = "FootagePipeline"
SETTINGS_ENV_VAR = "FOOTAGE_PIPELINE_SETTINGS"


def default_settings_path() -> Path:
    """Return the settings file path for this platform (env var wins)."""
    override = os.environ.get(SETTINGS_ENV_VAR)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if os.uname().sysname == "Darwin":
        return home / "Library" / "Application Support" / APP_NAME / "settings.json"
    return home / ".config" / "footage_pipeline" / "settings.json"


@dataclass
class Settings:
    """User-visible, persisted configuration.

    Attributes:
        backup_root: Destination root for backups. Set once via the native
            folder picker and changeable in the settings screen.
        last_source: The source folder used for the most recent run, so the UI
            can pre-fill it. The source itself is always re-chosen per run.
    """

    backup_root: str | None = None
    last_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        # Ignore unknown keys so an older build can read a newer file.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, returning defaults when the file is missing or corrupt."""
    path = Path(path) if path is not None else default_settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt settings file must never block the app; fall back to
        # defaults and let the user re-pick their backup root.
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write settings atomically (temp file in the same dir + ``os.replace``)."""
    path = Path(path) if path is not None else default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Only ever removes the temp file this call created.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path
