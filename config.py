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

def _resolve_eth_compact(root: Path | None) -> Path | None:
    """Return the ETH compact path, preferring .bin over .json when both exist."""
    if root is None:
        return None
    candidate = root / "opt/odin/data/Model3/dej/Model3_ETH.compact.json"
    if not candidate.exists() and candidate.with_suffix(".bin").exists():
        return candidate.with_suffix(".bin")
    return candidate

_DEFAULT_NODES_JSON    = _ROOT / "opt/odin/data/Model3/nodes.json" if _ROOT else None
_DEFAULT_ETH_COMPACT   = _resolve_eth_compact(_ROOT)
_DEFAULT_ODJ_DIR       = _ROOT / "opt/odin/data/Model3/odj" if _ROOT else None
_DEFAULT_ARTIFACTS_DIR = _ROOT / "deploy/seed_artifacts_v2" if _ROOT else None

NODES_JSON:    Path | None = _resolve("TM3_NODES_JSON",    _DEFAULT_NODES_JSON)
ETH_COMPACT:   Path | None = _resolve("TM3_ETH_COMPACT",   _DEFAULT_ETH_COMPACT)
ODJ_DIR:       Path | None = _resolve("TM3_ODJ_DIR",       _DEFAULT_ODJ_DIR)
ARTIFACTS_DIR: Path | None = _resolve("TM3_ARTIFACTS_DIR", _DEFAULT_ARTIFACTS_DIR)

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
