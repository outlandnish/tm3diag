#!/usr/bin/env python3
"""VCRIGHT node — right-body controller. 0x103 VCRIGHT_doorStatus is the 7th member of the
vcfrontMIA aggregate (the DIR resets its MIA timer on arrival); sourced by VCRIGHT, not VCFRONT.

0x103 is NOT arrival-only: the same DIR handler decodes three of the six VCFRONT/VCRIGHT status
codes that gate the DIR chassis hold/roll FSM. After the DLC-check (id 0x103, len 8) it extracts
three nibbles: byte0 bits0-3, byte0 bits4-7, byte7 bits0-3. The gate forces the FSM not-ready
unless all six status codes == 2. Companion to the VCFRONT 0x102/0x2E1 status codes.
"""
from __future__ import annotations

from sim_core import Node, SimFrame
from tesla_frames import pack_le


def _vcright_0x103() -> bytearray:  # 0x103 status nibbles -> DIR hold/roll FSM gate (must be 2)
    return pack_le([(0, 4, 2), (4, 4, 2), (56, 4, 2)], 8)  # byte0=0x22, byte7=0x02


class Vcright(Node):
    name = "VCRIGHT"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("VCRIGHT_doorStatus", 0x103, 0.100, _vcright_0x103)]


NODE = Vcright
