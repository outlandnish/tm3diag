#!/usr/bin/env python3
"""UI/GUI node — the cluster's command frames. All bus A / CANA.

uiMIA (DIR a088) is an aggregate of SIX UI msgs; it clears only when all six are alive:
  0x82  UI_tripPlanning       DLC8  arrival-only
  0x213 UI_cruiseControl      DLC2  ctr@4  cks@8
  0x284 UI_vehicleModes       DLC8  arrival-only thru 2024; ctr@52 cks@56 ENFORCED from 2026.8.3
  0x293 UI_chassisControl     DLC8  ctr@52 cks@56
  0x313 UI_trackModeSettings  DLC8  ctr@52 cks@56
  0x334 UI_powertrainControl  DLC8  ctr@52 cks@56  (self-checksummed)

The node owns the UiConfig (pedal map / stopping / motor / traction / winch / trailer /
track); the DI acts on 0x334/0x293/0x313. The driver sets those via ``set_ui``.
"""
from __future__ import annotations

from functools import partial

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import (
    UI_SETTINGS,
    UiConfig,
    UiPowertrainControl,
    apply_ui_setting,
    pack_le,
    ui_chassis_control,
    ui_cruise_control,
    ui_track_mode_settings,
)


def _ui_tripPlanning(_c) -> bytearray:  # 0x82, DLC8, arrival-only
    return bytearray(8)


def _ui_vehicleModes(c) -> bytearray:  # 0x284, DLC8 arrival-only (DLC5 -> a094 canDataBusA)
    # UI_serviceMode @3: the DI refuses ROTOR/RESOLVER_LEARNING (NOT_IN_SERVICE_MODE) without it.
    return pack_le([(3, 1, c.service_mode)])


def _ui_status(c) -> bytearray:  # 0x353 UI_status, DLC8
    # UI_developmentCar @40 (byte5 bit0) is the DIR dyno-inhibit bypass: =1 (dev car) keeps
    # dyno latched; =0 makes dyno one-shot per ignition cycle.
    return pack_le([(40, 1, c.development_car)])


class Ui(Node):
    name = "UI"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.uicfg = UiConfig()
        self.ui_pt = UiPowertrainControl(self.uicfg)
        # Charge-request state (0x333). Idle default: no request (all-zero frame).
        self.charge_enable = False
        self.charge_limit_a = 0        # UI_acChargeCurrentLimit (A, scale 1)
        self.charge_termination_pct = 0.0  # UI_chargeTerminationPct (%, scale 0.1)
        self.open_charge_port = False
        self.close_charge_port = False

    def frames(self) -> list[SimFrame]:
        c = self.uicfg
        return [
            SimFrame("UI_tripPlanning", 0x82, 1.000, partial(_ui_tripPlanning, c)),
            SimFrame("UI_cruiseControl", 0x213, 0.100, partial(ui_cruise_control, c), 4, 8),
            SimFrame("UI_vehicleModes", 0x284, 0.100, partial(_ui_vehicleModes, c)),
            SimFrame("UI_chassisControl", 0x293, 0.100, partial(ui_chassis_control, c), 52, 56),
            SimFrame(
                "UI_trackModeSettings", 0x313, 0.100,
                partial(ui_track_mode_settings, c), 52, 56,
            ),
            SimFrame("UI_powertrainControl", 0x334, 0.100, self.ui_pt.frame),
            SimFrame("UI_chargeRequest", 0x333, 0.500, self._charge_request),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        # 2022.45.15 adds 0x3B3 UI_vehicleControl2 as a uiMIA a088 member (absent in the 2020 DIR).
        # DLC8, arrival-only; zeros(8) clears the MIA. Drive-state-gated.
        #
        # 0x353 UI_status is supervised separately and IS read: UI_developmentCar (@bit40) is the
        # dyno-inhibit bypass (=1 keeps dyno latched, =0 makes it one-shot per cycle), so it is
        # sent with the development_car knob. 0x500 is supervised-but-not-read -> droppable.
        c = self.uicfg
        return [
            *self.frames(),
            SimFrame("UI_vehicleControl2", 0x3B3, 0.100, zeros(8)),
            SimFrame("UI_status", 0x353, 0.100, partial(_ui_status, c)),
        ]

    def _frames_2026(self) -> list[SimFrame]:
        # 2026.8.3: UI_vehicleModes 0x284 gains a rolling counter + checksum AND the DIR now
        # ENFORCES them -- a failed check drops the frame like a DLC mismatch. So a 2022-style
        # 0x284 silently stops delivering UI_serviceMode and the rotor/resolver learn routines
        # refuse to start (NOT_IN_SERVICE_MODE). Counter = byte6 bits 4-7, (prev+1)&0xF;
        # checksum = byte7, magic 0x86 = tesla_frames.magic(0x284). DBC agrees:
        # UI_vehicleModesChecksum @56|8, UI_vehicleModesCounter @52|4.
        #
        # 2026 also adds two MIA-supervised UI frames to the DIR's rx set; arrival clears the MIA:
        #   0x238 UI_driverAssistMapData -- gated, ctr@52 cks@56; payload unread -> zeros(8).
        #   0x3FD UI_autopilotControl -- not gated, DLC8; zeros pass its (word0 & 7) == 0 check.
        c = self.uicfg
        return [
            *[
                SimFrame("UI_vehicleModes", 0x284, 0.100, partial(_ui_vehicleModes, c), 52, 56)
                if f.can_id == 0x284 else f
                for f in self._frames_2022()
            ],
            SimFrame("UI_driverAssistMapData", 0x238, 0.100, zeros(8), 52, 56),
            SimFrame("UI_autopilotControl", 0x3FD, 0.100, zeros(8)),
        ]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }

    def set_ui(self, field: str, value) -> int:
        """Driver externality: set a UiConfig field (pedal_map, stopping_mode, ...)."""
        return apply_ui_setting(self.uicfg, field, value)

    def set_charge(
        self,
        enable: bool | None = None,
        limit_a: int | None = None,
        termination_pct: float | None = None,
    ) -> None:
        """Driver externality: the user's charge request (UI_chargeRequest 0x333). Enabling
        without a termination % defaults it to 80%."""
        if enable is not None:
            self.charge_enable = bool(enable)
            if self.charge_enable and termination_pct is None and self.charge_termination_pct == 0.0:
                self.charge_termination_pct = 80.0
        if limit_a is not None:
            self.charge_limit_a = int(limit_a)
        if termination_pct is not None:
            self.charge_termination_pct = float(termination_pct)

    def configure(self, **s) -> None:
        # scenario keys: charge_enable/charge_limit_a/charge_termination_pct + any UI setting
        enable = s.pop("charge_enable", None)
        limit = s.pop("charge_limit_a", None)
        term = s.pop("charge_termination_pct", None)
        if enable is not None or limit is not None or term is not None:
            self.set_charge(enable=enable, limit_a=limit, termination_pct=term)
        for field in [k for k in s if k in UI_SETTINGS]:
            self.set_ui(field, s.pop(field))
        super().configure(**s)

    def _charge_request(self) -> bytearray:  # 0x333, 500ms, dlc4
        return pack_le(
            [
                (0, 1, int(self.open_charge_port)),   # UI_openChargePortDoorRequest
                (1, 1, int(self.close_charge_port)),  # UI_closeChargePortDoorRequest
                (2, 1, int(self.charge_enable)),      # UI_chargeEnableRequest
                (8, 7, self.charge_limit_a),          # UI_acChargeCurrentLimit
                (16, 10, self.charge_termination_pct / 0.1),  # UI_chargeTerminationPct
            ],
            4,
        )


NODE = Ui
