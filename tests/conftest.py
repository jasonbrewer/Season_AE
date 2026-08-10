"""Shared fixtures.

The source tree is expensive to build (it contains a 50 MiB file), so it is
made once per session and treated as read-only by every test — which is also a
direct check that the engine never modifies its source.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from make_test_tree import make_test_tree  # noqa: E402

#: Size of the single large file in the generated tree, in MiB.
LARGE_FILE_MB = 50


@pytest.fixture(scope="session")
def source_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A nested dummy footage tree, generated once for the whole session."""
    root = tmp_path_factory.mktemp("source") / "CARD_A"
    return make_test_tree(root, large_mb=LARGE_FILE_MB, seed=99)


@pytest.fixture
def backup_root(tmp_path: Path) -> Path:
    """An empty destination root, fresh for each test."""
    root = tmp_path / "backup"
    root.mkdir()
    return root
