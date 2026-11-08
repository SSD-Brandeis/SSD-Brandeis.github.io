#!/usr/bin/env python3
"""
_fetch_artifacts.py — mirror artifact folders from ~/Dropbox/_control/ to assets/.

~/Dropbox/_control/ is the ground truth. After each run, the destination folders
are identical to the source folders: new and updated files are copied, and files
present in assets/ but absent in _control/ are deleted.

Folder pairs synced:
  ~/Dropbox/_control/fulltext/   →  assets/fulltext/
  ~/Dropbox/_control/img/        →  assets/img/
  ~/Dropbox/_control/resources/  →  assets/resources/

Stdlib only — no external packages required.
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
CONTROL_DIR = Path.home() / "Dropbox" / "_control"

FOLDER_PAIRS = [
    (CONTROL_DIR / "fulltext",  REPO_ROOT / "assets" / "fulltext"),
    (CONTROL_DIR / "img",       REPO_ROOT / "assets" / "img"),
    (CONTROL_DIR / "resources", REPO_ROOT / "assets" / "resources"),
]

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_folders(src: Path, dst: Path) -> tuple[int, int, int]:
    """Mirror src → dst recursively. Returns (copied, deleted, unchanged)."""
    dst.mkdir(parents=True, exist_ok=True)
    copied = deleted = unchanged = 0

    # Copy new or updated files from src to dst.
    for src_path in src.rglob("*"):
        rel = src_path.relative_to(src)
        dst_path = dst / rel
        if src_path.is_dir():
            dst_path.mkdir(parents=True, exist_ok=True)
        else:
            if dst_path.exists() and filecmp.cmp(src_path, dst_path, shallow=False):
                unchanged += 1
            else:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)
                copied += 1

    # Delete files and directories in dst not present in src.
    # Sort so that deeper paths come first, and at equal depth files before dirs,
    # ensuring a directory is empty before rmdir() is attempted.
    dst_items = sorted(
        dst.rglob("*"),
        key=lambda p: (-len(p.parts), p.is_dir()),
    )
    for dst_path in dst_items:
        rel = dst_path.relative_to(dst)
        if not (src / rel).exists():
            if dst_path.is_file() or dst_path.is_symlink():
                dst_path.unlink()
                deleted += 1
            elif dst_path.is_dir():
                try:
                    dst_path.rmdir()
                    deleted += 1
                except OSError:
                    pass  # non-empty; contents will be removed in the same pass

    return copied, deleted, unchanged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    any_change = False
    for src, dst in FOLDER_PAIRS:
        if not src.exists():
            print(f"  WARN: _fetch_artifacts: source not found, skipping: {src}",
                  file=sys.stderr)
            continue
        copied, deleted, unchanged = sync_folders(src, dst)
        if copied or deleted:
            any_change = True
            print(f"  artifacts/{src.name}/: "
                  f"{copied} copied, {deleted} deleted, {unchanged} unchanged",
                  file=sys.stderr)
    if not any_change:
        print("  artifacts: already up to date", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
