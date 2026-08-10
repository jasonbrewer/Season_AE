#!/usr/bin/env python3
"""Generate a nested dummy folder tree for exercising the backup stage.

The tree deliberately includes the things that break naive copiers: nested
folders, dotfiles, sidecar files, an empty folder, a file with spaces and
unicode in its name, a zero-byte file, and one large file that must never be
read into memory all at once.

Usage::

    python scripts/make_test_tree.py /tmp/footage-src
    python scripts/make_test_tree.py /tmp/footage-src --large-mb 50 --seed 7
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
from pathlib import Path

#: (relative path, size in bytes) for the small fixed files.
SMALL_FILES: tuple[tuple[str, int], ...] = (
    ("A001/CLIP_0001.MOV", 512 * 1024),
    ("A001/CLIP_0001.MOV.xmp", 3 * 1024),
    ("A001/CLIP_0002.MOV", 256 * 1024),
    ("A001/.DS_Store", 6 * 1024),
    ("A001/proxies/CLIP_0001_proxy.mp4", 128 * 1024),
    ("A002/day two/CLIP 0003 – take 2.mov", 700 * 1024),
    ("A002/day two/notes.txt", 1 * 1024),
    ("A002/.hidden_sidecar", 512),
    ("audio/scene_01.wav", 1024 * 1024),
    ("audio/scene_01.wav.md5", 64),
    ("empty.txt", 0),
    ("README.md", 2 * 1024),
)

#: Folders created but left empty (they must still be mirrored).
EMPTY_DIRS: tuple[str, ...] = ("A003_empty_card", "audio/unused")

LARGE_FILE = "A001/BIGCLIP_0001.MOV"

CHUNK = 1024 * 1024


def write_random_file(path: Path, size: int, rng: random.Random) -> None:
    """Write ``size`` pseudo-random bytes, streaming a chunk at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    remaining = size
    with open(path, "wb") as handle:
        while remaining > 0:
            block = min(CHUNK, remaining)
            handle.write(rng.randbytes(block))
            remaining -= block


def make_test_tree(
    root: Path,
    large_mb: int = 50,
    seed: int = 1234,
    include_symlink: bool = True,
    clean: bool = False,
) -> Path:
    """Create the dummy tree under ``root`` and return the root path.

    Args:
        root: Directory to create (parents are created as needed).
        large_mb: Size of the single large file, in MiB.
        seed: Seed for reproducible content.
        include_symlink: Also create a symlink, which the backup must skip.
        clean: Remove ``root`` first if it already exists.
    """
    root = Path(root).expanduser()
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    for relative, size in SMALL_FILES:
        write_random_file(root / relative, size, rng)

    for relative in EMPTY_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)

    write_random_file(root / LARGE_FILE, large_mb * 1024 * 1024, rng)

    if include_symlink:
        link = root / "A001" / "link_to_clip.MOV"
        if not link.exists() and not link.is_symlink():
            os.symlink(root / "A001" / "CLIP_0001.MOV", link)

    return root


def describe(root: Path) -> str:
    """Summarise a generated tree for the console."""
    files = 0
    links = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            if (Path(dirpath) / name).is_symlink():
                links += 1
                dirnames.remove(name)
        for name in filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                links += 1
                continue
            files += 1
            total += entry.stat().st_size
    return f"{root}: {files} files, {links} symlinks, {total / (1024 * 1024):.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="Directory to create the tree in")
    parser.add_argument("--large-mb", type=int, default=50, help="Size of the big file in MiB")
    parser.add_argument("--seed", type=int, default=1234, help="Seed for reproducible content")
    parser.add_argument("--no-symlink", action="store_true", help="Skip the symlink")
    parser.add_argument("--clean", action="store_true", help="Delete the root first")
    args = parser.parse_args(argv)

    root = make_test_tree(
        Path(args.root),
        large_mb=args.large_mb,
        seed=args.seed,
        include_symlink=not args.no_symlink,
        clean=args.clean,
    )
    print(describe(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
