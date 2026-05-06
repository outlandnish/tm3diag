"""Per-ECU broadcast (heartbeat) CAN IDs, decoded from update.img.

The gateway firmware's `enter_bootloader_v0` (update.img @ `0x40000732`)
watches each ECU's broadcast CAN ID, snapshots a counter, and short-circuits
its keep-alive loop the instant a new broadcast frame from that ECU arrives —
the positive signal that the ECU has rebooted into the bootloader.

These IDs are **independent** of the UDS request/response IDs in `nodes.json`:
  * UDS request/response only fire when we send a request.
  * Broadcasts here are emitted by the ECU itself at a fixed cadence
    (typically ~10 Hz), independent of any host activity.

Source of truth: update.img's bus-0 broadcast tracker table @ `0x400331a8`
and bus-2 table @ `0x4003325c`, walked by the CAN-receive task
`FUN_40000c00`. Each entry: `{u16 node_id, u16 can_id, u16 counter, ...}`.

Quirks captured here:
  * **EPBR and ESP both register CAN ID `0x551` on bus 2.** `FUN_40000c00`
    returns on the first match, so ESP's counter never advances. The firmware's
    `enter_bootloader_v0` falls back to the full 3.34 s wait for ESP. We
    replicate that — ESP gets `None` here.
  * **`HVBMS` has all-zero per-node config**: no broadcast tracking at all,
    full fixed wait.
  * **Higher-indexed nodes** (any index ≥ 0x1e/30 in the firmware's table —
    notably `TPMS`, all `*BU`/`*BL` bootloader variants, all `*RAMAPP`
    variants, `OPC`, `OPCS`, `SCCMSUB`, `THS`, `LUMBAR{L,R}`, `BLEEP*`,
    `HCM{L,R}`, `OHC`, `UBLOX`, `CBC`, `UMC`, `CC`, `ROHC`, `CPPLC*`, `TLC`,
    `VCFRONTBU`) hit the firmware's `node_id < 0x1e` guard and return
    `0xff` for bus — no broadcast tracking. Those entries here are also `None`.

For tm3diag's purpose: install a `can.Listener` filtered to the configured
`can_id` and break Phase 1 of `wait_for_bootloader` the moment the count
advances. Falls back to fixed-wait if the lookup returns `None`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BroadcastInfo:
    """Heartbeat CAN ID + bus assignment for one node.

    `bus` is the gateway's internal numbering (0 or 2). It's informational —
    tm3diag connects to whichever CAN bus the caller specified at session
    construction; matching only uses `can_id`. Useful for sanity-checking
    that you're on the right physical bus for the target ECU.
    """

    bus: int
    can_id: int


# Keyed by the uppercase node name used in `data/nodes.json`.
# `None` = no broadcast tracking; `wait_for_bootloader` will fall back to a
# fixed-budget Phase 1 (matching the firmware's behavior for these nodes).
NODE_BROADCAST_CONFIG: dict[str, BroadcastInfo | None] = {
    # ---- Bus 0 ----
    "CMP":     BroadcastInfo(bus=0, can_id=0x5F2),
    "DIS":     BroadcastInfo(bus=0, can_id=0x633),
    "EPAS3P":  BroadcastInfo(bus=0, can_id=0x5FE),
    "ESPCAL":  BroadcastInfo(bus=0, can_id=0x533),
    "VCFRONT": BroadcastInfo(bus=0, can_id=0x532),
    "HVP":     BroadcastInfo(bus=0, can_id=0x501),
    "IBST":    BroadcastInfo(bus=0, can_id=0x510),
    "VCLEFT":  BroadcastInfo(bus=0, can_id=0x502),
    "PCS":     BroadcastInfo(bus=0, can_id=0x504),
    "PM":      BroadcastInfo(bus=0, can_id=0x5F4),
    "PMS":     BroadcastInfo(bus=0, can_id=0x5E4),
    "RCMCAL":  BroadcastInfo(bus=0, can_id=0x503),
    "SCCM":    BroadcastInfo(bus=0, can_id=0x519),  # firmware names this 'sccmk'
    "VCRIGHT": BroadcastInfo(bus=0, can_id=0x529),
    # ---- Bus 2 ----
    "CP":      BroadcastInfo(bus=2, can_id=0x639),
    "EPBR":    BroadcastInfo(bus=2, can_id=0x551),
    "GTW3":    BroadcastInfo(bus=2, can_id=0x646),
    "IBSTCAL": BroadcastInfo(bus=2, can_id=0x66D),
    "OCS1P":   BroadcastInfo(bus=2, can_id=0x515),
    "PARK":    BroadcastInfo(bus=2, can_id=0x5FE),
    "RCM":     BroadcastInfo(bus=2, can_id=0x5F1),
    "VCSEC":   BroadcastInfo(bus=2, can_id=0x521),
    # ---- ECUs the firmware does NOT track (no broadcast → fixed wait) ----
    "ESP":     None,  # collides with EPBR's 0x551; first-match wins, never advances
    "HVBMS":   None,  # all-zero per-node config
    "DAS":     None,
    "DI":      None,
    "EPAS3S":  None,
    "EPBL":    None,
    "PTC":     None,
    "RADC":    None,
    "TAS":     None,
    "TPMS":    None,  # node index ≥ 0x1e
}


def broadcast_for(node_name: str) -> BroadcastInfo | None:
    """Return the broadcast info for `node_name`, or None if untracked.

    Case-insensitive lookup. Returns None when:
      * the node isn't in our table (typo, or a node we don't have data for), or
      * the firmware doesn't track a broadcast for that ECU.

    Callers should treat `None` as "fall back to fixed-budget wait".
    """
    return NODE_BROADCAST_CONFIG.get(node_name.upper())
