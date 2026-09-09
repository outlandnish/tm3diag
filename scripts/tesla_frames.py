#!/usr/bin/env python3
"""Shared Tesla Model 3 CAN frame builders (2020 / 12603 gen-26).

Canonical, single-source builders shared by ``vehicle_sim.py`` (rest-of-car
liveness) and ``di.py`` (the interactive dashboard). Keeping one copy avoids the
two tools drifting — and, more importantly, avoids two *different* encoders for
the same arbitration ID.

Contents:
  * bit/counter/checksum helpers (Tesla additive checksum)
  * ``e2e_p02_crc8`` + ``SccmRightStalk`` — SCCM_rightStalk 0x229 gear stalk
    (AutoSAR E2E Profile-2 CRC), with gesture actuation for gear changes
  * ``UiConfig`` + UI builders — UI_powertrainControl 0x334 pedal map etc.

Gear-via-stalk is firmware-confirmed: both PMR and
DIR receive 0x229; the DIR runs the authoritative gear FSM and publishes the
gear to the PMR over IPC, which reports it as DI_gear on 0x118.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# bit packing (compact.json convention: LITTLE-endian, start_position = LSB bit)
# ---------------------------------------------------------------------------


def pack_le(signals: list[tuple[int, int, float]], length: int = 8) -> bytearray:
    """Pack (start_bit, width, raw_value) tuples into a little-endian frame."""
    acc = 0
    for start, width, value in signals:
        acc |= (int(round(value)) & ((1 << width) - 1)) << start
    return bytearray(acc.to_bytes(length, "little"))


def magic(msg_id: int) -> int:
    """Tesla per-message checksum magic = id_low + id_high (mod 256)."""
    if msg_id == 0x3A1:
        return 0x2A  # 2022+ VCFRONT_vehicleStatus reseed (was 0xA4=id_lo+id_hi in 2020)
    return ((msg_id & 0xFF) + ((msg_id >> 8) & 0xFF)) & 0xFF


def place_counter(frame: bytearray, start_bit: int, ctr: int, width: int = 4) -> None:
    bi, sh = start_bit // 8, start_bit % 8  # assumes the counter fits within one byte
    mask = (1 << width) - 1
    frame[bi] = (frame[bi] & ~(mask << sh) & 0xFF) | ((ctr & mask) << sh)


def place_checksum(frame: bytearray, msg_id: int, cksum_start_bit: int) -> None:
    cb = cksum_start_bit // 8
    s = sum(frame[i] for i in range(len(frame)) if i != cb)
    frame[cb] = (magic(msg_id) + s) & 0xFF


def set_bitfield(frame: bytearray, start_bit: int, width: int, value: int) -> None:
    """Set a little-endian bitfield in an existing frame (clear-then-set, not OR)."""
    acc = int.from_bytes(frame, "little")
    mask = ((1 << width) - 1) << start_bit
    acc = (acc & ~mask) | ((int(value) << start_bit) & mask)
    frame[:] = acc.to_bytes(len(frame), "little")


# ---------------------------------------------------------------------------
# SCCM_rightStalk 0x229 -- the gear stalk (bus A / CANA, len 3, 100 ms)
# ---------------------------------------------------------------------------
# UNLIKE the other chassis frames this uses an AutoSAR E2E Profile-2 CRC, NOT the
# Tesla additive checksum -- firmware-confirmed:
# CRC@byte0 + counter@byte1 lo-nibble, CRC-8/H2F (poly 0x2F, init 0xFF, xorout
# 0xFF) over the data bytes + DataID[counter]. The DataIDs match
# compact.json SCCM_rightStalk.autosarDataIds.
# Layout: byte0=CRC, byte1=counter(b0-3)+rightStalkStatus(b4-6), byte2=parkButton(b0-1).
#
# Gear mapping (DIR authoritative):
#   UP_2 (2, full up)      -> Reverse
#   DOWN_2 (4, full down)  -> Drive
#   UP_1/DOWN_1 (1/3, first detent, HELD) -> Neutral
#   park button (byte2 b0-1 == 1)         -> Park
# The DIR requires the detent be held through a multi-tick debounce (a single
# 100 ms frame won't commit); on the bench (motor not spinning) all gears are
# permitted, so only the hold matters. Verify via DI_gear on 0x118.
SCCM_RIGHTSTALK_ID = 0x229
SCCM_RIGHTSTALK_DATA_IDS = [
    124,
    182,
    240,
    47,
    105,
    163,
    221,
    28,
    86,
    144,
    202,
    9,
    67,
    125,
    183,
    241,
]

# SCCM_rightStalkStatus enum
STALK_IDLE = 0
STALK_UP_1 = 1
STALK_UP_2 = 2
STALK_DOWN_1 = 3
STALK_DOWN_2 = 4

# Default gesture hold durations (seconds). The DIR debounce tick rate is not
# pinned; ~0.6 s reliably clears it on the bench. Neutral wants a longer hold.
HOLD_DRIVE_S = 0.6
HOLD_REVERSE_S = 0.6
HOLD_NEUTRAL_S = 1.0
HOLD_PARK_S = 0.4


def e2e_p02_crc8(data: bytes, data_id: int) -> int:
    """AutoSAR E2E Profile 2 CRC: CRC-8/H2F (poly 0x2F) over data bytes then DataID, xorout 0xFF."""
    crc = 0xFF
    for b in bytes(data) + bytes([data_id & 0xFF]):
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x2F) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


def j1850_crc8(data: bytes) -> int:
    """SAE J1850 CRC-8 (poly 0x1D, init 0xFF, xorout 0xFF) over ``data``.

    The DIR validates ESP_party3 0x38D and IBST 0x38E with it (CRC table @DIR
    0xb7d24 entry[1]=0x1D); cf. ``e2e_p02_crc8`` (AutoSAR E2E-P02, poly 0x2F) used
    by SCCM_rightStalk 0x229 — three distinct CRC/checksum schemes coexist on this bus.
    """
    crc = 0xFF
    for b in bytes(data):
        crc ^= b & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc ^ 0xFF


class J1850Frame:
    """CANB liveness frame with J1850 CRC@byte0 + 4-bit rolling counter@byte1
    lo-nibble (ESP_party3 0x38D len7, IBST 0x38E len6). The CRC covers bytes
    1..len-1 (all data except the CRC byte). Own rolling counter, like the other
    validated-frame builders here."""

    def __init__(self, length: int) -> None:
        self._ctr = 0
        self._len = length

    def frame(self) -> bytes:
        data = bytearray(self._len)
        data[1] = (data[1] & 0xF0) | (self._ctr & 0xF)  # counter in byte1 lo-nibble
        data[0] = j1850_crc8(bytes(data[1:]))  # CRC over bytes 1..len-1
        self._ctr = (self._ctr + 1) & 0xF
        return bytes(data)

    def rollback(self) -> None:
        """Undo the last frame()'s counter advance. Called (via SimFrame.note_send) when the
        send DROPPED: the counter value never reached the wire, so reusing it on the next frame
        keeps the on-wire sequence gapless -- else the DIR sees a jump and its validated-frame
        MIA won't reset until resync. The counter lives here (not in SimFrame) because the J1850
        CRC is computed over it, so the rollback must live here too."""
        self._ctr = (self._ctr - 1) & 0xF


class SccmRightStalk:
    """0x229 builder: own rolling counter + AutoSAR E2E-P02 CRC.

    Call ``frame()`` every 100 ms. By default it emits a steady IDLE stalk (keeps
    sccmMIA cleared, changes no gear). To actuate a gear change, call one of the
    gesture methods (``drive``/``reverse``/``neutral``/``park``) or ``pulse``:
    the requested detent is held for ``duration_s`` and then auto-returns to IDLE,
    exactly like a momentary stalk press.
    """

    def __init__(self, status: int = STALK_IDLE, park: int = 0) -> None:
        self._ctr = 0
        self._status = status
        self._park = park
        self._hold_until = 0.0

    def pulse(self, status: int = STALK_IDLE, park: int = 0, duration_s: float = 0.6) -> None:
        """Assert a momentary gesture (status/park) for duration_s, then IDLE."""
        self._status = status & 0x7
        self._park = park & 0x3
        self._hold_until = time.monotonic() + duration_s

    def drive(self, duration_s: float = HOLD_DRIVE_S) -> None:
        self.pulse(STALK_DOWN_2, duration_s=duration_s)

    def reverse(self, duration_s: float = HOLD_REVERSE_S) -> None:
        self.pulse(STALK_UP_2, duration_s=duration_s)

    def neutral(self, duration_s: float = HOLD_NEUTRAL_S) -> None:
        # First-detent held -> Neutral. DOWN_1 and UP_1 both map to N.
        self.pulse(STALK_DOWN_1, duration_s=duration_s)

    def park(self, duration_s: float = HOLD_PARK_S) -> None:
        self.pulse(STALK_IDLE, park=1, duration_s=duration_s)

    def idle(self) -> None:
        self._status = STALK_IDLE
        self._park = 0
        self._hold_until = 0.0

    def frame(self) -> bytes:
        if time.monotonic() >= self._hold_until:
            self._status = STALK_IDLE
            self._park = 0
        b1 = (self._ctr & 0xF) | ((self._status & 0x7) << 4)
        b2 = self._park & 0x3
        data = bytearray([0x00, b1, b2])
        data[0] = e2e_p02_crc8(bytes(data[1:]), SCCM_RIGHTSTALK_DATA_IDS[self._ctr & 0xF])
        self._ctr = (self._ctr + 1) & 0xF
        return bytes(data)

    def rollback(self) -> None:
        """Undo the last frame()'s counter advance on a dropped send (see J1850Frame.rollback)."""
        self._ctr = (self._ctr - 1) & 0xF


# ---------------------------------------------------------------------------
# UI command frames -- the DI acts on these (pedal map, stopping mode, ...)
# ---------------------------------------------------------------------------
PEDAL_MAP = {"chill": 0, "sport": 1, "performance": 2}
STOPPING_MODE = {"standard": 0, "creep": 1, "hold": 2}
MOTOR_ON_MODE = {"normal": 0, "front": 1, "rear": 2}
WINCH_MODE = {"idle": 0, "enter": 1, "exit": 2}
TRACTION_MODE = {"normal": 0, "slip_start": 1, "rolls": 4, "dyno": 5}


@dataclass
class UiConfig:
    """UI-command options the DI acts on (pedal map / stopping mode / ...)."""

    pedal_map: int = 0  # UI_pedalMap: CHILL
    stopping_mode: int = 0  # UI_stoppingMode: STANDARD (0=std,1=creep,2=hold)
    motor_on_mode: int = 0  # UI_motorOnMode: NORMAL
    track_mode: int = 2  # UI_trackModeRequest: OFF (2); ON=1
    winch_mode: int = 0  # UI_winchModeRequest: IDLE
    trailer_mode: int = 0  # UI_trailerMode: OFF
    traction_mode: int = 0  # UI_tractionControlMode: NORMAL


def ui_cruise_control(_c: UiConfig) -> bytearray:  # 0x213, DLC2
    return pack_le([(0, 3, 0)], length=2)  # UI_cruiseSpeedCommand = IDLE


def ui_chassis_control(c: UiConfig) -> bytearray:  # 0x293, DLC8
    return pack_le(
        [
            (2, 3, c.traction_mode),  # UI_tractionControlMode
            (8, 2, c.winch_mode),  # UI_winchModeRequest
            (12, 1, c.trailer_mode),  # UI_trailerMode
        ]
    )


def ui_track_mode_settings(c: UiConfig) -> bytearray:  # 0x313, DLC8
    return pack_le(
        [
            (0, 2, c.track_mode),  # UI_trackModeRequest  (0=idle,1=on,2=off)
            (8, 8, 0),  # UI_trackRotationTendency 0%
            (16, 8, 0),  # UI_trackStabilityAssist 0%
        ]
    )


def ui_powertrain_control(c: UiConfig) -> bytearray:  # 0x334, DLC8 (raw payload)
    # limits -> SNA so the DI is NOT power/torque/speed capped by the UI on the bench.
    return pack_le(
        [
            (0, 5, 31),  # UI_systemPowerLimit  = SNA
            (5, 2, c.pedal_map),  # UI_pedalMap
            (8, 6, 63),  # UI_systemTorqueLimit = SNA
            (16, 8, 255),  # UI_speedLimit        = SNA
            (34, 2, c.motor_on_mode),  # UI_motorOnMode
            (40, 2, c.stopping_mode),  # UI_stoppingMode
        ]
    )


# 0x334 needs the Tesla additive checksum + rolling counter (ctr @52, cksum @56).
UI_POWERTRAIN_CONTROL_ID = 0x334


class UiPowertrainControl:
    """0x334 builder: reads a UiConfig live + own rolling counter/checksum.

    Used by di.py's periodic scheduler. vehicle_sim.py applies the counter/checksum
    via its own SimFrame wrapper instead, so it uses ``ui_powertrain_control`` raw.
    """

    def __init__(self, cfg: UiConfig) -> None:
        self.cfg = cfg
        self._ctr = 0

    def frame(self) -> bytes:
        data = ui_powertrain_control(self.cfg)
        place_counter(data, 52, self._ctr)  # @52 w4 (byte6 hi nibble) 2022 layout
        place_checksum(data, UI_POWERTRAIN_CONTROL_ID, 56)  # @56 (byte7) 2022 layout
        self._ctr = (self._ctr + 1) & 0xF
        return bytes(data)

    def rollback(self) -> None:
        """Undo the last frame()'s counter advance on a dropped send (see J1850Frame.rollback)."""
        self._ctr = (self._ctr - 1) & 0xF


# ---------------------------------------------------------------------------
# UiConfig control registry — single source of truth for which UiConfig fields
# are user-controllable and the option names each accepts. Consumed by di.py's
# `ui()` verb and tm3web.py's dashboard so both drive the same knobs.
# ---------------------------------------------------------------------------
UI_SETTINGS: dict[str, dict] = {
    "pedal_map": {"label": "Pedal map", "options": dict(PEDAL_MAP)},
    "stopping_mode": {"label": "Stopping mode", "options": dict(STOPPING_MODE)},
    "motor_on_mode": {"label": "Motor on mode", "options": dict(MOTOR_ON_MODE)},
    "traction_mode": {"label": "Traction control", "options": dict(TRACTION_MODE)},
    "winch_mode": {"label": "Winch mode", "options": dict(WINCH_MODE)},
    "track_mode": {"label": "Track mode", "options": {"on": 1, "off": 2}},
    "trailer_mode": {"label": "Trailer mode", "options": {"off": 0, "on": 1}},
}


def apply_ui_setting(cfg: UiConfig, field: str, value: object) -> int:
    """Set a UiConfig field from an option name (str) or raw int. Returns the
    applied int. Raises KeyError (unknown field) or ValueError (bad value)."""
    if field not in UI_SETTINGS:
        raise KeyError(f"unknown UI setting {field!r}; try {list(UI_SETTINGS)}")
    opts = UI_SETTINGS[field]["options"]
    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        key = value.strip().lower()
        if key not in opts:
            raise ValueError(f"{field}: expected one of {list(opts)}, got {value!r}")
        ival = opts[key]
    else:
        ival = int(value)
        if ival not in opts.values():
            raise ValueError(f"{field}: {ival} invalid; valid: {sorted(opts.values())}")
    setattr(cfg, field, ival)
    return ival


# ---------------------------------------------------------------------------
# VCFRONT_LVPowerState 0x221 -- LV power / vehicle power state (50 ms, muxed,
# counter@52 + Tesla checksum@56 magic 0x23). VCFRONT_vehiclePowerState (byte0 b5-6):
#   off=0 conditioning=1 accessory=2 drive=3. Shared by vehicle_sim + tm3web.
# NOTE: any continuous 0x221 re-pokes the immobilizer drive-readiness eval each frame.
# ---------------------------------------------------------------------------
VCFRONT_LVPOWERSTATE_ID = 0x221
VEHICLE_POWER_STATE = {"off": 0, "conditioning": 1, "accessory": 2, "drive": 3}
_LV_MUX0_BITS = (8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48)
_LV_MUX1_BITS = (8, 10, 12, 14, 16, 18)
_LV_ON = 1  # VCFRONT LVPowerState per-ECU enum: LV_ON


class LvPowerState:
    """0x221 builder: alternates mux0/mux1, all per-ECU LV states = LV_ON, own
    rolling counter + canonical checksum. ``vps`` = VCFRONT_vehiclePowerState (0-3)."""

    def __init__(self, vehicle_power_state: int = 3) -> None:
        self._ctr = 0
        self._mux = 0
        self.vps = vehicle_power_state

    def frame(self) -> bytes:
        mux = self._mux
        signals = [(0, 5, mux), (5, 2, self.vps)]  # index + vehiclePowerState (both muxes)
        for start in _LV_MUX0_BITS if mux == 0 else _LV_MUX1_BITS:
            signals.append((start, 2, _LV_ON))
        data = pack_le(signals, 8)
        place_counter(data, 52, self._ctr)  # @52 w4 (byte6) 2022 layout
        place_checksum(data, VCFRONT_LVPOWERSTATE_ID, 56)  # @56 (byte7), magic 0x23
        self._ctr = (self._ctr + 1) & 0xF
        self._mux ^= 1
        return bytes(data)

    def rollback(self) -> None:
        """Undo the last frame()'s counter + mux advance on a dropped send (see
        J1850Frame.rollback) so the reused frame is byte-identical to the one that dropped."""
        self._ctr = (self._ctr - 1) & 0xF
        self._mux ^= 1


# ---------------------------------------------------------------------------
# GTW_carConfig 0x7FF -- multiplexed car config, built from NAMED signals via the
# DB encoder (every field configurable) instead of a canned frame.
# ---------------------------------------------------------------------------
GTW_CARCONFIG_ID = 0x7FF
GTW_DEFAULTS = {"GTW_chassisType": 2, "GTW_drivetrainType": 0}  # RWD Model 3


class MuxedConfigTx:
    """Builds a multiplexed config message one page per call from named signal
    overrides, via the DB signal encoder. Cycles all mux pages; ``schema()`` feeds
    the dashboard editor. Duck-typed on ``db`` (needs .messages + .encode_frame)."""

    def __init__(self, db, msg_id: int, defaults: dict | None = None) -> None:
        self.db = db
        self.msg_id = msg_id
        self.msg = db.messages[msg_id]
        self.signals = self.msg["signals"]
        self.muxer_name = next(sn for sn, s in self.signals.items() if s.get("is_muxer"))
        self.pages = sorted(
            {s.get("mux_id") for s in self.signals.values() if s.get("mux_id") is not None}
        )
        self.values: dict[str, int] = {}
        self._i = 0
        for sn, v in (defaults or {}).items():
            self.set(sn, v)

    def _coerce(self, sname: str, value: object) -> int:
        vd = self.signals[sname].get("value_description")
        if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
            for label, raw in (vd or {}).items():
                if label.lower() == value.strip().lower():
                    return int(raw)
            raise ValueError(f"{sname}: expected one of {list(vd or [])}, got {value!r}")
        return int(value)

    def set(self, sname: str, value: object) -> int:
        sig = self.signals.get(sname)
        if sig is None or sig.get("is_muxer"):
            raise KeyError(f"unknown config signal {sname!r}")
        self.values[sname] = self._coerce(sname, value)
        return self.values[sname]

    def next_frame(self) -> bytes:
        page = self.pages[self._i % len(self.pages)]
        self._i += 1
        sv: dict[str, float | int] = {self.muxer_name: page}
        for sn, raw in self.values.items():
            if self.signals[sn].get("mux_id") == page:
                sv[sn] = raw
        return self.db.encode_frame(self.msg_id, sv)

    def schema(self) -> dict:
        pages = []
        for pg in self.pages:
            sigs = sorted(
                (
                    {
                        "name": sn,
                        "width": s["width"],
                        "options": s.get("value_description"),
                        "value": self.values.get(sn, 0),
                    }
                    for sn, s in self.signals.items()
                    if s.get("mux_id") == pg
                ),
                key=lambda x: x["name"],
            )
            pages.append({"page": pg, "signals": sigs})
        return {"msg": self.msg.get("name", ""), "id": self.msg_id, "pages": pages}


# ---------------------------------------------------------------------------
# EPB closed-loop responder -- answers DI_epbRequest (0x118) with EPBL/EPBR status.
# DI_epbRequest: 0=NO_REQUEST 1=PARK 2=UNPARK.  systemStatus @0 w4.
# ---------------------------------------------------------------------------
EPBL_STATUS_ID = 0x2A8
EPBR_STATUS_ID = 0x2E8
EPB_RELEASED = 1
EPB_PARKED = 2


class EpbResponder:
    """Closed-loop parking brake. Feed DI_epbRequest to ``on_epb_request`` and read
    ``payload()`` for the EPBL/EPBR_status bytes. The rolling counter + checksum are
    added by the frame wrapper (vehicle_sim ctr@52/cksum@56).

    Fields the DIR actually unpacks from EPBL/EPBR_status (layout symmetric):
      bits0-3  systemStatus       (RELEASED/PARKED, driven by DI_epbRequest)
      bits4-5  freeRollModeStatus (UNAVAILABLE=0, left 0 = correct)
      bit17    summonEnabled      (not summoning=0, left 0)
      bit47    okToPark           (EPBL/EPBR)
    A healthy stationary EPB asserts okToPark; the old zeros-except-status payload left it 0.
    telltale(bits12-14) is UI-only (not DIR-consumed) -- tracked here only for HUD fidelity."""

    def __init__(self) -> None:
        self.status = EPB_RELEASED

    def on_epb_request(self, di_epb_request: int) -> None:
        if di_epb_request == 1:  # PARK
            self.status = EPB_PARKED
        elif di_epb_request == 2:  # UNPARK
            self.status = EPB_RELEASED

    def payload(self) -> bytearray:
        telltale = 1 if self.status == EPB_PARKED else 0  # 1=RED_ON when parked, 0=OFF (UI only)
        return pack_le([(0, 4, self.status), (12, 3, telltale), (47, 1, 1)], 8)  # +okToPark=1


# ---------------------------------------------------------------------------
# VehicleController -- single source of truth for all DU-facing command state.
# vehicle_sim builds the interactive + closed-loop frames from it; tm3web mutates
# it (via vehicle_sim's control server). One owner, no cross-tool frame collisions.
# ---------------------------------------------------------------------------
GEAR_GESTURE = {
    "P": "park",
    "PARK": "park",
    "R": "reverse",
    "REVERSE": "reverse",
    "N": "neutral",
    "NEUTRAL": "neutral",
    "D": "drive",
    "DRIVE": "drive",
}


class VehicleController:
    def __init__(self, db) -> None:
        self.stalk = SccmRightStalk()
        self.uicfg = UiConfig()
        self.ui_pt = UiPowertrainControl(self.uicfg)
        self.lv = LvPowerState(VEHICLE_POWER_STATE["drive"])
        self.carcfg = MuxedConfigTx(db, GTW_CARCONFIG_ID, defaults=GTW_DEFAULTS)
        self.epb = EpbResponder()
        self.last_gear_cmd: str | None = None

    def gear(self, letter: str) -> str:
        verb = GEAR_GESTURE.get(str(letter).strip().upper())
        if verb is None:
            raise ValueError("gear must be P/R/N/D")
        getattr(self.stalk, verb)()
        self.last_gear_cmd = str(letter).strip().upper()
        return self.last_gear_cmd

    def set_ui(self, field: str, value: object) -> int:
        return apply_ui_setting(self.uicfg, field, value)

    def set_lv(self, state: str) -> int:
        key = str(state).strip().lower()
        if key not in VEHICLE_POWER_STATE:
            raise ValueError(f"lv state must be one of {list(VEHICLE_POWER_STATE)}")
        self.lv.vps = VEHICLE_POWER_STATE[key]
        return self.lv.vps

    def set_carconfig(self, signal: str, value: object) -> int:
        return self.carcfg.set(signal, value)

    def state(self) -> dict:
        """JSON-able snapshot of commanded state (consumed by tm3web's dashboard)."""
        return {
            "commanded_gear": self.last_gear_cmd,
            "lv_state": next((k for k, v in VEHICLE_POWER_STATE.items() if v == self.lv.vps), None),
            "lv_options": list(VEHICLE_POWER_STATE),
            "ui": {f: getattr(self.uicfg, f) for f in UI_SETTINGS},
            "ui_schema": {
                f: {"label": s["label"], "options": s["options"]} for f, s in UI_SETTINGS.items()
            },
            "epb_status": self.epb.status,
        }
