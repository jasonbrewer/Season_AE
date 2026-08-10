"""Tests for the backup engine (T1–T5 from the spec, plus invariants).

Every test drives :func:`footage_pipeline.backup.run_backup` directly — the
same entry point the web layer uses — so what is verified here is exactly what
the UI runs.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from footage_pipeline.backup import (
    FileStatus,
    PreflightError,
    hash_file,
    read_manifest,
    run_backup,
)
from footage_pipeline.backup.manifest import MANIFEST_DIR_NAME


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def snapshot_tree(root: Path) -> dict[str, tuple[int, str, int]]:
    """Map relative path -> (size, xxHash64, mtime_ns) for every real file."""
    snapshot: dict[str, tuple[int, str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            rel = entry.relative_to(root).as_posix()
            stat = entry.stat()
            snapshot[rel] = (stat.st_size, hash_file(entry), stat.st_mtime_ns)
    return snapshot


def statuses(result) -> dict[str, str]:
    """Map relative path -> status from a run's manifest."""
    return {row.relative_path: row.status for row in result.manifest.files}


def rows_with(result, status: str) -> list:
    return [row for row in result.manifest.files if row.status == status]


def file_rows(result) -> list:
    """Manifest rows for real files (symlink rows excluded)."""
    return [
        row for row in result.manifest.files if row.status != FileStatus.SYMLINK_SKIPPED
    ]


# --------------------------------------------------------------------------
# T1 — fresh backup
# --------------------------------------------------------------------------


def test_t1_fresh_backup_copies_and_verifies_everything(
    source_tree: Path, backup_root: Path
) -> None:
    """T1: empty destination -> every file COPIED and verified, overall PASS."""
    before = snapshot_tree(source_tree)

    result = run_backup(source_tree, backup_root)

    assert result.overall == "PASS"
    assert result.manifest.totals["failed"] == 0
    assert result.manifest.totals["conflicts"] == 0
    assert result.manifest.totals["skipped"] == 0
    assert result.manifest.totals["copied"] == len(before)

    # Every file row is COPIED, and source/dest hashes agree on every one.
    for row in file_rows(result):
        assert row.status == FileStatus.COPIED, row
        assert row.source_hash == row.dest_hash != None  # noqa: E711

    # Straight mirror: dest = backup_root + source-relative path, byte-for-byte.
    after_dest = snapshot_tree(backup_root)
    mirrored = {
        rel: value
        for rel, value in after_dest.items()
        if not rel.startswith(f"{MANIFEST_DIR_NAME}/")
    }
    assert set(mirrored) == set(before)
    for rel, (size, digest, mtime_ns) in before.items():
        dest_size, dest_digest, dest_mtime_ns = mirrored[rel]
        assert (dest_size, dest_digest) == (size, digest), rel
        # copystat semantics: modification time is preserved.
        assert dest_mtime_ns == mtime_ns, rel

    # Empty source folders are mirrored too.
    assert (backup_root / "A003_empty_card").is_dir()
    assert (backup_root / "audio" / "unused").is_dir()

    # Symlinks are skipped, not followed, and recorded in the report.
    symlink_rows = rows_with(result, FileStatus.SYMLINK_SKIPPED)
    assert [row.relative_path for row in symlink_rows] == ["A001/link_to_clip.MOV"]
    assert not (backup_root / "A001" / "link_to_clip.MOV").exists()
    assert result.manifest.totals["symlinks_skipped"] == 1

    # The manifest on disk says the same thing, and the log exists.
    manifest = read_manifest(Path(result.manifest_path))
    assert manifest.overall == "PASS"
    assert manifest.hash_algorithm == "xxhash64"
    assert len(manifest.files) == len(result.manifest.files)
    assert Path(result.log_path).stat().st_size > 0
    assert Path(result.manifest_path).parent.parent.name == MANIFEST_DIR_NAME

    # The source is untouched.
    assert snapshot_tree(source_tree) == before


# --------------------------------------------------------------------------
# T2 — incremental re-run
# --------------------------------------------------------------------------


def test_t2_immediate_rerun_skips_everything(source_tree: Path, backup_root: Path) -> None:
    """T2: re-running immediately skips every file and copies nothing."""
    first = run_backup(source_tree, backup_root)
    assert first.overall == "PASS"
    dest_after_first = snapshot_tree(backup_root)

    second = run_backup(source_tree, backup_root)

    assert second.overall == "PASS"
    assert second.manifest.totals["copied"] == 0
    assert second.manifest.totals["copied_bytes"] == 0
    assert second.manifest.totals["conflicts"] == 0
    assert second.manifest.totals["failed"] == 0
    assert second.manifest.totals["skipped"] == first.manifest.totals["copied"]
    assert all(row.status == FileStatus.SKIPPED for row in file_rows(second))

    # Skips are decided by hashing, so both hashes are recorded and equal.
    for row in file_rows(second):
        assert row.source_hash == row.dest_hash != None  # noqa: E711

    # The second run wrote its own manifest and touched no mirrored file.
    assert second.manifest_path != first.manifest_path
    mirrored_before = {
        k: v for k, v in dest_after_first.items() if not k.startswith(f"{MANIFEST_DIR_NAME}/")
    }
    mirrored_after = {
        k: v
        for k, v in snapshot_tree(backup_root).items()
        if not k.startswith(f"{MANIFEST_DIR_NAME}/")
    }
    assert mirrored_after == mirrored_before


# --------------------------------------------------------------------------
# T3 — conflict
# --------------------------------------------------------------------------


def test_t3_modified_destination_is_a_conflict_and_is_left_untouched(
    source_tree: Path, backup_root: Path
) -> None:
    """T3: a changed destination file is flagged CONFLICT and never rewritten."""
    first = run_backup(source_tree, backup_root)
    assert first.overall == "PASS"

    victim_rel = "A002/day two/notes.txt"
    victim = backup_root / victim_rel
    victim.write_bytes(b"someone edited the backup copy by hand\n")
    tampered = snapshot_tree(backup_root)[victim_rel]
    source_before = snapshot_tree(source_tree)

    result = run_backup(source_tree, backup_root)

    assert result.overall == "FAIL"
    assert result.manifest.totals["conflicts"] == 1
    assert result.manifest.totals["copied"] == 0
    assert result.manifest.totals["failed"] == 0

    by_path = statuses(result)
    assert by_path[victim_rel] == FileStatus.CONFLICT
    others = [
        status
        for path, status in by_path.items()
        if path != victim_rel and status != FileStatus.SYMLINK_SKIPPED
    ]
    assert others and all(status == FileStatus.SKIPPED for status in others)

    # The conflicting destination file is byte-for-byte as the test left it.
    assert snapshot_tree(backup_root)[victim_rel] == tampered
    assert victim.read_bytes() == b"someone edited the backup copy by hand\n"

    # The conflict row records both sides, and they differ.
    row = rows_with(result, FileStatus.CONFLICT)[0]
    assert row.source_hash and row.dest_hash and row.source_hash != row.dest_hash
    assert row.source_hash == hash_file(source_tree / victim_rel)
    assert row.dest_hash == hash_file(victim)

    # And the source was not touched to "resolve" anything.
    assert snapshot_tree(source_tree) == source_before


# --------------------------------------------------------------------------
# T4 — write failure
# --------------------------------------------------------------------------


def test_t4_write_failure_is_recorded_and_the_run_continues(
    source_tree: Path, backup_root: Path
) -> None:
    """T4: a destination that cannot be written fails that file only.

    The failure is injected by putting a regular file where the engine needs a
    directory (``<backup_root>/A001``), which makes every write under that
    subtree raise ``FileExistsError``. Unlike a permission bit, this reproduces
    for any user — including root, which ignores mode bits.
    """
    blocked = backup_root / "A001"
    blocked.write_bytes(b"not a directory\n")
    source_before = snapshot_tree(source_tree)

    result = run_backup(source_tree, backup_root)

    assert result.overall == "FAIL"
    failed = rows_with(result, FileStatus.FAILED)
    assert failed, "expected the blocked subtree to produce failures"
    assert all(row.relative_path.startswith("A001/") for row in failed)
    assert all(row.error for row in failed)

    # Everything outside the blocked subtree was still processed and verified.
    copied = rows_with(result, FileStatus.COPIED)
    assert copied, "the rest of the tree must still be backed up"
    assert all(not row.relative_path.startswith("A001/") for row in copied)
    assert (backup_root / "audio" / "scene_01.wav").is_file()
    assert hash_file(backup_root / "audio" / "scene_01.wav") == hash_file(
        source_tree / "audio" / "scene_01.wav"
    )

    # The failures are in the manifest and the log, not just in memory.
    manifest = read_manifest(Path(result.manifest_path))
    assert manifest.overall == "FAIL"
    assert manifest.totals["failed"] == len(failed)
    log_text = Path(result.log_path).read_text(encoding="utf-8")
    assert "FAILED" in log_text

    # No partial fragments were left behind, and the source is untouched.
    assert blocked.read_bytes() == b"not a directory\n"
    assert snapshot_tree(source_tree) == source_before


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_t4_readonly_destination_folder_is_recorded(
    source_tree: Path, backup_root: Path
) -> None:
    """T4 (permissions flavour): a read-only destination subfolder fails cleanly."""
    readonly = backup_root / "A001"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        result = run_backup(source_tree, backup_root)
    finally:
        os.chmod(readonly, 0o700)

    assert result.overall == "FAIL"
    failed = rows_with(result, FileStatus.FAILED)
    assert failed and all(row.relative_path.startswith("A001/") for row in failed)
    assert rows_with(result, FileStatus.COPIED)


def test_failed_copy_leaves_no_partial_file_so_a_rerun_recovers(
    source_tree: Path, backup_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that dies mid-stream must not leave a fragment behind.

    A fragment would look like a modified destination on the next run and turn
    into a permanent CONFLICT, which would break resumption.
    """
    from footage_pipeline.backup import core

    target_rel = "audio/scene_01.wav"
    real_copy = core.copy_and_hash

    def exploding_copy(src, dst, chunk_size=core.DEFAULT_CHUNK_SIZE, on_chunk=None):
        if Path(src).name == "scene_01.wav":
            # Create the destination the way a real copy would, then die.
            with open(dst, "xb") as handle:
                handle.write(b"partial")
            raise OSError(5, "Input/output error")
        return real_copy(src, dst, chunk_size, on_chunk)

    monkeypatch.setattr(core, "copy_and_hash", exploding_copy)

    first = run_backup(source_tree, backup_root)
    assert first.overall == "FAIL"
    assert [row.relative_path for row in rows_with(first, FileStatus.FAILED)] == [target_rel]
    assert not (backup_root / target_rel).exists()

    monkeypatch.undo()

    second = run_backup(source_tree, backup_root)
    assert second.overall == "PASS"
    assert statuses(second)[target_rel] == FileStatus.COPIED
    assert hash_file(backup_root / target_rel) == hash_file(source_tree / target_rel)


# --------------------------------------------------------------------------
# T5 — pre-flight free space
# --------------------------------------------------------------------------


def test_t5_insufficient_free_space_refuses_to_start(
    source_tree: Path, backup_root: Path
) -> None:
    """T5: not enough room at the destination -> refuse, and write nothing."""
    with pytest.raises(PreflightError) as excinfo:
        run_backup(source_tree, backup_root, free_space_fn=lambda path: 1024)

    message = str(excinfo.value)
    assert "free space" in message.lower()
    assert str(backup_root) in message

    # Refusing means refusing: no manifest directory, no copied files.
    assert list(backup_root.iterdir()) == []


def test_preflight_allows_a_run_that_exactly_fits(
    source_tree: Path, backup_root: Path
) -> None:
    """The free-space check is a shortfall check, not an off-by-one gate."""
    from footage_pipeline.backup import plan_backup

    plan = plan_backup(source_tree, backup_root)
    result = run_backup(
        source_tree, backup_root, free_space_fn=lambda path: plan.planned_copy_bytes
    )
    assert result.overall == "PASS"


# --------------------------------------------------------------------------
# Path / safety invariants
# --------------------------------------------------------------------------


def test_backup_root_inside_source_is_refused(source_tree: Path, tmp_path: Path) -> None:
    """A destination nested in the source would copy the backup into itself."""
    nested = source_tree / "nested_backup"
    nested.mkdir(exist_ok=True)
    try:
        with pytest.raises(PreflightError, match="inside the source"):
            run_backup(source_tree, nested)
    finally:
        shutil.rmtree(nested)


def test_missing_backup_root_is_refused(source_tree: Path, tmp_path: Path) -> None:
    """An unmounted drive must produce a clear refusal, not a half-run."""
    with pytest.raises(PreflightError, match="does not exist"):
        run_backup(source_tree, tmp_path / "not_mounted")


def test_progress_callback_reports_monotonic_completion(
    source_tree: Path, backup_root: Path
) -> None:
    """The UI's progress feed is well-formed: monotonic and ending complete."""
    seen: list[tuple[int, int, str]] = []

    def record(progress) -> None:
        seen.append((progress.files_done, progress.bytes_done, progress.phase))

    result = run_backup(source_tree, backup_root, progress_cb=record, progress_interval=0)

    assert seen
    assert [item[0] for item in seen] == sorted(item[0] for item in seen)
    assert [item[1] for item in seen] == sorted(item[1] for item in seen)
    assert seen[-1][2] == "done"
    assert result.progress.files_done == result.manifest.totals["total_files"]
    assert result.progress.bytes_done == result.manifest.totals["total_bytes"]
    assert result.progress.total_bytes >= 50 * 1024 * 1024  # the large file is real
