"""Decrypt odin .bin files (ODJ and compact CAN DB) from a firmware extraction.

This build ships no decryption recipe. Decryption is delegated to a provider
exposing ``decrypt_bin(data: bytes, key: str) -> object``; ``key`` is
``TM3_BIN_KEY``. Resolution: ``TM3_BIN_PROVIDER`` (importable module name or
``.py`` path), else a local gitignored ``bin_decrypt_impl`` module, else fail
closed. See docs/SECURITY_PROVIDER.md. Plain ``.json`` files need neither.

Usage:
    python decode_bin.py <firmware_data_dir>
    python decode_bin.py <firmware_data_dir> \\
        --odj-dir data/odj --compact data/Model3_ETH.compact.json

Defaults to the paths in config.py (TM3_ODJ_DIR / TM3_ETH_COMPACT).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path

import config  # type: ignore[import-untyped]

_PROVIDER_ENV = "TM3_BIN_PROVIDER"
_LOCAL = "bin_decrypt_impl"  # gitignored drop-in
_KEY_ENV = "TM3_BIN_KEY"
_DOC = "docs/SECURITY_PROVIDER.md"

_provider_cache: object | None = None


def _load_provider():
    """Return the .bin decrypt provider (cached). Fails closed with RuntimeError."""
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache
    spec = os.environ.get(_PROVIDER_ENV)
    if not spec:
        try:
            spec = importlib.util.find_spec(_LOCAL) and _LOCAL
        except ImportError:
            spec = None
    if not spec:
        raise RuntimeError(
            f"No .bin decrypt provider configured. This build ships no decryption recipe; "
            f"set {_PROVIDER_ENV} to a module exposing decrypt_bin(data, key). See {_DOC}."
        )
    path = Path(spec).expanduser()
    try:
        if path.suffix == ".py" or path.exists():
            if not path.is_file():
                raise ImportError(f"no such file: {path}")
            mspec = importlib.util.spec_from_file_location("bin_decrypt_provider", path)
            mod = importlib.util.module_from_spec(mspec)
            mspec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(spec)
    except ImportError as exc:
        raise RuntimeError(f"{_PROVIDER_ENV}={spec!r} could not be loaded ({exc}). See {_DOC}.") from exc
    if hasattr(mod, "get_bin_decrypt_provider"):
        mod = mod.get_bin_decrypt_provider()
    if not callable(getattr(mod, "decrypt_bin", None)):
        raise RuntimeError(f".bin decrypt provider is missing decrypt_bin(). See {_DOC}.")
    _provider_cache = mod
    return mod


def decode_bin(path: Path) -> object:
    key = os.environ.get(_KEY_ENV)
    if not key:
        raise RuntimeError(f"{_KEY_ENV} is not set (see .env.example).")
    return _load_provider().decrypt_bin(path.read_bytes(), key)


def load_json(path: Path) -> dict:
    """Load a JSON or encrypted .bin file, decrypting automatically if needed."""
    if path.suffix == ".bin":
        return decode_bin(path)
    with open(path) as f:
        return json.load(f)


def _write_json(obj: object, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def decode_all(src_dir: Path, odj_dir: Path, compact_out: Path) -> None:
    src_dir = src_dir.expanduser().resolve()

    # DEJ: compact.json.bin -> single output file
    dej_bins = list(src_dir.rglob("*.compact.json.bin"))
    for path in dej_bins:
        print(f"DEJ  {path.name} ...", end=" ", flush=True)
        try:
            obj = decode_bin(path)
            _write_json(obj, compact_out)
            print(f"-> {compact_out}")
        except Exception as e:
            print(f"FAILED: {e}")

    # ODJ: <name>.odj.bin -> odj_dir/<name>.odj
    odj_bins = list(src_dir.rglob("*.odj.bin"))
    for path in sorted(odj_bins):
        stem = path.name.removesuffix(".bin")
        dest = odj_dir / stem
        print(f"ODJ  {path.name} ...", end=" ", flush=True)
        try:
            obj = decode_bin(path)
            _write_json(obj, dest)
            print(f"-> {dest}")
        except Exception as e:
            print(f"FAILED: {e}")

    total = len(dej_bins) + len(odj_bins)
    print(f"\n{total} file(s) processed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "src_dir",
        help="Root of the firmware data directory (searched recursively for *.bin)",
    )
    parser.add_argument(
        "--odj-dir",
        type=Path,
        default=config.ODJ_DIR,
        help="Output directory for .odj files",
    )
    parser.add_argument(
        "--compact",
        type=Path,
        default=config.ETH_COMPACT,
        help="Output path for compact.json",
    )
    args = parser.parse_args()

    decode_all(Path(args.src_dir), args.odj_dir, args.compact)


if __name__ == "__main__":
    main()
