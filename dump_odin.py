#!/usr/bin/env python3
"""Extract and decompile the odin PyInstaller binary from a firmware root.

odin (opt/odin/odin) is a PyInstaller-frozen ELF. The CAN signal/interface
definitions, the .bin decryption constant, and the alert-name hash recipe all
live inside it -- the loose JSON (compact DB, bus-alerts-map) is derived and
redactable, so the binary is the authoritative source.

Pipeline:
  1. pyinstxtractor.py   ELF -> <out>/extracted/  (incl. out00-PYZ.pyz_extracted)
  2. detect the bundled Python version from the PYZ / .pyc magic
  3. decompile every .pyc -> <out>/src/  (pycdc; uncompyle6 fallback for <=3.8)

Builds differ: older odin is Python 3.6, newer is 3.10. pycdc spans both;
uncompyle6 only reaches ~3.8, so it is a fallback for the old builds only.

The .bin decryption key (TM3_BIN_KEY in .env) is the base64 constant ``C`` in
    <out>/src/.../odin/platforms/binary_metadata_utils.py
Decode of ``binary_metadata_reader`` there: salt = first 16 bytes of the file,
key = PBKDF2-HMAC-SHA256(base64decode(C), salt, 123456 iters, 32 bytes) then
Fernet-decrypt + ``pickle.loads``. To validate a build / refresh the key:
    grep -rn "^C = " <out>/src/.../odin/platforms/binary_metadata_utils.py
and compare against TM3_BIN_KEY in .env (see .env.example).

Usage:
  python dump_odin.py <firmware_root> [--out DIR] [--tools DIR] [--no-decompile]

Example:
  python dump_odin.py /home/outlandnish/dev/tesla-fw/2026.8.3.ice \\
      --out ~/dev/odin-dumps/2026.8.3
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path

_DEFAULT_TOOLS = Path("~/dev/tools").expanduser()

# CPython bytecode magic (first 2 bytes of a .pyc) -> python version label.
# Enough to pick a decompiler; extend as new builds appear.
_MAGIC = {
    3360: "3.6", 3361: "3.6", 3377: "3.6", 3379: "3.6",
    3390: "3.7", 3391: "3.7", 3392: "3.7", 3393: "3.7", 3394: "3.7",
    3400: "3.8", 3401: "3.8", 3410: "3.8", 3411: "3.8", 3412: "3.8", 3413: "3.8",
    3420: "3.9", 3421: "3.9", 3422: "3.9", 3423: "3.9", 3424: "3.9", 3425: "3.9",
    3430: "3.10", 3431: "3.10", 3432: "3.10", 3433: "3.10", 3434: "3.10", 3435: "3.10",
    3436: "3.10", 3437: "3.10", 3438: "3.10", 3439: "3.10",
    3450: "3.11", 3451: "3.11", 3452: "3.11", 3453: "3.11", 3454: "3.11", 3455: "3.11",
    3531: "3.12", 3571: "3.13",
}


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, cwd=cwd).returncode


def _has_docker() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _resolve_extractor(pyver: str | None) -> tuple[str, str]:
    """Choose how to run pyinstxtractor under the target's Python version.

    pyinstxtractor must unmarshal the PYZ under the SAME major.minor as the
    frozen build, or it silently skips PYZ extraction. Resolution order:
      1. host interpreter, if it already matches
      2. a uv-managed interpreter (`uv python install <ver>`)
      3. a Docker `python:<ver>-slim` image (covers EOL/arch-missing versions
         like 3.6 on aarch64, which uv can't provide)

    Returns (kind, ref): ("local", interpreter_path) or ("docker", image_tag).
    """
    if not pyver:
        return ("local", sys.executable)

    cur = f"{sys.version_info.major}.{sys.version_info.minor}"
    if cur == pyver:
        return ("local", sys.executable)

    uv = Path("~/.local/bin/uv").expanduser()
    if uv.exists():
        find = subprocess.run([str(uv), "python", "find", pyver],
                              capture_output=True, text=True)
        if find.returncode == 0 and find.stdout.strip():
            return ("local", find.stdout.strip())

    if _has_docker():
        return ("docker", f"python:{pyver}-slim")

    print(f"  ! no Python {pyver} via host/uv and no Docker; using {cur} — "
          f"PYZ may not unmarshal. Try: uv python install {pyver}")
    return ("local", sys.executable)


def _detect_pyver(extracted: Path) -> str | None:
    """Read the magic from any .pyc under the extraction to label the version."""
    for pyc in extracted.rglob("*.pyc"):
        try:
            with open(pyc, "rb") as f:
                magic = struct.unpack("<H", f.read(2))[0]
        except Exception:
            continue
        if magic in _MAGIC:
            return _MAGIC[magic]
    return None


def _pyver_from_libpython(odin_dir: Path) -> str | None:
    """Detect the frozen Python version from the shipped libpythonX.Y.so.

    The firmware ships opt/odin/libpython3.XX.so.1.0 alongside the binary; its
    name is the authoritative interpreter version and lets us pick the matching
    extractor BEFORE extraction (the magic-byte detection only works after).
    """
    for so in odin_dir.glob("libpython3.*.so*"):
        # libpython3.10.so.1.0 / libpython3.6m.so.1.0
        part = so.name.split("libpython")[1].split(".so")[0]
        major_minor = part.rstrip("m")  # strip 3.6's 'm' ABI suffix
        if major_minor.startswith("3."):
            return major_minor
    return None


def extract(odin_bin: Path, out: Path, tools: Path,
            extractor: tuple[str, str]) -> Path:
    pyinstx = tools / "pyinstxtractor.py"
    if not pyinstx.exists():
        sys.exit(f"pyinstxtractor.py not found at {pyinstx} "
                 "(fetch it into --tools first)")
    out.mkdir(parents=True, exist_ok=True)
    extracted = out / "extracted"
    extracted.mkdir(exist_ok=True)

    kind, ref = extractor
    if kind == "docker":
        # Mount the odin binary, the extractor script, and the output dir; run
        # pyinstxtractor inside the container with the output dir as cwd so the
        # <name>_extracted tree lands on the host. -u for live output.
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{odin_bin}:/in/odin:ro",
            "-v", f"{pyinstx}:/in/pyinstxtractor.py:ro",
            "-v", f"{extracted}:/out",
            "-w", "/out",
            ref,
            "python", "-u", "/in/pyinstxtractor.py", "/in/odin",
        ]
    else:
        # pyinstxtractor writes <name>_extracted in cwd; run it inside
        # `extracted`, under an interpreter matching the frozen build.
        cmd = [ref, str(pyinstx), str(odin_bin)]

    rc = _run(cmd, cwd=None if kind == "docker" else extracted)
    if rc != 0:
        sys.exit(f"pyinstxtractor failed (rc={rc})")
    return extracted


def _pycdc_path(tools: Path) -> Path | None:
    p = tools / "pycdc" / "pycdc"
    return p if p.exists() else None


def decompile(extracted: Path, out: Path, pyver: str | None, tools: Path) -> None:
    src = out / "src"
    src.mkdir(parents=True, exist_ok=True)
    pycdc = _pycdc_path(tools)

    use_uncompyle = False
    if pycdc is None:
        if pyver and tuple(int(x) for x in pyver.split(".")) <= (3, 8):
            use_uncompyle = True
            print("  pycdc not built; falling back to uncompyle6 (py<=3.8 only)")
        else:
            sys.exit(
                f"pycdc not found at {tools/'pycdc'/'pycdc'} and python {pyver} "
                "is out of uncompyle6 range. Build pycdc:\n"
                "  sudo apt-get install -y cmake\n"
                "  cd ~/dev/tools/pycdc && cmake . && make -j$(nproc)")

    pycs = sorted(extracted.rglob("*.pyc"))
    print(f"  decompiling {len(pycs)} .pyc -> {src}")
    ok = fail = 0
    for pyc in pycs:
        rel = pyc.relative_to(extracted).with_suffix(".py")
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if use_uncompyle:
                res = subprocess.run(["uncompyle6", str(pyc)],
                                     capture_output=True, text=True)
                out_text = res.stdout
            else:
                res = subprocess.run([str(pycdc), str(pyc)],
                                     capture_output=True, text=True)
                out_text = res.stdout
            if out_text.strip():
                dst.write_text(out_text)
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"    ! {pyc.name}: {e}")
            fail += 1
    print(f"  decompiled: {ok} ok, {fail} empty/failed")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware_root", type=Path,
                    help="Squashfs root of a firmware extraction")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: ~/dev/odin-dumps/<root name>)")
    ap.add_argument("--tools", type=Path, default=_DEFAULT_TOOLS,
                    help="Dir holding pyinstxtractor.py and pycdc/ (default: ~/dev/tools)")
    ap.add_argument("--no-decompile", action="store_true",
                    help="Only extract the PyInstaller archive; skip decompile")
    args = ap.parse_args()

    root = args.firmware_root.expanduser()
    odin_bin = root / "opt/odin/odin"
    if not odin_bin.exists():
        sys.exit(f"odin binary not found at {odin_bin}")

    out = args.out.expanduser() if args.out else \
        Path("~/dev/odin-dumps").expanduser() / root.name
    tools = args.tools.expanduser()

    # Detect the frozen Python version up front (from libpython) so extraction
    # runs under a matching interpreter — otherwise pyinstxtractor skips the PYZ.
    pyver = _pyver_from_libpython(odin_bin.parent)
    extractor = _resolve_extractor(pyver)
    print(f"[1/3] extracting {odin_bin}")
    print(f"  target python: {pyver or 'unknown'}  "
          f"extractor: {extractor[0]}:{extractor[1]}")
    extracted = extract(odin_bin, out, tools, extractor)

    print("[2/3] confirming bundled Python version (from .pyc magic)")
    pyver = _detect_pyver(extracted) or pyver
    print(f"  python: {pyver or 'unknown'}")

    if args.no_decompile:
        print("[3/3] skipped (--no-decompile)")
    else:
        print("[3/3] decompiling")
        decompile(extracted, out, pyver, tools)

    print(f"\nDone. Output under {out}")
    print("Grep the decompiled tree for signal/interface defs, e.g.:")
    print(f"  grep -rl 'accelPedal\\|DIR_torque\\|vcPcsDCDC' {out}/src")
    print("The .bin decryption key (TM3_BIN_KEY in .env) is constant C in:")
    print(f"  grep -rn '^C = ' {out}/src/*/odin/platforms/binary_metadata_utils.py")


if __name__ == "__main__":
    main()
