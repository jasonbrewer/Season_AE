"""Shared fixtures.

The only shared setup left is making ``src/`` importable, so the tests run
against the working tree whether or not the package is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
