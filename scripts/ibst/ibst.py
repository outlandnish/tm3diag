#!/usr/bin/env python3
"""IBST node — iBooster brake actuator. All party (bus B / CANB).

ibstMIA (DIR a158 / da6 b12,13) is an aggregate over {0x38E, 0x39D}. 0x39D IBST_status
is len 5 with a Tesla additive checksum (ctr@8 cksum@0, magic 0xA0); 0x38E is len 6
with a SAE J1850 CRC-8 (poly 0x1D) @byte0 + ctr@byte1-lo (``J1850Frame``).
"""
from __future__ import annotations

from sim_core import BASELINE_FW, PARTY_RATE_S, Node, SimFrame
from tesla_frames import J1850Frame, pack_le

# IBST_status 0x39D brake posture -> (driverBrakeApply@16w2, internalState@18w3, rod raw@21w12).
# Rod travel is IBST_sInputRodDriver: mm = raw * 0.015625 - 5.0, so raw = (mm + 5) * 64.
# "released" keeps raw 0 (= -5 mm) to stay byte-identical to the original builder.
_BRAKE = {
    "released": (1, 0, 0),      # BRAKES_NOT_APPLIED + NO_MODE_ACTIVE
    "applied": (2, 2, 1088),    # DRIVER_APPLYING_BRAKES + LOCAL_BRAKE_REQUEST, ~12 mm rod
    "off": (0, 0, 0),           # NOT_INIT_OR_OFF
    "fault": (3, 0, 0),         # FAULT
}


class Ibst(Node):
    name = "IBST"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        # Brake posture drives BOTH 0x39D copies (party + the 2022 vehicle-bus one). Must agree
        # with the ESP node's `brake` — the DI sees driverBrakeApply from both.
        self.brake = "released"

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]
        return [
            SimFrame("IBST_status", 0x39D, rate, self._ibst_status, 8, 0, bus="party"),
            SimFrame("IBST_0x38E", 0x38E, rate, J1850Frame(6).frame, bus="party"),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        # 2022 DIR validates IBST_status 0x39D on bus A (CANA/vehicle) into a110_brakeMIA;
        # add the vehicle-bus copy alongside the party copies (a158 ibstMIA).
        rate = PARTY_RATE_S[self.name]
        return [
            *self.frames(),
            SimFrame("IBST_status_A", 0x39D, rate, self._ibst_status, 8, 0, bus="vehicle"),
        ]

    def _ibst_status(self) -> bytearray:  # 0x39D, 40ms, len 5, ctr@8 cksum@0 magic 0xA0
        brake_apply, internal_state, rod_raw = _BRAKE[self.brake]
        return pack_le(
            [
                (12, 3, 4),  # IBST_iBoosterStatus = IBOOSTER_ACTIVE_GOOD_CHECK
                (16, 2, brake_apply),     # IBST_driverBrakeApply
                (18, 3, internal_state),  # IBST_internalState
                (21, 12, rod_raw),        # IBST_sInputRodDriver
            ],
            length=5,
        )

    def set_brake(self, posture: str) -> str:
        """Driver externality: brake pedal posture (released|applied|off|fault) -> 0x39D
        driverBrakeApply + internalState + rod travel. Keep in step with ESP's `brake`."""
        key = str(posture).strip().lower()
        if key not in _BRAKE:
            raise ValueError(f"IBST brake must be one of {list(_BRAKE)}")
        self.brake = key
        return key

    def configure(self, **s) -> None:  # scenario keys: brake
        brake = s.pop("brake", None)
        if brake is not None:
            self.set_brake(brake)
        super().configure(**s)

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Ibst
