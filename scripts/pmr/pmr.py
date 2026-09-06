#!/usr/bin/env python3
"""PMR node — the REAR power module (originNode=pmr): the CAN/logic half of the rear unit.

On the 2020 dual-core F28377D rear inverter, PMR is CPU1 (the CAN stack) and DIR is CPU2
(motor control); together they are the rear drive unit the DI aggregate represents. PMR
sources only a few vehicle-facing frames -- PMR_alertMatrix1 (0x385) + PMR_info (0x6D4) --
in this firmware (PMR_udsResponse 0x614 is event-driven, omitted).

On the drive bench the PMR is part of the connected rear-inverter hardware, so mark it (with
DIR + DI) ``real`` and the sim won't transmit these IDs. Skeleton (all-zero) payloads for
now. The front power module (PMF) is the AWD follow-up.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros

# name, arbitration id, period (s), dlc  -- originNode=pmr, send_type=Cyclic
_FRAMES = [
    ("PMR_alertMatrix1", 0x385, 1.000, 8),
    ("PMR_info", 0x6D4, 1.000, 8),
]


class Pmr(Node):
    name = "PMR"

    def frames(self) -> list[SimFrame]:
        return [SimFrame(n, i, p, zeros(d)) for n, i, p, d in _FRAMES]


NODE = Pmr
