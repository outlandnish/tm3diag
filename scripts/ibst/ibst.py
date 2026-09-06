#!/usr/bin/env python3
"""IBST node — iBooster brake actuator. All party (bus B / CANB).

ibstMIA (DIR a158 / da6 b12,13) is an aggregate over {0x38E, 0x39D}. 0x39D IBST_status
is len 5 with a Tesla additive checksum (ctr@8 cksum@0, magic 0xA0); 0x38E is len 6
with a SAE J1850 CRC-8 (poly 0x1D) @byte0 + ctr@byte1-lo (``J1850Frame``).
"""
from __future__ import annotations

from sim_core import BASELINE_FW, PARTY_RATE_S, Node, SimFrame
from tesla_frames import J1850Frame, pack_le


def _ibst_status() -> bytearray:  # 0x39D, 40ms, len 5, ctr@8 cksum@0 magic 0xA0
    return pack_le(
        [
            (12, 3, 4),  # IBST_iBoosterStatus = IBOOSTER_ACTIVE_GOOD_CHECK
            (16, 2, 1),  # IBST_driverBrakeApply = BRAKES_NOT_APPLIED
        ],
        length=5,
    )


class Ibst(Node):
    name = "IBST"

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]  # per-node party liveness rate (sim_core; bench-tunable)
        return [
            SimFrame("IBST_status", 0x39D, rate, _ibst_status, 8, 0, bus="party"),
            SimFrame("IBST_0x38E", 0x38E, rate, J1850Frame(6).frame, bus="party"),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        # 2022 DIR validates IBST_status 0x39D on bus A (CANA/vehicle): its bus-A
        # cksumCtr validator (dir 0xab7fb) feeds a110_brakeMIA. 2020 had 0x39D on
        # party only (not a canDataBusA member). Keep the party copies (a158 ibstMIA)
        # and add the vehicle-bus copy so the 2022 brakeMIA supervisor sees it fresh.
        rate = PARTY_RATE_S[self.name]
        return [
            *self.frames(),
            SimFrame("IBST_status_A", 0x39D, rate, _ibst_status, 8, 0, bus="vehicle"),
        ]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Ibst
