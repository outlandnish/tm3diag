#!/usr/bin/env python3
"""PCS (Power Conversion System) bench, built on the shared ecu_bench core.

Emulates the vehicle environment a Tesla PCS expects: transmits the 14 periodic
keepalive frames (10/50/100 ms groups), decodes inbound frames into a signal
cache, and exposes operating-mode control (off / dcdc / charge / both) plus a
closed-loop precharge sequence that ramps the DC link via DC-DC boost and closes
the contactors.

This is the ecu_bench port of the original pcs_send.py. Frame payloads and the
mode/contactor/BMS state machine are unchanged; the scheduler, RX cache, and
shell now come from ecu_bench.

Usage:
  python scripts/pcs/pcs.py --channel can0
  # in the shell:
  pcs_mode("dcdc"); precharge(); current_limits(15); status()
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config as _cfg  # noqa: E402
import ecu_bench  # noqa: E402
from ecu_bench import BenchSpec, BenchState, Frame  # noqa: E402

# Tunable defaults (control verbs update these via state.vars).
DEFAULTS = {
    "hv_voltage": 67.2,    # V — DC link target (18S pack)
    "ac_limit": 15,        # A
    "dcdc_voltage": 15.0,  # V — 12V rail target
    "charge_power": 0,     # W
    "evse_limit": 15,      # A
}

# Diagnostic frame: extended 29-bit ID outside all Model 3 standard IDs.
DIAG_ID = 0x1FFF0000

# Mode -> (pcsControl, charge_hw, dcdc_hw, contactor_stage, bms_mode)
MODE_MAP = {
    "off":    ("SHUTDOWN", False, False, "open",   "off"),
    "dcdc":   ("SUPPORT",  False, True,  "closed", "dcdc"),
    "charge": ("SUPPORT",  True,  False, "closed", "charge"),
    "both":   ("SUPPORT",  True,  True,  "closed", "charge"),
}


def _d(state: BenchState, key: str):
    return state.get("defaults", DEFAULTS)[key]


# ---------------------------------------------------------------------------
# Frame builders — read live state from BenchState.vars
# ---------------------------------------------------------------------------

def _build_hvp_pcs_control(state: BenchState) -> bytes | None:
    """HVP_pcsControl (0x22A). Skipped until a mode is set (pcs_control None)."""
    control = state.get("pcs_control")
    if control is None:
        return None  # heartbeat skips 0x22A until pcs_mode() is called
    v_raw = int(_d(state, "hv_voltage") / 0.1) & 0xFFFF
    ctrl_map = {"SHUTDOWN": 0, "SUPPORT": 1, "PRECHARGE": 2, "DISCHARGE": 3}
    word = v_raw
    word |= ctrl_map.get(control, 0) << 16
    word |= int(state.get("charge_hw", False)) << 18
    word |= int(state.get("dcdc_hw", False)) << 19
    _emit_diag(state, control != "SHUTDOWN",
               state.get("charge_hw", False), state.get("dcdc_hw", False))
    return bytes(word.to_bytes(4, "little"))


def _build_hvp_contactor_state(state: BenchState) -> bytes | None:
    """HVP_contactorState (0x20A). Skipped until a mode is set."""
    if state.get("pcs_control") is None:
        return None
    stage = state.get("contactor_stage", "open")
    if stage == "open":
        neg, pos, setst, closing, energize = 1, 1, 1, 0, 0
    elif stage == "precharge":
        neg, pos, setst, closing, energize = 4, 2, 2, 1, 1
    else:  # closed
        neg, pos, setst, closing, energize = 6, 6, 5, 1, 1
    word = neg | (pos << 3) | (setst << 8)
    word |= closing << 35
    word |= energize << 36
    word |= 1 << 40  # hvilStatus=STATUS_OK
    return bytes(word.to_bytes(6, "little"))


def _build_bms_status(state: BenchState) -> bytes:
    """BMS_status (0x212), encoding varies with bms_mode."""
    params = {
        # mode:        ctrs uiChg hvState chgReq state
        "off":       (1, 1, 0, 0, 0),
        "dcdc":      (4, 1, 6, 0, 2),
        "precharge": (4, 3, 1, 1, 3),
        "charge":    (4, 3, 4, 1, 3),
    }
    ctrs, ui_chg, hv, chg, st = params.get(state.get("bms_mode", "off"), params["off"])
    word = (1 << 0) | (1 << 4) | (1 << 7)
    word |= ctrs << 8
    word |= ui_chg << 11
    word |= hv << 16
    word |= chg << 29
    word |= st << 32
    word |= st << 56
    return bytes(word.to_bytes(8, "little"))


def _build_obc_control(state: BenchState) -> bytes:
    """OBC_control (0x13D)."""
    return bytes([0x05, int(_d(state, "ac_limit") / 0.5), 0xAA, 0x1A, 0xFF, 0x02])


def _build_cp_evse_status(state: BenchState) -> bytes:
    """CP_evseStatus (0x21D)."""
    pa = _d(state, "evse_limit")
    ca = int(_d(state, "evse_limit"))
    word = (1 << 0) | (3 << 2) | (2 << 4)
    word |= int(pa / 0.5) << 8
    word |= (ca & 0x7F) << 24
    word |= 2 << 38
    word |= 3 << 53
    return bytes(word.to_bytes(8, "little"))


def _build_cp_charge_status(state: BenchState) -> bytes:
    """CP_chargeStatus (0x23D)."""
    word = (5 << 0) | (int(_d(state, "ac_limit") / 0.5) << 8)
    return bytes(word.to_bytes(4, "little"))


def _build_ui_charge_request(state: BenchState) -> bytes:
    """UI_chargeRequest (0x333)."""
    a = int(_d(state, "ac_limit"))
    term_raw = int(80.0 / 0.1)
    word = (1 << 2) | ((a & 0x7F) << 8) | ((term_raw & 0x3FF) << 16)
    return bytes(word.to_bytes(4, "little"))


def _build_vcfront_sensors(state: BenchState) -> bytes:
    """VCFRONT_sensors (0x321) — fixed plausible temps/levels."""
    word = 556 | (557 << 10) | (1 << 21) | (2 << 22)
    word |= 127 << 24
    word |= 2 << 32
    word |= 127 << 40
    return bytes(word.to_bytes(8, "little"))


def _build_vcfront_vehicle_status(state: BenchState) -> bytes:
    """VCFRONT_vehicleStatus (0x3A1) with rolling counter + checksum."""
    ctr = (state.get("_vcf_ctr", 0) + 1) & 0xF
    state.set("_vcf_ctr", ctr)
    v_raw = int(_d(state, "dcdc_voltage") / 0.01) & 0x7FF
    word = (1 << 0) | (1 << 5) | (1 << 14) | (v_raw << 16) | ((ctr & 0xF) << 52)
    buf = bytearray(word.to_bytes(8, "little"))
    buf[7] = sum(buf[:7]) & 0xFF
    return bytes(buf)


def _build_charge_power(state: BenchState) -> bytes:
    w = int(_d(state, "charge_power"))
    return bytes([w & 0xFF, (w >> 8) & 0xFF, 0x00, 0x00, 0x00])


def _build_bms_log2(state: BenchState) -> bytes:
    """BMS_log2 (0x3B2) — alternates two payloads to prevent bmsMia."""
    mux = state.get("_bms_mux", False)
    state.set("_bms_mux", not mux)
    return (bytes([0xE5, 0x0D, 0xEB, 0xFF, 0x0C, 0x66, 0xBB, 0x11]) if mux
            else bytes([0xE3, 0x5D, 0xFB, 0xFF, 0x0C, 0x66, 0xBB, 0x06]))


def _build_vcfront_hb(state: BenchState) -> bytes:
    """VCFront heartbeat (0x545) — alternating payload, counter, CRC."""
    ctr = state.get("_hb_ctr", 0)
    mux = state.get("_vcf_mux", False)
    state.set("_hb_ctr", (ctr + 1) & 0xF)
    state.set("_vcf_mux", not mux)
    if mux:
        p = [0x14, 0x00, 0x3F, 0x70, 0x9F, 0x01, (ctr << 4) | 0x0A, 0x00]
    else:
        p = [0x03, 0x19, 0x64, 0x32, 0x19, 0x00, (ctr << 4), 0x00]
    p[7] = (sum(p[:7]) + 0x45 + 0x05) & 0xFF
    return bytes(p)


def _emit_diag(state: BenchState, pcs_en: bool, charge_en: bool, dcdc_en: bool) -> None:
    """Emit the 0x1FFF0000 diag frame only when [pcs,charge,dcdc] changes."""
    cur = (int(pcs_en), int(charge_en), int(dcdc_en))
    if state.get("_last_diag") == cur or state.bus is None:
        return
    state.set("_last_diag", cur)
    import contextlib

    import can
    with contextlib.suppress(can.CanError):
        state.bus.send(can.Message(arbitration_id=DIAG_ID, data=bytes(cur),
                                   is_extended_id=True))


# Static (no live state) frames sent as fixed bytes.
def _const(payload: list[int]):
    return lambda _state: bytes(payload)


FRAMES = [
    # 10 ms group
    Frame("HVP_pcsControl", 0x22A, 10, builder=_build_hvp_pcs_control),
    Frame("OBC_control", 0x13D, 10, builder=_build_obc_control),
    Frame("BMS_log2", 0x3B2, 10, builder=_build_bms_log2),
    # 50 ms group
    Frame("VCFront_heartbeat", 0x545, 50, builder=_build_vcfront_hb),
    # 100 ms group
    Frame("HVP_contactorState", 0x20A, 100, builder=_build_hvp_contactor_state),
    Frame("BMS_status", 0x212, 100, builder=_build_bms_status),
    Frame("msg_0x232", 0x232, 100, builder=_const([0x0A, 0x02, 0xD5, 0x09, 0xCB, 0x04, 0x00, 0x00])),
    Frame("msg_0x25D", 0x25D, 100, builder=_const([0xD8, 0x8C, 0x01, 0xB5, 0x4A, 0xC1, 0x0A, 0xE0])),
    Frame("VCFRONT_sensors", 0x321, 100, builder=_build_vcfront_sensors),
    Frame("UI_chargeRequest", 0x333, 100, builder=_build_ui_charge_request),
    Frame("CP_evseStatus", 0x21D, 100, builder=_build_cp_evse_status),
    Frame("CP_chargeStatus", 0x23D, 100, builder=_build_cp_charge_status),
    Frame("charge_power", 0x2B2, 100, builder=_build_charge_power),
    Frame("VCFRONT_vehicleStatus", 0x3A1, 100, builder=_build_vcfront_vehicle_status),
]


# ---------------------------------------------------------------------------
# RX hook — IVT-S shunt frames carry raw big-endian int32 fields (not in DB)
# ---------------------------------------------------------------------------

def _ivt_rx_hook(state: BenchState, can_id: int, data: bytes) -> None:
    if len(data) < 6:
        return
    if can_id == 0x521:
        state.signals["IVT_I"] = int.from_bytes(data[2:6], "big", signed=True) / 1000.0
    elif can_id == 0x522:
        state.signals["IVT_U1"] = int.from_bytes(data[2:6], "big", signed=True) / 1000.0
    elif can_id == 0x523:
        state.signals["IVT_U2"] = int.from_bytes(data[2:6], "big", signed=True) / 1000.0


# ---------------------------------------------------------------------------
# Control verbs
# ---------------------------------------------------------------------------

def _make_controls(state: BenchState) -> dict:
    def pcs_mode(mode: str = "off", hv_voltage: float | None = None) -> None:
        """pcs_mode('off'|'dcdc'|'charge'|'both', hv_voltage) — set operating mode."""
        if mode not in MODE_MAP:
            print(f"  unknown mode {mode!r}; choose {list(MODE_MAP)}")
            return
        if hv_voltage is not None:
            DEFAULTS["hv_voltage"] = hv_voltage
        ctrl, chg, dcdc, stage, bms = MODE_MAP[mode]
        state.set("pcs_control", ctrl)
        state.set("charge_hw", chg)
        state.set("dcdc_hw", dcdc)
        state.set("contactor_stage", stage)
        state.set("bms_mode", bms)
        print(f"  mode={mode} contactors={stage} bms={bms}")

    def precharge(hv_voltage: float | None = None, timeout_s: float = 30.0) -> None:
        """precharge(hv_voltage, timeout_s) — ramp DC link, then close contactors."""
        v = hv_voltage if hv_voltage is not None else DEFAULTS["hv_voltage"]
        threshold = v * 0.95
        print(f"  precharge: target {v:.1f}V threshold {threshold:.1f}V timeout {timeout_s}s")
        state.set("contactor_stage", "precharge")
        state.set("bms_mode", "precharge")
        state.set("pcs_control", "PRECHARGE")
        state.set("charge_hw", True)
        state.set("dcdc_hw", True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            dc_v = state.signal("IVT_U2") or state.signal("PCS_dcdcHvBusVolt")
            if dc_v is not None:
                print(f"  DC link: {dc_v:.1f}V / {v:.1f}V", end="\r")
                if dc_v >= threshold:
                    break
            time.sleep(0.1)
        else:
            print(f"\n  precharge TIMEOUT after {timeout_s}s — aborting")
            pcs_mode("off")
            return
        print("\n  precharge complete — closing contactors, entering charge")
        state.set("contactor_stage", "closed")
        state.set("bms_mode", "charge")
        state.set("pcs_control", "SUPPORT")
        state.set("charge_hw", True)
        state.set("dcdc_hw", False)

    def charge_power(watts: int | None = None) -> None:
        """charge_power(watts) — set charge power request (0x2B2)."""
        if watts is not None:
            DEFAULTS["charge_power"] = watts
        print(f"  charge_power -> {DEFAULTS['charge_power']} W")

    def dcdc_voltage(volts: float | None = None) -> None:
        """dcdc_voltage(volts) — set 12V rail target (0x3A1)."""
        if volts is not None:
            DEFAULTS["dcdc_voltage"] = volts
        print(f"  dcdc_voltage -> {DEFAULTS['dcdc_voltage']} V")

    def current_limits(evse_a: float | None = None, ac_a: float | None = None) -> None:
        """current_limits(evse_a, ac_a) — EVSE + AC charge current limits."""
        if evse_a is not None:
            DEFAULTS["evse_limit"] = evse_a
            if ac_a is None:
                DEFAULTS["ac_limit"] = evse_a
        if ac_a is not None:
            DEFAULTS["ac_limit"] = ac_a
        print(f"  limits: evse={DEFAULTS['evse_limit']}A ac={DEFAULTS['ac_limit']}A")

    def status() -> None:
        """Show commanded mode + key measured signals."""
        print(f"  pcs={state.get('pcs_control')} contactors={state.get('contactor_stage')} "
              f"bms={state.get('bms_mode')} | "
              f"IVT_U2={state.signal('IVT_U2')} IVT_U1={state.signal('IVT_U1')}")

    return {
        "pcs_mode": pcs_mode, "precharge": precharge, "charge_power": charge_power,
        "dcdc_voltage": dcdc_voltage, "current_limits": current_limits, "status": status,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="PCS bench (operating modes + precharge), on ecu_bench",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--channel", default=None)
    p.add_argument("--interface", default=None)
    p.add_argument("--no-interactive", action="store_true")
    _cfg.apply_defaults(p)
    args = p.parse_args()

    def on_init(state: BenchState) -> dict:
        state.set("defaults", DEFAULTS)
        state.set("pcs_control", None)   # 0x22A/0x20A skipped until pcs_mode()
        state.set("bms_mode", "off")
        state.set("contactor_stage", "open")
        return _make_controls(state)

    spec = BenchSpec(name="PCS", frames=FRAMES, on_init=on_init,
                     rx_hook=_ivt_rx_hook)
    ecu_bench.run(spec, args.channel, interface=args.interface,
                  interactive=not args.no_interactive)


if __name__ == "__main__":
    main()
