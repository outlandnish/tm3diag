#!/usr/bin/env python3
"""DAS node — driver-assist. All party (bus B / CANB).

The dasMIA aggregate (DI a141) is drive-state-gated (supervised only in R/D or with VCFRONT
0x221 power==DRIVE). Membership grew across firmware:
  * 2020.8.1 baseline (2 members {0x2B9, 0x389}):
    - 0x389 DAS_status2 (500ms) feeds the active dasMIA. Tesla additive checksum
      (ctr@52, cksum@56, magic 0x8C).
    - 0x2B9 DAS_control (40ms): ACC state, set speed and accel/jerk limits (ctr@53 w3,
      cksum@56, magic 0xBB). Manual driving by default (ACC_CANCEL_GENERIC_SILENT);
      scenario ``acc_state = "ACC_ON"`` claims cruise at 52.5 km/h. Limits never bound torque.
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

# DAS_accState (DBC VAL_). APC_* start the DIR's autopark state machine.
ACC_STATE = {
    "ACC_CANCEL_GENERIC": 0, "ACC_BACKWARD": 1, "ACC_HOLD": 3, "ACC_ON": 4,
    "APC_BACKWARD": 5, "APC_FORWARD": 6, "APC_COMPLETE": 7, "APC_ABORT": 8, "APC_PAUSE": 9,
    "APC_UNPARK_COMPLETE": 10, "APC_SELFPARK_START": 11, "ACC_CANCEL_GENERIC_SILENT": 13,
    "FAULT_SNA": 15,
}
ACC_ON = ACC_STATE["ACC_ON"]


def _das_control(acc_state: int = ACC_STATE["ACC_CANCEL_GENERIC_SILENT"]) -> bytearray:
    # 0x2B9, 40ms. All-ones in any field is SNA. The limits are the widest non-SNA values, so
    # they never bound the DI's torque; a real ACC request narrows accelMin..accelMax.
    return pack_le(
        [
            (0, 12, 525 if acc_state == ACC_ON else 0),  # DAS_setSpeed 52.5 km/h (0.1)
            (12, 4, acc_state),                          # DAS_accState
            (16, 2, 0),         # DAS_aebEvent
            (18, 9, 0),         # DAS_jerkMin -15.232 m/s^3 (0.03, -15.232)
            (27, 8, 0xFE),      # DAS_jerkMax 14.99 m/s^3 (0.059)
            (35, 9, 0),         # DAS_accelMin -15 m/s^2 (0.04, -15)
            (44, 9, 0x1FE),     # DAS_accelMax 5.4 m/s^2 (0.04, -15)
        ],
        8,
    )


class Das(Node):
    name = "DAS"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.acc_state = ACC_STATE["ACC_CANCEL_GENERIC_SILENT"]  # manual control

    def configure(self, **s) -> None:  # scenario key: acc_state (DAS_accState label or raw)
        if (v := s.pop("acc_state", None)) is not None:
            if isinstance(v, str):
                if v.upper() not in ACC_STATE:
                    raise ValueError(f"DAS acc_state {v!r}: one of {sorted(ACC_STATE)}")
                v = ACC_STATE[v.upper()]
            if not 0 <= v <= 15:
                raise ValueError(f"DAS acc_state {v} out of range 0..15")
            self.acc_state = v
        super().configure(**s)

    def frames(self) -> list[SimFrame]:
        """BASELINE (2020.8.1): the 2-member dasMIA set."""
        return [
            # 0x389: ctr@52 cks@56 magic 0x8C
            SimFrame("DAS_status2", 0x389, 0.500, zeros(8), 52, 56, bus="party"),
            SimFrame(
                "DAS_control", 0x2B9, 0.040, lambda: _das_control(self.acc_state), 53, 56,
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
