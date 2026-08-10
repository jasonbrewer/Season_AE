"""UI-agnostic copy + verify + manifest engine.

This module knows nothing about FastAPI, HTTP, or any UI. It exposes two entry
points:

* :func:`plan_backup`  — scan + pre-flight (what would happen, and does it fit?)
* :func:`run_backup`   — do it, writing a manifest and a human-readable log

Rules enforced here (see README for the full statement):

* **Straight mirror.** ``dest = backup_root / <path relative to source_root>``.
* **Everything is mirrored** — dotfiles, sidecars, every extension, empty
  directories. Symlinks are skipped (never followed) and recorded.
* **Hashing decides**, never a prior manifest: destination absent -> copy;
  present with an equal xxHash64 -> SKIPPED; present with a different one ->
  CONFLICT, left byte-for-byte untouched.
* **Non-destructive.** The source is opened read-only. Destination files are
  created with ``O_EXCL``, so an existing destination file can never be
  overwritten or truncated by this code, and nothing pre-existing is ever
  deleted. The single ``unlink`` in this module removes only a partial file
  that this run itself created and failed to finish (see
  :func:`_discard_partial_copy`), which is what makes a re-run resumable.
* **Streaming.** Files are read in chunks; whole files are never held in
  memory.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import xxhash

from ..logging_setup import get_logger, run_log_handler
from .manifest import (
    MANIFEST_FILENAME,
    RUN_LOG_FILENAME,
    FileRecord,
    FileStatus,
    RunManifest,
    format_report,
    manifest_run_dir,
    write_manifest,
)

#: Read/write chunk size. Large enough to keep spinning disks busy, small
#: enough that a 500 GB file costs 4 MB of RAM rather than 500 GB.
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024

HASH_ALGORITHM = "xxhash64"

_logger = get_logger("backup")


class PreflightError(Exception):
    """Raised when a run must not start (bad paths, not enough free space)."""


@dataclass(frozen=True)
class SourceFile:
    """A regular file discovered under the source root."""

    relative_path: str
    size: int


@dataclass(frozen=True)
class SourceSymlink:
    """A symlink discovered under the source root; skipped, never followed."""

    relative_path: str
    target: str
    is_dir: bool


@dataclass
class Progress:
    """A snapshot of run progress, safe to serialise straight to a UI."""

    phase: str = "idle"  # idle | scanning | preflight | running | done
    total_files: int = 0
    files_done: int = 0
    total_bytes: int = 0
    bytes_done: int = 0
    copied_bytes: int = 0
    current_file: str = ""
    copied: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    symlinks_skipped: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


ProgressCallback = Callable[[Progress], None]
FreeSpaceFn = Callable[[Path], int]


@dataclass
class BackupPlan:
    """Result of scan + pre-flight."""

    source_root: str
    backup_root: str
    files: list[SourceFile] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    symlinks: list[SourceSymlink] = field(default_factory=list)
    total_bytes: int = 0
    #: Conservative estimate of bytes to write: files whose destination is
    #: absent, or present at a different size. Files whose destination already
    #: matches in size are excluded (they are the SKIP candidates). Size is a
    #: cheap proxy here; the real decision is always made by hashing during the
    #: run. Over-estimating is the safe direction for a free-space check.
    planned_copy_bytes: int = 0
    free_bytes: int = 0

    @property
    def fits(self) -> bool:
        return self.free_bytes >= self.planned_copy_bytes

    def to_dict(self) -> dict:
        return {
            "source_root": self.source_root,
            "backup_root": self.backup_root,
            "total_files": len(self.files),
            "total_directories": len(self.directories),
            "total_symlinks": len(self.symlinks),
            "total_bytes": self.total_bytes,
            "planned_copy_bytes": self.planned_copy_bytes,
            "free_bytes": self.free_bytes,
            "fits": self.fits,
        }


@dataclass
class BackupResult:
    """Everything a caller (UI, later pipeline stage) needs after a run."""

    overall: str
    manifest: RunManifest
    manifest_path: str
    log_path: str
    progress: Progress

    @property
    def passed(self) -> bool:
        return self.overall == "PASS"

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "manifest_path": self.manifest_path,
            "log_path": self.log_path,
            "totals": self.manifest.totals,
            "source_root": self.manifest.source_root,
            "backup_root": self.manifest.backup_root,
            "started_at": self.manifest.started_at,
            "finished_at": self.manifest.finished_at,
            "message": self.manifest.message,
            "files": [record.to_dict() for record in self.manifest.files],
        }


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def scan_source(source_root: Path) -> tuple[list[SourceFile], list[str], list[SourceSymlink]]:
    """Walk ``source_root`` without following symlinks.

    Returns ``(files, directories, symlinks)`` where every path is relative to
    ``source_root`` and uses POSIX separators. Directories are returned so that
    empty ones can still be mirrored.
    """
    source_root = Path(source_root)
    files: list[SourceFile] = []
    directories: list[str] = []
    symlinks: list[SourceSymlink] = []

    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        current = Path(dirpath)
        # Sorting keeps runs deterministic, which makes manifests diffable.
        dirnames.sort()
        filenames.sort()

        kept_dirs: list[str] = []
        for name in dirnames:
            entry = current / name
            rel = entry.relative_to(source_root).as_posix()
            if entry.is_symlink():
                symlinks.append(SourceSymlink(rel, _read_link(entry), is_dir=True))
                continue  # dropped from dirnames -> os.walk will not descend
            kept_dirs.append(name)
            directories.append(rel)
        dirnames[:] = kept_dirs

        for name in filenames:
            entry = current / name
            rel = entry.relative_to(source_root).as_posix()
            if entry.is_symlink():
                symlinks.append(SourceSymlink(rel, _read_link(entry), is_dir=False))
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                # Unreadable stat is a per-file problem, not a scan-killer; the
                # run records the failure when it tries to process the file.
                size = 0
            files.append(SourceFile(rel, size))

    return files, directories, symlinks


def _read_link(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return "<unreadable>"


# --------------------------------------------------------------------------
# Hashing / copying primitives
# --------------------------------------------------------------------------


def hash_file(
    path: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_chunk: Callable[[int], None] | None = None,
) -> str:
    """Stream ``path`` and return its xxHash64 hex digest."""
    digest = xxhash.xxh64()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
            if on_chunk is not None:
                on_chunk(len(block))
    return digest.hexdigest()


def copy_and_hash(
    src: Path,
    dst: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_chunk: Callable[[int], None] | None = None,
) -> str:
    """Copy ``src`` to ``dst`` in one pass, returning the source's xxHash64.

    ``dst`` is created with ``O_EXCL``: if anything already exists at that path
    the copy fails rather than overwriting it. Modification time and mode are
    preserved with ``copystat`` once the bytes are down.
    """
    digest = xxhash.xxh64()
    # "xb" == O_CREAT | O_EXCL — the guarantee that we never clobber. If this
    # open succeeds, the file at dst is ours and nothing pre-existing is at
    # risk, which is what makes the cleanup below safe.
    with open(src, "rb") as reader, open(dst, "xb") as writer:
        try:
            while True:
                block = reader.read(chunk_size)
                if not block:
                    break
                writer.write(block)
                digest.update(block)
                if on_chunk is not None:
                    on_chunk(len(block))
            writer.flush()
            os.fsync(writer.fileno())
        except BaseException:
            # A disk that fills or disconnects mid-stream must not leave a
            # truncated file behind; see _discard_partial_copy.
            _discard_partial_copy(dst)
            raise
    shutil.copystat(src, dst, follow_symlinks=False)
    return digest.hexdigest()


def _discard_partial_copy(dst: Path) -> None:
    """Remove a file this run created but failed to finish or verify.

    This is the only removal in the engine. It runs exclusively on a path that
    ``copy_and_hash`` just created via ``O_EXCL`` in this run — never on
    pre-existing destination data. Leaving the fragment behind would turn every
    future run into a permanent CONFLICT on that path and break resumption.
    """
    try:
        os.unlink(dst)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Planning / pre-flight
# --------------------------------------------------------------------------


def _default_free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def _validate_roots(source_root: Path, backup_root: Path) -> tuple[Path, Path]:
    source_root = Path(source_root).expanduser()
    backup_root = Path(backup_root).expanduser()

    if not source_root.exists():
        raise PreflightError(f"Source folder does not exist: {source_root}")
    if not source_root.is_dir():
        raise PreflightError(f"Source is not a folder: {source_root}")
    if not backup_root.exists():
        raise PreflightError(
            f"Backup root does not exist: {backup_root}. "
            "Set it in Settings (is the drive mounted?)."
        )
    if not backup_root.is_dir():
        raise PreflightError(f"Backup root is not a folder: {backup_root}")

    source_root = source_root.resolve()
    backup_root = backup_root.resolve()

    if source_root == backup_root:
        raise PreflightError("Source and backup root are the same folder.")
    if backup_root.is_relative_to(source_root):
        raise PreflightError(
            "Backup root is inside the source folder — that would copy the "
            "backup into itself. Choose a backup root outside the source."
        )
    if source_root.is_relative_to(backup_root):
        raise PreflightError(
            "Source folder is inside the backup root. Choose a source outside "
            "the backup root."
        )
    return source_root, backup_root


def plan_backup(
    source_root: Path,
    backup_root: Path,
    free_space_fn: FreeSpaceFn | None = None,
) -> BackupPlan:
    """Scan the source and pre-flight the run. Raises :class:`PreflightError`
    for unusable paths; free-space shortfall is reported via ``plan.fits``."""
    source_root, backup_root = _validate_roots(source_root, backup_root)
    files, directories, symlinks = scan_source(source_root)

    total_bytes = sum(item.size for item in files)
    planned = 0
    for item in files:
        dest = backup_root / item.relative_path
        try:
            dest_size = dest.stat().st_size
        except OSError:
            planned += item.size  # absent (or unstattable) -> assume a copy
            continue
        if dest_size != item.size:
            planned += item.size

    free_fn = free_space_fn or _default_free_space
    return BackupPlan(
        source_root=str(source_root),
        backup_root=str(backup_root),
        files=files,
        directories=directories,
        symlinks=symlinks,
        total_bytes=total_bytes,
        planned_copy_bytes=planned,
        free_bytes=free_fn(backup_root),
    )


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _process_file(
    item: SourceFile,
    source_root: Path,
    backup_root: Path,
    chunk_size: int,
    on_chunk: Callable[[int], None],
) -> FileRecord:
    """Classify and handle one file. Never raises for per-file problems."""
    src = source_root / item.relative_path
    dst = backup_root / item.relative_path

    try:
        if dst.exists() or dst.is_symlink():
            # Destination present: the decision is made by hashing both sides,
            # right now. No prior manifest is consulted.
            source_hash = hash_file(src, chunk_size, on_chunk)
            dest_hash = hash_file(dst, chunk_size)
            if source_hash == dest_hash:
                return FileRecord(
                    relative_path=item.relative_path,
                    size=item.size,
                    status=FileStatus.SKIPPED,
                    source_hash=source_hash,
                    dest_hash=dest_hash,
                )
            return FileRecord(
                relative_path=item.relative_path,
                size=item.size,
                status=FileStatus.CONFLICT,
                source_hash=source_hash,
                dest_hash=dest_hash,
                error="Destination exists with different content; left untouched.",
            )

        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_hash = copy_and_hash(src, dst, chunk_size, on_chunk)
            dest_hash = hash_file(dst, chunk_size)
        except OSError:
            # We only reach this branch when dst did not exist, so anything at
            # dst now was created by this attempt and is safe to clear away.
            if dst.exists():
                _discard_partial_copy(dst)
            raise

        if source_hash != dest_hash:
            # Verified failure: what landed on disk is not what we read.
            _discard_partial_copy(dst)
            return FileRecord(
                relative_path=item.relative_path,
                size=item.size,
                status=FileStatus.FAILED,
                source_hash=source_hash,
                dest_hash=dest_hash,
                error="Verification failed: destination hash does not match source.",
            )

        return FileRecord(
            relative_path=item.relative_path,
            size=item.size,
            status=FileStatus.COPIED,
            source_hash=source_hash,
            dest_hash=dest_hash,
        )

    except OSError as exc:
        return FileRecord(
            relative_path=item.relative_path,
            size=item.size,
            status=FileStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_backup(
    source_root: Path,
    backup_root: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress_cb: ProgressCallback | None = None,
    free_space_fn: FreeSpaceFn | None = None,
    progress_interval: float = 0.25,
) -> BackupResult:
    """Mirror ``source_root`` into ``backup_root``, verifying every file.

    Args:
        source_root: Folder chosen for this run. Never modified.
        backup_root: Persisted destination root. Nothing here is ever
            overwritten or deleted.
        chunk_size: Streaming chunk size in bytes.
        progress_cb: Called with a :class:`Progress` snapshot as work advances
            (throttled to ``progress_interval`` seconds, plus once per file).
        free_space_fn: Override for free-space lookup (used by tests).
        progress_interval: Minimum seconds between throttled callbacks.

    Returns:
        A :class:`BackupResult` whose ``overall`` is ``"PASS"`` only when there
        were no conflicts and no failures.

    Raises:
        PreflightError: Unusable paths, or not enough free space at the
            destination. Nothing is written when this is raised.
    """
    started = datetime.now(timezone.utc)
    progress = Progress(phase="scanning")

    last_emit = 0.0

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        if progress_cb is None:
            return
        now = time.monotonic()
        if force or (now - last_emit) >= progress_interval:
            last_emit = now
            progress_cb(progress)

    emit(force=True)

    plan = plan_backup(source_root, backup_root, free_space_fn=free_space_fn)
    resolved_source = Path(plan.source_root)
    resolved_backup = Path(plan.backup_root)

    progress.phase = "preflight"
    progress.total_files = len(plan.files)
    progress.total_bytes = plan.total_bytes
    emit(force=True)

    if not plan.fits:
        raise PreflightError(
            "Not enough free space at the backup root. "
            f"Need {_human_bytes(plan.planned_copy_bytes)}, "
            f"{_human_bytes(plan.free_bytes)} available at {resolved_backup}."
        )

    timestamp = started.astimezone().strftime("%Y%m%dT%H%M%S")
    run_dir = manifest_run_dir(resolved_backup, timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / MANIFEST_FILENAME
    log_path = run_dir / RUN_LOG_FILENAME

    manifest = RunManifest(
        source_root=str(resolved_source),
        backup_root=str(resolved_backup),
        started_at=started.isoformat(),
        hash_algorithm=HASH_ALGORITHM,
        chunk_size=chunk_size,
    )

    with run_log_handler(log_path) as logger:
        logger.info("Backup started")
        logger.info("Source: %s", resolved_source)
        logger.info("Dest:   %s", resolved_backup)
        logger.info(
            "Scanned %d files (%s), %d directories, %d symlinks; "
            "planned copy %s, free %s",
            len(plan.files),
            _human_bytes(plan.total_bytes),
            len(plan.directories),
            len(plan.symlinks),
            _human_bytes(plan.planned_copy_bytes),
            _human_bytes(plan.free_bytes),
        )

        progress.phase = "running"
        emit(force=True)

        # Mirror the tree shape first so empty source folders survive the trip.
        for rel in plan.directories:
            try:
                (resolved_backup / rel).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("Could not create directory %s: %s", rel, exc)

        for link in plan.symlinks:
            manifest.files.append(
                FileRecord(
                    relative_path=link.relative_path,
                    size=0,
                    status=FileStatus.SYMLINK_SKIPPED,
                    link_target=link.target,
                    error="Symlink skipped (not followed).",
                )
            )
            progress.symlinks_skipped += 1
            logger.info("SYMLINK_SKIPPED %s -> %s", link.relative_path, link.target)
        emit(force=True)

        def on_chunk(count: int) -> None:
            progress.bytes_done += count
            emit()

        for item in plan.files:
            progress.current_file = item.relative_path
            emit(force=True)
            record = _process_file(
                item, resolved_source, resolved_backup, chunk_size, on_chunk
            )
            manifest.files.append(record)

            if record.status == FileStatus.COPIED:
                progress.copied += 1
                progress.copied_bytes += item.size
                logger.info("COPIED   %s (%s)", record.relative_path, _human_bytes(item.size))
            elif record.status == FileStatus.SKIPPED:
                progress.skipped += 1
                logger.info("SKIPPED  %s (already backed up)", record.relative_path)
            elif record.status == FileStatus.CONFLICT:
                progress.conflicts += 1
                logger.warning(
                    "CONFLICT %s (source %s != dest %s) — destination left untouched",
                    record.relative_path,
                    record.source_hash,
                    record.dest_hash,
                )
            else:
                progress.failed += 1
                logger.error("FAILED   %s — %s", record.relative_path, record.error)

            progress.files_done += 1
            emit(force=True)

        finished = datetime.now(timezone.utc)
        overall = "PASS" if (progress.conflicts == 0 and progress.failed == 0) else "FAIL"
        manifest.finished_at = finished.isoformat()
        manifest.overall = overall
        manifest.totals = {
            "total_files": len(plan.files),
            "total_bytes": plan.total_bytes,
            "total_directories": len(plan.directories),
            "copied": progress.copied,
            "copied_bytes": progress.copied_bytes,
            "skipped": progress.skipped,
            "conflicts": progress.conflicts,
            "failed": progress.failed,
            "symlinks_skipped": progress.symlinks_skipped,
            "duration_seconds": round((finished - started).total_seconds(), 3),
        }

        write_manifest(manifest_path, manifest)
        progress.phase = "done"
        progress.current_file = ""
        emit(force=True)

        logger.info("Manifest: %s", manifest_path)
        for line in format_report(manifest).splitlines():
            logger.info(line)

    return BackupResult(
        overall=overall,
        manifest=manifest,
        manifest_path=str(manifest_path),
        log_path=str(log_path),
        progress=progress,
    )


def _human_bytes(count: int) -> str:
    """Format a byte count for logs and UI messages."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"
