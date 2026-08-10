"""Footage pipeline — macOS footage ingest/backup tooling.

This package is deliberately layered:

* :mod:`footage_pipeline.config`        — persisted settings (local JSON file)
* :mod:`footage_pipeline.logging_setup` — shared logging helpers
* :mod:`footage_pipeline.backup`        — UI-agnostic copy/verify/manifest engine
* :mod:`footage_pipeline.web`           — FastAPI layer that drives the engine

Nothing below :mod:`footage_pipeline.web` may import from it; the engine has no
knowledge of how it is being driven.
"""

__version__ = "0.1.0"
