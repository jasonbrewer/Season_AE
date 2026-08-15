# Season Assistant Editor

macOS desktop companion to DaVinci Resolve Studio: a local FastAPI server with
a browser UI that automates the repetitive post-production plumbing for a video
series.

This repository is currently a **skeleton**. The backup/copy feature that made
up the first slice has been removed — the operator does all copying, backup,
and folder creation by hand. What remains is the reusable infrastructure the
next slices build on: settings, logging, the native folder picker, the
background job runner, and the web shell.

`docs/SPEC.md` is the product spec. It is marked **STALE** — it still
describes the removed backup feature and is being rewritten. Do not implement
from it. `CLAUDE.md` holds the working conventions.

## Install

Requires macOS and Python 3.11+ (only the native folder picker needs macOS;
everything else is platform-neutral).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`pyproject.toml` is the single dependency source. Runtime: `fastapi`,
`uvicorn`, `xxhash`. The `dev` extra adds `pytest` and `httpx` (needed by
`fastapi.testclient`).

## Run

```bash
footage-pipeline-web            # or: python -m footage_pipeline.web.app
```

Then open <http://127.0.0.1:8000>. The server must run **on the Mac with the
drives attached** — the folder dialog is drawn by the machine running the
backend.

## Test

```bash
pytest
```

## Layout

```
src/footage_pipeline/
    config.py           persisted settings (local JSON file — no database)
    logging_setup.py    shared logging + per-run log file handler
    native_picker.py    native macOS "choose folder" dialog via osascript
    web/
        app.py          FastAPI app + endpoints
        runner.py       one-at-a-time background job runner with progress
        static/         frontend (plain HTML/CSS/JS, no build step)
tests/
```

Nothing below `web/` may import from it.

### Why the picker is a backend endpoint

Browsers deliberately do not expose absolute filesystem paths (`<input
webkitdirectory>` gives you relative paths and file handles, not
`/Volumes/CARD_A`). This pipeline needs the real path, so the backend opens
the system dialog with `osascript -e 'choose folder'` and returns the absolute
POSIX path.

## Settings file

Persistence is a single local JSON file — there is no database in this
project.

```
~/Library/Application Support/FootagePipeline/settings.json
```

Override the location with `FOOTAGE_PIPELINE_SETTINGS` (the tests use this so
they never touch your real settings).

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/settings` | Persisted settings + picker availability |
| `POST` | `/api/settings` | Persist the stored root path |
| `POST` | `/api/pick-folder` | Open the native dialog, return an absolute path |
