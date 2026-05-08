"""Decrypt odin .bin files from a firmware squashfs extraction.

Both ODJ (UDS diagnostic object) and DEJ (compact CAN message database)
files share the same envelope:

    [16-byte random salt][Fernet token]

Key derivation (from odin/platforms/binary_metadata_utils.py):
    password = base64.b64decode(C)   # hardcoded in firmware
    key = urlsafe_b64encode(
        PBKDF2HMAC(SHA256, 32, salt, 123456).derive(password)
    )
    plaintext = pickle.loads(Fernet(key).decrypt(token))

Usage:
    python decode_bin.py <firmware_data_dir>
    python decode_bin.py <firmware_data_dir> \\
        --odj-dir data/odj --compact data/Model3_ETH.compact.json

Defaults to the paths in config.py (TM3_ODJ_DIR / TM3_ETH_COMPACT).
"""

from __future__ import annotations

import argparse
import base64
import json
import pickle
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import os

import config  # type: ignore[import-untyped]


def _get_password() -> bytes:
    val = os.environ.get("TM3_BIN_KEY")
    if not val:
        raise RuntimeError(
            "TM3_BIN_KEY is not set. "
            "Extract it from a firmware squashfs at "
            "opt/odin/odin (PyInstaller bundle) → "
            "odin/platforms/binary_metadata_utils.py constant C, "
            "then add TM3_BIN_KEY=<value> to your .env file."
        )
    return base64.b64decode(val.encode())


def _get_key(salt: bytes) -> bytes:
    password = _get_password()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=123456,
        backend=default_backend(),
    )
    return base64.urlsafe_b64encode(kdf.derive(password))


def decode_bin(path: Path) -> object:
    data = path.read_bytes()
    salt = data[:16]
    token = data[16:]
    key = _get_key(salt)
    plaintext = Fernet(key).decrypt(token)
    return pickle.loads(plaintext)


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
        stem = path.name.removesuffix(".bin")  # keeps .odj extension
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
