#!/usr/bin/env python3
"""ESP node — stability control, brake + VDC inputs. All party (bus B / CANB).

espMIA (DIR a091) is an aggregate over ALL of
{0x105, 0x11D, 0x145, 0x155, 0x175, 0x185, 0x38D}; it clears only when every member arrives
with a valid checksum + rolling counter. 0x11D was re-homed here from UNKNOWN (2026-08-18):
firmware-pinned ESP-sourced (feeds the DIR ESP-MIA aggregate a091), and it ALSO feeds the
DIR VDC freshness watchdog (-> a195/a196/a197 vdcEspSlip + a210), so it must run at
PARTY_LIVENESS_S.
Three checksum schemes here:
  - Tesla additive (byte7 cksum, ctr@52 byte6-hi): 0x105/0x155/0x175/0x185.
  - Tesla additive (byte0 cksum, ctr@8): 0x145 (magic 0x46), 0x11D (magic 0x1E).
  - SAE J1850 CRC-8 (poly 0x1D) @byte0, ctr@byte1-lo: 0x38D (``J1850Frame``).

Several payloads also carry VDC input-validity status sub-fields (bench-confirmed
2026-08-02): the DIR treats value==0 as valid only when
the matching availability status bit is SET, so these builders assert those bits
(brake torque / MC pressure / wheel-speed direction) to clear a193/a194/a198/a200/…
Override any field for further bisection with --set.
"""
from __future__ import annotations

from sim_core import PARTY_RATE_S, Node, SimFrame, zeros
from tesla_frames import J1850Frame, pack_le


def _esp_status() -> bytearray:  # 0x145, 20ms, ctr@8 cksum@0 magic 0x46
    return pack_le(
        [
            (29, 2, 1),  # ESP_driverBrakeApply = Not_Applied
            (34, 2, 1),  # ESP_cdpStatus = CDP_IS_AVAILABLE
            # ESP_ptcTargetState (bits36-37) feeds the DIR traction-mode state machine.
            # Set to 2 = plausible real drive value.
            # NOTE: this is NOT the VDC-cluster fix. Traction 2/3 also needs the DIR's
            # drive-motion state == actual motion (unreachable parked), and the traction
            # fault feeds the DIR control-law chassis inputs, NOT the vdcState readiness MIN
            # that gates a199/a222/a210. Those collapse leaving {2,3} ~2s into warmup when one
            # silent VDC input is absent — orthogonal to this signal. Kept as a correctness
            # improvement; may only quiet the a223/a203 traction sub-path.
            (36, 2, 2),  # ESP_ptcTargetState = 2 (real drive value; not the VDC-cluster fix)
            # VDC availability -> the DIR ESP signal-status bits 10-13; clears a200 (0x145 alone).
            (23, 1, 1),
            (25, 1, 1),
            (26, 1, 1),
            (48, 1, 1),
        ]
    )


def _esp_0x105_valid() -> bytearray:  # 0x105: brake torque=0 + MC pressure=0 bar + availability
    return pack_le(
        [
            (13, 1, 1),
            (14, 1, 1),
            (15, 1, 1),  # word0 status -> DIR ESP signal-status bits 4/5/6 (signal available)
            (33, 1, 1),
            (34, 1, 1),
            (35, 1, 1),  # word2 status -> DIR ESP signal-status bits 7/8/9
            (36, 10, 0x64),  # MC pressure 10-bit: raw 0x64 = 0 bar
        ]
    )


def _esp_0x155_valid() -> bytearray:  # 0x155: availability (feeds a232 velocity est + a198)
    return pack_le([(40, 1, 1), (41, 1, 1)])  # word2 bits 8/9 -> DIR ESP signal-status bits 14/15


def _esp_0x185_wheelspeeds_valid() -> bytearray:  # 0x185: wheel speeds=0 (stationary) + direction
    return pack_le([(50, 1, 1)])  # direction bit -> DIR status bit3 = shared wheel-speed validity


def _esp_0x11d_valid() -> bytearray:  # 0x11D otherControllerState = present+valid (clears DI a210)
    # bits54-55 = 2 -> the DIR stores this "other controller" state code; it raises a210
    # unless the signal validates, which needs this 2-bit code == 2. a210 holds the
    # VDC-operational gate down, so clearing it drops the rollups a199 vdcFaulted / a222 vdcDisabled /
    # a223 tractionControlDisabled too. The other four 2-bit fields (56-63) feed the slip/sat
    # evaluator (a195/196/197) where 0 is benign -> leave them zero, do NOT blanket-set to 2.
    return pack_le([(54, 2, 2)])


class Esp(Node):
    name = "ESP"

    def frames(self) -> list[SimFrame]:
        # esp is 6 of the 12 fast party frames -> half the bus; trimmed here (sim_core, 50Hz).
        rate = PARTY_RATE_S[self.name]
        return [
            SimFrame("ESP_status", 0x145, rate, _esp_status, 8, 0, bus="party"),
            # 0x11D: re-homed from UNKNOWN. ctr@8 cksum@0 magic 0x1E (auto). Zeros pass the MIA
            # freshness (espMIA/a091) but leave otherControllerState=0 -> a210 stuck, which strands
            # a199/a222/a223. Send bits54-55=2 so a210 clears. Feeds VDC slip watchdog too ->
            # party-liveness rate. Override any status field for bisection with --set 0x11D:56:N etc.
            SimFrame("ESP_0x11D", 0x11D, rate, _esp_0x11d_valid, 8, 0, bus="party"),
            SimFrame("ESP_0x105", 0x105, rate, _esp_0x105_valid, 52, 56, bus="party"),
            SimFrame("ESP_0x155", 0x155, rate, _esp_0x155_valid, 52, 56, bus="party"),
            SimFrame("ESP_0x175", 0x175, rate, zeros(8), 52, 56, bus="party"),
            SimFrame(
                "ESP_0x185_wheelSpeeds", 0x185, rate, _esp_0x185_wheelspeeds_valid,
                52, 56, bus="party",
            ),
            SimFrame("ESP_party3", 0x38D, rate, J1850Frame(7).frame, bus="party"),
        ]


NODE = Esp
