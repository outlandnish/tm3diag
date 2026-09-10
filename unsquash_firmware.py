#!/usr/bin/env python3
"""Unsquash a Tesla firmware image and expand its nested .dirsquashed parts.

A Tesla firmware blob is a squashfs filesystem. Inside the root it ships further
squashfs images named with their mount path flattened into the filename: dots are
path separators and %2E is a literal dot (as the on-car bin/mount-all-dirsquashed
mounts them). This extracts the top-level image, then recursively finds
*.dirsquashed under the root and unsquashfs each into its decoded path until none
remain.

The original download is deleted after a successful extraction; --keep-download
retains it.

Usage:
  python unsquash_firmware.py <firmware_file> [--name NAME] [--out DIR]
                              [--keep-squashed] [--keep-download]

Example:
  python unsquash_firmware.py ~/Downloads/2024.44.ssq --name 2024.44.x.ice
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"command failed (rc={rc}): {cmd[0]}")


def decode_mount_path(filename: str) -> str:
    """`<dotted>.dirsquashed` -> relative mount path.

    Strip the .dirsquashed suffix, '.' -> '/', then unescape %2E -> '.'.
    """
    stem = filename[:-len(".dirsquashed")] if filename.endswith(".dirsquashed") \
        else filename
    return stem.replace(".", "/").replace("%2E", ".")


def unsquash(image: Path, dest: Path) -> None:
    """unsquashfs `image` into `dest` (dest/ becomes the squashfs root)."""
    dest.mkdir(parents=True, exist_ok=True)
    # -f: overwrite dest, -d: target dir, -no-xattrs: skip security xattrs.
    _run(["unsquashfs", "-f", "-no-xattrs", "-d", str(dest), str(image)])


def expand_dirsquashed(root: Path, keep: bool) -> int:
    """Find every *.dirsquashed under root, extract to its decoded path.

    Returns the number expanded this pass.
    """
    count = 0
    for sq in sorted(root.rglob("*.dirsquashed")):
        if not sq.is_file():
            continue
        rel = decode_mount_path(sq.name)
        target = root / rel
        print(f"  {sq.relative_to(root)}  ->  {rel}/")
        tmp = sq.with_name(sq.name + ".extract_tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        unsquash(sq, tmp)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(tmp), str(target))
        if not keep:
            sq.unlink()
        count += 1
    return count


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware_file", type=Path, help="Downloaded squashfs blob")
    ap.add_argument("--name", default=None,
                    help="Clean name for the extraction dir (default: file stem)")
    ap.add_argument("--out", type=Path, default=Path("~/dev/tesla-fw").expanduser(),
                    help="Parent dir for the extraction (default: ~/dev/tesla-fw)")
    ap.add_argument("--keep-squashed", action="store_true",
                    help="Keep the .dirsquashed files after extracting them")
    ap.add_argument("--keep-download", action="store_true",
                    help="Keep the original downloaded blob (default: delete it "
                         "after a successful extraction to save space)")
    args = ap.parse_args()

    src = args.firmware_file.expanduser()
    if not src.is_file():
        sys.exit(f"not a file: {src}")

    name = args.name or src.stem
    root = args.out.expanduser() / name

    print(f"[1/2] unsquashfs top-level -> {root}")
    unsquash(src, root)

    print("[2/2] expanding nested *.dirsquashed (recursive)")
    total = 0
    while True:
        n = expand_dirsquashed(root, args.keep_squashed)
        total += n
        if n == 0:
            break

    if not args.keep_download:
        print(f"  removing original download: {src}")
        src.unlink()

    print(f"\nDone. Expanded {total} dirsquashed part(s).")
    print(f"Firmware root: {root}")


if __name__ == "__main__":
    main()
