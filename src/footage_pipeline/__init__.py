"""Season Assistant Editor — macOS post-production tooling.

This package is deliberately layered:

* :mod:`footage_pipeline.config`        — persisted settings (local JSON file)
* :mod:`footage_pipeline.logging_setup` — shared logging helpers
* :mod:`footage_pipeline.native_picker` — native macOS folder dialog
* :mod:`footage_pipeline.web`           — FastAPI layer that drives the engines

Nothing below :mod:`footage_pipeline.web` may import from it; an engine has no
knowledge of how it is being driven.
"""

__version__ = "0.1.0"
