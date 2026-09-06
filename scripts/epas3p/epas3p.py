#!/usr/bin/env python3
"""EPAS3P node — power-steering. Spans BOTH buses (node = transmitter; each frame
carries its own bus): 0x392 alertMatrix on the vehicle bus, 0x3D1/0x370 on party.

epas3pMIA (DIR a161 / da6 b3,4) is an aggregate over {0x370, 0x3D1}; both must
arrive with valid checksum + counter to clear it. 0x370 carries a steering=0 deg +
availability payload (ctr@48 = byte6 LO nibble, checksum@56). Both party frames
transmit at the EPAS3P party rate (sim_core.PARTY_RATE_S["EPAS3P"], default 100Hz)
rather than their slow native cycles (0x3D1 1Hz, 0x370 10Hz) -- high rate is redundancy
against single-peer bench-bus drops, not a firmware staleness requirement.

**0x392 is an ID REASSIGNMENT across firmware** (DBC-confirmed: 2020 = EPAS3P_alertMatrix,
2022+ = BMS_packConfig). So 0x392 stays here on the 2020 BASELINE (epas3p transmits its alert
matrix on the vehicle bus; the 2020 DIR doesn't even consume it), but the 2022.45.15 variant
DROPS it -- the bms node reclaims the ID as BMS_packConfig (see bms.py `_frames_2022`), so a
2022 DU gets the packConfig content and there is no duplicate arbitration ID.
"""
from __future__ import annotations

from sim_core import BASELINE_FW, PARTY_RATE_S, Node, SimFrame, zeros
from tesla_frames import pack_le


def _epas3p_0x370_valid() -> bytearray:  # 0x370: steering=0 deg + availability
    return pack_le([(4, 1, 1), (32, 8, 0x20)])  # avail bit4 + byte4=0x20 -> 0x2000 = 0 deg


class Epas3p(Node):
    name = "EPAS3P"

    def _party_frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]  # per-node party liveness rate (sim_core; bench-tunable)
        return [
            SimFrame("EPAS3P_angleCalib", 0x3D1, rate, zeros(8), bus="party"),
            SimFrame("EPAS3P_0x370", 0x370, rate, _epas3p_0x370_valid, 48, 56, bus="party"),
        ]

    def frames(self) -> list[SimFrame]:
        """BASELINE (2020.8.1): party pair + 0x392 EPAS3P_alertMatrix (vehicle bus)."""
        return [SimFrame("EPAS3P_alertMatrix", 0x392, 1.000, zeros(8)), *self._party_frames()]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15: DROP 0x392 (reassigned to BMS_packConfig, sent by the bms node)."""
        return self._party_frames()

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Epas3p
