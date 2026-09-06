#!/usr/bin/env python3
"""Node registry — the ordered list of peer-ECU SimNodes vehicle_sim aggregates.

Each node lives in its own folder (``scripts/<node>/<node>.py``) and owns the messages
that ECU *sources*; this thin module ties them together (no central node package).
Adding/attributing a message = editing one node's ``build()``; moving a frame between
ECUs (e.g. out of UNKNOWN as RE pins its source) is a one-line change there.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from app.app import NODE as APP
from bms.bms import NODE as BMS
from cmp.cmp import NODE as CMP
from cp.cp import NODE as CP
from das.das import NODE as DAS
from di.di import NODE as DI
from dir.dir import NODE as DIR
from epas3p.epas3p import NODE as EPAS3P
from epb.epb import NODE as EPB
from esp.esp import NODE as ESP
from gtw.gtw import NODE as GTW
from hvp.hvp import NODE as HVP
from ibst.ibst import NODE as IBST
from pcs.pcs import NODE as PCS
from pmr.pmr import NODE as PMR
from ptc.ptc import NODE as PTC
from rcm.rcm import NODE as RCM
from sccm.sccm import NODE as SCCM
from sim_core import FW_INHERIT, FirmwareVersion, Node, SimFrame
from ui.ui import NODE as UI
from unknown.unknown import NODE as UNKNOWN
from vcfront.vcfront import NODE as VCFRONT
from vcright.vcright import NODE as VCRIGHT
from vcsec.vcsec import NODE as VCSEC

import config as _cfg
import ecu_bench

# Ordered roughly bus-A ECUs first, then party (bus-B) ECUs; EPAS3P spans both, and
# per-bus print order is cosmetic (a frame's bus is fixed by its own ``bus`` field).
# These are node CLASSES; a run instantiates the selected ones (fresh state per run).
NODES: list[type[Node]] = [
    BMS, CP, PCS, HVP, VCFRONT, VCRIGHT, EPAS3P, SCCM, EPB, CMP, PTC, APP,
    ESP, IBST, RCM, DAS, GTW, UI, UNKNOWN, VCSEC,
    # Drive-inverter nodes: the rear unit the drive bench exercises. Regular nodes, but the
    # bench config marks whichever hardware is connected `real` (see scenarios/drive.toml) so
    # the sim doesn't collide with it. DIF/PMF (front, AWD) are the follow-up.
    DI, DIR, PMR,
]

# Drive-inverter family -- used to warn when a run would SIMULATE the inverter (harmless for a
# virtual car, but a collision if a real DIR/PMR is connected and not marked `real`).
INVERTER_NODES = frozenset({"DI", "DIR", "DIF", "PMR", "PMF"})

# Name -> node class, for selection (--sim/--real) and validation. Names are unique.
BY_NAME: dict[str, type[Node]] = {n.name: n for n in NODES}


def instantiate(classes: list[type[Node]] | None = None, ctx=None) -> list[Node]:
    """Instantiate the given node classes (default: all) with a shared NodeContext.
    The returned instances OWN their state; the driver seeds/orchestrates them."""
    return [cls(ctx) for cls in (NODES if classes is None else classes)]


def collect_frames(nodes: list[Node], fw=FW_INHERIT) -> list[SimFrame]:
    """Gather the periodic SimFrames each (instantiated) node broadcasts for firmware ``fw``.

    Default (``FW_INHERIT``) uses each node's own ``self.fw`` (the driver sets it at selection);
    pass an explicit FirmwareVersion / version string to override, or ``None`` for each node's
    newest authored set. Selection is newest authored revision <= target, clamped to the
    oldest. See ``Node.frames_for``.
    """
    frames: list[SimFrame] = []
    for node in nodes:
        frames.extend(node.frames_for(fw))
    return frames


def to_bench_frames(frames: list[SimFrame]) -> list:
    """Convert SimFrames to ecu_bench.Frames for the shared engine.

    period_s -> interval_ms (floored at 1), the SimFrame's ``.frame()`` becomes the
    builder (counter/checksum/overrides stay inside the SimFrame), and ``bus`` carries
    through. Both vehicle_sim and di.py run their node-sourced frames this way.
    """
    return [
        ecu_bench.Frame(
            sf.name, sf.can_id, max(1, round(sf.period_s * 1000)),
            builder=(lambda st, sf=sf: sf.frame()), bus=sf.bus,
            on_result=sf.note_send,  # roll the counter back if this send dropped (gapless on-wire)
        )
        for sf in frames
    ]


def _norm(names) -> set[str]:
    return {str(n).strip().upper() for n in (names or []) if str(n).strip()}


def select_nodes(
    sim: list[str] | None = None, real: list[str] | None = None
) -> list[type[Node]]:
    """Return the ordered node CLASSES to simulate.

    ``sim`` is a whitelist of node names (None => all nodes); ``real`` names ECUs that
    are present on the bus for real (physical hardware / another process) and must NOT be
    simulated -- e.g. the drive bench marks the connected inverter real (DIR/PMR/DI) so the
    sim doesn't collide with it, while a fully virtual car leaves them simulated. Names are
    case-insensitive; an unknown name raises ValueError.
    """
    sim_set = None if sim is None else _norm(sim)
    real_set = _norm(real)
    unknown = sorted(((sim_set or set()) | real_set) - set(BY_NAME))
    if unknown:
        raise ValueError(f"unknown node(s): {', '.join(unknown)}; known: {', '.join(BY_NAME)}")
    chosen: list[type[Node]] = []
    for n in NODES:
        if sim_set is not None and n.name not in sim_set:
            continue
        if n.name in real_set:
            continue
        chosen.append(n)
    return chosen


# ---------------------------------------------------------------------------
# MIA aggregates -- firmware-confirmed OR-aggregates the DIR clears only when ALL
# member frames arrive (valid checksum + rolling counter). Membership spans ECUs,
# so it is orthogonal to node ownership: deselecting a node can leave an aggregate
# partially covered, which can NEVER clear. Used to warn on partial coverage.
# ---------------------------------------------------------------------------
MIA_AGGREGATES: dict[str, frozenset[int]] = {
    "vcfrontMIA": frozenset({0x221, 0x241, 0x321, 0x3A1, 0x102, 0x3C2, 0x103}),
    "espMIA": frozenset({0x105, 0x145, 0x155, 0x175, 0x185, 0x38D}),
    "ibstMIA": frozenset({0x38E, 0x39D}),
    "epas3pMIA": frozenset({0x370, 0x3D1}),
    "rcmMIA": frozenset({0x101, 0x111}),
    "gtwMIA": frozenset({0x7FF, 0x528, 0x3ED}),
    "uiMIA": frozenset({0x82, 0x213, 0x284, 0x293, 0x313, 0x334}),
}


def mia_coverage_warnings(active_ids: set[int]) -> list[str]:
    """For each MIA aggregate that is PARTIALLY covered by ``active_ids`` (some members
    present, some missing), return a warning naming the missing IDs. A partially
    covered aggregate can never clear, so this surfaces "party MIA will never clear"
    up front. Fully covered or fully absent aggregates produce no warning.
    """
    out: list[str] = []
    for name, members in MIA_AGGREGATES.items():
        present = members & active_ids
        missing = members - active_ids
        if present and missing:
            miss = ", ".join(f"0x{c:03X}" for c in sorted(missing))
            out.append(f"{name}: missing {miss} -> aggregate can never clear")
    return out


# ---------------------------------------------------------------------------
# Top-level bench config (TOML) -- which ECUs to simulate + message->bus overrides.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "sim.toml"
VALID_BUSES = ("vehicle", "party", "charge")


class BenchConfig:
    """Parsed bench TOML: node selection (``sim``/``real``) + a message ``bus`` map.

    ``sim`` is a whitelist of node names (None => all); ``real`` names ECUs present on
    the bus for real (not simulated); ``bus`` maps arbitration ID -> logical bus,
    overriding a message's firmware-correct default. All are optional.
    """

    def __init__(
        self,
        sim: list[str] | None = None,
        real: list[str] | None = None,
        bus: dict[int, str] | None = None,
        scenario: dict[str, dict] | None = None,
        fw: str | None = None,
    ) -> None:
        self.sim = sim
        self.real = real or []
        self.bus = bus or {}
        # node name -> initial-state dict the orchestrator applies via node.configure().
        self.scenario = scenario or {}
        # Target firmware revision (``[firmware] version``); None => newest authored per node.
        self.fw = fw


def load_bench_config(path) -> BenchConfig:
    """Load + validate a bench TOML file.

    Schema::

        [firmware]
        version = "2024.8.9"     # target fw revision; omit => newest authored per node

        [nodes]
        sim  = ["BMS", "CP"]     # optional whitelist; omit for all nodes
        real = ["PCS"]           # present on the bus for real -> not simulated

        [bus]                    # message-level bus override (id -> vehicle|party|charge)
        0x370 = "charge"

        [scenario.CP]            # per-node initial state (the orchestrator; node.configure)
        evse_connected = true
        evse_limit_a   = 32
        [scenario.HVP]
        mode = "charge"

    ``[firmware] version`` selects each node's message set (newest authored revision <= it,
    clamped to the oldest); ``--fw`` on the CLI overrides it. Bus names are normalized via
    config.canonical_bus (ETH / unknown -> vehicle). The ``[scenario.<NODE>]`` tables are
    applied at startup via each node's ``configure()`` (the keys are node-specific, validated
    there). Raises ValueError on an unknown node name, a non-integer [bus] ID key, a
    non-table [scenario.<NODE>], or an unparseable ``[firmware] version``.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    fw = data.get("firmware", {}).get("version")
    if fw is not None:
        try:
            FirmwareVersion(fw)  # validate now so a typo fails at load, not mid-run
        except (TypeError, ValueError) as e:
            raise ValueError(f"[firmware] version {fw!r}: {e}") from e
    nodes = data.get("nodes", {})
    sim = nodes.get("sim")
    real = nodes.get("real", [])
    names = {str(n).strip().upper() for n in (list(sim or []) + list(real))}
    unknown = sorted(names - set(BY_NAME))
    if unknown:
        raise ValueError(f"unknown node(s) in [nodes]: {', '.join(unknown)}")
    bus: dict[int, str] = {}
    for key, val in data.get("bus", {}).items():
        try:
            cid = int(str(key), 0)
        except ValueError as e:
            raise ValueError(f"[bus] key {key!r} is not a valid arbitration ID") from e
        # eth / unknown -> vehicle (assume the vehicle bus unless another is named).
        bus[cid] = _cfg.canonical_bus(val)
    scenario: dict[str, dict] = {}
    for node_name, settings in data.get("scenario", {}).items():
        up = str(node_name).strip().upper()
        if up not in BY_NAME:
            raise ValueError(f"unknown node in [scenario]: {node_name}")
        if not isinstance(settings, dict):
            raise ValueError(f"[scenario.{node_name}] must be a table of settings")
        scenario[up] = settings
    return BenchConfig(sim=sim, real=real, bus=bus, scenario=scenario, fw=fw)
