#!/usr/bin/env python3
"""PCS node — Power Conversion System (DC-DC converter + AC charger).

Sources PCS_dcdcStatus (0x224), the liveness the DIR monitors for pcsMIA. The real PCS
transmits eight cyclic status frames (0x204/0x224/0x264/0x2A4/0x2B4/0x2C4/0x3A4/0x3C4 per
Model3_ETH.compact.json); only 0x224 is needed to clear pcsMIA on the drive bench today, so
that is what this node broadcasts (adding the others is additive).

On the PCS bench the PCS is the real hardware -> select it as --real so its frames are NOT
simulated; on the drive bench it is a simulated peer. Either way the orchestrator drives a
charge session by poking the OTHER nodes' state (HVP/BMS/VCFRONT/CP/UI), never the PCS.

This file used to be the standalone PCS bench (a port of pcs_send.py: a hand-built FRAMES
list + pcs_mode/precharge/current_limits verbs + main()). That has folded onto the node
model, one frame to its real owner:
  * HVP_pcsControl 0x22A / HVP_contactorState 0x20A -> HVP node (scripts/hvp/hvp.py)
  * CP_evseStatus 0x21D (EVSE connection) -> CP node; UI_chargeRequest 0x333 -> UI node
  * BMS_status 0x212 charge signals -> BMS.set_mode; VCFRONT_vehicleStatus 0x3A1 charge
    signals -> VCFRONT.set_charge_enable
  * 0x545 was a mistranslation of decimal 545 = 0x221 (VCFRONT_LVPowerState), already sourced
    by VCFRONT; the two undocumented frames 0x13D and 0x2B2 -> the UNKNOWN holding pen,
    pending PCS-firmware RE to attribute them.
Charge scenarios are now configured via the orchestrator ([scenario] in sim.toml), which sets
the peer nodes' initial state (EVSE connected + limits, charge request, HVP mode).
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros


class Pcs(Node):
    name = "PCS"

    def frames(self) -> list[SimFrame]:
        return [SimFrame("PCS_dcdcStatus", 0x224, 0.100, zeros(8))]


NODE = Pcs
