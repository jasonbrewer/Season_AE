# Footage Pipeline — Backup stage

First slice of a macOS footage pipeline. This slice does one job, thoroughly:
**mirror a chosen source folder to a persisted backup root, verifying every
byte with xxHash64, and never touching anything it shouldn't.**

No DaVinci Resolve integration, no external APIs, no database — later slices
build on the modules established here.

---

## Install

Requires macOS and Python 3.11+ (the engine and tests are platform-neutral;
only the native folder picker needs macOS).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Dependencies are pinned in `pyproject.toml`: `fastapi`, `uvicorn`, `xxhash`,
plus the standard library. `pytest` and `httpx` are the dev extras.

## Run

```bash
footage-pipeline-web            # or: python -m footage_pipeline.web.app
```

Then open <http://127.0.0.1:8000>.

The server must run **on the Mac with the drives attached** — the folder
dialog is drawn by the machine running the backend.

1. **Destination** — click *Set / change…* and pick your backup root. It is
   saved and reused for every future run.
2. **Source** — click *Choose folder…* each run. The last-used source is
   pre-filled for convenience but is always re-chosen explicitly.
3. **Start backup** — live progress (files, bytes, current file), then a
   report card with copied / skipped / conflicts / failures, the overall
   PASS/FAIL, and the manifest path.

## Test

```bash
python -m pytest
```

Generate a dummy tree by hand if you want to poke at it manually:

```bash
python scripts/make_test_tree.py /tmp/footage-src --large-mb 50 --clean
```

---

## Behaviour

### Layout: a straight mirror

```
dest_path = backup_root / <file's path relative to the chosen source root>
```

Nothing else. The source tree is reproduced exactly, including empty folders.

Everything is mirrored — dotfiles, sidecars, every extension. There is no
extension filtering anywhere in the code.

### Verification is real

Every file is hashed with **xxHash64**, streamed in 4 MB chunks; a whole file
is never held in memory (these are video files). On a copy, the source is
hashed *while* it is being written (single pass), then the destination is read
back and hashed, and the two are compared. Modification times and mode are
preserved (`copystat`).

### Skip / conflict decisions come from hashing

Decisions are made by hashing both sides at run time. A prior manifest is a
record of what happened, never an input:

| Destination state | Outcome |
| --- | --- |
| Absent | **COPIED** — copy, then verify by re-reading and hashing |
| Present, hash matches source | **SKIPPED** — already backed up |
| Present, hash differs | **CONFLICT** — left untouched, recorded, run continues |

### Non-destructive by construction

- The source is opened **read-only**. Nothing writes to it.
- Destination files are created with `O_EXCL`, so an existing destination file
  cannot be overwritten or truncated by this code — the syscall refuses.
- Nothing pre-existing at the destination is ever deleted.
- Conflicts are **flagged only**; resolving them is a human decision.

There is exactly one `unlink` in the engine (`_discard_partial_copy`). It runs
only on a file that *this run* just created via `O_EXCL` and then failed to
finish or verify. Without it, an interrupted copy would leave a truncated file
that every later run would report as a permanent CONFLICT, and re-running
would never resume.

### Symlinks

Skipped, never followed (so link loops cannot hang a run), and recorded in the
manifest and the report with their target.

### Pre-flight

Before any writing: paths are validated (source exists; backup root exists and
is reachable; neither root nested inside the other), the tree is scanned, and
bytes-to-copy is checked against free space at the destination. Insufficient
space refuses the run with a clear message and writes nothing.

The bytes-to-copy estimate counts files whose destination is absent or a
different size — size is a cheap proxy used only for the space check, and it
errs high. The real classification is always the hash.

### Failure handling

A per-file error (I/O, permissions, disconnect, verify mismatch) is recorded
and the run **continues** with the remaining files, ending FAIL. Because
classification is incremental and hash-based, simply re-running resumes: the
files that succeeded are SKIPPED and the failures are retried.

### Manifest and log

One directory per run:

```
<backup_root>/_backup_manifests/<timestamp>/
    manifest.json    run metadata, totals, and a row per file
    backup.log       human-readable log of the same run
```

Each manifest row records the relative path, size, source hash, destination
hash, status (`COPIED` / `SKIPPED` / `CONFLICT` / `FAILED` /
`SYMLINK_SKIPPED`), and any error. A run is **PASS** only with zero conflicts
and zero failures.

---

## Layout

```
src/footage_pipeline/
    config.py           persisted settings (local JSON file — no database)
    logging_setup.py    shared logging + per-run log file handler
    native_picker.py    native macOS "choose folder" dialog via osascript
    backup/
        core.py         UI-agnostic copy + verify + manifest engine
        manifest.py     manifest read/write and per-file records
    web/
        app.py          FastAPI app + endpoints
        runner.py       background-thread wrapper around the engine
        static/         frontend (plain HTML/CSS/JS)
scripts/make_test_tree.py
tests/
```

`backup/core.py` is **UI-agnostic**: it imports nothing from `web/`, knows
nothing about HTTP, and reports progress through a plain callback. The web
layer calls `run_backup()` and renders what it returns — no copy, verify, or
manifest logic is duplicated there. Later pipeline stages reuse the engine the
same way.

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

```json
{
  "backup_root": "/Volumes/Archive/Footage",
  "last_source": "/Volumes/CARD_A"
}
```

Override the location with `FOOTAGE_PIPELINE_SETTINGS` (the tests use this so
they never touch your real settings).

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/settings` | Backup root, last source, picker availability |
| `POST` | `/api/settings` | Persist the backup root |
| `POST` | `/api/pick-folder` | Open the native dialog, return an absolute path |
| `POST` | `/api/backup/plan` | Scan + pre-flight without copying |
| `POST` | `/api/backup/start` | Start a run on a background thread |
| `GET` | `/api/backup/status` | Poll progress; carries the report once done |
