#!/usr/bin/env python3
"""RCM node — restraints/inertial module (0x101/0x111). All party (bus B / CANB).

rcmMIA (DIR a157 / da7 b0,1) is an aggregate over {0x101, 0x111}. Both carry a Tesla
additive checksum (ctr@52, cksum@56); the payloads assert QF=valid on the yaw/roll/pitch
and long/lat/vert quality bits so the DIR accepts them. Both transmit at the RCM party rate
(sim_core.PARTY_RATE_S["RCM"], default 100Hz -- not their 20ms native cycle).
"""
from __future__ import annotations

from sim_core import PARTY_RATE_S, Node, SimFrame
from tesla_frames import pack_le


def _rcm_inertial1() -> bytearray:  # 0x101, 20ms
    # Static content captured from a known-good drive log: bytes0-5 = B8 FF F2 FF 36 40.
    # The DIR decodes words0-2 as inertial (accel/rate); these raw values are
    # non-SNA (SNA sentinels 0x8000 / -0x4000) so they read as valid. byte6 bit0 ->
    # the DIR ESP signal-status "signal available" bit; byte6 bit2 is the other QF bit.
    # counter@52(w4)/cksum@56 overlaid by SimFrame reproduce the log byte6/7 exactly
    # (e.g. ctr=0 -> byte6=0x05, byte7=0x25, matching the log).
    return pack_le(
        [
            (0, 16, 0xFFB8),   # word0 inertial field (raw -72; non-SNA)
            (16, 16, 0xFFF2),  # word1 inertial field (raw -14; non-SNA)
            (32, 16, 0x4036),  # word2 (byte4=0x36 data + byte5=0x40 QF)
            (48, 1, 1),        # byte6 bit0 QF -> DIR ESP signal-status "available" bit
            (50, 2, 1),        # byte6 bit2 QF
        ],
        8,
    )


def _rcm_inertial2() -> bytearray:  # 0x111, 20ms  (long/lat/vert QF ok)
    return pack_le([(48, 1, 1), (49, 1, 1), (50, 1, 1)], 8)


class Rcm(Node):
    name = "RCM"

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]  # per-node party liveness rate (sim_core; bench-tunable)
        return [
            SimFrame("RCM_inertial1", 0x101, rate, _rcm_inertial1, 52, 56, bus="party"),
            SimFrame("RCM_inertial2", 0x111, rate, _rcm_inertial2, 52, 56, bus="party"),
        ]


NODE = Rcm
