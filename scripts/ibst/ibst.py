#!/usr/bin/env python3
"""IBST node — iBooster brake actuator. All party (bus B / CANB).

ibstMIA (DIR a158 / da6 b12,13) is an aggregate over {0x38E, 0x39D}. 0x39D IBST_status
is len 5 with a Tesla additive checksum (ctr@8 cksum@0, magic 0xA0); 0x38E is len 6
with a SAE J1850 CRC-8 (poly 0x1D) @byte0 + ctr@byte1-lo (``J1850Frame``).
"""
from __future__ import annotations

from sim_core import BASELINE_FW, PARTY_RATE_S, Node, SimFrame, clamp_pct
from tesla_frames import (
    DI_BRAKE_SWITCH_ID,
    GTW_CARCONFIG_ID,
    J1850Frame,
    di_brake_switch_pressed,
    gtw_brake_line_switch_type,
    normalize_brake_line_switch_type,
    pack_le,
)

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
        self.brake_pressure = None  # optional 0-100%; stored for a future rod-travel scale
        # GTW_brakeLineSwitchType; default VC_ONLY -> ignore the DI's 0x1D6 and keep UI/manual
        # control. Learned live from GTW_carConfig 0x7FF mux3 (or set via configure); DI_VC_SHARED
        # -> follow the DI's wired switch.
        self.brake_line_switch_type = "vc_only"

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

    def set_brake(self, posture: str, pressure=None) -> str:
        """Brake posture (released|applied|off|fault) -> 0x39D driverBrakeApply/internalState/rod.
        Keep in step with ESP's `brake`. Optional pressure (0-100%) stored, not yet packed."""
        key = str(posture).strip().lower()
        if key not in _BRAKE:
            raise ValueError(f"IBST brake must be one of {list(_BRAKE)}")
        self.brake = key
        self.brake_pressure = clamp_pct(pressure)
        return key

    def rx_handlers(self):
        # Mirror the DI's wired brake switch (0x1D6) into IBST 0x39D (VoteA) -- but only in
        # DI_VC_SHARED. VC_ONLY -> ignore 0x1D6, keep UI control.
        return {DI_BRAKE_SWITCH_ID: self._on_di_brake, GTW_CARCONFIG_ID: self._on_carconfig}

    def _on_di_brake(self, data, send) -> None:
        if self.brake_line_switch_type == "di_vc_shared":
            self.set_brake("applied" if di_brake_switch_pressed(data) else "released")

    def _on_carconfig(self, data, send) -> None:
        t = gtw_brake_line_switch_type(data)
        if t is not None:
            self.brake_line_switch_type = t

    def configure(self, **s) -> None:  # scenario keys: brake, brake_line_switch_type
        brake = s.pop("brake", None)
        if brake is not None:
            self.set_brake(brake)
        lt = s.pop("brake_line_switch_type", None)
        if lt is not None:
            self.brake_line_switch_type = normalize_brake_line_switch_type(lt)
        super().configure(**s)

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Ibst
