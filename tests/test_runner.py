"""Tests for the background job runner.

The runner is deliberately job-agnostic, so these use trivial fake jobs: a
short sleep that reports progress, and a job that blocks until released.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from footage_pipeline.web.runner import JobBusy, JobRunner, ProgressCallback

#: Generous ceiling; the fake jobs finish in milliseconds.
TIMEOUT_SECONDS = 10
STEPS = 5


def wait_until_done(runner: JobRunner) -> dict[str, Any]:
    """Poll the status snapshot the way the frontend would, until the job ends."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = runner.status()
        if not status["running"]:
            return status
        time.sleep(0.01)
    raise AssertionError("job did not finish within the timeout")


def counting_job(on_progress: ProgressCallback) -> dict[str, Any]:
    """Report ``STEPS`` increments, then return a result."""
    for step in range(1, STEPS + 1):
        time.sleep(0.005)
        on_progress({"phase": "running", "done": step, "total": STEPS})
    return {"steps": STEPS}


def test_a_job_runs_to_completion_and_status_reflects_it() -> None:
    runner = JobRunner()
    assert runner.status()["running"] is False
    assert runner.status()["progress"]["phase"] == "idle"

    runner.start(counting_job, name="counting")

    status = wait_until_done(runner)
    assert status["job"] == "counting"
    assert status["error"] is None
    assert status["result"] == {"steps": STEPS}
    assert status["progress"]["phase"] == "done"
    assert status["progress"]["done"] == STEPS


def test_a_failing_job_is_reported_as_an_error() -> None:
    runner = JobRunner()

    def exploding_job(on_progress: ProgressCallback) -> None:
        raise ValueError("no good")

    runner.start(exploding_job, name="explode")

    status = wait_until_done(runner)
    assert status["result"] is None
    assert status["error"] == "ValueError: no good"
    assert status["progress"]["phase"] == "error"


def test_a_second_job_is_refused_while_one_is_running() -> None:
    runner = JobRunner()
    release = threading.Event()

    def blocking_job(on_progress: ProgressCallback) -> str:
        release.wait(TIMEOUT_SECONDS)
        return "released"

    runner.start(blocking_job, name="first")
    try:
        assert runner.is_running is True
        with pytest.raises(JobBusy):
            runner.start(counting_job, name="second")
    finally:
        release.set()

    status = wait_until_done(runner)
    # The refused job never displaced the running one.
    assert status["job"] == "first"
    assert status["result"] == "released"

    # Once it is done, the runner accepts the next job.
    runner.start(counting_job, name="third")
    assert wait_until_done(runner)["job"] == "third"


def test_progress_is_monotonic() -> None:
    runner = JobRunner()
    runner.start(counting_job, name="counting")

    seen = []
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = runner.status()
        seen.append(status["progress"].get("done", 0))
        if not status["running"]:
            break
        time.sleep(0.001)

    assert seen[-1] == STEPS
    assert seen == sorted(seen), f"progress went backwards: {seen}"
