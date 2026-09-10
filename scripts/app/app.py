#!/usr/bin/env python3
"""APP node — liveness the DIR monitors (DI_a108_appMIA). Bus A / CANA.

0x25C exists only on the 2022 DIR (not 2020): DLC1, arrival-only (no
checksum/counter). byte0 bit0 is read into a stored flag; a zero payload is
valid liveness that clears appMIA. Lives only in fw_variants()["2022.45.15"].
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros


class App(Node):
    name = "APP"

    def frames(self) -> list[SimFrame]:
        """BASELINE (2020.8.1): the 2020 DIR does not receive an app message."""
        return []

    def _frames_2022(self) -> list[SimFrame]:
        # 0x25C: DLC1, arrival-only (no E2E). Zero byte0 b0 flag is valid -> clears appMIA a108.
        return [SimFrame("APP_liveness_0x25C", 0x25C, 0.100, zeros(1))]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}


NODE = App
