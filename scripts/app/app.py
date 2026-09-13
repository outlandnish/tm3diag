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

    def _frames_2026(self) -> list[SimFrame]:
        # 2026.8.3 RETIRES 0x25C and adds 0x25B APP_environment -- firmware-confirmed by
        # enumerating both DIRs' rx sets: 0x25C is the ONLY frame the 2026 DIR dropped, and
        # 0x25B is new. Same role (the app-liveness the DIR supervises), so this is a renumber:
        # keep sending 0x25C on 2026 and it is simply unhandled, while the frame the DIR now
        # waits on never arrives.
        #
        # Two differences from 0x25C, both firmware-read, NEITHER visible in the DBC (which
        # lists no checksum/counter signals for APP_environment -- the DBC is incomplete here,
        # the firmware is authoritative):
        #   * DLC 8, not 1. The DIR's DLC check is an EXACT match (both short and long are
        #     rejected), so a 1-byte frame is dropped.
        #   * It is GATED with the same counter+checksum check as 0x284 -- counter = byte6
        #     bits 4-7 (prev+1)&0xF,
        #     checksum = byte7. The seed is RESEEDED to 0x5B (id_lo+id_hi would be 0x5D).
        #
        # Payload: the handler reads bit13, bit43 and a small field; zero is a valid value for
        # each (as it was for 0x25C's byte0 b0), so zeros + E2E is safe liveness.
        return [
            SimFrame(
                "APP_environment", 0x25B, 0.100, zeros(8), 52, 56, cksum_magic=0x5B,
            )
        ]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }


NODE = App
