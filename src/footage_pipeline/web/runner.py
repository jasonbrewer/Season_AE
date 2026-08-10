"""Background run management for the web UI.

This is the thin bridge between HTTP and the engine: it runs
:func:`footage_pipeline.backup.run_backup` on a worker thread and keeps the
latest :class:`~footage_pipeline.backup.Progress` snapshot available for the
polled status endpoint. All copy/verify/manifest behaviour lives in the engine.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..backup import BackupResult, PreflightError, Progress, run_backup
from ..logging_setup import get_logger

_logger = get_logger("web.runner")


class BackupBusy(RuntimeError):
    """Raised when a run is requested while one is already in flight."""


class BackupRunner:
    """Owns at most one in-flight backup and its observable state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress = Progress()
        self._result: BackupResult | None = None
        self._error: str | None = None
        self._source: str | None = None
        self._backup_root: str | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, source_root: str, backup_root: str) -> None:
        """Kick off a run in the background.

        Raises:
            BackupBusy: A run is already in progress.
        """
        with self._lock:
            if self.is_running:
                raise BackupBusy("A backup is already running.")
            self._progress = Progress(phase="scanning")
            self._result = None
            self._error = None
            self._source = source_root
            self._backup_root = backup_root
            thread = threading.Thread(
                target=self._run,
                args=(source_root, backup_root),
                name="backup-run",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _on_progress(self, progress: Progress) -> None:
        # The engine mutates one Progress object as it goes; copy it so the
        # status endpoint never reads a half-updated snapshot.
        with self._lock:
            self._progress = replace(progress)

    def _run(self, source_root: str, backup_root: str) -> None:
        try:
            result = run_backup(
                Path(source_root),
                Path(backup_root),
                progress_cb=self._on_progress,
            )
        except PreflightError as exc:
            with self._lock:
                self._error = str(exc)
                self._progress = replace(self._progress, phase="error", current_file="")
            _logger.error("Backup refused to start: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._progress = replace(self._progress, phase="error", current_file="")
            _logger.exception("Backup run crashed")
        else:
            with self._lock:
                self._result = result
                self._progress = replace(result.progress)

    def status(self) -> dict[str, Any]:
        """A JSON-ready snapshot for the polled status endpoint."""
        with self._lock:
            return {
                "running": self.is_running,
                "source": self._source,
                "backup_root": self._backup_root,
                "progress": self._progress.to_dict(),
                "error": self._error,
                "result": self._result.to_dict() if self._result else None,
            }
