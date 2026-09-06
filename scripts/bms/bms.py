#!/usr/bin/env python3
"""BMS node — HV battery liveness (feeds DIR_a092_bmsMIA).

No counter/checksum: the DIR's BMS handlers reset their MIA down-counter on any
DLC>=8 arrival regardless of payload (same as the PCS VCFRONT RE). Content is set
to a plausible drive-ready pack so the readiness FSM can advance.
"""

from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame
from tesla_frames import pack_le


def _bms_hvBusStatus() -> bytearray:  # 0x132, 10ms
    return pack_le(
        [
            (0, 16, 373.0 / 0.01),  # BMS_packVoltage     373 V
            (16, 16, 0),  # BMS_packCurrent     0 A (SNA=-32768)
            (32, 16, (0 - (-500)) / 0.05),  # BMS_currentUnfiltered 0 A (offset -500)
        ]
    )


def _bms_powerAvailable() -> bytearray:  # 0x252, 100ms  -> drive-ready
    return pack_le(
        [
            (0, 16, 60.0 / 0.01),  # BMS_maxRegenPower      ~60
            (16, 16, 150.0 / 0.01),  # BMS_maxDischargePower  ~150
            (48, 1, 1),  # BMS_powerLimitsState = POWER_CALCULATED_FOR_DRIVE
        ]
    )


def _bms_driveLimits() -> bytearray:  # 0x2D2, 100ms
    return pack_le(
        [
            (0, 16, 300.0 / 0.01),  # BMS_minBusVoltage      300 V
            (16, 16, 400.0 / 0.01),  # BMS_maxBusVoltage      400 V
            (48, 14, 500.0 / 0.128),  # BMS_maxDischargeCurrent ~500 A
        ]
    )


def _bms_packConfig_0x392() -> bytearray:  # 0x392, DLC8 — 2022.45.15 only (reassigned from epas3p)
    # 0x392 was EPAS3P_alertMatrix in 2020, reassigned to BMS_packConfig in 2022+ (DBC-confirmed).
    # The 2022 DIR reads the config only when byte0 (BMS_packConfigMultiplexer) == 1, then stores a
    # scaled value -> g@1468c. byte0=1 + zeros = a valid mux-1 frame (clears bmsMIA AND delivers the
    # packConfig value, unlike the epas3p alertMatrix frame whose byte0 != 1 was silently skipped).
    return pack_le([(0, 8, 1)], 8)


def _bms_limits_0x452() -> bytearray:  # 0x452, DLC3, no E2E — 2022.45.15 only
    # Two packed limits the DIR clamps torque against (b0-9 and b10-20, min()'d into the
    # powertrain torque/power set). Values are a proven-good captured payload
    # (bytes C2 40 1F = b0-9:194, b10-20:2000, both non-SNA); raise if a drive test needs a
    # higher torque ceiling. SNA would floor the limit -> no torque.
    return pack_le([(0, 10, 194), (10, 11, 2000)], 3)


def _bms_thermalStatus() -> bytearray:  # 0x312, 1000ms
    return pack_le(
        [
            (17, 9, (25 - (-25)) / 0.25),  # BMS_inletActiveCoolTargetT 25 C
            (44, 9, (25 - (-25)) / 0.25),  # BMS_minPackTemperature     25 C
            (53, 9, (25 - (-25)) / 0.25),  # BMS_maxPackTemperature     25 C
        ]
    )


# BMS_status 0x212 signal sets per operating mode (start,width,value), from the
# PCS operating-mode tables + compact.json signal layout. "drive" is the pre-charge
# default (byte-identical to the old drive build); charge/support move HV/contactors/state.
_BMS_STATUS_MODES = {
    "drive": [
        (1, 1, 0),  # BMS_notEnoughPowerForDrive = 0
        (8, 3, 4),  # BMS_contactorState = BMS_CTRSET_CLOSED
        (16, 3, 3),  # BMS_hvState        = HV_UP_FOR_DRIVE
        (32, 4, 1),  # BMS_state          = BMS_DRIVE
        (56, 4, 1),  # BMS_smStateRequest = BMS_DRIVE
    ],
    "dcdc": [
        (8, 3, 4),  # contactorState = CLOSED
        (16, 3, 6),  # hvState        = HV_UP
        (32, 4, 2),  # state          = BMS_SUPPORT
        (56, 4, 2),  # smStateRequest = BMS_SUPPORT
    ],
    "charge": [
        (8, 3, 4),  # contactorState = CLOSED
        (11, 3, 3),  # uiChargeStatus = BMS_CHARGING
        (16, 3, 4),  # hvState        = HV_UP_FOR_CHARGE
        (29, 1, 1),  # chargeRequest  = 1
        (32, 4, 3),  # state          = BMS_CHARGE
        (56, 4, 3),  # smStateRequest = BMS_CHARGE
    ],
}


class Bms(Node):
    name = "BMS"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.mode = "drive"  # drive | dcdc | charge — driver externality (BMS_status 0x212)

    def frames(self) -> list[SimFrame]:
        # NOTE: this is the proven working bench set. Of these, only 0x212+0x312 exist in the
        # 2020 DIR; 0x132/0x252/0x2D2 are 2022-new but harmless on a 2020 DU (no
        # handler -> filtered), so they stay here rather than risk a working-bench regression.
        return [
            SimFrame("BMS_hvBusStatus", 0x132, 0.010, _bms_hvBusStatus),
            SimFrame("BMS_status", 0x212, 0.100, self._bms_status),
            SimFrame("BMS_powerAvailable", 0x252, 0.100, _bms_powerAvailable),
            SimFrame("BMS_driveLimits", 0x2D2, 0.100, _bms_driveLimits),
            SimFrame("BMS_thermalStatus", 0x312, 1.000, _bms_thermalStatus),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15 adds two firmware-confirmed 2022-new bmsMIA a092 members absent in the 2020
        DIR: BMS_limits 0x452 (the torque-limit input) and BMS_packConfig 0x392 (reassigned from
        epas3p's EPAS3P_alertMatrix -- epas3p.py drops it in its 2022 variant, so no collision)."""
        return [
            *self.frames(),
            SimFrame("BMS_limits", 0x452, 0.100, _bms_limits_0x452),
            SimFrame("BMS_packConfig", 0x392, 1.000, _bms_packConfig_0x392),
        ]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}

    def set_mode(self, mode: str) -> str:
        """Driver externality: pack operating mode reflected in BMS_status
        (drive|dcdc|charge). The orchestrator moves it to charge for a charge session."""
        key = str(mode).strip().lower()
        if key not in _BMS_STATUS_MODES:
            raise ValueError(f"BMS mode must be one of {list(_BMS_STATUS_MODES)}")
        self.mode = key
        return key

    def configure(self, **s) -> None:  # scenario key: mode
        mode = s.pop("mode", None)
        if mode is not None:
            self.set_mode(mode)
        super().configure(**s)

    def rx_handlers(self):
        return {0x3A1: self._on_vcfront_status}  # VCFRONT_vehicleStatus

    def _on_vcfront_status(self, data, send) -> None:
        # Follow VCFRONT: go to charge state once it authorizes HV charging
        # (bmsHvChargeEnable @0), else drive.
        charge = bool(int.from_bytes(bytes(data), "little") & 1)
        self.mode = "charge" if charge else "drive"

    def _bms_status(self) -> bytearray:  # 0x212, 100ms
        return pack_le(_BMS_STATUS_MODES[self.mode])


NODE = Bms
