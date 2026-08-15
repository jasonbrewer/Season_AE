"""Background run management for the web UI.

This is the thin bridge between HTTP and whatever long-running work the app
needs to do: it runs a caller-supplied job on a worker thread and keeps the
latest progress snapshot available for a polled status endpoint. It knows
nothing about what the job actually does.

The job is a callable taking one argument — a progress callback — and
returning a JSON-ready result (or ``None``). It reports progress by calling
that callback with a JSON-ready dict.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..logging_setup import get_logger

_logger = get_logger("web.runner")

#: What a job calls to publish a progress snapshot.
ProgressCallback = Callable[[dict[str, Any]], None]
#: What the runner runs on its worker thread.
Job = Callable[[ProgressCallback], Any]


class JobBusy(RuntimeError):
    """Raised when a run is requested while one is already in flight."""


class JobRunner:
    """Owns at most one in-flight job and its observable state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress: dict[str, Any] = {"phase": "idle"}
        self._result: Any = None
        self._error: str | None = None
        self._name: str | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, job: Job, name: str = "job") -> None:
        """Kick off a job in the background.

        Raises:
            JobBusy: A job is already in progress.
        """
        with self._lock:
            if self.is_running:
                raise JobBusy(f"A job is already running: {self._name}")
            self._progress = {"phase": "running"}
            self._result = None
            self._error = None
            self._name = name
            thread = threading.Thread(
                target=self._run,
                args=(job,),
                name=name,
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _on_progress(self, progress: dict[str, Any]) -> None:
        # A job may mutate one dict as it goes; copy it so the status endpoint
        # never reads a half-updated snapshot.
        with self._lock:
            self._progress = dict(progress)

    def _run(self, job: Job) -> None:
        try:
            result = job(self._on_progress)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._progress = {**self._progress, "phase": "error"}
            _logger.exception("Job %r crashed", self._name)
        else:
            with self._lock:
                self._result = result
                self._progress = {**self._progress, "phase": "done"}

    def status(self) -> dict[str, Any]:
        """A JSON-ready snapshot for the polled status endpoint."""
        with self._lock:
            return {
                "running": self.is_running,
                "job": self._name,
                "progress": dict(self._progress),
                "error": self._error,
                "result": self._result,
            }
