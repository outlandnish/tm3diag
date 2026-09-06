#!/usr/bin/env python3
"""APP node — the "app" liveness the DIR monitors -> DI_a108_appMIA. Bus A / CANA.

Firmware-CONFIRMED 2022-new: the 2020 DIR has no CAN-id load for 0x25C; the 2022 DIR
receives it (DLC1, NO checksum/counter — arrival-only). It reads byte0 bit0 into a stored
flag; 0 is a valid value, so a zero payload is safe liveness that clears appMIA. Because it
does not exist in 2020, it lives ONLY in fw_variants()["2022.45.15"] -- a --fw 2020 bench
sends nothing here; a --fw 2022.45.15 (or newer) bench sends it.
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
