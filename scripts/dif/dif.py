#!/usr/bin/env python3
"""DIF node — the FRONT drive inverter, i.e. the half of an AWD pair the bench doesn't have.

An AWD rear ("Master", usage digit 0) expects a front drive unit on the bus: it rx's four
front-inverter frames, supervises all four for MIA, and cross-checks the front against itself
(``DIR_a040 difMIA``, ``DI_a138 frontUnitDisabled``, ``DI_a207 vdcPowertrainTorque_dif``,
``DI_a211 vdcMotorSpeed_dif``). A RWD rear ("Single", usage 2) rx's none of them. So this node
exists to let a *single* AWD rear unit run on the bench with a simulated front.

The four frames are the entire front->rear surface — enumerated from the 2022.45.15 AWD DIR's
rx set and diffed against the RWD DIR at the same revision, so the set is the AWD delta and
nothing else.

Two things here are NOT what the DBC says, and both are firmware-verified:

* **0x2D5 is DLC 7 on 2022.45.15**, where every DBC says 8. The DLC check is an exact match —
  a DLC-8 frame is rejected outright and the frame goes MIA looking healthy on the wire. It is
  DLC 8 again on 2026.8.3.
* **Checksum in byte 0, counter in byte 1 low nibble** (``counter_start=8, cksum_start=0``) —
  not the byte7/byte6 placement most validated frames use. Same layout ESP_status 0x145 and
  IBST_status 0x39D already use here.

Seeds are the plain ``id_lo + id_hi`` rule (0x87 / 0x88 / 0xD7), so ``place_checksum`` needs no
per-message override. 0x2E5 has no checksum or counter at all.

Payloads are zeros, per the ``zeros()`` contract: MIA gates on DLC + checksum/counter, not on
signal values, and zero is a valid non-SNA value for these fields (the 0x2E5 handler only
special-cases its 9-bit field at 0x1FF). If the rear's VDC torque/speed cross-checks against the
front (a207/a211) later need consistent content, that is a payload change here, not a new frame.

Buses are firmware-correct and split: the three chassis-bus frames ride `party`, 0x2E5 rides
`vehicle`. That split is read off which of the DIR's two per-bus rx paths handles each.

No PMF node yet: the 2022 AWD DIR rx's no PMF frame at all. ``0x1D5 PMF_state4`` arrives on the
DIR only at 2026 (the 2022 DIF rx's it, but nothing here simulates a DIF's own peers).
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros

# Cycle times are the DBC's GenMsgCycleTime for the target revision. 0x187 is in no ETH DBC at
# any revision; it is sent at 10 ms, which cannot cause an MIA (only too-slow can).
_P_0X187 = 0.010


class Dif(Node):
    name = "DIF"

    # --- baseline (2020.8.1) ---------------------------------------------------------------
    # DBC-derived, NOT firmware-verified: no 2020 AWD DIR is imported. Only the 2022.45.15 and
    # 2026.8.3 sets below were read out of firmware.
    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("DIF_torque", 0x186, 0.100, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_0x187", 0x187, _P_0X187, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_status", 0x2D5, 0.010, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_power", 0x2E5, 0.010, zeros(8), bus="vehicle"),
        ]

    # --- 2022.45.15 ------------------------------------------------------------------------
    # Firmware-verified against the 2022 AWD DIR. Differs from baseline only in the 0x2D5 DLC.
    def _frames_2022(self) -> list[SimFrame]:
        return [
            SimFrame("DIF_torque", 0x186, 0.100, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_0x187", 0x187, _P_0X187, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_status", 0x2D5, 0.010, zeros(7), 8, 0, bus="party"),
            SimFrame("DIF_power", 0x2E5, 0.010, zeros(8), bus="vehicle"),
        ]

    # --- 2026.8.3 --------------------------------------------------------------------------
    # Firmware-verified against the 2026 AWD DIR: 0x2D5 is DLC 8 again, and 0x186 drops to 10 ms.
    def _frames_2026(self) -> list[SimFrame]:
        return [
            SimFrame("DIF_torque", 0x186, 0.010, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_0x187", 0x187, _P_0X187, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_status", 0x2D5, 0.010, zeros(8), 8, 0, bus="party"),
            SimFrame("DIF_power", 0x2E5, 0.010, zeros(8), bus="vehicle"),
        ]

    def fw_variants(self):
        # A 2024.8.9 target resolves to the 2022 entry and so sends 0x2D5 at DLC 7. That is
        # UNVERIFIED — no 2024 AWD DIR is imported, and the DBC is not evidence here (it says
        # DLC 8 for 2022 too, where the firmware wants 7). Verify against the 2024.8.9 AWD DIR
        # before running a 2024 AWD bench.
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }


NODE = Dif
