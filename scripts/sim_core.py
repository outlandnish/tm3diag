#!/usr/bin/env python3
"""Core types for the node-centric bench.

A ``Node`` is a stateful peer-ECU emulator: it owns its state, broadcasts ``SimFrame``s
reflecting that state, and transitions on frames it receives (``on_rx``). Low-level Tesla
payload packing lives in ``tesla_frames``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import total_ordering

from tesla_frames import place_checksum, place_counter, set_bitfield

# Bus messages are authored per firmware revision. BASELINE_FW is the revision every node is
# authored against (signal layouts from Model3_ETH.compact.json, 2020.8.1, the only revision
# that ships a decrypted compact DB); newer per-revision layouts are hand-authored into a
# node's ``fw_variants``.
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

    ``target is None`` -> the newest authored entry. Otherwise the entry with the greatest
    revision <= target, clamped to the oldest if target predates every authored revision.
    Raises ``ValueError`` on an empty map.
    """
    if not variants:
        raise ValueError("no firmware variants registered")
    ordered = sorted(
        ((FirmwareVersion(k), v) for k, v in variants.items()), key=lambda kv: kv[0]
    )
    if target is None:
        return ordered[-1]
    tgt = FirmwareVersion(target)
    chosen = ordered[0]  # clamp floor: oldest
    for entry in ordered:
        if entry[0] <= tgt:
            chosen = entry
        else:
            break
    return chosen


def resolve_fw_variants(
    variants: dict[str, Callable], target: FirmwareVersion | str | None
) -> Callable:
    """The builder ``_pick_fw_entry`` selects for ``target`` (see it for the fallback rules)."""
    return _pick_fw_entry(variants, target)[1]


class _FwInherit:
    """Sentinel default for ``frames_for``/``resolved_fw``/``collect_frames``: resolve against
    the node's own ``self.fw``. Distinct from ``None`` (explicit "newest authored per node")."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<inherit node.fw>"


FW_INHERIT = _FwInherit()

# Party-bus (group2 / CANB) liveness rate. The DIR clears its group2 CANB MIAs
# (rcm/esp/ibst/epas3p) only when their member frames arrive at ~100Hz+; baked into each
# MIA-owning node's SimFrame, not a per-run override. ESP 0x11D is also baked at this rate:
# it feeds espMIA a091 and drives the DIR VDC freshness watchdog (a195/a196/a197 + a210 stale).
PARTY_LIVENESS_S = 0.010  # 100 Hz -- group2 CANB MIA-clear floor

# Per-MIA-node party TX period (seconds); each node reads PARTY_RATE_S[self.name].
PARTY_RATE_S = {
    "RCM": PARTY_LIVENESS_S,  # 0x101/0x111
    "ESP": PARTY_LIVENESS_S,  # 0x105/145/155/175/185/38D
    "IBST": PARTY_LIVENESS_S,  # 0x38E/0x39D
    "EPAS3P": PARTY_LIVENESS_S,  # 0x370/0x3D1
}


@dataclass
class SimFrame:
    """One periodic CAN transmission: a payload builder + cycle time + optional
    Tesla rolling-counter / additive-checksum placement.

    ``build`` is a zero-arg callable returning the raw payload; ``.frame()`` layers
    any overrides, then the rolling counter, then the checksum (so the checksum covers
    the counter + overrides).

    ``bus`` is the message's logical bus — "vehicle" (group1 / CANA), "party" (group2 /
    CANB), or "charge" — resolved to a physical channel via ``config.can_channel``. A
    bench may reassign an ID to another bus via the bus-map override.
    """

    name: str
    can_id: int
    period_s: float
    build: Callable[[], bytes | bytearray]
    counter_start: int | None = None  # None => plain frame (no counter/checksum)
    cksum_start: int | None = None
    counter_width: int = 4  # rolling-counter width (DAS uses 3)
    bus: str = "vehicle"  # logical bus: "vehicle" | "party" | "charge"
    # (start_bit, width, value) overrides layered onto the payload before counter/checksum.
    overrides: list = field(default_factory=list)
    # Per-message checksum seed override. None => tesla_frames.magic(can_id). Set this in a
    # node's fw_variants entry when a revision reseeds the message (see place_checksum).
    cksum_magic: int | None = None
    _ctr: int = field(default=0)

    def frame(self) -> bytes:
        data = bytearray(self.build())
        for start_bit, width, value in self.overrides:
            set_bitfield(data, start_bit, width, value)
        if self.counter_start is not None:
            place_counter(data, self.counter_start, self._ctr, self.counter_width)
            self._ctr = (self._ctr + 1) & ((1 << self.counter_width) - 1)
        if self.cksum_start is not None:
            place_checksum(data, self.can_id, self.cksum_start, self.cksum_magic)
        return bytes(data)

    def note_send(self, ok: bool) -> None:
        """Post-send hook (wired to ecu_bench.Frame.on_result). On a failed send, roll the
        rolling counter back one step so the on-wire counter stays gapless across drops. No-op
        for plain (counter-less) frames."""
        if ok:
            return
        # SimFrame-managed counter (Tesla additive-checksum frames).
        if self.counter_start is not None:
            self._ctr = (self._ctr - 1) & ((1 << self.counter_width) - 1)
        # Builder-managed counter (J1850Frame 0x38D/0x38E, LvPowerState 0x221, SccmRightStalk
        # 0x229, UiPowertrainControl 0x334): delegate rollback to the builder object.
        owner = getattr(self.build, "__self__", None)
        rollback = getattr(owner, "rollback", None)
        if callable(rollback):
            rollback()


def zeros(n: int = 8) -> Callable[[], bytearray]:
    """Return a builder emitting an ``n``-byte all-zero payload.

    A plain-liveness MIA gates on DLC (+ checksum/counter for validated frames), not the
    signal values, so a zero payload of the exact DLC keeps such a handler's MIA cleared.
    """
    return lambda: bytearray(n)


def clamp_pct(value):
    """None -> None; else a 0-100 float (ValueError if non-numeric or out of range)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"pressure must be a number 0-100, got {value!r}") from None
    if not 0.0 <= v <= 100.0:
        raise ValueError(f"pressure must be 0-100, got {v}")
    return v


@dataclass
class NodeContext:
    """What a node may need at construction. ``db`` is the CanDatabase for nodes that
    encode frames by signal name (GTW car-config); most nodes ignore it."""

    db: object = None  # can_decoder.CanDatabase | None


class Node:
    """A stateful peer-ECU emulator.

    A node owns its internal state, broadcasts periodic frames reflecting that state
    (``frames()``), and transitions on frames it receives (``on_rx``). The driver injects
    externalities (drive vs charge intent, "plugged in", pedal, gear) via the node's methods.
    Stateless nodes override only ``frames()``; reactive ones (EPB, VCSEC immobilizer) also
    override ``rx_handlers``.

    Whether a node is present as real hardware is a property of the bench: mark it in the
    config's ``[nodes] real`` list so the sim doesn't transmit its IDs and collide.

    A node's state is a single firmware-independent model. What varies by firmware is behavior:
    frame encoding (``fw_variants``/``frames_for``) and inbound decoding (``rx_handlers``). The
    driver sets ``self.fw`` once, so a handler can branch on it (e.g.
    ``if self.fw is None or self.fw >= "2024.8.9": ...``).
    """

    name: str = "?"

    def __init__(self, ctx: NodeContext | None = None) -> None:
        self.ctx = ctx or NodeContext()
        # Resolved target firmware for behavior selection (driver-set). None => each node's
        # newest authored set (see frames_for / FW_INHERIT).
        self.fw: FirmwareVersion | None = None

    def frames(self) -> list[SimFrame]:
        """This node's baseline periodic broadcasts (the ``BASELINE_FW`` set). Builders read
        the node's state, so a state change takes effect on the next transmission. Callers reach
        it through ``frames_for`` so revision selection + fallback are honored."""
        return []

    def fw_variants(self) -> dict[str, Callable[[], list[SimFrame]]]:
        """Revision-keyed frame builders: ``{fw_string: () -> list[SimFrame]}``.

        Default: the single baseline set (``frames``) tagged at ``BASELINE_FW``. A node whose
        messages changed across revisions overrides this to register each authored set::

            def fw_variants(self):
                return {"2020.8.1": self.frames, "2024.8.9": self._frames_2024}

        ``frames_for(target)`` selects one (newest revision <= target, clamped to the oldest)."""
        return {BASELINE_FW: self.frames}

    def frames_for(self, fw=FW_INHERIT) -> list[SimFrame]:
        """This node's periodic broadcasts resolved for firmware revision ``fw``. Default
        (``FW_INHERIT``) resolves against the node's own ``self.fw``; pass an explicit revision
        (or ``None`` = newest authored) to override."""
        target = self.fw if fw is FW_INHERIT else fw
        return resolve_fw_variants(self.fw_variants(), target)()

    def resolved_fw(self, fw=FW_INHERIT) -> FirmwareVersion:
        """The revision ``frames_for(fw)`` actually selects for this node (default: against
        ``self.fw``)."""
        target = self.fw if fw is FW_INHERIT else fw
        return _pick_fw_entry(self.fw_variants(), target)[0]

    def rx_handlers(self) -> dict[int, Callable[[bytes, Callable[[int, bytes], None]], None]]:
        """Map arbitration ID -> handler(data, send) for the frames this node reacts to.

        Default: none (a fixed-liveness node registers nothing). A reactive node returns its
        {id: bound-method} map. Each handler takes (data, send); ``send(id, bytes)`` transmits
        a reactive reply."""
        return {}

    def configure(self, **settings) -> None:
        """Apply a scenario's initial state to this node (from ``[scenario.<NODE>]`` in the
        bench config). Base nodes have no settable state, so any keys are an error. Stateful
        nodes override to map their scenario keys onto their setters, popping what they know and
        calling ``super().configure(**leftover)`` to reject the rest."""
        if settings:
            raise ValueError(
                f"{self.name}: no configurable scenario state; unexpected keys {sorted(settings)}"
            )
