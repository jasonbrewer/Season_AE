"""UI-agnostic backup engine: copy, verify, manifest.

Import from here rather than reaching into submodules::

    from footage_pipeline.backup import run_backup, PreflightError
"""

from .core import (
    DEFAULT_CHUNK_SIZE,
    BackupPlan,
    BackupResult,
    PreflightError,
    Progress,
    copy_and_hash,
    hash_file,
    plan_backup,
    run_backup,
    scan_source,
)
from .manifest import (
    MANIFEST_DIR_NAME,
    FileRecord,
    FileStatus,
    RunManifest,
    read_manifest,
    write_manifest,
)

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MANIFEST_DIR_NAME",
    "BackupPlan",
    "BackupResult",
    "FileRecord",
    "FileStatus",
    "PreflightError",
    "Progress",
    "RunManifest",
    "copy_and_hash",
    "hash_file",
    "plan_backup",
    "read_manifest",
    "run_backup",
    "scan_source",
    "write_manifest",
]
