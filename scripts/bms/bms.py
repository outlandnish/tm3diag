#!/usr/bin/env python3
"""BMS node — HV battery liveness (feeds DIR_a092_bmsMIA).

DLC>=8 arrival clears bmsMIA regardless of payload; content is a drive-ready pack.
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


def _bms_packConfig_0x392() -> bytearray:  # 0x392, DLC8 — 2022.45.15 only
    # 0x392 = BMS_packConfig in 2022+ (was EPAS3P_alertMatrix in 2020, DBC-confirmed). The DIR
    # reads the config only when byte0 (BMS_packConfigMultiplexer) == 1.
    return pack_le([(0, 8, 1)], 8)


def _bms_limits_0x452() -> bytearray:  # 0x452, DLC3, no E2E — 2022.45.15 only
    # Two packed torque limits (b0-9, b10-20) the DIR min()'s into the powertrain torque/power set.
    # Captured payload C2 40 1F = b0-9:194, b10-20:2000 (both non-SNA).
    return pack_le([(0, 10, 194), (10, 11, 2000)], 3)


def _bms_thermalStatus() -> bytearray:  # 0x312, 1000ms
    return pack_le(
        [
            (17, 9, (25 - (-25)) / 0.25),  # BMS_inletActiveCoolTargetT 25 C
            (44, 9, (25 - (-25)) / 0.25),  # BMS_minPackTemperature     25 C
            (53, 9, (25 - (-25)) / 0.25),  # BMS_maxPackTemperature     25 C
        ]
    )


# BMS_status 0x212 signal sets per operating mode (start,width,value), from the PCS
# operating-mode tables + compact.json signal layout.
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
        # Only 0x212 + 0x312 exist in the 2020 DIR; 0x132/0x252/0x2D2 are 2022-new (no 2020 handler).
        return [
            SimFrame("BMS_hvBusStatus", 0x132, 0.010, _bms_hvBusStatus),
            SimFrame("BMS_status", 0x212, 0.100, self._bms_status),
            SimFrame("BMS_powerAvailable", 0x252, 0.100, _bms_powerAvailable),
            SimFrame("BMS_driveLimits", 0x2D2, 0.100, _bms_driveLimits),
            SimFrame("BMS_thermalStatus", 0x312, 1.000, _bms_thermalStatus),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15 adds two bmsMIA (a092) members absent in the 2020 DIR:
        BMS_limits 0x452 and BMS_packConfig 0x392."""
        return [
            *self.frames(),
            SimFrame("BMS_limits", 0x452, 0.100, _bms_limits_0x452),
            SimFrame("BMS_packConfig", 0x392, 1.000, _bms_packConfig_0x392),
        ]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}

    def set_mode(self, mode: str) -> str:
        """Pack operating mode reflected in BMS_status (drive|dcdc|charge)."""
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
        # bmsHvChargeEnable @0: charge when set, else drive.
        charge = bool(int.from_bytes(bytes(data), "little") & 1)
        self.mode = "charge" if charge else "drive"

    def _bms_status(self) -> bytearray:  # 0x212, 100ms
        return pack_le(_BMS_STATUS_MODES[self.mode])


NODE = Bms
