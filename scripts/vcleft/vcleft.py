#!/usr/bin/env python3
"""VCLEFT node — left-body controller. Sources VCLEFT_switchStatus 0x3C2 (its own ECU).

0x3C2 is a vcfrontMIA member (a155) AND the VC's view of the physical brake-switch line the
DI cross-checks against its own GPIO: a mismatch makes the DI publish DI_brakePedalState =
INVALID (live plausibility state, no DTC). So it tracks the PHYSICAL switch, not the ESP/IBST
CAN posture.

When GTW_brakeLineSwitchType == DI_VC_SHARED the switch line is shared with the DI, so the VC
mirrors the DI's own report of it (0x1D6 bit33) onto VCLEFT_brakeSwitchPressed -- keeping the two
in agreement so the DI's cross-check doesn't fault. In VC_ONLY (the bench default) the switch is
wired to the VC alone, so it sources its own state (set_brake_switch) and ignores 0x1D6.
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import (
    DI_BRAKE_SWITCH_ID,
    GTW_CARCONFIG_ID,
    di_brake_switch_pressed,
    gtw_brake_line_switch_type,
    normalize_brake_line_switch_type,
    pack_le,
)


class Vcleft(Node):
    name = "VCLEFT"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.brake_switch_pressed = False
        # GTW_brakeLineSwitchType; default VC_ONLY -> source our own switch and ignore the DI's
        # 0x1D6. Learned live from GTW_carConfig 0x7FF mux3, or set via configure(); DI_VC_SHARED
        # -> mirror the DI's wired switch onto 0x3C2.
        self.brake_line_switch_type = "vc_only"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("VCLEFT_switchStatus", 0x3C2, 0.050, self._switch_status)]  # a155 member

    def _frames_2026(self) -> list[SimFrame]:
        # 2026.8.3 adds 0x142 VCLEFT_liftgateStatus to the DIR's rx set (firmware-enumerated).
        # DLC8, NOT gated (no validator, unlike 0x238/0x318). It is MIA-supervised, so it is sent
        # for the same reason 0x3B3 is on 2022: arrival is what clears the MIA, and optional-node
        # MIA only bites in DRIVE -- which is exactly where a spin test lives.
        #
        # zeros(8) is safe AND sufficient: the handler clears its MIA bit inside
        # `(word0 & 3) == 0`, so a zero payload satisfies the gate. The only decoded field is a
        # liftgate-state nibble (word0 bits 3-6) that nothing in the torque path reads.
        return [*self.frames(), SimFrame("VCLEFT_liftgateStatus", 0x142, 0.100, zeros(8))]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2026.8.3": self._frames_2026}

    def _switch_status(self) -> bytearray:  # 0x3C2 mux0
        pressed = int(self.brake_switch_pressed)
        return pack_le([(4, 1, pressed), (60, 1, pressed)], 8)  # index@0=0 -> mux0

    def set_brake_switch(self, on: bool) -> bool:
        """Physical brake-switch line the DI/VC share (0x3C2 VCLEFT_brakeSwitchPressed)."""
        self.brake_switch_pressed = bool(on)
        return self.brake_switch_pressed

    def set_brake_line_switch_type(self, value) -> str:
        """GTW_brakeLineSwitchType: di_vc_shared (share the line with the DI) | vc_only."""
        self.brake_line_switch_type = normalize_brake_line_switch_type(value)
        return self.brake_line_switch_type

    def rx_handlers(self):
        return {DI_BRAKE_SWITCH_ID: self._on_di_brake, GTW_CARCONFIG_ID: self._on_carconfig}

    def _on_di_brake(self, data, send) -> None:  # 0x1D6: the DI's own wired-switch report
        if self.brake_line_switch_type == "di_vc_shared":
            self.brake_switch_pressed = di_brake_switch_pressed(data)

    def _on_carconfig(self, data, send) -> None:  # 0x7FF mux3 carries GTW_brakeLineSwitchType @39|1
        t = gtw_brake_line_switch_type(data)
        if t is not None:
            self.brake_line_switch_type = t

    def configure(self, **s) -> None:  # brake_switch_pressed, brake_line_switch_type
        bs = s.pop("brake_switch_pressed", None)
        if bs is not None:
            self.set_brake_switch(bs)
        lt = s.pop("brake_line_switch_type", None)
        if lt is not None:
            self.set_brake_line_switch_type(lt)
        super().configure(**s)


NODE = Vcleft
