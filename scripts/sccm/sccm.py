#!/usr/bin/env python3
"""SCCM node — steering-column module: the gear stalk (0x229, feeds sccmMIA/DI 0x368).

0x229 SCCM_rightStalk is bus A / CANA. It uses an AutoSAR E2E Profile-2 CRC (poly 0x2F),
not the Tesla additive checksum: CRC@byte0 + counter@byte1 lo-nibble. The node owns the
stalk (``SccmRightStalk``), which builds the CRC + counter internally, so this SimFrame
declares no counter/checksum. Steady IDLE keeps sccmMIA cleared; the driver actuates gear
via ``gear()``.
"""
from __future__ import annotations

from sim_core import Node, SimFrame
from tesla_frames import GEAR_GESTURE, SccmRightStalk


class Sccm(Node):
    name = "SCCM"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.stalk = SccmRightStalk()
        self.last_gear_cmd: str | None = None

    def frames(self) -> list[SimFrame]:
        return [SimFrame("SCCM_rightStalk", 0x229, 0.100, self.stalk.frame)]

    def gear(self, letter: str) -> str:
        """Actuate a gear gesture (P/R/N/D). The DIR commits it at standstill once the
        detent is held through debounce (watch DI_gear on 0x118)."""
        verb = GEAR_GESTURE.get(str(letter).strip().upper())
        if verb is None:
            raise ValueError("gear must be P/R/N/D")
        getattr(self.stalk, verb)()
        self.last_gear_cmd = str(letter).strip().upper()
        return self.last_gear_cmd

    def configure(self, **s) -> None:  # scenario key: gear
        g = s.pop("gear", None)
        if g is not None:
            self.gear(g)
        super().configure(**s)


NODE = Sccm
