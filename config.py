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

# TM3_ROOT points to the squashfs-root of a firmware extraction. All data
# paths derive from it (and TM3_PRODUCT); there are no per-path overrides.
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


def _resolve_compact(bus: str, dej_dir: Path | None = None,
                     product: str | None = None) -> Path | None:
    """Return the compact DB path for a given bus, preferring .json over .bin.

    bus is the uppercased bus token in the filename, e.g. "ETH", "VCRIGHTV".
    product defaults to the module-level PRODUCT but is passed explicitly by
    FwPaths so each product resolves its own `<product>_<bus>.compact.json`.
    """
    dej_dir = dej_dir if dej_dir is not None else _DEJ_DIR
    product = product if product is not None else PRODUCT
    if dej_dir is None:
        return None
    return _prefer_decrypted(dej_dir / f"{product}_{bus}.compact.json")


def compact_dbs(dej_dir: Path | None = None, product: str | None = None) -> dict[str, Path]:
    """Discover every compact DB under the product's dej/ dir, keyed by bus.

    Returns {bus_token: path} for each `<PRODUCT>_<BUS>.compact.json[.bin]`,
    preferring the decrypted .json over the encrypted .bin when both exist.
    Empty if the dej dir is unknown or absent.
    """
    dej_dir = dej_dir if dej_dir is not None else _DEJ_DIR
    product = product if product is not None else PRODUCT
    if dej_dir is None or not dej_dir.is_dir():
        return {}
    prefix = f"{product}_"
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


def available_products() -> list[str]:
    """Return product names available under opt/odin/data/ in the firmware root.

    Each product folder must contain a nodes.json to be considered valid.
    Returns an empty list if TM3_ROOT is not set or the data dir is absent.
    """
    if _ROOT is None:
        return []
    data_dir = _ROOT / "opt/odin/data"
    if not data_dir.is_dir():
        return []
    return sorted(
        p.name for p in data_dir.iterdir()
        if p.is_dir() and (p / "nodes.json").exists()
    )


def resolve_odin_bundle(root: Path | None,
                        override: str | None = None) -> Path | None:
    """Resolve the ODIN graph bundle's ``networks/`` dir.

    The firmware ships ``opt/odin/odin_bundle.zip``; once unzipped, the graphs
    live under ``opt/odin/.../networks`` (the exact wrapper dir depends on how it
    was extracted). ``TM3_ODIN_BUNDLE`` overrides the whole path; otherwise we try
    the known layouts under ``<root>/opt/odin``. Returns None if unresolved.
    """
    if override:
        return Path(override).expanduser()
    if root is None:
        return None
    odin = root / "opt/odin"
    for cand in ("odin_bundle_extracted/odin_bundle/networks",
                 "odin_bundle/odin_bundle/networks",
                 "odin_bundle/networks"):
        p = odin / cand
        if p.is_dir():
            return p
    return None


class FwPaths:
    """Resolved firmware data paths for a specific product.

    All paths derive from TM3_ROOT (and the chosen product); there are no
    per-path env overrides, so each product resolves to its own data tree.
    """
    def __init__(self, product: str) -> None:
        self.product = product
        data_dir = _ROOT / "opt/odin/data" / product if _ROOT else None
        dej_dir = data_dir / "dej" if data_dir else None
        self.nodes_json: Path | None = (
            data_dir / "nodes.json" if data_dir else None)
        self.eth_compact: Path | None = _resolve_compact(
            "ETH", dej_dir, product)
        self.odj_dir: Path | None = data_dir / "odj" if data_dir else None
        self.artifacts_dir: Path | None = (
            _ROOT / "deploy/seed_artifacts_v2" if _ROOT else None)
        self.compact_dbs: dict[str, Path] = compact_dbs(dej_dir, product)


# Module-level paths for the default product. All derive from TM3_ROOT
# (and TM3_PRODUCT) — there are no per-path env overrides; use FwPaths to
# resolve a non-default product.
ROOT:          Path | None = _ROOT  # squashfs-root of the firmware extraction
NODES_JSON:    Path | None = _DATA_DIR / "nodes.json" if _DATA_DIR else None
ETH_COMPACT:   Path | None = _resolve_compact("ETH")
ODJ_DIR:       Path | None = _DATA_DIR / "odj" if _DATA_DIR else None
ARTIFACTS_DIR: Path | None = _ROOT / "deploy/seed_artifacts_v2" if _ROOT else None
# ODIN graph bundle (networks/ dir) — derived from TM3_ROOT, or TM3_ODIN_BUNDLE.
ODIN_BUNDLE:   Path | None = resolve_odin_bundle(_ROOT, os.environ.get("TM3_ODIN_BUNDLE"))

# All compact DBs for the selected product, keyed by bus token (ETH, VCRIGHTV, ...).
COMPACT_DBS: dict[str, Path] = compact_dbs()

# ---------------------------------------------------------------------------
# CAN channels — the three Model 3 buses
# ---------------------------------------------------------------------------
# Downstream tools (odin_runner, ...) pick the right channel per bus.
# TM3_VEHICLE_CHANNEL is the vehicle bus; it falls back to the generic
# TM3_CHANNEL so existing single-bus .env files keep working. Party/charge are
# None unless configured (a simple bench has only the vehicle bus, and callers
# fall back to it).
VEHICLE_CHANNEL: str | None = (
    os.environ.get("TM3_VEHICLE_CHANNEL") or os.environ.get("TM3_CHANNEL")
)
PARTY_CHANNEL:   str | None = os.environ.get("TM3_PARTY_CHANNEL")
CHARGE_CHANNEL:  str | None = os.environ.get("TM3_CHARGE_CHANNEL")

CAN_CHANNELS: dict[str, str | None] = {
    "vehicle": VEHICLE_CHANNEL,
    "party":   PARTY_CHANNEL,
    "charge":  CHARGE_CHANNEL,
}

# Target firmware revision for message layouts. None => newest authored set.
FW_VERSION: str | None = os.environ.get("TM3_FW")

# ODIN/Tesla bus tokens -> our bus keys.
_BUS_ALIASES = {
    "veh": "vehicle", "vehicle": "vehicle", "eth": "vehicle",
    "party": "party", "pt": "party",
    "ch": "charge", "chg": "charge", "charge": "charge",
}


def canonical_bus(bus: str | None = None) -> str:
    """Normalize an ODIN/Tesla bus token to a canonical key: 'vehicle', 'party', or
    'charge'. ETH (the vehicle backbone) and any unknown/empty token map to 'vehicle'
    -- i.e. assume the vehicle bus unless another is explicitly named.
    """
    return _BUS_ALIASES.get(str(bus or "").strip().lower(), "vehicle")


def can_channel(bus: str | None = None) -> str | None:
    """Resolve an ODIN/Tesla bus token (VEH / PARTY / CH / ETH …) to a CAN
    channel. Unknown or empty bus -> the vehicle channel; a bus with no channel
    configured returns None (the caller falls back to the vehicle channel).
    """
    return CAN_CHANNELS.get(canonical_bus(bus))


# ---------------------------------------------------------------------------
# CAN / argparse defaults
# ---------------------------------------------------------------------------

_ENV_MAP = {
    "channel":       ("TM3_CHANNEL",       str,  "vcan0"),
    "interface":     ("TM3_INTERFACE",      str,  "socketcan"),
    "bitrate":       ("TM3_BITRATE",        int,  None),
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
