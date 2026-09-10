#!/usr/bin/env python3
"""PTC node — cabin PTC heater liveness -> DI_a231_ptcMIA. Bus A / CANA.

0x207 (arrival-only, DLC8, zeros suffice) clears ptcMIA via the PMR group-1 arrival
consumer. 0x207 is absent from ODIN compact.json; it is the frame the DI monitors, NOT
ODIN PTC_info 0x345.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros


class Ptc(Node):
    name = "PTC"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("PTC_liveness_0x207", 0x207, 0.100, zeros(8))]


NODE = Ptc
