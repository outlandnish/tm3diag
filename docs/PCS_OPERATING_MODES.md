# PCS Operating Modes

Reference for entering each PCS operating mode via `scripts/pcs/pcs.py`. Covers what
we transmit, what the PCS and surrounding ECUs report back, and what confirms
the mode is active.

All signal layouts are from `Model3_ETH.dbc` / `Model3_ETH.compact.json`.

---

## Common prerequisites (all modes)

These messages must be running continuously before the PCS will cooperate.
`start_heartbeats()` covers all of them.

| CAN ID | Message | Key signals | Rate |
|--------|---------|-------------|------|
| 0x20A | HVP_contactorState | see per-mode contactor state below | 100 ms |
| 0x212 | BMS_status | `BMS_updateAllowed=1`, `BMS_pcsPwmEnabled=1` | 100 ms |
| 0x21D | CP_evseStatus | `CP_evseAccept=1`, `CP_proximity=LATCHED(3)` | 100 ms |
| 0x23D | CP_chargeStatus | `CP_hvChargeStatus=CP_CHARGE_ENABLED(5)` | 100 ms |
| 0x232 | (raw) | fixed payload | 100 ms |
| 0x25D | (raw) | fixed payload | 100 ms |
| 0x321 | VCFRONT_sensors | coolant temps, coolant level | 100 ms |
| 0x333 | UI_chargeRequest | `UI_chargeEnableRequest=1` | 100 ms |
| 0x3A1 | VCFRONT_vehicleStatus | `VCFRONT_bmsHvChargeEnable=1`, `VCFRONT_12vStatusForDrive=READY(1)`, `VCFRONT_pcs12vVoltageTarget` | 100 ms |
| 0x3B2 | BMS_log2 | mux 5/3 alternating (bmsMia keepalive) | 10 ms |
| 0x13D | OBC_control | `enabled=True`, AC current limit | 10 ms |
| 0x545 | VCFront heartbeat | counter + CRC (vcfrontMia keepalive) | 50 ms |

---

## Mode 0 — Off (SHUTDOWN)

PCS is idle. No HV conversion active. Default state on startup.

### We send (0x22A HVP_pcsControl)

| Signal | Value |
|--------|-------|
| `HVP_pcsControlRequest` | `SHUTDOWN (0)` |
| `HVP_dcLinkVoltageRequest` | target V × 10 (e.g. 672 = 67.2 V) |
| `HVP_pcsChargeHwEnabled` | `0` |
| `HVP_pcsDcdcHwEnabled` | `0` |
| `HVP_dcLinkVoltageFiltered` | echo of `PCS_dcdcHvBusVolt` (or 0 if no reading yet) |

### Contactor state (0x20A)

| Signal | Value |
|--------|-------|
| `HVP_packContNegativeState` | `OPEN (1)` |
| `HVP_packContPositiveState` | `OPEN (1)` |
| `HVP_packContactorSetState` | `OPEN (1)` |
| `HVP_packCtrsClosingAllowed` | `0` |
| `HVP_dcLinkAllowedToEnergize` | `0` |
| `HVP_hvilStatus` | `STATUS_OK (1)` |

### Confirm via signal_cache

| Signal | Expected |
|--------|----------|
| `PCS_chgMainState` | `PCS_CHG_STATE_IDLE (1)` |
| `PCS_dcdcMainState` | `DCDC_STATE_STANDBY (0)` |
| `PCS_hvChargeStatus` | `PCS_CHARGE_STANDBY (0)` |

---

## Mode 1 — DC-DC Support (12 V output only)

PCS steps HV bus down to 12 V to power the low-voltage system. No AC charging.
This is the normal "ignition on" mode when the car is running.

### We send (0x22A)

| Signal | Value |
|--------|-------|
| `HVP_pcsControlRequest` | `SUPPORT (1)` |
| `HVP_dcLinkVoltageRequest` | pack voltage target |
| `HVP_pcsChargeHwEnabled` | `0` |
| `HVP_pcsDcdcHwEnabled` | `1` |
| `HVP_dcLinkVoltageFiltered` | echo of `PCS_dcdcHvBusVolt` |

### Contactor state (0x20A)

| Signal | Value |
|--------|-------|
| `HVP_packContNegativeState` | `ECONOMIZED (6)` |
| `HVP_packContPositiveState` | `ECONOMIZED (6)` |
| `HVP_packContactorSetState` | `CLOSED (5)` |
| `HVP_packCtrsClosingAllowed` | `1` |
| `HVP_dcLinkAllowedToEnergize` | `1` |
| `HVP_hvilStatus` | `STATUS_OK (1)` |

### Additional BMS signals (0x212)

| Signal | Value |
|--------|-------|
| `BMS_hvState` | `HV_UP (6)` or `HV_UP_FOR_CHARGE (4)` |
| `BMS_contactorState` | `BMS_CTRSET_CLOSED (4)` |
| `BMS_state` | `BMS_SUPPORT (2)` |
| `BMS_smStateRequest` | `BMS_SUPPORT (2)` |

### VCFRONT vehicleStatus (0x3A1)

| Signal | Value |
|--------|-------|
| `VCFRONT_pcs12vVoltageTarget` | desired 12 V output (e.g. 15.0 V → raw 1500) |
| `VCFRONT_12vStatusForDrive` | `READY_FOR_DRIVE_12V (1)` |
| `VCFRONT_inAccessoryPlus` | `1` |
| `VCFRONT_bmsHvChargeEnable` | `1` |

### Confirm via signal_cache

| Signal | Expected |
|--------|----------|
| `PCS_dcdcMainState` | `DCDC_STATE_12V_SUPPORT_ACTIVE (1)` |
| `PCS_dcdcHvBusVolt` | rising toward pack voltage |
| `PCS_dcdcLvBusVolt` | ~12–15 V |
| `VCFRONT_pcsLVState` | `LV_ON (1)` |

---

## Mode 2 — AC Battery Charging (charger only)

PCS rectifies AC mains and charges the HV battery. DC-DC may also run
simultaneously to power 12 V loads — use `pcs_mode('both')` in that case.

### We send (0x22A)

| Signal | Value |
|--------|-------|
| `HVP_pcsControlRequest` | `SUPPORT (1)` |
| `HVP_dcLinkVoltageRequest` | pack voltage target |
| `HVP_pcsChargeHwEnabled` | `1` |
| `HVP_pcsDcdcHwEnabled` | `0` (or `1` for both) |
| `HVP_dcLinkVoltageFiltered` | echo of `PCS_dcdcHvBusVolt` |

### Contactor state (0x20A) — same as DC-DC support

Contactors must be closed before charging begins.

### BMS status (0x212)

| Signal | Value |
|--------|-------|
| `BMS_hvState` | `HV_UP_FOR_CHARGE (4)` |
| `BMS_contactorState` | `BMS_CTRSET_CLOSED (4)` |
| `BMS_uiChargeStatus` | `BMS_CHARGING (3)` |
| `BMS_chargeRequest` | `1` |
| `BMS_state` | `BMS_CHARGE (3)` |
| `BMS_smStateRequest` | `BMS_CHARGE (3)` |
| `BMS_pcsPwmEnabled` | `1` |

### CP evseStatus (0x21D)

| Signal | Value |
|--------|-------|
| `CP_evseAccept` | `1` |
| `CP_proximity` | `LATCHED (3)` |
| `CP_pilot` | `LINE_CHARGE (2)` |
| `CP_pilotCurrent` | EVSE limit A / 0.5 |
| `CP_cableCurrentLimit` | EVSE limit A |
| `CP_evseChargeType` | `AC_CHARGER_PRESENT (2)` |
| `CP_acChargeState` | `AC_CHARGE_ENABLED (3)` |

### CP chargeStatus (0x23D)

| Signal | Value |
|--------|-------|
| `CP_hvChargeStatus` | `CP_CHARGE_ENABLED (5)` |
| `CP_chargeShutdownRequest` | `NO_SHUTDOWN_REQUESTED (0)` |
| `CP_acChargeCurrentLimit` | AC limit A / 0.5 |

### OBC control (0x13D)

| Signal | Value |
|--------|-------|
| byte 0 | `0x05` (enabled) |
| byte 1 | AC limit A / 0.5 |

### UI chargeRequest (0x333)

| Signal | Value |
|--------|-------|
| `UI_chargeEnableRequest` | `1` |
| `UI_acChargeCurrentLimit` | AC limit A |
| `UI_chargeTerminationPct` | e.g. 800 (= 80.0 %) |

### Confirm via signal_cache

| Signal | Expected |
|--------|----------|
| `PCS_chgMainState` | `PCS_CHG_STATE_ENABLE (6)` |
| `PCS_hvChargeStatus` | `PCS_CHARGE_ENABLED (2)` |
| `PCS_chgInputVoltage` | AC line voltage (e.g. ~120–240 V) |
| `PCS_chargeShutdownRequest` | `NO_SHUTDOWN_REQUESTED (0)` |

---

## Mode 3 — Precharge

PCS uses its DC-DC converter in boost mode to charge the HV bus capacitors from
the LV side up toward pack voltage before contactors close. This prevents
inrush current damage when the pack contactors snap shut.

### Sequence

1. **Hold contactors in precharge position** — negative closed, positive via
   precharge resistor, set-state = CLOSING.
2. **Send PRECHARGE on 0x22A** with `dcdc_hw=True`.
3. **Poll `PCS_dcdcHvBusVolt`** until ≥ 95 % of `HVP_dcLinkVoltageRequest`.
4. **Advance contactors to CLOSED** and switch to SUPPORT.

### We send (0x22A) — step 2

| Signal | Value |
|--------|-------|
| `HVP_pcsControlRequest` | `PRECHARGE (2)` |
| `HVP_dcLinkVoltageRequest` | pack voltage target |
| `HVP_pcsChargeHwEnabled` | `1` (charger also enabled in our use case) |
| `HVP_pcsDcdcHwEnabled` | `1` |
| `HVP_dcLinkVoltageFiltered` | echo of `PCS_dcdcHvBusVolt` (0 until first reading) |

### Contactor state (0x20A) — step 1

| Signal | Value |
|--------|-------|
| `HVP_packContNegativeState` | `PULLED_IN (4)` |
| `HVP_packContPositiveState` | `PRECHARGE (2)` |
| `HVP_packContactorSetState` | `CLOSING (2)` |
| `HVP_packCtrsClosingAllowed` | `1` |
| `HVP_dcLinkAllowedToEnergize` | `1` |
| `HVP_hvilStatus` | `STATUS_OK (1)` |

### Contactor state (0x20A) — step 4 (after precharge complete)

Transition to the closed values from Mode 1 / Mode 2.

### BMS signals (0x212) during precharge

Same as Mode 2 (charge intent), since we're entering charging after precharge.

### PCS precharge substates to monitor

`PCS_dcdcInitialPrechargeSubState` (on the muxed status frame) progresses:

```
PCHG_FAST_DIS_HVBUS → PCHG_DWELL_CHARGE → PCHG_DWELL_WAIT → PCHG_ACTIVE
```

`PCHG_ACTIVE (10)` means precharge is sustaining — safe to close contactors.

### Completion criteria (poll signal_cache)

| Signal | Condition |
|--------|-----------|
| `PCS_dcdcPrechargeStatus` | `PCS_DCDC_PRECHARGE_ACTIVE (1)` |
| `PCS_dcdcMainState` | `DCDC_STATE_PRECHARGE_ACTIVE (3)` |
| `PCS_dcdcHvBusVolt` | ≥ 95 % of `HVP_dcLinkVoltageRequest` |

### Fault signals to watch

| Signal | Meaning |
|--------|---------|
| `PCS_dcdcPrechargeStatus == FAULTED (2)` | precharge failed, retry or abort |
| `PCS_a043_hvBusPrechargeFailure` | HV bus precharge fault |
| `PCS_a082_dcdcPchgUnsafeDiVoltage` | DI voltage unsafe for precharge |
| `PCS_dcdcPrechargeRtyCnt` | increments on each retry attempt |

---

## Signal summary by mode

| Signal | Off | DC-DC | Charge | Precharge |
|--------|-----|-------|--------|-----------|
| `HVP_pcsControlRequest` | SHUTDOWN | SUPPORT | SUPPORT | PRECHARGE |
| `HVP_pcsChargeHwEnabled` | 0 | 0 | 1 | 1 |
| `HVP_pcsDcdcHwEnabled` | 0 | 1 | 0 | 1 |
| Contactors | OPEN | CLOSED | CLOSED | CLOSING→CLOSED |
| `BMS_hvState` | HV_DOWN | HV_UP | HV_UP_FOR_CHARGE | HV_COMING_UP |
| `BMS_state` | STANDBY | SUPPORT | CHARGE | CHARGE |
| `CP_acChargeState` | INACTIVE | INACTIVE | ENABLED | STANDBY |
| `PCS_chgMainState` | IDLE | IDLE | ENABLE | STARTUP |
| `PCS_dcdcMainState` | STANDBY | 12V_SUPPORT | STANDBY | PRECHARGE_ACTIVE |
