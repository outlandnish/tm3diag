#!/usr/bin/env python3
"""BMS node — HV battery liveness (feeds DIR_a092_bmsMIA).

DLC>=8 arrival clears bmsMIA regardless of payload; content is a drive-ready pack.
"""

from __future__ import annotations

import itertools
import time

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import pack_le, vcfront_hv_charge_enable

# DBC scaling that moved 2020.8.1 -> 2022.45.15 (layouts unchanged). 2020 LSBs on a 2022 DI read
# minBusVoltage 300 V as 600 V (clamped 470 V) > the 373 V pack, so the DI's min-bus-voltage
# limiter cut discharge power to ~0 in drive -> DI_a125 noBatteryPower. The 0x2D2/0x252 LSBs are
# firmware-confirmed on the 2022 DIR; currentUnfiltered is DBC-only.
_SCALE_2020 = {"bus_v": 0.01, "dis_i": 0.128, "dis_p": 0.01, "i_unf_off": -500.0}
_SCALE_2022 = {"bus_v": 0.02, "dis_i": 0.15, "dis_p": 0.013, "i_unf_off": -822.0}


# BMS_packVoltage reports a MEASURED bus voltage, never a fixed one (a fixed value splits the
# DIR's bus-voltage cross-check, DIR_a038_busVsensor, 5%): the DIR's DIR_vBat (0x126 @0|10,
# 1 V/LSB 2022+, 0.5 V in 2020) if it is on the bus, else a real PCS's PCS_dcdcHvBusVolt
# (0x2B4 @10|12, 0.146484375 V; the sim's PCS doesn't send 0x2B4), else 0 V.
_HV_SOURCE_STALE_S = 1.0


def _bms_hvBusStatus(s=_SCALE_2020, v=0.0) -> bytearray:  # 0x132, 10ms
    return pack_le(
        [
            (0, 16, v / 0.01),  # BMS_packVoltage
            (16, 16, 0),  # BMS_packCurrent     0 A (SNA=-32768)
            (32, 16, (0 - s["i_unf_off"]) / 0.05),  # BMS_currentUnfiltered 0 A
        ]
    )


def _bms_hvBusStatus_2026(s=_SCALE_2022, v=0.0) -> bytearray:  # 0x132, DLC **6**
    # 2026.8.3 shortens 0x132 to DLC 6 and the DIR's DLC check is an EXACT match (both
    # short AND long frames are rejected), so the
    # 8-byte 2022 build is dropped outright on a 2026 DU -- taking the HV bus voltage with it.
    # Bytes 0-5 are unchanged: BMS_packVoltage @0|16 was RENAMED to BMS_dcLinkVoltage at the
    # same offset, width and 0.01 V/LSB scale. Only BMS_chgTimeToFull @48|12 (bytes 6-7) was
    # dropped -- we never populated it, so the payload is identical and only the length changes.
    return pack_le(
        [
            (0, 16, v / 0.01),  # BMS_dcLinkVoltage (was BMS_packVoltage)
            (16, 16, 0),  # BMS_packCurrent     0 A (SNA=-32768)
            (32, 16, (0 - s["i_unf_off"]) / 0.05),  # BMS_currentUnfiltered 0 A
        ],
        6,
    )


def _bms_powerAvailable_2026(s=_SCALE_2022, regen_kw=60.0, dis_kw=150.0) -> bytearray:  # 0x252
    # 2026.8.3 re-lays 0x252: BMS_powerLimitsState moved @48|1 -> @42|1 (and
    # BMS_notEnoughPowerForHeatPump @42 -> @52, BMS_totalHvPowerBudget @43|9 is new, replacing
    # BMS_hvacPowerBudget). Leaving powerLimitsState at 48 would land it inside
    # totalHvPowerBudget and read as POWER_NOT_CALCULATED -> the DIR's min() power limiter sees
    # no drive power. maxRegenPower/maxDischargePower keep their offsets AND their 0.01/0.013
    # kW/LSB scales, so the a125 noBatteryPower inputs are untouched.
    return pack_le(
        [
            (0, 16, regen_kw / 0.01),  # BMS_maxRegenPower
            (16, 16, dis_kw / s["dis_p"]),  # BMS_maxDischargePower
            (42, 1, 1),  # BMS_powerLimitsState = POWER_CALCULATED_FOR_DRIVE (was @48)
        ]
    )


def _bms_powerAvailable(s=_SCALE_2020, regen_kw=60.0, dis_kw=150.0) -> bytearray:  # 0x252
    return pack_le(
        [
            (0, 16, regen_kw / 0.01),  # BMS_maxRegenPower
            (16, 16, dis_kw / s["dis_p"]),  # BMS_maxDischargePower
            (48, 1, 1),  # BMS_powerLimitsState = POWER_CALCULATED_FOR_DRIVE
        ]
    )


def _bms_driveLimits(s=_SCALE_2020, v_min=300.0, i_chg=0.0, v_max=400.0,
                     i_dis=500.0) -> bytearray:  # 0x2D2
    return pack_le(
        [
            (0, 16, v_min / s["bus_v"]),  # BMS_minBusVoltage (DIR clamps to >= 210 V)
            # BMS_maxBusVoltage: the DI's regen voltage ceiling (DIR clamps 210..470 V); in D/R a
            # measured bus above it (+ margin) raises DI_a021. Keep it just above the supply.
            (16, 16, v_max / s["bus_v"]),
            # BMS_maxChargeCurrent (0.1 A, both revs): the DIR's regen current limit. 0 = no regen,
            # the safe default for a bench PSU that can't sink current.
            (32, 14, min(i_chg / 0.1, 0x3FFF)),
            # BMS_maxDischargeCurrent: keep within the supply's rating -- a DI drawing past the
            # supply's current limit sags the bus, then overshoots it (DIR_a021 HW bus OV).
            (48, 14, min(i_dis / s["dis_i"], 0x3FFF)),
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


def _mux_pages(*pages):
    """Builder that cycles through ``pages`` (zero-arg builders), one per call."""
    it = itertools.cycle(pages)
    return lambda: next(it)()


def _t25(c: float) -> float:  # 0.25 degC/LSB, -25 offset
    return (c + 25) / 0.25


# 0x312 on 2026.8.3 is multiplexed (BMS_thermalStatusMultiplexer @0|2) and fully re-laid; the
# pack temps moved to mux 0 @32|10 (0.2, -40) / @43|11 (0.1, -40). Mux 1 limit values are
# plausible placeholders, not captured.
def _bms_thermalStatus_2026_m0() -> bytearray:
    return pack_le(
        [
            (0, 2, 0),
            (2, 9, _t25(25)),  # BMS_inletActiveCoolTargetT 25 C
            (11, 9, _t25(25)),  # BMS_inletPassiveTargetT    25 C
            (20, 9, _t25(25)),  # BMS_inletActiveHeatTargetT 25 C
            (32, 10, (25 + 40) / 0.2),  # BMS_minPackTemperature 25 C
            (43, 11, (25 + 40) / 0.1),  # BMS_maxPackTemperature 25 C
        ]
    )


def _bms_thermalStatus_2026_m1() -> bytearray:
    return pack_le(
        [
            (0, 2, 1),
            (2, 9, _t25(-20)),  # BMS_coldStagnationLimit     -20 C
            (11, 9, _t25(45)),  # BMS_hotStagnationLimit       45 C
            (20, 9, _t25(60)),  # BMS_hotCellTempLimit         60 C
            (32, 9, _t25(35)),  # BMS_activeCoolCellReferenceT 35 C
            (41, 9, _t25(20)),  # BMS_activeHeatCellTargetT    20 C
        ]
    )


# 0x2B2 BMS_energyStatus (2022+ DBC; absent from the 2020 DBC). Multiplexed @0|2. The 2022 PCS
# supervises it (PCS_a022 bmsMIA, with 0x3B2). Sent at 100 ms, not the DBC 2 s, as PCS liveness.
_PACK_KWH, _REMAIN_KWH = 75.0, 60.0


def _bms_energyStatus_m0() -> bytearray:
    return pack_le(
        [
            (0, 2, 0),
            (16, 16, _PACK_KWH / 0.02),  # BMS_nominalFullPackEnergy
            (32, 16, _REMAIN_KWH / 0.02),  # BMS_nominalEnergyRemaining
            (48, 16, _REMAIN_KWH / 0.02),  # BMS_idealEnergyRemaining
        ]
    )


def _bms_energyStatus_m1() -> bytearray:
    return pack_le(
        [
            (0, 2, 1),
            (16, 16, 3.0 / 0.01),  # BMS_energyBuffer 3 kWh
            (32, 16, _REMAIN_KWH / 0.02),  # BMS_expectedEnergyRemaining
            (48, 16, (_PACK_KWH - _REMAIN_KWH) / 0.02),  # BMS_energyToChargeComplete
        ]
    )


# 0x3B2 BMS_log2: the 2022 PCS's other PCS_a022 bmsMIA member. Sent as the mux 0 (CAC1) page
# only, at 100 ms rather than the DBC 30 s, as PCS liveness. Brick capacities are plausible
# Model 3 values, not captured. By 2024.8.9 the mux is 7 bits and page 0 is re-laid.
_CAC_AH = (220.0, 218.0, 222.0)  # avg, min, max
_CAC_BRICK = (1, 96)  # min, max brick id (raw = id - 1)


def _bms_log2_cac1() -> bytearray:
    avg, lo, hi = _CAC_AH
    return pack_le(
        [
            (0, 6, 0),  # BMS_log2MuxId = CAC1
            (6, 13, avg / 0.1),  # BMS_cacAvg
            (19, 13, lo / 0.1),  # BMS_cacMin
            (32, 7, _CAC_BRICK[0] - 1),  # BMS_cacMinBrickId
            (40, 13, hi / 0.1),  # BMS_cacMax
            (56, 7, _CAC_BRICK[1] - 1),  # BMS_cacMaxBrickId
        ]
    )


def _bms_log2_cac1_2026() -> bytearray:
    avg, lo, hi = _CAC_AH
    return pack_le(
        [
            (0, 7, 0),  # BMS_log2MuxId = CAC1 (7-bit by 2024)
            (8, 13, avg / 0.1),  # BMS_cacAvg
            (24, 13, lo / 0.1),  # BMS_cacMin
            (37, 7, _CAC_BRICK[0] - 1),  # BMS_cacMinBrickId
            (44, 13, hi / 0.1),  # BMS_cacMax
            (57, 7, _CAC_BRICK[1] - 1),  # BMS_cacMaxBrickId
        ]
    )


def _bms_energyStatus_m2() -> bytearray:  # mux 2 (display energy), new by 2024.8.9
    return pack_le(
        [
            (0, 2, 2),
            (8, 16, _REMAIN_KWH / 0.02),  # BMS_energyRemainingDisplay
            (24, 16, _REMAIN_KWH / 0.02),  # BMS_energyRemainingTDisplay
            (52, 11, 100 * _REMAIN_KWH / _PACK_KWH / 0.05),  # BMS_userFacingSoe %
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


# BMS_status 0x212 for 2026.8.3. Same operating modes, re-laid: BMS_hvState moved @16|3 -> @60|3
# (byte7 bits 4-6, sharing byte 7 with smStateRequest @56|4) and BMS_chargeRequest @29 -> @30.
# contactorState/state/smStateRequest kept their offsets. Left at the 2022 offsets, hvState would
# land in BMS_batteryInputPower @14|16 (new) and the DIR would read HV as DOWN.
_BMS_STATUS_MODES_2026 = {
    "drive": [
        (1, 1, 0),  # BMS_notEnoughPowerForDrive = 0
        (8, 3, 4),  # BMS_contactorState = BMS_CTRSET_CLOSED
        (32, 4, 1),  # BMS_state          = BMS_DRIVE
        (56, 4, 1),  # BMS_smStateRequest = BMS_DRIVE
        (60, 3, 3),  # BMS_hvState        = HV_UP_FOR_DRIVE   (was @16)
    ],
    "dcdc": [
        (8, 3, 4),  # contactorState = CLOSED
        (32, 4, 2),  # state          = BMS_SUPPORT
        (56, 4, 2),  # smStateRequest = BMS_SUPPORT
        (60, 3, 6),  # hvState        = HV_UP                 (was @16)
    ],
    "charge": [
        (8, 3, 4),  # contactorState = CLOSED
        (11, 3, 3),  # uiChargeStatus = BMS_CHARGING
        (30, 1, 1),  # chargeRequest  = 1                     (was @29)
        (32, 4, 3),  # state          = BMS_CHARGE
        (56, 4, 3),  # smStateRequest = BMS_CHARGE
        (60, 3, 4),  # hvState        = HV_UP_FOR_CHARGE      (was @16)
    ],
}


# Live-adjustable pack limits (scenario keys + tm3web dash). The DIR clamps both bus voltages to
# 210..470 V; BMS_maxBusVoltage is its regen voltage ceiling, BMS_maxChargeCurrent its regen current.
BMS_LIMITS: dict[str, dict] = {
    "min_bus_voltage": {"label": "Min bus voltage", "signal": "BMS_minBusVoltage",
                        "unit": "V", "range": (0.0, 1300.0), "step": 1},
    "max_bus_voltage": {"label": "Max bus voltage", "signal": "BMS_maxBusVoltage",
                        "unit": "V", "range": (0.0, 1300.0), "step": 1},
    "max_charge_current": {"label": "Max charge (regen) current", "signal": "BMS_maxChargeCurrent",
                           "unit": "A", "range": (0.0, 1638.3), "step": 1},
    "max_regen_power": {"label": "Max regen power", "signal": "BMS_maxRegenPower",
                        "unit": "kW", "range": (0.0, 655.35), "step": 0.5},
    # Drive side: match the supply. Below ~2 kW the DI raises a125 noBatteryPower.
    "max_discharge_current": {"label": "Max discharge current",
                              "signal": "BMS_maxDischargeCurrent",
                              "unit": "A", "range": (0.0, 2450.0), "step": 1},
    "max_discharge_power": {"label": "Max discharge power", "signal": "BMS_maxDischargePower",
                            "unit": "kW", "range": (0.0, 650.0), "step": 0.5},
}


class Bms(Node):
    name = "BMS"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.mode = "drive"  # drive | dcdc | charge — driver externality (BMS_status 0x212)
        self.min_bus_voltage = 300.0
        self.max_charge_current = 0.0  # A, BMS_maxChargeCurrent
        self.max_bus_voltage = 400.0  # V, BMS_maxBusVoltage
        self.max_regen_power = 60.0  # kW, BMS_maxRegenPower
        self.max_discharge_current = 500.0  # A, BMS_maxDischargeCurrent
        self.max_discharge_power = 150.0  # kW, BMS_maxDischargePower
        self._hv: dict[str, tuple[float, float]] = {}  # source -> (volts, monotonic time)

    def _pack_v(self) -> float:
        now = time.monotonic()
        for src in ("DIR", "PCS"):  # the inverter's own measurement first
            v, t = self._hv.get(src, (None, 0.0))
            if v is not None and now - t <= _HV_SOURCE_STALE_S:
                return v
        return 0.0

    def frames(self) -> list[SimFrame]:
        # Proven bench set. Only 0x212 + 0x312 exist in the 2020 DIR; 0x132/0x252/0x2D2 are
        # 2022-new but harmless on a 2020 DU (no handler -> filtered). 0x2B2 (not in the 2020
        # DBC) is 5 zero bytes as 2020 PCS BMS liveness.
        return [
            *self._frames(_SCALE_2020),
            SimFrame("BMS_0x2B2", 0x2B2, 0.100, zeros(5)),
        ]

    def _frames(self, s) -> list[SimFrame]:
        return [
            SimFrame("BMS_hvBusStatus", 0x132, 0.010, lambda: _bms_hvBusStatus(s, self._pack_v())),
            SimFrame("BMS_status", 0x212, 0.100, self._bms_status),
            SimFrame("BMS_powerAvailable", 0x252, 0.100,
                     lambda: _bms_powerAvailable(s, self.max_regen_power,
                                                 self.max_discharge_power)),
            SimFrame("BMS_driveLimits", 0x2D2, 0.100,
                     lambda: _bms_driveLimits(s, self.min_bus_voltage, self.max_charge_current,
                                              self.max_bus_voltage, self.max_discharge_current)),
            SimFrame("BMS_thermalStatus", 0x312, 1.000, _bms_thermalStatus),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15: the baseline set at 2022 DBC scaling (``_SCALE_2022``), plus two
        bmsMIA (a092) members absent in the 2020 DIR: BMS_limits 0x452 and BMS_packConfig
        0x392 (reassigned from EPAS3P_alertMatrix; epas3p drops it in its 2022 variant). Also
        the PCS_a022 bmsMIA members BMS_energyStatus 0x2B2 and BMS_log2 0x3B2."""
        return [
            *self._frames(_SCALE_2022),
            SimFrame("BMS_limits", 0x452, 0.100, _bms_limits_0x452),
            SimFrame("BMS_packConfig", 0x392, 1.000, _bms_packConfig_0x392),
            SimFrame("BMS_energyStatus", 0x2B2, 0.100,
                     _mux_pages(_bms_energyStatus_m0, _bms_energyStatus_m1)),
            SimFrame("BMS_log2", 0x3B2, 0.100, _bms_log2_cac1),
        ]

    def _frames_2024(self) -> list[SimFrame]:
        """2024.8.9: the 2022 set with 0x3B2 page 0 already at its 2026 layout (7-bit mux) and
        0x2B2's mux 2 added. Both read out of the 2024 crackMessage by bit-probing; every other
        frame here decodes at its 2022 position."""
        repl = {
            0x2B2: SimFrame("BMS_energyStatus", 0x2B2, 0.100,
                            _mux_pages(_bms_energyStatus_m0, _bms_energyStatus_m1,
                                       _bms_energyStatus_m2)),
            0x3B2: SimFrame("BMS_log2", 0x3B2, 0.100, _bms_log2_cac1_2026),
        }
        return [repl.get(f.can_id, f) for f in self._frames_2022()]

    def _frames_2026(self) -> list[SimFrame]:
        """2026.8.3: the 2022 set with the re-laid frames swapped in. 0x2D2 BMS_driveLimits is
        identical across 2022->2026 in BOTH layout and scaling; 0x392 only gained fields the sim
        leaves 0, and 0x452 is not in any DBC, so those carry over. 0x312 is now multiplexed,
        0x2B2 gains a mux 2 and 0x3B2 re-lays page 0. Scaling stays ``_SCALE_2022`` -- no BMS LSB on the DIR's frames
        moved between 2022.45.15 and 2026.8.3 (checked against both DBCs), so the a125
        noBatteryPower calibration that _SCALE_2022 exists for still holds."""
        s = _SCALE_2022
        repl = {
            0x132: SimFrame("BMS_hvBusStatus", 0x132, 0.010,
                            lambda: _bms_hvBusStatus_2026(s, self._pack_v())),
            0x212: SimFrame("BMS_status", 0x212, 0.100, self._bms_status_2026),
            0x252: SimFrame(
                "BMS_powerAvailable", 0x252, 0.100,
                lambda: _bms_powerAvailable_2026(s, self.max_regen_power,
                                                 self.max_discharge_power),
            ),
            0x312: SimFrame("BMS_thermalStatus", 0x312, 1.000,
                            _mux_pages(_bms_thermalStatus_2026_m0, _bms_thermalStatus_2026_m1)),
            0x2B2: SimFrame("BMS_energyStatus", 0x2B2, 0.100,
                            _mux_pages(_bms_energyStatus_m0, _bms_energyStatus_m1,
                                       _bms_energyStatus_m2)),
            0x3B2: SimFrame("BMS_log2", 0x3B2, 0.100, _bms_log2_cac1_2026),
        }
        return [repl.get(f.can_id, f) for f in self._frames_2022()]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2024.8.9": self._frames_2024,
            "2026.8.3": self._frames_2026,
        }

    def set_mode(self, mode: str) -> str:
        """Pack operating mode reflected in BMS_status (drive|dcdc|charge)."""
        key = str(mode).strip().lower()
        if key not in _BMS_STATUS_MODES:
            raise ValueError(f"BMS mode must be one of {list(_BMS_STATUS_MODES)}")
        self.mode = key
        return key

    def set_limit(self, field: str, value) -> float:
        """Live-set one of BMS_LIMITS (dash / scenario)."""
        if field not in BMS_LIMITS:
            raise ValueError(f"BMS limit must be one of {list(BMS_LIMITS)}")
        lo, hi = BMS_LIMITS[field]["range"]
        v = float(value)
        if not lo <= v <= hi:
            raise ValueError(f"BMS {field} must be {lo:g}..{hi:g} {BMS_LIMITS[field]['unit']}")
        setattr(self, field, v)
        return v

    def limits(self) -> dict[str, float]:
        return {f: getattr(self, f) for f in BMS_LIMITS}

    def configure(self, **s) -> None:
        """Scenario keys: mode + BMS_LIMITS (min/max_bus_voltage, max_charge_current,
        max_regen_power)."""
        mode = s.pop("mode", None)
        if mode is not None:
            self.set_mode(mode)
        for field in BMS_LIMITS:
            if (v := s.pop(field, None)) is not None:
                self.set_limit(field, v)
        super().configure(**s)

    def rx_handlers(self):
        return {0x3A1: self._on_vcfront_status,  # VCFRONT_vehicleStatus
                0x126: self._on_dir_hv_status,   # DIR_hvStatus
                0x2B4: self._on_pcs_rail_status}  # PCS_dcdcRailStatus

    def _on_dir_hv_status(self, data, send) -> None:
        if len(data) >= 2:
            raw = data[0] | (data[1] & 3) << 8
            lsb = 1.0 if self.fw is None or self.fw >= "2022.45.15" else 0.5
            self._hv["DIR"] = (raw * lsb, time.monotonic())

    def _on_pcs_rail_status(self, data, send) -> None:
        if len(data) >= 3:
            raw = int.from_bytes(data[:3], "little") >> 10 & 0xFFF
            self._hv["PCS"] = (raw * 0.146484375, time.monotonic())

    def _on_vcfront_status(self, data, send) -> None:
        # VCFRONT_bmsHvChargeEnable (2020: bit 0; 2022+: 0x3A1 page 1): charge, else drive.
        charge = vcfront_hv_charge_enable(data, muxed=self.fw is None or self.fw >= "2022.45.15")
        if charge is not None:
            self.mode = "charge" if charge else "drive"

    def _bms_status(self) -> bytearray:  # 0x212, 100ms
        return pack_le(_BMS_STATUS_MODES[self.mode])

    def _bms_status_2026(self) -> bytearray:  # 0x212, 100ms (2026.8.3 layout)
        return pack_le(_BMS_STATUS_MODES_2026[self.mode])


NODE = Bms
