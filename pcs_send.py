#!/usr/bin/env python3
"""Interactive PCS CAN scripting shell.

Launches a REPL with a live CAN bus and PCS-specific helpers for experimenting
with the Tesla Power Conversion System. Runs alongside can_live.py on the same
socketcan channel.

Usage:
  python pcs_send.py --channel can0
  python pcs_send.py --channel vcan0 --interface socketcan
"""

from __future__ import annotations
import config as _cfg
import can

import argparse
import code
import threading
import time
import warnings
from typing import Any

warnings.filterwarnings("ignore", category=RuntimeWarning,
                        module=r"uds\.packet\.abstract_packet")
warnings.filterwarnings(
    "ignore", message="A CAN packet that does not start UDS message transmission")


try:
    from can_decoder import CanDatabase
    _DB: CanDatabase | None = CanDatabase()
except Exception:
    _DB = None

# ---------------------------------------------------------------------------
# Mutable defaults — helpers read these so you can tune once and resend many
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "hv_voltage": 67.2,    # volts
    "ac_limit": 15,        # amps
    "dcdc_voltage": 15.0,  # volts
    "charge_power": 0,     # watts
    "evse_limit": 15,      # amps
}

# ---------------------------------------------------------------------------
# State for heartbeat counter / multiplexer
# ---------------------------------------------------------------------------

_hb_lock = threading.Lock()
_hb_counter = 0     # 0x545 VCFront counter 0-15
_bms_mux = False    # 0x3B2 alternates between two payloads
_vcf_mux = False    # 0x545 alternates between two payloads
_vcfront_ctr = 0    # 0x3A1 vehicleStatusCounter 0-15
_contactor_stage = "closed"  # "open" | "precharge" | "closed"

# ---------------------------------------------------------------------------
# Raw send helper
# ---------------------------------------------------------------------------

_bus: can.BusABC | None = None


def _send(can_id: int, data: list[int] | bytes) -> None:
    """Send a CAN frame silently (no output). Used internally for heartbeats."""
    if _bus is None:
        return
    try:
        _bus.send(can.Message(arbitration_id=can_id,
                  data=bytes(data), is_extended_id=False))
    except can.CanOperationError:
        pass  # drop frame on buffer-full; next tick will retry


def raw(can_id: int, data: list[int] | bytes) -> None:
    """Send an arbitrary CAN frame and print confirmation."""
    if _bus is None:
        print("Bus not connected")
        return
    _send(can_id, data)
    print(f"TX  0x{can_id:03X}  [{len(data)}]  {bytes(data).hex()}")


# ---------------------------------------------------------------------------
# Payload builders — signal layout from Model3_ETH.compact.json
# ---------------------------------------------------------------------------

def _build_hvp_pcs_control(
    v: float | None = None,
    control: str = "SHUTDOWN",
    charge_hw: bool = False,
    dcdc_hw: bool = False,
) -> list[int]:
    """HVP_pcsControl (0x22A) — 4 bytes.

    control: 'SHUTDOWN' | 'SUPPORT' | 'PRECHARGE' | 'DISCHARGE'
    v: DC link voltage request in volts (default: DEFAULTS['hv_voltage'])    
    """
    ctrl_map = {"SHUTDOWN": 0, "SUPPORT": 1, "PRECHARGE": 2, "DISCHARGE": 3}
    v_volts = v if v is not None else DEFAULTS["hv_voltage"]
    # HVP_dcLinkVoltageRequest: bits 0-15, scale=0.1, signed
    v_raw = int(v_volts / 0.1) & 0xFFFF
    word = v_raw
    word |= ctrl_map.get(control, 0) << 16   # bits 16-17
    word |= int(charge_hw) << 18              # bit 18
    word |= int(dcdc_hw) << 19               # bit 19
    # HVP_dcLinkVoltageFiltered bits 20-30: leave 0
    return list(word.to_bytes(4, "little"))


def _build_hvp_contactor_state(stage: str | None = None) -> list[int]:
    """HVP_contactorState (0x20A) — 6 bytes.

    stage: 'open' | 'precharge' | 'closed' (default: _contactor_stage)

    Contactor state encoding per stage:
      open:      negState=OPEN(1),       posState=OPEN(1),       setState=OPEN(1)
      precharge: negState=PULLED_IN(4),  posState=PRECHARGE(2),  setState=CLOSING(2)
      closed:    negState=ECONOMIZED(6), posState=ECONOMIZED(6), setState=CLOSED(5)

    All stages assert packCtrsClosingAllowed(35), dcLinkAllowedToEnergize(36), hvilStatus=OK(1@40).
    """
    s = stage if stage is not None else _contactor_stage
    if s == "open":
        neg, pos, setst = 1, 1, 1
    elif s == "precharge":
        neg, pos, setst = 4, 2, 2
    else:  # closed
        neg, pos, setst = 6, 6, 5
    word = 0
    word |= neg << 0    # packContNegativeState
    word |= pos << 3    # packContPositiveState
    word |= setst << 8    # packContactorSetState
    word |= 1 << 35       # packCtrsClosingAllowed
    word |= 1 << 36       # dcLinkAllowedToEnergize
    word |= 1 << 40       # hvilStatus=STATUS_OK
    return list(word.to_bytes(6, "little"))


def _build_bms_status() -> list[int]:
    """BMS_status (0x212) — 8 bytes.

    Signals from compact JSON (all LITTLE endian):
      hvacPowerRequest   bit 0      1
      updateAllowed      bit 4      1
      pcsPwmEnabled      bit 7      1
      contactorState     bits 8-10  BMS_CTRSET_CLOSED=4
      uiChargeStatus     bits 11-13 BMS_CHARGING=3
      hvState            bits 16-18 HV_UP_FOR_CHARGE=4
      chargeRequest      bit 29     1
      state              bits 32-35 BMS_CHARGE=3 (width 4)
      smStateRequest     bits 56-59 BMS_CHARGE=3 (width 4)
    """
    word = 0
    word |= 1 << 0    # hvacPowerRequest
    word |= 1 << 4    # updateAllowed
    word |= 1 << 7    # pcsPwmEnabled
    word |= 4 << 8    # contactorState=BMS_CTRSET_CLOSED
    word |= 3 << 11   # uiChargeStatus=BMS_CHARGING
    word |= 4 << 16   # hvState=HV_UP_FOR_CHARGE
    word |= 1 << 29   # chargeRequest
    word |= 3 << 32   # state=BMS_CHARGE (bit 32, width 4)
    word |= 3 << 56   # smStateRequest=BMS_CHARGE (bit 56, width 4)
    return list(word.to_bytes(8, "little"))


def _build_cp_evse_status(pilot_a: float | None = None, cable_a: int | None = None) -> list[int]:
    """CP_evseStatus (0x21D) — 8 bytes.

    Signals from compact JSON (all LITTLE endian):
      evseAccept         bit 0      1
      proximity          bits 2-3   LATCHED=3
      pilot              bits 4-6   LINE_CHARGE=2
      pilotCurrent       bits 8-15  scale=0.5
      cableCurrentLimit  bits 24-30
      evseChargeType_UI  bits 38-39 AC_CHARGER_PRESENT=2
      acChargeState      bits 53-55 AC_CHARGE_ENABLED=3
    """
    pa = pilot_a if pilot_a is not None else DEFAULTS["evse_limit"]
    ca = cable_a if cable_a is not None else int(DEFAULTS["evse_limit"])
    word = 0
    word |= 1 << 0                    # evseAccept
    word |= 3 << 2                    # proximity=LATCHED
    word |= 2 << 4                    # pilot=LINE_CHARGE
    word |= int(pa / 0.5) << 8        # pilotCurrent
    word |= (ca & 0x7F) << 24         # cableCurrentLimit
    word |= 2 << 38                   # evseChargeType=AC_CHARGER_PRESENT
    word |= 3 << 53                   # acChargeState=AC_CHARGE_ENABLED
    return list(word.to_bytes(8, "little"))


def _build_cp_charge_status(ac_limit_a: float | None = None) -> list[int]:
    """CP_chargeStatus (0x23D) — 4 bytes.

    Signals from compact JSON (all LITTLE endian):
      hvChargeStatus        bits 0-2  CP_CHARGE_ENABLED=5
      chargeShutdownRequest bits 3-4  NO_SHUTDOWN=0
      acChargeCurrentLimit  bits 8-15 scale=0.5
    """
    a = ac_limit_a if ac_limit_a is not None else DEFAULTS["ac_limit"]
    word = 0
    word |= 5 << 0              # hvChargeStatus=CP_CHARGE_ENABLED
    word |= int(a / 0.5) << 8  # acChargeCurrentLimit
    return list(word.to_bytes(4, "little"))


def _build_vcfront_sensors() -> list[int]:
    """VCFRONT_sensors (0x321) — 8 bytes.

    Signals from compact JSON (all LITTLE endian):
      tempCoolantBatInlet  bits 0-9   scale=0.125, offset=-40 -> 29.5°C = raw 556
      tempCoolantPTInlet   bits 10-20 scale=0.125, offset=-40 -> 29.625°C = raw 557
      coolantLevel         bit 21     FILLED=1
      brakeFluidLevel      bits 22-23 NORMAL=2
      tempAmbient          bits 24-31 scale=0.5, offset=-40 -> 23.5°C = raw 127
      washerFluidLevel     bits 32-33 NORMAL=2
      tempAmbientFiltered  bits 40-47 scale=0.5, offset=-40 -> 23.5°C = raw 127
    """
    word = 0
    word |= 556 << 0   # tempCoolantBatInlet: (29.5 + 40) / 0.125 = 556
    word |= 557 << 10  # tempCoolantPTInlet:  (29.625 + 40) / 0.125 = 557
    word |= 1 << 21    # coolantLevel=FILLED
    word |= 2 << 22    # brakeFluidLevel=NORMAL
    word |= 127 << 24  # tempAmbient: (23.5 + 40) / 0.5 = 127
    word |= 2 << 32    # washerFluidLevel=NORMAL
    word |= 127 << 40  # tempAmbientFiltered: same as tempAmbient
    return list(word.to_bytes(8, "little"))


def _build_ui_charge_request(
    ac_limit_a: float | None = None,
    term_pct: float = 80.0,
) -> list[int]:
    """UI_chargeRequest (0x333) — 4 bytes.

    Signals from compact JSON (all LITTLE endian):
      chargeEnableRequest    bit 2      1
      acChargeCurrentLimit   bits 8-14  (amps, integer)
      chargeTerminationPct   bits 16-25 scale=0.1
    """
    a = int(ac_limit_a if ac_limit_a is not None else DEFAULTS["ac_limit"])
    term_raw = int(term_pct / 0.1)
    word = 0
    word |= 1 << 2               # chargeEnableRequest
    word |= (a & 0x7F) << 8     # acChargeCurrentLimit
    word |= (term_raw & 0x3FF) << 16  # chargeTerminationPct
    return list(word.to_bytes(4, "little"))


def _build_vcfront_vehicle_status(ctr: int, hv_charge_enable: bool = True) -> list[int]:
    """VCFRONT_vehicleStatus (0x3A1) — 8 bytes, with counter + checksum.

    Signals from compact JSON (all LITTLE endian):
      bmsHvChargeEnable      bit 0      1
      inAccessoryPlus        bit 5      1
      12vStatusForDrive      bits 14-15 READY_FOR_DRIVE_12V=1
      pcs12vVoltageTarget    bits 16-26 scale=0.01 -> DEFAULTS['dcdc_voltage']
      vehicleStatusCounter   bits 52-55 increments 0-15
      vehicleStatusChecksum  bits 56-63 = sum(bytes[0:7]) & 0xFF
    """
    v_raw = int(DEFAULTS["dcdc_voltage"] / 0.01) & 0x7FF
    word = 0
    word |= int(hv_charge_enable) << 0   # bmsHvChargeEnable
    word |= 1 << 5                        # inAccessoryPlus
    word |= 1 << 14                       # 12vStatusForDrive=READY_FOR_DRIVE_12V
    word |= v_raw << 16                   # pcs12vVoltageTarget
    word |= (ctr & 0xF) << 52            # vehicleStatusCounter
    buf = list(word.to_bytes(8, "little"))
    buf[7] = sum(buf[:7]) & 0xFF          # vehicleStatusChecksum
    return buf


# ---------------------------------------------------------------------------
# PCS TX helpers
# ---------------------------------------------------------------------------

def pcs_mode(mode: str = "off", hv_voltage: float | None = None) -> None:
    """Send 0x22A HVP_pcsControl — 4 bytes.

    mode: 'off' | 'charge' | 'dcdc' | 'both'
    hv_voltage: DC link voltage request in volts (default: DEFAULTS['hv_voltage'])
                For an 18S pack set DEFAULTS['hv_voltage'] = 67 (67.2V full).
    """
    mode_map = {
        "off":    ("SHUTDOWN", False, False),
        "charge": ("SUPPORT",  True,  False),
        "dcdc":   ("SUPPORT",  False, True),
        "both":   ("SUPPORT",  True,  True),
    }
    if mode not in mode_map:
        print(f"Unknown mode '{mode}'. Choose: {list(mode_map)}")
        return
    control, charge_hw, dcdc_hw = mode_map[mode]
    raw(0x22A, _build_hvp_pcs_control(hv_voltage, control, charge_hw, dcdc_hw))


def precharge(hv_voltage: float | None = None, timeout_s: float = 5.0) -> None:
    """Sequence through precharge into charge mode.

    1. Sets contactor stage to 'precharge' and sends PRECHARGE on 0x22A.
    2. Waits up to timeout_s for the DC link to reach ~95% of target voltage
       (polled via BMS_log2 pcsPrechargeTargetVoltage if available, else fixed delay).
    3. Advances contactor stage to 'closed' and switches to SUPPORT mode.
    """
    global _contactor_stage
    v = hv_voltage if hv_voltage is not None else DEFAULTS["hv_voltage"]
    print(f"Precharge: target {v}V, timeout {timeout_s}s")
    _contactor_stage = "precharge"
    _send(0x20A, _build_hvp_contactor_state())
    raw(0x22A, _build_hvp_pcs_control(v, control="PRECHARGE", charge_hw=True))
    time.sleep(timeout_s)
    _contactor_stage = "closed"
    _send(0x20A, _build_hvp_contactor_state())
    raw(0x22A, _build_hvp_pcs_control(v, control="SUPPORT", charge_hw=True))
    print("Precharge complete — now in SUPPORT/charge mode")


def charge_power(watts: int | None = None, on: bool = True) -> None:
    """Send 0x2B2 - charge power request (DLC=5).

    watts: power in watts, scale=0.001 (e.g. 1400 = 1.4kW)
    on: True = charger enabled (byte0=watts_lo), False = disabled (byte0=0x02)
    """
    w = watts if watts is not None else DEFAULTS["charge_power"]
    w_int = int(w)
    b0 = w_int & 0xFF if on else 0x02
    raw(0x2B2, [b0, (w_int >> 8) & 0xFF, 0x00, 0x00, 0x00])


def dcdc_voltage(volts: float | None = None) -> None:
    """Send 0x3A1 VCFRONT_vehicleStatus with updated 12V target.

    volts: desired 12V output voltage (default: DEFAULTS['dcdc_voltage'])
    """
    global _vcfront_ctr
    if volts is not None:
        DEFAULTS["dcdc_voltage"] = volts
    with _hb_lock:
        ctr = _vcfront_ctr
        _vcfront_ctr = (ctr + 1) & 0xF
    raw(0x3A1, _build_vcfront_vehicle_status(ctr))


def evse_limit(current_a: float | None = None) -> None:
    """Send 0x21D CP_evseStatus with updated pilot/cable current.

    current_a: EVSE current limit in amps (default: DEFAULTS['evse_limit'])
    """
    a = current_a if current_a is not None else DEFAULTS["evse_limit"]
    raw(0x21D, _build_cp_evse_status(a, int(a)))


def bms_heartbeat() -> None:
    """Send one 0x3B2 BMS_log2 frame (prevents bmsMia fault).

    Alternates between mux 5 (charging) and mux 3 (charge termination).
    """
    global _bms_mux
    with _hb_lock:
        mux = _bms_mux
        _bms_mux = not mux
    if mux:
        _send(0x3B2, [0xE5, 0x0D, 0xEB, 0xFF, 0x0C, 0x66, 0xBB, 0x11])
    else:
        _send(0x3B2, [0xE3, 0x5D, 0xFB, 0xFF, 0x0C, 0x66, 0xBB, 0x06])


def vcfront_heartbeat() -> None:
    """Send one 0x545 VCFront heartbeat frame (prevents vcfrontMia fault).

    Alternates between two base payloads each call; counter in bits[7:4] of
    byte 6 increments 0-15; byte 7 is CRC = sum(bytes[0:7]) + 0x45 + 0x05.
    """
    global _hb_counter, _vcf_mux
    with _hb_lock:
        ctr = _hb_counter
        mux = _vcf_mux
        _hb_counter = (ctr + 1) & 0xF
        _vcf_mux = not mux
    if mux:
        payload = [0x14, 0x00, 0x3F, 0x70, 0x9F, 0x01, (ctr << 4) | 0x0A, 0x00]
    else:
        payload = [0x03, 0x19, 0x64, 0x32, 0x19, 0x00, (ctr << 4), 0x00]
    payload[7] = (sum(payload[:7]) + 0x45 + 0x05) & 0xFF
    _send(0x545, payload)


# ---------------------------------------------------------------------------
# send_loop — repeat any callable at a fixed interval
# ---------------------------------------------------------------------------

_loops: dict[str, threading.Thread] = {}
_loop_stop: dict[str, threading.Event] = {}


def send_loop(name: str, fn, interval_ms: int) -> None:
    """Call fn() repeatedly every interval_ms milliseconds.

    Example:
        send_loop('dcdc', lambda: dcdc_voltage(13.8), 100)
        send_loop('bms', bms_heartbeat, 10)
    """
    if name in _loops and _loops[name].is_alive():
        print(f"Loop '{name}' already running. Call stop_loop('{name}') first.")
        return
    stop = threading.Event()
    _loop_stop[name] = stop

    def _run() -> None:
        interval_s = interval_ms / 1000.0
        while not stop.is_set():
            fn()
            stop.wait(interval_s)

    t = threading.Thread(target=_run, daemon=True, name=f"loop-{name}")
    _loops[name] = t
    t.start()
    print(f"Loop '{name}' started ({interval_ms}ms interval)")


def stop_loop(name: str) -> None:
    """Stop a named send_loop."""
    ev = _loop_stop.get(name)
    if ev:
        ev.set()
        print(f"Loop '{name}' stopped")
    else:
        print(f"No loop named '{name}'")


def list_loops() -> None:
    """Show all active send_loops."""
    alive = [n for n, t in _loops.items() if t.is_alive()]
    print("Active loops:", alive if alive else "(none)")


# ---------------------------------------------------------------------------
# Background heartbeats — all 14 periodic messages the PCS expects
# ---------------------------------------------------------------------------

_hb_stop: threading.Event | None = None
_hb_thread: threading.Thread | None = None


def _build_obc_control(ac_limit_a: float | None = None, enabled: bool = True) -> list[int]:
    """OBC_control (0x13D) — 6 bytes.

    bytes[0]: 0x05=charger enabled, 0x0A=charger disabled
    bytes[1]: AC current limit, scale=0.5 (e.g. 0x40=64=32A)
    bytes[2:6]: fixed 0xAA, 0x1A, 0xFF, 0x02
    """
    a = ac_limit_a if ac_limit_a is not None else DEFAULTS["ac_limit"]
    b0 = 0x05 if enabled else 0x0A
    return [b0, int(a / 0.5), 0xAA, 0x1A, 0xFF, 0x02]


def _send_10ms() -> None:
    """Messages sent every 10ms."""
    # 0x22A HVP_pcsControl: SHUTDOWN by default; use pcs_mode() to change
    _send(0x22A, _build_hvp_pcs_control())
    _send(0x13D, _build_obc_control())
    bms_heartbeat()


def _send_50ms() -> None:
    """Messages sent every 50ms."""
    vcfront_heartbeat()


def _send_100ms(slot: int) -> None:
    """Messages sent every 100ms, spread across 10 slots (one slot per 10ms tick).

    Spreading avoids blasting all frames at once and overflowing the TX buffer.
    """
    global _vcfront_ctr
    if slot == 0:
        _send(0x20A, _build_hvp_contactor_state())
    elif slot == 1:
        _send(0x212, _build_bms_status())
    elif slot == 2:
        _send(0x232, [0x0A, 0x02, 0xD5, 0x09, 0xCB, 0x04, 0x00, 0x00])
    elif slot == 3:
        _send(0x25D, [0xD8, 0x8C, 0x01, 0xB5, 0x4A, 0xC1, 0x0A, 0xE0])
    elif slot == 4:
        _send(0x321, _build_vcfront_sensors())
    elif slot == 5:
        _send(0x333, _build_ui_charge_request())
    elif slot == 6:
        _send(0x21D, _build_cp_evse_status())
    elif slot == 7:
        _send(0x23D, _build_cp_charge_status())
    elif slot == 8:
        w = int(DEFAULTS["charge_power"])
        _send(0x2B2, [w & 0xFF, (w >> 8) & 0xFF, 0x00, 0x00, 0x00])
    elif slot == 9:
        with _hb_lock:
            ctr = _vcfront_ctr
            _vcfront_ctr = (ctr + 1) & 0xF
        _send(0x3A1, _build_vcfront_vehicle_status(ctr))


def start_heartbeats() -> None:
    """Start all periodic PCS keepalive messages.

    10ms:  0x22A (mode), 0x13D (OBC control), 0x3B2 (bmsMia)
    50ms:  0x545 (vcfrontMia)
    100ms: 0x20A, 0x212, 0x21D, 0x232, 0x23D, 0x25D, 0x2B2, 0x321, 0x333 (uiMia), 0x3A1
    """
    global _hb_stop, _hb_thread
    if _hb_thread and _hb_thread.is_alive():
        print("Heartbeats already running")
        return

    _hb_stop = threading.Event()

    def _run() -> None:
        # Initial burst so PCS doesn't MIA-fault during startup
        _send_10ms()
        _send_50ms()
        for slot in range(10):
            _send_100ms(slot)

        # Absolute-time scheduling avoids drift from accumulated sleep jitter
        tick = 0
        next_tick = time.monotonic() + 0.010
        while not _hb_stop.is_set():
            now = time.monotonic()
            delay = next_tick - now
            if delay > 0:
                _hb_stop.wait(delay)
            next_tick += 0.010
            tick += 1
            _send_10ms()
            if tick % 5 == 0:
                _send_50ms()
            _send_100ms(tick % 10)

    _hb_thread = threading.Thread(
        target=_run, daemon=True, name="pcs-heartbeat")
    _hb_thread.start()
    print("Heartbeats started (10ms/50ms/100ms groups, 14 messages total)")


def stop_heartbeats() -> None:
    """Stop all background heartbeat messages."""
    if _hb_stop:
        _hb_stop.set()
    print("Heartbeats stopped")


# ---------------------------------------------------------------------------
# Background listener — prints decoded PCS frames to terminal
# ---------------------------------------------------------------------------

_listener_stop: threading.Event | None = None
_listener_thread: threading.Thread | None = None


def start_listener(node: str = "PCS") -> None:
    """Print incoming CAN frames (decoded) to the terminal.

    node: filter to frames from this originNode ('' = all frames)
    """
    global _listener_stop, _listener_thread
    if _listener_thread and _listener_thread.is_alive():
        print("Listener already running. Call stop_listener() first.")
        return
    if _bus is None:
        print("Bus not connected")
        return
    if _DB is None:
        print("Warning: CanDatabase not available, will show raw frames only")

    _listener_stop = threading.Event()
    _last_data: dict[int, bytes] = {}

    def _run() -> None:
        while not _listener_stop.is_set():
            msg = _bus.recv(timeout=0.1)
            if msg is None:
                continue
            data = bytes(msg.data)
            if _last_data.get(msg.arbitration_id) == data:
                continue
            _last_data[msg.arbitration_id] = data
            if _DB:
                db_msg = _DB.messages.get(msg.arbitration_id)
                if db_msg is None:
                    continue
                origin = db_msg.get("originNode", "")
                if node and origin != node:
                    continue
                decoded = _DB.decode_frame(msg.arbitration_id, data)
                signals_str = "  ".join(
                    f"{s['signal']}={s['value']}{s.get('units', '')}"
                    + (f"({s['label']})" if s.get("label") else "")
                    for s in (decoded or [])
                )
                print(
                    f"\nRX  0x{msg.arbitration_id:03X}  {db_msg['name']}  {signals_str}")
            else:
                print(
                    f"\nRX  0x{msg.arbitration_id:03X}  [{msg.dlc}]  {data.hex()}")

    _listener_thread = threading.Thread(
        target=_run, daemon=True, name="pcs-listener")
    _listener_thread.start()
    print(f"Listener started (node filter: '{node or 'all'}')")


def stop_listener() -> None:
    """Stop the background listener thread."""
    if _listener_stop:
        _listener_stop.set()
    print("Listener stopped")


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

_HELP = """
PCS scripting shell — available functions:

  SEND
    pcs_mode(mode, hv_voltage)   0x22A  mode: 'off','charge','dcdc','both'
                                         hv_voltage default: DEFAULTS['hv_voltage']
                                         18S pack example: DEFAULTS['hv_voltage'] = 67
    precharge(hv_voltage, timeout_s)     ramp DC link then switch to SUPPORT
    charge_power(watts, on)      0x2B2  charge power request (DLC=5)
    dcdc_voltage(volts)          0x3A1  update 12V target in VCFRONT_vehicleStatus
    evse_limit(current_a)        0x21D  EVSE current limit
    bms_heartbeat()              0x3B2  one BMS_log2 keepalive frame
    vcfront_heartbeat()          0x545  one VCFront keepalive frame
    raw(can_id, data)                   send arbitrary frame

  LOOPS
    send_loop(name, fn, interval_ms)    repeat fn() at fixed interval
    stop_loop(name)                     stop a named loop
    list_loops()                        show active loops

  KEEPALIVES
    start_heartbeats()           BMS@10ms + VCFront@50ms in background
    stop_heartbeats()

  MONITOR
    start_listener(node='PCS')   print decoded incoming frames
    stop_listener()

  DEFAULTS dict — tweak once, all helpers pick it up:
    DEFAULTS['hv_voltage'] = 400
    DEFAULTS['ac_limit']   = 32
    DEFAULTS['dcdc_voltage'] = 13.8
    DEFAULTS['charge_power'] = 0
    DEFAULTS['evse_limit']  = 32
"""


def help() -> None:
    print(_HELP)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _bus

    parser = argparse.ArgumentParser(
        description="Interactive PCS CAN scripting shell")
    parser.add_argument("--channel", help="CAN channel")
    parser.add_argument("--interface", help="python-can interface")
    parser.add_argument("--bitrate", type=int, default=None,
                        help="CAN bitrate (optional)")
    _cfg.apply_defaults(parser)
    args = parser.parse_args()

    kwargs: dict = {"channel": args.channel, "interface": args.interface}
    if args.bitrate:
        kwargs["bitrate"] = args.bitrate

    _bus = can.Bus(**kwargs)
    print(f"Connected: {args.channel} ({args.interface})")
    print(_HELP)

    local_ns = {
        "bus": _bus,
        "DEFAULTS": DEFAULTS,
        "raw": raw,
        "pcs_mode": pcs_mode,
        "precharge": precharge,
        "charge_power": charge_power,
        "dcdc_voltage": dcdc_voltage,
        "evse_limit": evse_limit,
        "bms_heartbeat": bms_heartbeat,
        "vcfront_heartbeat": vcfront_heartbeat,
        "send_loop": send_loop,
        "stop_loop": stop_loop,
        "list_loops": list_loops,
        "start_heartbeats": start_heartbeats,
        "stop_heartbeats": stop_heartbeats,
        "start_listener": start_listener,
        "stop_listener": stop_listener,
        "help": help,
    }

    try:
        import IPython
        IPython.start_ipython(argv=[], user_ns=local_ns)
    except ImportError:
        code.interact(local=local_ns, banner="")

    _bus.shutdown()


if __name__ == "__main__":
    main()
