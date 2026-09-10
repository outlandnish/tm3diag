#!/usr/bin/env python3
"""PCS node — Power Conversion System (DC-DC converter + AC charger).

Sources PCS_dcdcStatus (0x224), the liveness the DIR monitors for pcsMIA. The real PCS
transmits eight cyclic status frames (0x204/0x224/0x264/0x2A4/0x2B4/0x2C4/0x3A4/0x3C4 per
Model3_ETH.compact.json); only 0x224 is needed to clear pcsMIA. On the PCS bench mark it
--real (real hardware, frames not simulated); on the drive bench it is a simulated peer.
Charge scenarios are configured via the orchestrator ([scenario] in sim.toml), which sets
the peer nodes' initial state (EVSE connected + limits, charge request, HVP mode).
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros


class Pcs(Node):
    name = "PCS"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("PCS_dcdcStatus", 0x224, 0.100, zeros(8))]


NODE = Pcs
