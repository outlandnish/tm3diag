#!/usr/bin/env python3
"""DAS node — driver-assist. All party (bus B / CANB).

The dasMIA aggregate (DI a141) is drive-state-gated (supervised only in R/D or with VCFRONT
0x221 power==DRIVE). Membership grew across firmware:
  * 2020.8.1 baseline (2 members {0x2B9, 0x389}):
    - 0x389 DAS_status2 (500ms) feeds the active dasMIA. Tesla additive checksum
      (ctr@52, cksum@56, magic 0x8C).
    - 0x2B9 DAS_control (40ms), autopilot longitudinal command, used as extra dasMIA
      liveness; provisional (id/bus/counter positions not pinned in 2020/12603 PMR fw), so
      gated by --no-das (ctr@53 w3, cksum@56, magic 0xBB).
  * 2022.45.15 adds 2 more members {0x289, 0x39B} -> a 4-member dasMIA: the 2020 DIR has no
    CAN-id load for 0x289/0x39B, the 2022 DIR receives both. E2E: 0x289 = cksum@0/ctr@8 w3
    (magic 0x8B), 0x39B = cksum@56/ctr@52 w4 (magic 0x9E). The DIR reads one field each
    (0x289 @11 w9, 0x39B @0 w4); 0 is valid (non-SNA), so zero payloads are safe liveness.
    Native cycles unpinned -> 100ms (dasMIA window ≥500ms per 0x389).
  --fw 2020.8.1 sends 2 DAS frames; --fw 2022.45.15 (or newer / default) sends 4.
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import pack_le


def _das_control() -> bytearray:  # 0x2B9, 40ms
    # Static content captured from a known-good drive log: bytes0-5 = 0D 42 88 2F B1 8B,
    # byte6 bits0-4 = 0x19 (high bits of the last wheel-speed field, read across words2-3).
    # The firmware reads 0x2B9 as ESP wheel-speed/brake, not DAS_control as the DBC labels it.
    # counter@53(w3)/cksum@56 are overlaid by SimFrame.
    return pack_le(
        [
            (0, 16, 0x420D),   # word0 (bytes0-1 = 0D 42)
            (16, 16, 0x2F88),  # word1 (bytes2-3 = 88 2F)
            (32, 16, 0x8BB1),  # word2 (bytes4-5 = B1 8B)
            (48, 5, 0x19),     # byte6 bits0-4 = high bits of last wheel-speed field
        ],
        8,
    )


class Das(Node):
    name = "DAS"

    def frames(self) -> list[SimFrame]:
        """BASELINE (2020.8.1): the 2-member dasMIA set."""
        return [
            # 0x389: ctr@52 cks@56 magic 0x8C
            SimFrame("DAS_status2", 0x389, 0.500, zeros(8), 52, 56, bus="party"),
            SimFrame(
                "DAS_control", 0x2B9, 0.040, _das_control, 53, 56,
                counter_width=3, bus="party",
            ),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15: baseline + the two dasMIA members the 2020 DIR never received."""
        return [
            *self.frames(),
            # 0x289 (DLC3): ctr@8 w3, cksum@0, magic 0x8B (auto). DIR reads @11 w9; 0 valid.
            SimFrame(
                "DAS_0x289", 0x289, 0.100, zeros(3), 8, 0,
                counter_width=3, bus="party",
            ),
            # 0x39B (DLC8): ctr@52 w4, cksum@56, magic 0x9E (auto). DIR reads @0 w4; 0 valid.
            SimFrame("DAS_0x39B", 0x39B, 0.100, zeros(8), 52, 56, bus="party"),
        ]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Das
