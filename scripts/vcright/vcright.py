#!/usr/bin/env python3
"""VCRIGHT node — right-body controller. 0x103 VCRIGHT_doorStatus is the 7th member
of the vcfrontMIA aggregate (the DIR resets its MIA timer on arrival); it is
sourced by VCRIGHT (not VCFRONT), so it lives here per the attribution principle.

0x103 is NOT arrival-only: the same DIR handler also decodes three of the SIX
VCFRONT/VCRIGHT status codes that gate the DIR chassis hold/roll FSM. It runs the
DLC-check (id 0x103, len 8) then extracts three nibbles:
  byte0 bits0-3, byte0 bits4-7, byte7 bits0-3.
The DIR hold/roll input gate forces the FSM NOT-READY unless ALL SIX status codes == 2
-- so a nibble of 0 (what zeros(8) sends) keeps DI_locStatus rollPreventionState +
vehicleHoldState at FAULT. This
is the COMPANION to the VCFRONT 0x102/0x2E1 fix (commit 27fba15): the hold/roll gate
cannot clear until 0x103 carries the nibbles too. (Gate also needs drive-operational via
the DIR drive-FSM code -> may still be non-ready on a static bench; bisect there.)
"""
from __future__ import annotations

from sim_core import Node, SimFrame
from tesla_frames import pack_le


def _vcright_0x103() -> bytearray:  # 0x103 status nibbles -> DIR hold/roll FSM gate (must be 2)
    # byte0 bits0-3=2, byte0 bits4-7=2, byte7 bits0-3=2 -> the three DIR gate status codes.
    return pack_le([(0, 4, 2), (4, 4, 2), (56, 4, 2)], 8)  # byte0=0x22, byte7=0x02


class Vcright(Node):
    name = "VCRIGHT"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("VCRIGHT_doorStatus", 0x103, 0.100, _vcright_0x103)]


NODE = Vcright
