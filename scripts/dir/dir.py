#!/usr/bin/env python3
"""DIR node — the REAR drive inverter (originNode=dir): the physical rear power stage.

DIR_torque (0x108), DIR_status (0x256), DIR_power (0x266), DIR_hvStatus, temperatures,
alert matrices, etc. -- the per-axle inverter, distinct from the vehicle-level DI aggregate
(see scripts/di/di_node.py). On the RWD drive bench the DIR is the real hardware, so mark it
(with PMR + DI) ``real`` in the config and the sim won't transmit these IDs. With no inverter
connected, the sim broadcasts them (a virtual rear inverter).

Frames = the originNode=dir cyclic set from Model3_ETH.compact.json (2020.8.1); the
event-driven DIR_udsResponse (0x616) is not a periodic broadcast, so it's omitted. Payloads
are skeleton (all-zero) for now -- the layout matters; fill in real signal content when a
virtual inverter must report meaningful torque/status. Bus defaults to vehicle (provisional).
The front inverter (DIF/PMF) is the AWD follow-up.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros

# name, arbitration id, period (s), dlc  -- originNode=dir, send_type=Cyclic
_FRAMES = [
    ("DIR_torque", 0x108, 0.010, 8),
    ("DIR_hvStatus", 0x126, 0.100, 3),
    ("DIR_status", 0x256, 0.010, 5),
    ("DIR_power", 0x266, 0.010, 8),
    ("DIR_temperature", 0x317, 1.000, 8),
    ("DIR_info", 0x335, 1.000, 8),
    ("DIR_oilPump", 0x397, 0.100, 8),
    ("DIR_alertMatrix1", 0x3A5, 1.000, 8),
    ("DIR_alertMatrix2", 0x3B5, 1.000, 8),
    ("DIR_alertMatrix3", 0x3C5, 1.000, 8),
    ("DIR_alertMatrix4", 0x3E5, 1.000, 8),
    ("DIR_thermalControl", 0x5D7, 1.000, 4),
    ("DIR_debug", 0x7D5, 0.100, 8),
    ("DIR_dyno", 0x7D6, 0.010, 8),
]


class Dir(Node):
    name = "DIR"

    def frames(self) -> list[SimFrame]:
        return [SimFrame(n, i, p, zeros(d)) for n, i, p, d in _FRAMES]


NODE = Dir
