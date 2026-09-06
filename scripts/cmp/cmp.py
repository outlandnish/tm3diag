#!/usr/bin/env python3
"""CMP node — A/C compressor liveness -> DI_a230_cmpMIA. Bus A / CANA.

BENCH-CONFIRMED 2026-08-02 by bisection: 0x247 (arrival-only DLC8, zeros suffice)
clears cmpMIA -- the PMR group-1 arrival consumer clears the compressor-present bit.
This is the frame the DI actually monitors, NOT the ODIN-documented CMP_info 0x363
(not in the PMR RX filter set). compact.json mislabels 0x247 "DAS_autopilotDebug".
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros


class Cmp(Node):
    name = "CMP"

    def frames(self) -> list[SimFrame]:
        """BASELINE (2020.8.1): 0x247, the cmpMIA frame the 2020 DIR monitors."""
        return [SimFrame("CMP_liveness_0x247", 0x247, 0.100, zeros(8))]

    def _frames_2022(self) -> list[SimFrame]:
        # 2022.45.15 adds 0x2A7 -- the config-selected ALTERNATE cmpMIA frame (a config bit
        # picks 0x247 vs 0x2A7). DLC8, arrival-only (no E2E), zeros suffice. Firmware-confirmed
        # absent in 2020. Sending both covers either variant selection on a 2022 DU.
        return [*self.frames(), SimFrame("CMP_liveness_0x2A7", 0x2A7, 0.100, zeros(8))]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = Cmp
