#!/usr/bin/env python3
"""PTC node — cabin PTC heater liveness -> DI_a231_ptcMIA. Bus A / CANA.

BENCH-CONFIRMED 2026-08-02 by bisection: 0x207 (arrival-only DLC8, zeros suffice)
clears ptcMIA -- the PMR group-1 arrival consumer clears the PTC-present bit. 0x207 is
absent from ODIN compact.json entirely; it is the frame the DI actually monitors, NOT
the ODIN PTC_info 0x345.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros


class Ptc(Node):
    name = "PTC"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("PTC_liveness_0x207", 0x207, 0.100, zeros(8))]


NODE = Ptc
