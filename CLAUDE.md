# Season Assistant Editor — working conventions

This file is conventions only. **The product spec is `docs/SPEC.md`. Read it
before any feature work.** (It is currently marked STALE — it describes a
backup feature that has been removed, and a rewrite is in progress. Do not
implement from it until it is rewritten.)

## Branching

- Branch off `main`. Never commit to `main`, and never merge.
- Deliver work as a pull request. The operator reviews and merges.

## The no-delete rule

**The running application never deletes anything.** Not media files, not
folders, and nothing inside the DaVinci Resolve project — no bins, clips, or
timelines. The app reads the Resolve project and adds to it; it never removes
from it. If a required bin is missing, the app stops and names it rather than
creating or altering anything.

This governs **runtime behavior only**. It does not apply to source files
during development: deleting code, tests, or docs in a PR is ordinary work.

## Storage

No database. No ORM, no migrations, no `.sqlite` file. Settings are a single
local JSON file (`src/footage_pipeline/config.py`).

## Layering

Stated in `src/footage_pipeline/__init__.py` and enforced by review:
**nothing below `web/` may import from it.** The engines have no knowledge of
how they are being driven; `web/` is a thin FastAPI wrapper over them.

## Running the tests

```
pip install -e ".[dev]"
pytest
```

`pyproject.toml` is the single dependency source — the `dev` extra carries the
test-only packages. Do not add `requirements.txt`, `requirements-dev.txt`,
`tox.ini`, or a second pytest config; pytest options live in
`[tool.pytest.ini_options]`.
