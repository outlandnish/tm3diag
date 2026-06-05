"""Dotenv-based config loader for tm3diag CLI tools.

Reads .env from the project root. CLI args always override .env values.
Copy .env.example to .env and edit to suit your setup.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).parent

load_dotenv(_PROJECT_DIR / ".env")

# ---------------------------------------------------------------------------
# Data paths — resolved once at import time
# ---------------------------------------------------------------------------

def _resolve(env_key: str, default: Path | None) -> Path | None:
    val = os.environ.get(env_key)
    return Path(val).expanduser() if val else default

# TM3_ROOT points to the squashfs-root of a firmware extraction.
# Individual vars override the derived paths when set explicitly.
_ROOT: Path | None = _resolve("TM3_ROOT", None)

# Product selects which odin data tree under opt/odin/data/ to use.
# Newer firmware ships multiple products (e.g. Model3, ModelY).
PRODUCT: str = os.environ.get("TM3_PRODUCT", "Model3")

_DATA_DIR  = _ROOT / "opt/odin/data" / PRODUCT if _ROOT else None
_DEJ_DIR   = _DATA_DIR / "dej" if _DATA_DIR else None


def _prefer_decrypted(path: Path) -> Path:
    """Given a .compact.json path, prefer it over its .bin twin when present.

    The encrypted twin appends .bin (Model3_ETH.compact.json.bin), so build it
    by suffixing rather than Path.with_suffix (which would replace .json).
    """
    bin_twin = path.with_name(path.name + ".bin")
    if not path.exists() and bin_twin.exists():
        return bin_twin
    return path


def _resolve_compact(bus: str, dej_dir: Path | None = None) -> Path | None:
    """Return the compact DB path for a given bus, preferring .json over .bin.

    bus is the uppercased bus token in the filename, e.g. "ETH", "VCRIGHTV".
    """
    dej_dir = dej_dir if dej_dir is not None else _DEJ_DIR
    if dej_dir is None:
        return None
    return _prefer_decrypted(dej_dir / f"{PRODUCT}_{bus}.compact.json")


def compact_dbs(dej_dir: Path | None = None) -> dict[str, Path]:
    """Discover every compact DB under the product's dej/ dir, keyed by bus.

    Returns {bus_token: path} for each `<PRODUCT>_<BUS>.compact.json[.bin]`,
    preferring the decrypted .json over the encrypted .bin when both exist.
    Empty if the dej dir is unknown or absent.
    """
    dej_dir = dej_dir if dej_dir is not None else _DEJ_DIR
    if dej_dir is None or not dej_dir.is_dir():
        return {}
    prefix = f"{PRODUCT}_"
    out: dict[str, Path] = {}
    for p in dej_dir.iterdir():
        name = p.name
        # Strip the .bin envelope suffix so .json and .bin collapse to one key.
        base = name[:-4] if name.endswith(".bin") else name
        if not (base.startswith(prefix) and base.endswith(".compact.json")):
            continue
        bus = base[len(prefix):-len(".compact.json")]
        out[bus] = _prefer_decrypted(dej_dir / base)
    return out


_DEFAULT_NODES_JSON    = _DATA_DIR / "nodes.json" if _DATA_DIR else None
_DEFAULT_ETH_COMPACT   = _resolve_compact("ETH")
_DEFAULT_ODJ_DIR       = _DATA_DIR / "odj" if _DATA_DIR else None
_DEFAULT_ARTIFACTS_DIR = _ROOT / "deploy/seed_artifacts_v2" if _ROOT else None

NODES_JSON:    Path | None = _resolve("TM3_NODES_JSON",    _DEFAULT_NODES_JSON)
ETH_COMPACT:   Path | None = _resolve("TM3_ETH_COMPACT",   _DEFAULT_ETH_COMPACT)
ODJ_DIR:       Path | None = _resolve("TM3_ODJ_DIR",       _DEFAULT_ODJ_DIR)
ARTIFACTS_DIR: Path | None = _resolve("TM3_ARTIFACTS_DIR", _DEFAULT_ARTIFACTS_DIR)

# All compact DBs for the selected product, keyed by bus token (ETH, VCRIGHTV, ...).
COMPACT_DBS: dict[str, Path] = compact_dbs()

# ---------------------------------------------------------------------------
# CAN / argparse defaults
# ---------------------------------------------------------------------------

_ENV_MAP = {
    "channel":       ("TM3_CHANNEL",       str,  "vcan0"),
    "interface":     ("TM3_INTERFACE",      str,  "socketcan"),
    "bitrate":       ("TM3_BITRATE",        int,  None),
    "artifacts":     ("TM3_ARTIFACTS_DIR",  str,  None),
    "force":         ("TM3_DFU_FORCE",      bool, None),
}

DFU_FORCE: bool | None = None
_dfu_force_val = os.environ.get("TM3_DFU_FORCE")
if _dfu_force_val is not None:
    DFU_FORCE = _dfu_force_val.lower() in ("1", "true", "yes")


def apply_defaults(parser) -> None:
    """Set argparse defaults from .env so CLI args still override.

    Call this after adding arguments but before parse_args():
        config.apply_defaults(parser)
    """
    known = {a.dest for a in parser._actions}
    overrides = {}
    for dest, (env_key, cast, fallback) in _ENV_MAP.items():
        if dest not in known:
            continue
        val = os.environ.get(env_key)
        if val is not None:
            overrides[dest] = cast(val)
        elif fallback is not None:
            overrides[dest] = fallback
    parser.set_defaults(**overrides)
