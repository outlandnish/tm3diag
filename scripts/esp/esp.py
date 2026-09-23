#!/usr/bin/env python3
"""ESP node — stability control, brake + VDC inputs. All party (bus B / CANB).

espMIA (DIR a091) is an aggregate over {0x105, 0x11D, 0x145, 0x155, 0x175, 0x185, 0x38D};
it clears only when every member arrives with a valid checksum + rolling counter. 0x11D also
feeds the DIR VDC freshness watchdog (a195/a196/a197 vdcEspSlip + a210), so it runs at
PARTY_LIVENESS_S.

Three checksum schemes:
  - Tesla additive (byte7 cksum, ctr@52 byte6-hi): 0x105/0x155/0x175/0x185.
  - Tesla additive (byte0 cksum, ctr@8): 0x145 (magic 0x46), 0x11D (magic 0x1E).
  - SAE J1850 CRC-8 (poly 0x1D) @byte0, ctr@byte1-lo: 0x38D (``J1850Frame``).

Several payloads carry VDC input-validity sub-fields: the DIR treats value==0 as valid only
when the matching availability status bit is SET, so these builders assert those bits
(brake torque / MC pressure / wheel-speed direction) to clear a193/a194/a198/a200/….
"""
from __future__ import annotations

from sim_core import PARTY_RATE_S, Node, SimFrame, clamp_pct
from tesla_frames import (
    DI_BRAKE_SWITCH_ID,
    GTW_CARCONFIG_ID,
    J1850Frame,
    di_brake_switch_pressed,
    gtw_brake_line_switch_type,
    gtw_wheel_type,
    normalize_brake_line_switch_type,
    pack_le,
)

# ESP_status 0x145 brake posture -> (driverBrakeApply@29w2, brakeApply@31, brakeLamp@21).
_BRAKE = {
    "released": (1, 0, 0),  # Not_Applied — bench default, DI sees no brake
    "applied": (2, 1, 1),   # Driver_applying_brakes + apply flag + lamp
    "off": (0, 0, 0),       # NotInit_orOff
    "sna": (3, 0, 0),       # Faulty_SNA
}
_ABS_EVENT = {"none": 0, "front_rear": 1, "front": 2, "rear": 3}  # ESP_absBrakeEvent2 @22w2
_STABILITY = {"init": 0, "on": 1, "engaged": 2, "faulted": 3}     # ESP_stabilityControlSts2 @14w2

_APPLIED_DEFAULT_PCT = 50.0  # 0x38D master-cyl pressure when "applied" with no explicit pressure
_BAR_PER_PCT = 1.0           # bench mapping: 100% pedal -> 100 bar master-cyl pressure

# ESP_wheelSpeeds 0x175 (ETH DBC): FrL@0 FrR@13 ReL@26 ReR@39, 13 bits, 0.042 km/h, 0x1FFF SNA.
# wheel_speeds="axle" reports the rear wheels turning with DIR_axleSpeed (0x108 @40 s16, 0.1 rpm),
# as a real ESP would with the car on a lift; "zero" = stationary.
# Tire size follows GTW_carConfig GTW_wheelType (0x7FF mux3), rim inches per the DBC value names.
_DIR_TORQUE_ID = 0x108
_WHEEL_SPEEDS = ("zero", "axle")
_WHEEL_TYPE_RIM_IN = {
    0: 18, 18: 18, 22: 18, 24: 18,                 # PINWHEEL_18 (+ cap kit / refresh)
    1: 19, 4: 19, 5: 19, 17: 19, 20: 19, 21: 19, 27: 19,  # STILETTO/GEMINI/APOLLO/ZEROG 19
    2: 20, 3: 20, 14: 20, 15: 20, 19: 20, 23: 20,  # STILETTO/INDUCTION/ZEROG/UBERTURBINE 20
    16: 21,                                        # UBERTURBINE_21
}
_TIRE_CIRCUMFERENCE_M = {  # stock fitment, circumference = pi * (rim + 2 * sidewall)
    18: 2.101,  # 235/45R18
    19: 2.107,  # 235/40R19
    20: 2.113,  # 235/35R20
    21: 2.236,  # 255/35R21
}
_KPH_PER_LSB = 0.042


def _esp_0x175_wheelspeeds(rear_kph: float | None) -> bytearray:
    raw = 0x1FFF if rear_kph is None else min(round(abs(rear_kph) / _KPH_PER_LSB), 0x1FFE)
    return pack_le([(26, 13, raw), (39, 13, raw)])


def _esp_0x105_valid() -> bytearray:  # 0x105: brake torque=0 + MC pressure=0 bar + availability
    return pack_le(
        [
            (13, 1, 1),
            (14, 1, 1),
            (15, 1, 1),  # word0 status -> DIR ESP signal-status bits 4/5/6 (signal available)
            (33, 1, 1),
            (34, 1, 1),
            (35, 1, 1),  # word2 status -> DIR ESP signal-status bits 7/8/9
            (36, 10, 0x64),  # MC pressure 10-bit: raw 0x64 = 0 bar
        ]
    )


def _esp_0x155_valid() -> bytearray:  # 0x155: availability (feeds a232 velocity est + a198)
    return pack_le([(40, 1, 1), (41, 1, 1)])  # word2 bits 8/9 -> DIR ESP signal-status bits 14/15


def _esp_0x185_brake_torques() -> bytearray:
    # 0x185 (not in the ETH DBC): per-wheel brake torque, FrL@0 FrR@12 ReL@24 ReR@36, 12 bits.
    # 0 = no brake torque. The DIR's rolls-learn gate needs the sum <= 0 to allow the ~720 rpm
    # spin ceiling (else ~11 rpm, OFFSET_RESULT 1). Bit 50 = validity flag for the brake-temp model.
    return pack_le([(50, 1, 1)])


def _esp_0x11d_valid() -> bytearray:  # 0x11D otherControllerState = present+valid (clears DI a210)
    # bits54-55 = 2 -> otherControllerState valid, clears a210 (and rollups a199/a222/a223).
    # bits56-63 feed the slip/sat evaluator (a195/196/197); 0 is benign.
    return pack_le([(54, 2, 2)])


class Esp(Node):
    name = "ESP"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        # ESP_status 0x145 state. Defaults = healthy stationary bench: brake released, no
        # ABS/skid event, all QF in spec. stability=ON (not the INIT the original builder left
        # at 0 by omission) — bench-confirmed as required for the DIR to permit drive.
        self.brake = "released"
        self.brake_pressure = None  # optional 0-100% MC pressure; stored for a future 0x38D pack
        # GTW_brakeLineSwitchType; default VC_ONLY -> ignore the DI's 0x1D6 and keep UI/manual
        # control. Learned live from GTW_carConfig 0x7FF mux3 (or set via configure); DI_VC_SHARED
        # -> follow the DI's wired switch.
        self.brake_line_switch_type = "vc_only"
        self.abs_event = "none"
        self.stability = "on"
        self.standstill_skid = False
        self.qf_in_spec = True
        self.abs_fault_lamp = False
        self.ebd_fault_lamp = False
        self.esp_fault_lamp = False
        self.wheel_speeds = "zero"
        self._axle_rpm = 0.0
        self._rim_in = 18  # until GTW_carConfig mux3 arrives

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]
        return [
            SimFrame("ESP_status", 0x145, rate, self._esp_status, 8, 0, bus="party"),
            SimFrame("ESP_0x11D", 0x11D, rate, _esp_0x11d_valid, 8, 0, bus="party"),
            SimFrame("ESP_0x105", 0x105, rate, _esp_0x105_valid, 52, 56, bus="party"),
            SimFrame("ESP_0x155", 0x155, rate, _esp_0x155_valid, 52, 56, bus="party"),
            SimFrame("ESP_wheelSpeeds", 0x175, rate, self._esp_wheel_speeds, 52, 56, bus="party"),
            SimFrame(
                "ESP_0x185_brakeTorques", 0x185, rate, _esp_0x185_brake_torques,
                52, 56, bus="party",
            ),
            SimFrame("ESP_party3", 0x38D, rate, J1850Frame(7, self._esp_party3).frame, bus="party"),
        ]

    def _brake_bar(self) -> float:
        if self.brake_pressure is not None:
            pct = self.brake_pressure
        elif self.brake == "applied":
            pct = _APPLIED_DEFAULT_PCT
        else:
            pct = 0.0
        return pct * _BAR_PER_PCT

    def _esp_party3(self) -> bytearray:  # 0x38D payload (CRC@0 + counter@8 filled by J1850Frame)
        # Master-cyl pressure = the DI brake vote's VoteB. Both the measured (brakeMasterCylPress,
        # 0.3/-30) and modeled (pMcVirtual, 0.25) fields carry it, QF=NORMAL so the DIR reads it
        # valid. 0 bar -> brakeMasterCylPress raw 100 (0x64), matching the 0x105 convention.
        bar = self._brake_bar()
        return pack_le(
            [
                (16, 10, round(bar / 0.25)),      # ESP_pMcVirtual (bar)
                (26, 2, 1),                       # ESP_pMcVirtualQF = NORMAL
                (44, 10, round((bar + 30.0) / 0.3)),  # ESP_brakeMasterCylPress (bar)
                (54, 2, 1),                       # ESP_brakeMasterCylPressQF = NORMAL
            ],
            length=7,
        )

    def _esp_status(self) -> bytearray:  # 0x145, 20ms, ctr@8 cksum@0 magic 0x46
        brake_apply, apply_flag, lamp = _BRAKE[self.brake]
        qf = int(self.qf_in_spec)
        return pack_le(
            [
                (14, 2, _STABILITY[self.stability]),  # ESP_stabilityControlSts2
                (16, 1, int(self.ebd_fault_lamp)),    # ESP_ebdFaultLamp
                (17, 1, int(self.abs_fault_lamp)),    # ESP_absFaultLamp
                (18, 1, int(self.esp_fault_lamp)),    # ESP_espFaultLamp
                (21, 1, lamp),                        # ESP_brakeLamp
                (22, 2, _ABS_EVENT[self.abs_event]),  # ESP_absBrakeEvent2
                # VDC availability -> DIR ESP signal-status bits 10-13; clears a200. These are
                # the four QF (quality-factor) bits = IN_SPEC.
                (24, 1, qf),  # ESP_longitudinalAccelQF
                (25, 1, qf),  # ESP_lateralAccelQF
                (26, 1, qf),  # ESP_yawRateQF
                (27, 1, qf),  # ESP_steeringAngleQF
                (29, 2, brake_apply),  # ESP_driverBrakeApply
                (31, 1, apply_flag),   # ESP_brakeApply
                (34, 2, 1),  # ESP_cdpStatus = CDP_IS_AVAILABLE
                (36, 2, 2),  # ESP_ptcTargetState = ON
                (48, 1, int(self.standstill_skid)),  # ESP_ebrStandstillSkid
            ]
        )

    def set_brake(self, posture: str, pressure=None) -> str:
        """Brake posture (released|applied|off|sna) -> 0x145 driverBrakeApply/brakeApply/lamp.
        Optional pressure (0-100%) is stored for a later 0x38D MC-pressure pack, not yet packed."""
        key = str(posture).strip().lower()
        if key not in _BRAKE:
            raise ValueError(f"ESP brake must be one of {list(_BRAKE)}")
        self.brake = key
        self.brake_pressure = clamp_pct(pressure)
        return key

    def _esp_wheel_speeds(self) -> bytearray:  # 0x175
        rpm = self._axle_rpm if self.wheel_speeds == "axle" else 0.0
        if rpm is None:  # DIR_axleSpeed SNA: report the rears SNA too
            return _esp_0x175_wheelspeeds(None)
        return _esp_0x175_wheelspeeds(rpm * _TIRE_CIRCUMFERENCE_M[self._rim_in] * 60.0 / 1000.0)

    def rx_handlers(self):
        # Mirror the DI's wired brake switch (0x1D6) into ESP brake posture -- but only in
        # DI_VC_SHARED (the switch is shared with the DI). VC_ONLY -> ignore 0x1D6, keep UI control.
        return {
            DI_BRAKE_SWITCH_ID: self._on_di_brake,
            GTW_CARCONFIG_ID: self._on_carconfig,
            _DIR_TORQUE_ID: self._on_dir_torque,
        }

    def _on_dir_torque(self, data, send) -> None:
        if len(data) >= 7:
            raw = int.from_bytes(data[5:7], "little", signed=True)
            self._axle_rpm = None if raw == -0x8000 else raw * 0.1  # 0x8000 = SNA

    def _on_di_brake(self, data, send) -> None:
        if self.brake_line_switch_type == "di_vc_shared":
            self.set_brake("applied" if di_brake_switch_pressed(data) else "released")

    def _on_carconfig(self, data, send) -> None:
        t = gtw_brake_line_switch_type(data)
        if t is not None:
            self.brake_line_switch_type = t
        w = gtw_wheel_type(data)
        if w is not None:
            self._rim_in = _WHEEL_TYPE_RIM_IN.get(w, 18)

    def set_abs_event(self, event: str) -> str:
        """Driver externality: ESP_absBrakeEvent2 (none|front_rear|front|rear). Anything but
        ``none`` tells the DI an ABS event is in progress."""
        key = str(event).strip().lower()
        if key not in _ABS_EVENT:
            raise ValueError(f"ESP abs_event must be one of {list(_ABS_EVENT)}")
        self.abs_event = key
        return key

    def set_stability(self, state: str) -> str:
        """Driver externality: ESP_stabilityControlSts2 (init|on|engaged|faulted)."""
        key = str(state).strip().lower()
        if key not in _STABILITY:
            raise ValueError(f"ESP stability must be one of {list(_STABILITY)}")
        self.stability = key
        return key

    def configure(self, **s) -> None:
        # scenario keys: brake, abs_event, stability, standstill_skid, qf_in_spec,
        #                abs_fault_lamp, ebd_fault_lamp, esp_fault_lamp, wheel_speeds
        for key, setter in (
            ("brake", self.set_brake),
            ("abs_event", self.set_abs_event),
            ("stability", self.set_stability),
        ):
            val = s.pop(key, None)
            if val is not None:
                setter(val)
        for flag in ("standstill_skid", "qf_in_spec", "abs_fault_lamp",
                     "ebd_fault_lamp", "esp_fault_lamp"):
            val = s.pop(flag, None)
            if val is not None:
                setattr(self, flag, bool(val))
        ws = s.pop("wheel_speeds", None)
        if ws is not None:
            ws = str(ws).strip().lower()
            if ws not in _WHEEL_SPEEDS:
                raise ValueError(f"ESP wheel_speeds must be one of {list(_WHEEL_SPEEDS)}")
            self.wheel_speeds = ws
        lt = s.pop("brake_line_switch_type", None)
        if lt is not None:
            self.brake_line_switch_type = normalize_brake_line_switch_type(lt)
        super().configure(**s)


NODE = Esp
