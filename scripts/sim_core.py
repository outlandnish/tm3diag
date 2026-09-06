#!/usr/bin/env python3
"""Core types for the node-centric bench.

A ``Node`` is a stateful peer-ECU emulator: it owns its state, BROADCASTS ``SimFrame``s
reflecting that state, and TRANSITIONS on frames it receives (``on_rx``). ``sim_registry``
holds the node classes; a driver (``vehicle_sim`` for drive, ``pcs.py`` for charge)
instantiates the selected nodes, sets their externalities, and runs them on the shared
``ecu_bench`` engine.

Low-level Tesla payload packing lives in ``tesla_frames`` (imported here); nodes import
their packing helpers + state classes (SccmRightStalk, LvPowerState, …) from there.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import total_ordering

from tesla_frames import place_checksum, place_counter, set_bitfield

# ---------------------------------------------------------------------------
# Firmware-revision message selection
# ---------------------------------------------------------------------------
# The bus messages are authored per firmware revision. A node declares revision-keyed frame
# builders (``Node.fw_variants``); ``Node.frames_for(target)`` picks the newest set whose
# revision is <= the target ("the last version for which we have messages"), clamped to the
# OLDEST authored set when the target predates all of them. With NO target (None) each node
# emits its NEWEST authored set -- so a plain run always uses the latest messages we have.
#
# 2020.8.1 is the baseline every node is authored against today (signal layouts verbatim from
# Model3_ETH.compact.json, 2020.8.1). Only that revision ships a *decrypted* compact DB -- the
# newer ../tesla-fw extractions (2022.45.15 / 2024.8.9 / 2025.26.8 / 2026.8.3) carry only the
# encrypted .compact.json.bin twin -- so newer per-revision layouts are hand-authored into a
# node's ``fw_variants`` as RE pins each delta, not auto-derived.
BASELINE_FW = "2020.8.1"

_FW_LEADING = re.compile(r"(\d+(?:\.\d+)*)")


@total_ordering
class FirmwareVersion:
    """A Tesla firmware revision as a comparable dotted-numeric version.

    Parses the leading ``N[.N...]`` out of any revision token, so the raw ``../tesla-fw``
    extraction directory names work verbatim: ``"2024.8.9.ice.extracted"`` -> 2024.8.9,
    ``"2020.8.1-9-ae1963092f.model3"`` -> 2020.8.1. Comparison is component-wise with the
    shorter version zero-padded (``2020.8`` == ``2020.8.0`` < ``2020.8.1``). Raises
    ``ValueError`` if the token has no leading digits.
    """

    __slots__ = ("raw", "parts")

    def __init__(self, raw: str | FirmwareVersion) -> None:
        if isinstance(raw, FirmwareVersion):
            self.raw, self.parts = raw.raw, raw.parts
            return
        text = str(raw).strip()
        m = _FW_LEADING.match(text)
        if not m:
            raise ValueError(f"not a firmware version: {raw!r}")
        self.raw = text
        self.parts: tuple[int, ...] = tuple(int(x) for x in m.group(1).split("."))

    def _padded(self, other: FirmwareVersion) -> tuple[tuple[int, ...], tuple[int, ...]]:
        n = max(len(self.parts), len(other.parts))
        return (
            self.parts + (0,) * (n - len(self.parts)),
            other.parts + (0,) * (n - len(other.parts)),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FirmwareVersion):
            try:
                other = FirmwareVersion(other)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return NotImplemented
        a, b = self._padded(other)
        return a == b

    def __lt__(self, other: str | FirmwareVersion) -> bool:
        if not isinstance(other, FirmwareVersion):
            other = FirmwareVersion(other)
        a, b = self._padded(other)
        return a < b

    def __hash__(self) -> int:
        # Strip trailing zeros so 2020.8 and 2020.8.0 hash equal (they compare equal).
        p = self.parts
        while len(p) > 1 and p[-1] == 0:
            p = p[:-1]
        return hash(p)

    def __str__(self) -> str:
        return ".".join(str(x) for x in self.parts)

    def __repr__(self) -> str:
        return f"FirmwareVersion({str(self)!r})"


def _pick_fw_entry(
    variants: dict[str, Callable], target: FirmwareVersion | str | None
) -> tuple[FirmwareVersion, Callable]:
    """Select ``(revision, builder)`` from ``{fw_string: builder}`` for ``target``.

    ``target is None`` -> the NEWEST authored entry (use the latest messages we have).
    Otherwise -> the entry with the greatest revision <= target ("the last version for which
    we have messages"); if target predates every authored revision, clamp to the OLDEST so an
    ancient target still yields the baseline rather than nothing. Raises ``ValueError`` on an
    empty map.
    """
    if not variants:
        raise ValueError("no firmware variants registered")
    ordered = sorted(
        ((FirmwareVersion(k), v) for k, v in variants.items()), key=lambda kv: kv[0]
    )
    if target is None:
        return ordered[-1]
    tgt = FirmwareVersion(target)
    chosen = ordered[0]  # clamp floor: oldest, overwritten by any authored revision <= target
    for entry in ordered:
        if entry[0] <= tgt:
            chosen = entry
        else:
            break  # ascending, so nothing further can be <= target
    return chosen


def resolve_fw_variants(
    variants: dict[str, Callable], target: FirmwareVersion | str | None
) -> Callable:
    """The builder ``_pick_fw_entry`` selects for ``target`` (see it for the fallback rules)."""
    return _pick_fw_entry(variants, target)[1]


class _FwInherit:
    """Sentinel default for ``frames_for``/``resolved_fw``/``collect_frames``: resolve against
    the node's OWN ``self.fw`` (the driver-set target). Distinct from ``None`` -- which is an
    explicit "newest authored per node" -- so a node can carry a concrete target that a bare
    ``frames_for()`` honors, while callers may still override with an explicit revision."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<inherit node.fw>"


FW_INHERIT = _FwInherit()

# ---------------------------------------------------------------------------
# Party-bus (group2 / CANB) liveness rate -- the ONE tuning knob for how fast the
# MIA-gating party frames transmit. Replaces the old ``--party-period`` CLI flatten.
#
# The DIR clears its group2 CANB MIAs (rcm/esp/ibst/epas3p) only when their member
# frames arrive FAST: the PMR->DIR IPC relay throttles CANB and the group2 staleness
# model needs ~100Hz+, so the firmware-native cycles (e.g. EPAS3P 0x3D1 @1Hz) leave
# those MIAs stuck (bench-confirmed 2026-08-02). 100Hz is
# that documented floor; 0.005 (200Hz) OVERRAN the throttled CANB path -> no-ACK tx
# errors, so DO NOT go faster without re-checking the bench.
#
# This is a CONSTANT baked into each MIA-owning node's SimFrame (rcm/esp/ibst/epas3p),
# not a per-run override -- the scheduler never rewrites a period. Truly non-waited-on party
# frames (das 0x389/0x2B9) keep their native rate so we don't flood the (USB/IP-limited) TX path.
# NOTE: ESP 0x11D (re-homed from UNKNOWN 2026-08-18) is ALSO baked at PARTY_LIVENESS_S -- besides
# feeding espMIA a091 it drives the DIR VDC freshness watchdog, which raises
# a195/a196/a197 + a210 when it goes stale. Tune here.
PARTY_LIVENESS_S = 0.010  # 100 Hz -- group2 CANB MIA-clear FLOOR (bench-confirmed, see below)

# Per-MIA-node party TX period (seconds) -- the bench rate knobs, all in one place.
# 100Hz is the confirmed floor: bench-tested 2026-08-09, 83Hz (0.0125) left rcm/esp/ibst/epas3p
# MIA in STEADY STATE (they blink but re-assert); 100Hz clears them all and HOLDS. The earlier
# "100Hz has periodic blips" was NOT the rate -- it was qdisc ENOBUFS drops from a tiny CAN TX
# queue (txqueuelen=10); once bumped to 1000 the drops vanished (tx err -> 0) and 100Hz runs
# clean. So don't drop below 100Hz to "shed load" -- the load was never the problem. Each node
# reads PARTY_RATE_S[self.name], so a single member can still be re-dialed here to bisect.
PARTY_RATE_S = {
    "RCM": PARTY_LIVENESS_S,  # 0x101/0x111 -- 2 frames
    "ESP": PARTY_LIVENESS_S,  # 0x105/145/155/175/185/38D -- 6 frames, half the bus
    "IBST": PARTY_LIVENESS_S,  # 0x38E/0x39D -- 2 frames
    "EPAS3P": PARTY_LIVENESS_S,  # 0x370/0x3D1 -- 2 frames
}


@dataclass
class SimFrame:
    """One periodic CAN transmission: a payload builder + cycle time + optional
    Tesla rolling-counter / additive-checksum placement.

    ``build`` is a zero-arg callable returning the raw payload; ``.frame()`` layers
    any ``--set`` overrides, then the rolling counter, then the checksum (in that
    order, so the checksum covers the counter + overrides).

    ``bus`` names the *logical* bus this message belongs to — "vehicle" (group1 /
    CANA), "party" (group2 / CANB), or "charge". This is the message's
    firmware-correct default; a bench may reassign a specific ID to another bus (see
    the bus-map override), and the logical bus resolves to a physical channel via
    ``config.can_channel``. The rigid group1/group2 tagging is firmware-enforced, so
    the default matters — a wrong-bus frame is tagged to the wrong group and dropped.
    """

    name: str
    can_id: int
    period_s: float
    build: Callable[[], bytes | bytearray]
    counter_start: int | None = None  # None => plain frame (no counter/checksum)
    cksum_start: int | None = None
    counter_width: int = 4  # rolling-counter width (DAS uses 3)
    bus: str = "vehicle"  # logical bus: "vehicle" | "party" | "charge"
    # VDC/ESP status-field bisection: (start_bit, width, value) overrides layered onto
    # the payload BEFORE counter/checksum so the checksum covers them. Wired from --set.
    overrides: list = field(default_factory=list)
    _ctr: int = field(default=0)

    def frame(self) -> bytes:
        data = bytearray(self.build())
        for start_bit, width, value in self.overrides:
            set_bitfield(data, start_bit, width, value)
        if self.counter_start is not None:
            place_counter(data, self.counter_start, self._ctr, self.counter_width)
            self._ctr = (self._ctr + 1) & ((1 << self.counter_width) - 1)
        if self.cksum_start is not None:
            place_checksum(data, self.can_id, self.cksum_start)
        return bytes(data)

    def note_send(self, ok: bool) -> None:
        """Post-send hook (wired to ecu_bench.Frame.on_result). On a FAILED send, roll the
        rolling counter back one step. frame() already advanced it, but a frame dropped locally
        (ENOBUFS) never reached the wire, so it must not consume a counter value -- otherwise the
        next successful frame shows the DIR a counter JUMP, and a validated-frame MIA won't reset
        its freshness timer until the sequence resyncs ("blips until it resyncs"). Rolling back
        keeps the on-wire counter gapless across drops. No-op for plain (counter-less) frames."""
        if ok:
            return
        # (a) SimFrame-managed counter (Tesla additive-checksum frames: rcm/esp/epas3p/...).
        if self.counter_start is not None:
            self._ctr = (self._ctr - 1) & ((1 << self.counter_width) - 1)
        # (b) Builder-managed counter -- when the counter is entangled with a CRC it lives inside
        # the builder object (J1850Frame 0x38D/0x38E, LvPowerState 0x221, SccmRightStalk 0x229,
        # UiPowertrainControl 0x334), not in SimFrame. build is that object's bound .frame method,
        # so reach the object via __self__ and delegate the rollback to it.
        owner = getattr(self.build, "__self__", None)
        rollback = getattr(owner, "rollback", None)
        if callable(rollback):
            rollback()


def zeros(n: int = 8) -> Callable[[], bytearray]:
    """Return a builder emitting an ``n``-byte all-zero payload.

    Zeros are valid, non-SNA physical values on this bus; a plain-liveness MIA gates
    on DLC (+ checksum/counter for validated frames), not the signal values, so a
    zero payload of the exact DLC is enough to keep such a handler's MIA cleared.
    """
    return lambda: bytearray(n)


@dataclass
class NodeContext:
    """What a node may need at construction. ``db`` is the CanDatabase for nodes that
    encode frames by signal name (GTW car-config today); most nodes ignore it. The
    driver sets scenario/externalities *after* construction via each node's methods."""

    db: object = None  # can_decoder.CanDatabase | None


class Node:
    """A stateful peer-ECU emulator.

    A node OWNS its internal state, BROADCASTS periodic frames that reflect that state
    (``frames()``), and TRANSITIONS on frames it receives (``on_rx``) — like a real ECU.
    The *driver* (vehicle_sim / pcs.py / an external orchestrator) injects the
    externalities that aren't physically on the bench (drive vs charge intent, "plugged
    in", pedal, gear) by calling the node's own methods. Stateless nodes (fixed liveness)
    just override ``frames()``; reactive ones (EPB, VCSEC immobilizer) override ``on_rx``.

    Whether a node is present as REAL hardware is a property of the *bench*, not the
    node: mark it in the config's ``[nodes] real`` list so the sim doesn't transmit its IDs
    and collide with the real device. With that hardware absent, simulating those frames (a
    virtual ECU) is correct -- so it's config, not a fixed node flag.

    STATE vs BEHAVIOR ACROSS FIRMWARE. A node's *state* is the physical/logical model (immo
    key, EPB PARK/RELEASED, LV=drive, gear) -- bench truth that does NOT vary by firmware, so
    it is a single shared model (never versioned; a field only newer firmware reads is just
    added to the model as a superset). What DOES vary by firmware is *behavior*: how state is
    ENCODED onto the wire (``fw_variants``/``frames_for``) and how inbound frames are DECODED
    into transitions + responses (``rx_handlers``). The driver sets ``self.fw`` (the resolved
    target revision) once, so a reactive handler can branch its response on it -- e.g.
    ``if self.fw is None or self.fw >= "2024.8.9": ...`` -- without threading fw through every
    call. Un-versioned reactions read nothing and stay identical.
    """

    name: str = "?"

    def __init__(self, ctx: NodeContext | None = None) -> None:
        self.ctx = ctx or NodeContext()
        # Resolved target firmware for behavior selection; the driver sets it after selection.
        # None => each node's newest authored set (see frames_for / FW_INHERIT). Behavior
        # (frame encode + rx decode/response) reads this; the node's STATE model does not.
        self.fw: FirmwareVersion | None = None

    def frames(self) -> list[SimFrame]:
        """This node's BASELINE periodic broadcasts (the ``BASELINE_FW`` set). SimFrame
        builders read the node's state, so a state change takes effect on the next
        transmission. A node that doesn't vary by firmware overrides only this; callers reach
        it through ``frames_for`` so revision selection + fallback are honored."""
        return []

    def fw_variants(self) -> dict[str, Callable[[], list[SimFrame]]]:
        """Revision-keyed frame builders: ``{fw_string: () -> list[SimFrame]}``.

        Default: the single baseline set (``frames``) tagged at ``BASELINE_FW`` -- so a node
        that doesn't vary by firmware just overrides ``frames()`` and is done. A node whose
        messages changed across revisions overrides this to register each authored set::

            def fw_variants(self):
                return {"2020.8.1": self.frames, "2024.8.9": self._frames_2024}

        reusing ``frames`` as the baseline builder. ``frames_for(target)`` then selects one
        (newest revision <= target, clamped to the oldest). Each value is a zero-arg callable
        returning that revision's SimFrames."""
        return {BASELINE_FW: self.frames}

    def frames_for(self, fw=FW_INHERIT) -> list[SimFrame]:
        """This node's periodic broadcasts resolved for firmware revision ``fw``. Default
        (``FW_INHERIT``) resolves against the node's own ``self.fw``; pass an explicit revision
        (or ``None`` = newest authored) to override. The registry/driver use this instead of
        ``frames()`` so the selected revision and its fallback take effect."""
        target = self.fw if fw is FW_INHERIT else fw
        return resolve_fw_variants(self.fw_variants(), target)()

    def resolved_fw(self, fw=FW_INHERIT) -> FirmwareVersion:
        """The revision ``frames_for(fw)`` actually selects for this node (default: against
        ``self.fw``) -- for --list-nodes diagnostics and the driver's fallback summary."""
        target = self.fw if fw is FW_INHERIT else fw
        return _pick_fw_entry(self.fw_variants(), target)[0]

    def rx_handlers(self) -> dict[int, Callable[[bytes, Callable[[int, bytes], None]], None]]:
        """Map arbitration ID -> handler(data, send) for the frames this node REACTS to.

        The engine builds ONE global {id: [handlers]} dispatch table from every selected
        node, so an inbound frame goes straight to only its registered handlers -- no
        per-frame scan of all nodes, and non-reactive IDs (the vast majority of broadcasts)
        are skipped entirely (that scan is what let RX bursts stall the TX scheduler).

        Default: none -- a fixed-liveness node registers nothing. A reactive node (EPB brake,
        VCSEC immobilizer, the charge cascade) returns its {id: bound-method} map. Each
        handler takes (data, send); ``send(id, bytes)`` transmits a reactive reply."""
        return {}

    def configure(self, **settings) -> None:
        """Apply a scenario's initial state to this node (from ``[scenario.<NODE>]`` in the
        bench config -- the external orchestrator). Base nodes have no settable state, so any
        keys are an error (this catches scenario typos). Stateful nodes override to map their
        scenario keys onto their own setters, popping what they know and calling
        ``super().configure(**leftover)`` to reject the rest."""
        if settings:
            raise ValueError(
                f"{self.name}: no configurable scenario state; unexpected keys {sorted(settings)}"
            )
