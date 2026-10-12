#!/usr/bin/env python3
"""RCM node — restraints/inertial module (0x101/0x111). All party (bus B / CANB).

rcmMIA (DIR a157 / da7 b0,1) is an aggregate over {0x101, 0x111}. Both carry a Tesla
additive checksum (ctr@52, cksum@56); the payloads assert QF=valid on the yaw/roll/pitch
and long/lat/vert quality bits so the DIR accepts them. Both transmit at the RCM party rate
(sim_core.PARTY_RATE_S["RCM"], default 100Hz -- not their 20ms native cycle).

0x111 longitudinal accel follows DIR_axleSpeed (0x108), so the DI's VDC plausibility checks
agree with the moving wheel speeds (DI_a204/a205/a206 vdcRcm*); vertical is gravity-compensated.
"""
from __future__ import annotations

import time

from sim_core import PARTY_RATE_S, Node, SimFrame
from tesla_frames import pack_le

# RCM_verticalAccel at rest (scenario key vertical_accel): gravity-compensated, 0. Bench-
# confirmed: +9.81 and -9.81 both trip the DI's VDC plausibility check VERT_ACCEL (a216 case 16);
# 0 clears it.
_G = 0.0
_ACCEL_LSB = 0.00125      # RCM_*Accel (ETH RCM_inertial2), signed 16-bit m/s^2
_WHEEL_CIRC_M = 2.1       # axle rpm -> vehicle speed; tire size barely matters for dv/dt
_ACCEL_TAU_S = 0.1        # low-pass on the differentiated speed
_DIR_TORQUE_ID = 0x108    # DIR_axleSpeed @40 s16, 0.1 rpm, 0x8000 SNA


def _rcm_inertial1() -> bytearray:  # 0x101, 20ms
    # bytes0-5 = B8 FF F2 FF 36 40 (from a known-good drive log). DIR decodes words0-2
    # as inertial (accel/rate); these raw values are non-SNA (sentinels 0x8000 / -0x4000)
    # so they read as valid. byte6 bit0 -> DIR ESP signal-status "available" bit; byte6
    # bit2 is the other QF bit. counter@52(w4)/cksum@56 are overlaid by SimFrame.
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


def _rcm_inertial2(long_mps2: float = 0.0, vert_mps2: float = _G) -> bytearray:  # 0x111, 20ms
    def raw(a):
        return max(-0x7FFF, min(0x7FFF, round(a / _ACCEL_LSB))) & 0xFFFF

    return pack_le(
        [
            (0, 16, raw(long_mps2)),  # RCM_longitudinalAccel
            (16, 16, 0),              # RCM_lateralAccel
            (32, 16, raw(vert_mps2)),  # RCM_verticalAccel
            (48, 1, 1), (49, 1, 1), (50, 1, 1),  # long/lat/vert QF
        ],
        8,
    )


class Rcm(Node):
    name = "RCM"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self._speed: float | None = None  # m/s from DIR_axleSpeed
        self._speed_t = 0.0
        self._long_accel = 0.0
        self.vertical_accel = _G

    def configure(self, **s) -> None:  # scenario key: vertical_accel (m/s^2)
        if (v := s.pop("vertical_accel", None)) is not None:
            self.vertical_accel = float(v)
        super().configure(**s)

    def frames(self) -> list[SimFrame]:
        rate = PARTY_RATE_S[self.name]  # per-node party liveness rate (sim_core; bench-tunable)
        return [
            SimFrame("RCM_inertial1", 0x101, rate, _rcm_inertial1, 52, 56, bus="party"),
            SimFrame("RCM_inertial2", 0x111, rate,
                     lambda: _rcm_inertial2(self._long_accel, self.vertical_accel),
                     52, 56, bus="party"),
        ]

    def rx_handlers(self):
        return {_DIR_TORQUE_ID: self._on_dir_torque}

    def _on_dir_torque(self, data, send) -> None:
        if len(data) < 7:
            return
        raw = int.from_bytes(data[5:7], "little", signed=True)
        if raw == -0x8000:  # SNA: hold the last accel
            return
        now = time.monotonic()
        speed = raw * 0.1 * _WHEEL_CIRC_M / 60.0
        if self._speed is not None and now > self._speed_t:
            dt = now - self._speed_t
            a = (speed - self._speed) / dt
            self._long_accel += (a - self._long_accel) * min(1.0, dt / _ACCEL_TAU_S)
        self._speed, self._speed_t = speed, now


NODE = Rcm
