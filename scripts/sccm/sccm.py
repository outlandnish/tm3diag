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

_DI_GEAR = {1: "P", 2: "R", 3: "N", 4: "D"}  # DI_gear, 0x118 @21 w3


class Sccm(Node):
    name = "SCCM"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.stalk = SccmRightStalk()
        self.last_gear_cmd: str | None = None
        self.di_gear: str | None = None  # what the DI reports, for the N push direction

    def frames(self) -> list[SimFrame]:
        return [SimFrame("SCCM_rightStalk", 0x229, 0.100, self.stalk.frame)]

    def rx_handlers(self):
        return {0x118: self._on_di_status}

    def _on_di_status(self, data, send) -> None:
        if len(data) >= 3:
            self.di_gear = _DI_GEAR.get((int.from_bytes(bytes(data[:4]), "little") >> 21) & 7)

    def gear(self, letter: str) -> str:
        """Actuate a gear gesture (P/R/N/D). The DIR commits it at standstill once the
        detent is held through debounce (watch DI_gear on 0x118)."""
        verb = GEAR_GESTURE.get(str(letter).strip().upper())
        if verb is None:
            raise ValueError("gear must be P/R/N/D")
        if verb == "neutral":
            self.stalk.neutral(from_gear=self.di_gear)
        else:
            getattr(self.stalk, verb)()
        self.last_gear_cmd = str(letter).strip().upper()
        return self.last_gear_cmd

    def configure(self, **s) -> None:  # scenario key: gear
        g = s.pop("gear", None)
        if g is not None:
            self.gear(g)
        super().configure(**s)


NODE = Sccm
