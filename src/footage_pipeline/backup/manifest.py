"""Manifest read/write.

One JSON manifest is written per run under
``<backup_root>/_backup_manifests/<timestamp>/manifest.json``.

The manifest is a *record* of what happened, never an input to a later run's
decisions: skip/conflict classification is always redone by hashing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_DIR_NAME = "_backup_manifests"
MANIFEST_FILENAME = "manifest.json"
RUN_LOG_FILENAME = "backup.log"
MANIFEST_SCHEMA_VERSION = 1


class FileStatus:
    """Per-file outcomes recorded in the manifest."""

    COPIED = "COPIED"
    """Destination was absent; file was copied and hash-verified."""

    SKIPPED = "SKIPPED"
    """Destination was present and its hash matched the source."""

    CONFLICT = "CONFLICT"
    """Destination was present with a different hash; left untouched."""

    FAILED = "FAILED"
    """An error occurred (I/O, permissions, or a post-copy verify mismatch)."""

    SYMLINK_SKIPPED = "SYMLINK_SKIPPED"
    """A symlink was found in the source; not followed, not copied."""


#: Statuses that make an overall run FAIL.
FAILING_STATUSES = frozenset({FileStatus.CONFLICT, FileStatus.FAILED})


@dataclass
class FileRecord:
    """One row of the manifest."""

    relative_path: str
    size: int
    status: str
    source_hash: str | None = None
    dest_hash: str | None = None
    error: str | None = None
    #: Symlink rows only: the raw link target, for the report.
    link_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class RunManifest:
    """Run metadata plus every per-file row."""

    source_root: str
    backup_root: str
    started_at: str
    finished_at: str | None = None
    overall: str = "FAIL"
    hash_algorithm: str = "xxhash64"
    chunk_size: int = 0
    schema_version: int = MANIFEST_SCHEMA_VERSION
    totals: dict[str, Any] = field(default_factory=dict)
    files: list[FileRecord] = field(default_factory=list)
    #: Set when the run refused to start (e.g. insufficient free space).
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = [record.to_dict() for record in self.files]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunManifest":
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload["files"] = [FileRecord.from_dict(row) for row in data.get("files", [])]
        return cls(**payload)


def write_manifest(path: Path, manifest: RunManifest) -> Path:
    """Serialise ``manifest`` to ``path`` as pretty-printed JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def read_manifest(path: Path) -> RunManifest:
    """Load a manifest previously written by :func:`write_manifest`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunManifest.from_dict(data)


def manifest_run_dir(backup_root: Path, timestamp: str) -> Path:
    """Return (and disambiguate) the manifest directory for a run."""
    base = Path(backup_root) / MANIFEST_DIR_NAME
    candidate = base / timestamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = base / f"{timestamp}-{suffix}"
    return candidate


def format_report(manifest: RunManifest) -> str:
    """Render a short human-readable summary of a run."""
    totals = manifest.totals
    lines = [
        f"Overall: {manifest.overall}",
        f"Source:  {manifest.source_root}",
        f"Dest:    {manifest.backup_root}",
        f"Files:   {totals.get('total_files', 0)} "
        f"(copied {totals.get('copied', 0)}, skipped {totals.get('skipped', 0)}, "
        f"conflicts {totals.get('conflicts', 0)}, failures {totals.get('failed', 0)}, "
        f"symlinks skipped {totals.get('symlinks_skipped', 0)})",
        f"Bytes:   copied {totals.get('copied_bytes', 0)} of "
        f"{totals.get('total_bytes', 0)} scanned",
    ]
    if manifest.message:
        lines.append(f"Note:    {manifest.message}")
    return "\n".join(lines)
