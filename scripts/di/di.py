#!/usr/bin/env python3
"""DI (drive inverter) bench: command a bare Model 3 rear drive unit over CAN.

Drives a standalone DI/DU on the bench by continuously transmitting the control
frames it expects, answering the immobilizer challenge, and exposing gear /
system control. Built on the shared ``ecu_bench`` core.

2020 firmware split (the DU pinout has analog accelerator + brake inputs):
  * Accelerator pedal and brake switch are HARDWIRED to the DU — there is no CAN
    pedal/torque command on 2020 firmware. The DU reports them on 0x118.
  * Gear/direction and system mode ARE commanded over CAN.

Control frames (from the Ingenext single-motor controller DBC; both @ 100 ms):
  * 0x64 PRND_command_for_control — gear (Park=8 Reverse=2 Neutral=1 Drive=4),
    drive mode, regen, rolling counter + checksum.
  * 0x54 System_for_control      — System_mode (off=0 Drive=1 Charge=2).

Immobilizer: optionally answers the runtime handshake on **0x3D9** using a key
supplied by your key-derivation provider (see ``docs/SECURITY_PROVIDER.md``).
Watch ``DI_immobilizerState`` on 0x118 advance REQUEST -> AUTHENTICATING ->
DISARMED. No responder algorithm or key ships in this repo; without a configured
provider the responder is unavailable (pass ``--no-immo`` to run liveness only).

CAVEAT: the control-frame layout is from the aftermarket DBC and is a strong
starting point, but the full set of gating frames the DU requires before it will
arm (BMS HV-up/contactors-closed, VCFRONT power state, etc.) needs real Model 3
vehicle-bus traces. The checksum algorithm for 0x64 is a PLACEHOLDER pending a
capture — see ``_prnd_checksum``.

Usage:
  python scripts/di/di.py                 # start bench + shell
  python scripts/di/di.py --no-immo       # skip immobilizer responder
  # in the shell:
  gear("D"); system("drive"); status()
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config as _cfg  # noqa: E402
import ecu_bench  # noqa: E402
from ecu_bench import BenchSpec, BenchState, Frame, ImmoSpec  # noqa: E402
from uds_local.node_config import load_node_config  # noqa: E402

# --- 0x64 PRND_command_for_control --------------------------------------------
# Bit layout from the Ingenext DBC (Motorola/BIG where noted):
#   PRND_command      @ bit7  w8  BIG   (Park=8 Reverse=2 Neutral=1 Drive=4)
#   AWD_RWD           @ bit9  w2  BIG   (Normal=0 FWD=1 RWD=2)
#   Pedal             @ bit16 w1  BIG   (Chill=0 Sport=1)
#   Stopping_mode     @ bit22 w3  BIG   (Standard=0 Creep=1 Hold=2)
#   Regen_torque_max  @ bit46 w8  BIG   scale 0.5 %
#   Counter_0_to_15   @ bit51 w4  BIG
#   Checksum          @ bit63 w8  BIG
PRND_ID = 0x64
PRND_GEARS = {"P": 8, "PARK": 8, "R": 2, "REVERSE": 2,
              "N": 1, "NEUTRAL": 1, "D": 4, "DRIVE": 4}
DRIVE_MODES = {"chill": 0, "sport": 1}
STOPPING_MODES = {"standard": 0, "creep": 1, "hold": 2}

# --- DI_systemStatus (0x118) — the DI's own report, RX-decoded into the cache ---
# Used to confirm what the inverter is actually doing. Enum maps are from the
# Tesla compact DB (DI internal encoding, distinct from PRND_command above).
DI_STATUS_ID = 0x118
DI_GEAR_LABELS = {0: "INVALID", 1: "P", 2: "R", 3: "N", 4: "D", 7: "SNA"}
DI_IMMO_LABELS = {
    0: "INIT_SNA", 1: "REQUEST", 2: "AUTHENTICATING", 3: "DISARMED",
    4: "IDLE", 5: "RESET", 6: "FAULT",
}
DI_SYS_LABELS = {
    0: "UNAVAILABLE", 1: "IDLE", 2: "STANDBY", 3: "FAULT", 4: "ABORT", 5: "ENABLE",
}

# --- 0x54 System_for_control --------------------------------------------------
#   System_mode @ bit16 w8 LITTLE (off=0 Drive=1 Charge=2)
SYSTEM_ID = 0x54
SYSTEM_MODES = {"off": 0, "drive": 1, "charge": 2}


def _set_motorola(data: bytearray, start_bit: int, width: int, value: int) -> None:
    """Set a big-endian (Motorola) signal in-place, matching DBC start@MSB."""
    value &= (1 << width) - 1
    b = start_bit // 8
    bit = start_bit % 8
    remaining = width
    while remaining > 0:
        bits_this = min(bit + 1, remaining)
        shift = bit + 1 - bits_this
        chunk = (value >> (remaining - bits_this)) & ((1 << bits_this) - 1)
        mask = ((1 << bits_this) - 1) << shift
        data[b] = (data[b] & ~mask) | (chunk << shift)
        remaining -= bits_this
        b += 1
        bit = 7


def _prnd_checksum(data: bytes) -> int:
    """PLACEHOLDER checksum for 0x64.

    Tesla powertrain frames typically use an 8-bit sum of the data bytes plus a
    per-message id constant, but the exact constant for 0x64 is unconfirmed
    without a bench capture. We use a plain byte-sum mod 256 over bytes[0:7] so
    the field is populated and self-consistent; REPLACE once a real trace
    reveals the algorithm (compare against an Ingenext-controlled bus).
    """
    return sum(data[0:7]) & 0xFF


def _build_prnd(state: BenchState) -> bytes:
    data = bytearray(8)
    gear = state.get("gear", PRND_GEARS["P"])
    drive_mode = state.get("drive_mode", 0)
    stopping = state.get("stopping_mode", 0)
    regen = state.get("regen_max_pct", 100.0)

    _set_motorola(data, 7, 8, gear)
    _set_motorola(data, 9, 2, state.get("awd_rwd", 0))
    _set_motorola(data, 16, 1, drive_mode)
    _set_motorola(data, 22, 3, stopping)
    _set_motorola(data, 46, 8, int(regen / 0.5))

    ctr = (state.get("_prnd_ctr", 0) + 1) & 0xF
    state.set("_prnd_ctr", ctr)
    _set_motorola(data, 51, 4, ctr)
    _set_motorola(data, 63, 8, _prnd_checksum(bytes(data)))
    return bytes(data)


def _build_system(state: BenchState) -> bytes:
    data = bytearray(8)
    mode = state.get("system_mode", SYSTEM_MODES["off"])
    data[2] = mode & 0xFF  # System_mode @ bit16 LITTLE = byte 2
    data[0] = state.get("battery_number", 1) & 0xFF  # @bit7 w8 BIG = byte 0
    return bytes(data)


FRAMES = [
    Frame("PRND_command_for_control", PRND_ID, 100, builder=_build_prnd),
    Frame("System_for_control", SYSTEM_ID, 100, builder=_build_system),
]


# ---------------------------------------------------------------------------
# Control verbs (exposed in the interactive shell)
# ---------------------------------------------------------------------------

def _make_controls(state: BenchState) -> dict:
    def gear(which: str) -> None:
        """gear('P'|'R'|'N'|'D') — set drive direction."""
        key = which.strip().upper()
        if key not in PRND_GEARS:
            print(f"  unknown gear {which!r}; use P/R/N/D")
            return
        state.set("gear", PRND_GEARS[key])
        print(f"  gear -> {key}")

    def system(mode: str) -> None:
        """system('off'|'drive'|'charge') — set System_mode."""
        m = mode.strip().lower()
        if m not in SYSTEM_MODES:
            print(f"  unknown mode {mode!r}; use off/drive/charge")
            return
        state.set("system_mode", SYSTEM_MODES[m])
        print(f"  system -> {m}")

    def drivemode(mode: str) -> None:
        """drivemode('chill'|'sport')."""
        m = mode.strip().lower()
        if m not in DRIVE_MODES:
            print(f"  unknown drive mode {mode!r}; use chill/sport")
            return
        state.set("drive_mode", DRIVE_MODES[m])
        print(f"  drive mode -> {m}")

    def stopping(mode: str) -> None:
        """stopping('standard'|'creep'|'hold') — regen stopping behavior."""
        m = mode.strip().lower()
        if m not in STOPPING_MODES:
            print(f"  unknown stopping mode {mode!r}; use standard/creep/hold")
            return
        state.set("stopping_mode", STOPPING_MODES[m])
        print(f"  stopping mode -> {m}")

    def regen(pct: float) -> None:
        """regen(percent) — max regen torque 0..100%."""
        state.set("regen_max_pct", max(0.0, min(100.0, float(pct))))
        print(f"  regen max -> {state.get('regen_max_pct')}%")

    def _di_report() -> str:
        """One-line view of what the DI reports on 0x118 (from the RX cache)."""
        immo = state.signal("DI_immobilizerState")
        sysst = state.signal("DI_systemState")
        di_gear = state.signal("DI_gear")
        pedal = state.signal("DI_accelPedalPos")
        brake = state.signal("DI_brakePedalState")
        if immo is None and sysst is None:
            return "  DI report: (no 0x118 seen yet — is the DU powered + on the bus?)"
        def lbl(table, v):
            return table.get(int(v), f"?{v}") if v is not None else "—"
        return (f"  DI report: immo={lbl(DI_IMMO_LABELS, immo)} "
                f"sys={lbl(DI_SYS_LABELS, sysst)} gear={lbl(DI_GEAR_LABELS, di_gear)} "
                f"pedal={pedal if pedal is not None else '—'}% "
                f"brake={'ON' if brake == 1 else 'OFF' if brake == 0 else '—'}")

    def status() -> None:
        """Show commanded state + what the DI reports back on 0x118."""
        inv = {v: k for k, v in PRND_GEARS.items() if len(k) == 1}
        sysinv = {v: k for k, v in SYSTEM_MODES.items()}
        print(f"  commanded: gear={inv.get(state.get('gear', 8), '?')} "
              f"system={sysinv.get(state.get('system_mode', 0), '?')} "
              f"drive_mode={state.get('drive_mode', 0)} "
              f"regen={state.get('regen_max_pct', 100)}%")
        print(_di_report())

    def watch(seconds: float = 10.0) -> None:
        """watch(seconds) — live-print the DI's 0x118 report; useful to see the
        immobilizer handshake (REQUEST -> AUTHENTICATING -> DISARMED)."""
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            line = _di_report()
            if line != last:
                print(line)
                last = line
            time.sleep(0.1)

    return {
        "gear": gear, "system": system, "drivemode": drivemode,
        "stopping": stopping, "regen": regen, "status": status, "watch": watch,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="DI drive-unit bench (gear/system command + immobilizer)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--channel", default=None)
    p.add_argument("--interface", default=None)
    p.add_argument("--node", default="DIR", help="DI node name in nodes.json (default DIR)")
    p.add_argument("--no-immo", action="store_true", help="don't run the 0x3D9 immobilizer handshake")
    p.add_argument("--no-interactive", action="store_true", help="run headless until Ctrl-C")
    _cfg.apply_defaults(p)
    args = p.parse_args()

    cfg = load_node_config(args.node, _cfg.NODES_JSON, _cfg.ETH_COMPACT, _cfg.ODJ_DIR)
    immo = None if args.no_immo else ImmoSpec(node_config=cfg)

    def on_init(state: BenchState) -> dict:
        # Seed safe defaults, then bind the control verbs to this live state.
        state.set("gear", PRND_GEARS["P"])
        state.set("system_mode", SYSTEM_MODES["off"])
        state.set("regen_max_pct", 100.0)
        return _make_controls(state)

    spec = BenchSpec(name="DI", frames=FRAMES, immo=immo, on_init=on_init)

    ecu_bench.run(
        spec, args.channel, interface=args.interface,
        interactive=not args.no_interactive,
    )


if __name__ == "__main__":
    main()
