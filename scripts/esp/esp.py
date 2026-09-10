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

from sim_core import PARTY_RATE_S, Node, SimFrame, zeros
from tesla_frames import J1850Frame, pack_le

# ESP_status 0x145 brake posture -> (driverBrakeApply@29w2, brakeApply@31, brakeLamp@21).
_BRAKE = {
    "released": (1, 0, 0),  # Not_Applied — bench default, DI sees no brake
    "applied": (2, 1, 1),   # Driver_applying_brakes + apply flag + lamp
    "off": (0, 0, 0),       # NotInit_orOff
    "sna": (3, 0, 0),       # Faulty_SNA
}
_ABS_EVENT = {"none": 0, "front_rear": 1, "front": 2, "rear": 3}  # ESP_absBrakeEvent2 @22w2
_STABILITY = {"init": 0, "on": 1, "engaged": 2, "faulted": 3}     # ESP_stabilityControlSts2 @14w2


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


def _esp_0x185_wheelspeeds_valid() -> bytearray:  # 0x185: wheel speeds=0 (stationary) + direction
    return pack_le([(50, 1, 1)])  # direction bit -> DIR status bit3 = shared wheel-speed validity


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
        self.abs_event = "none"
        self.stability = "on"
        self.standstill_skid = False
        self.qf_in_spec = True
        self.abs_fault_lamp = False
        self.ebd_fault_lamp = False
        self.esp_fault_lamp = False

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]
        return [
            SimFrame("ESP_status", 0x145, rate, self._esp_status, 8, 0, bus="party"),
            SimFrame("ESP_0x11D", 0x11D, rate, _esp_0x11d_valid, 8, 0, bus="party"),
            SimFrame("ESP_0x105", 0x105, rate, _esp_0x105_valid, 52, 56, bus="party"),
            SimFrame("ESP_0x155", 0x155, rate, _esp_0x155_valid, 52, 56, bus="party"),
            SimFrame("ESP_0x175", 0x175, rate, zeros(8), 52, 56, bus="party"),
            SimFrame(
                "ESP_0x185_wheelSpeeds", 0x185, rate, _esp_0x185_wheelspeeds_valid,
                52, 56, bus="party",
            ),
            SimFrame("ESP_party3", 0x38D, rate, J1850Frame(7).frame, bus="party"),
        ]

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

    def set_brake(self, posture: str) -> str:
        """Driver externality: brake pedal posture (released|applied|off|sna) -> 0x145
        driverBrakeApply + brakeApply + brakeLamp."""
        key = str(posture).strip().lower()
        if key not in _BRAKE:
            raise ValueError(f"ESP brake must be one of {list(_BRAKE)}")
        self.brake = key
        return key

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
        #                abs_fault_lamp, ebd_fault_lamp, esp_fault_lamp
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
        super().configure(**s)


NODE = Esp
