# pcs.py — Power Conversion System Bench

Interactive bench emulator for a standalone Tesla Model 3 PCS (Power Conversion System — the combined AC charger and DC-DC converter). Transmits all 14 periodic keepalive frames the PCS expects, and exposes operating-mode control and a closed-loop precharge sequence.

```
python scripts/pcs/pcs.py
python scripts/pcs/pcs.py --channel can0
```

## What it does

On startup the script:

1. Opens the CAN bus and starts transmitting 14 keepalive frames across three rate groups (10 / 50 / 100 ms) that emulate the BMS, VCFront, OBC, CP, UI, and HVP
2. Listens for IVT-S shunt frames (`0x521`–`0x523`) and caches current and voltage readings
3. Opens an interactive shell where you can call control functions

The PCS control frames (`0x22A` HVP_pcsControl and `0x20A` HVP_contactorState) are **not transmitted** until you call `pcs_mode()`. This prevents the PCS from enabling before you are ready.

## Options

| Flag | Default | Description |
|---|---|---|
| `--channel` | from `.env` or `vcan0` | CAN interface name |
| `--interface` | from `.env` or `socketcan` | python-can driver |
| `--no-interactive` | off | Run headless (no shell), Ctrl-C to stop |

## Shell functions

### `pcs_mode(mode, hv_voltage=None)`

Set the PCS operating mode. This starts transmitting `0x22A` and `0x20A` and configures the BMS state machine accordingly.

```python
pcs_mode("off")           # shut down, open contactors
pcs_mode("dcdc")          # 12V DC-DC only, contactors closed
pcs_mode("charge")        # AC charge only, contactors closed
pcs_mode("both")          # AC charge + DC-DC simultaneously
pcs_mode("dcdc", hv_voltage=67.2)   # override HV target voltage
```

| Mode | Contactors | BMS state | Use case |
|---|---|---|---|
| `off` | open | off | safe default |
| `dcdc` | closed | dcdc | 12V supply from HV pack |
| `charge` | closed | charge | AC charging |
| `both` | closed | charge | charge + 12V simultaneously |

### `precharge(hv_voltage=None, timeout_s=30.0)`

Ramp the DC link voltage via DC-DC boost, then close the main contactors. Watches `IVT_U2` (or `PCS_dcdcHvBusVolt` if no shunt) and waits until the link reaches 95% of target before closing.

```python
precharge()               # use default HV voltage (67.2 V)
precharge(400.0)          # target 400 V, 30 s timeout
precharge(400.0, 60.0)    # target 400 V, 60 s timeout
```

Progress is printed live. If the voltage does not reach threshold within `timeout_s` the script calls `pcs_mode("off")` and aborts.

### `current_limits(evse_a=None, ac_a=None)`

Set EVSE and AC charge current limits. If only `evse_a` is given, both are set to that value.

```python
current_limits(15)          # both EVSE and AC limit to 15 A
current_limits(32, 16)      # EVSE 32 A, AC 16 A
```

### `charge_power(watts=None)`

Set the charge power request transmitted on `0x2B2`.

```python
charge_power(3300)    # 3.3 kW
charge_power(0)       # no request
```

### `dcdc_voltage(volts=None)`

Set the 12V rail target voltage transmitted in `0x3A1` VCFRONT_vehicleStatus.

```python
dcdc_voltage(14.4)    # charge voltage
dcdc_voltage(13.6)    # float voltage
```

### `status()`

Print the current commanded mode and key measured signals.

```
  pcs=SUPPORT contactors=closed bms=charge | IVT_U2=398.5 IVT_U1=14.1
```

## Defaults

| Parameter | Default | Description |
|---|---|---|
| `hv_voltage` | `67.2` V | DC link target (18S pack) |
| `ac_limit` | `15` A | AC charge current limit |
| `dcdc_voltage` | `15.0` V | 12V rail target |
| `charge_power` | `0` W | Charge power request |
| `evse_limit` | `15` A | EVSE current limit |

## CAN frames transmitted

| Frame | CAN ID | Rate | Description |
|---|---|---|---|
| `HVP_pcsControl` | `0x22A` | 10 ms | PCS control word — mode, HV voltage, hw enables |
| `OBC_control` | `0x13D` | 10 ms | AC current limit |
| `BMS_log2` | `0x3B2` | 10 ms | BMS keepalive (alternates two payloads to prevent bmsMia) |
| `VCFront_heartbeat` | `0x545` | 50 ms | VCFront keepalive with counter + checksum |
| `HVP_contactorState` | `0x20A` | 100 ms | Contactor state machine |
| `BMS_status` | `0x212` | 100 ms | BMS HV state, charge request |
| `msg_0x232` | `0x232` | 100 ms | Fixed keepalive |
| `msg_0x25D` | `0x25D` | 100 ms | Fixed keepalive |
| `VCFRONT_sensors` | `0x321` | 100 ms | Temperatures and levels (fixed plausible values) |
| `UI_chargeRequest` | `0x333` | 100 ms | UI charge request |
| `CP_evseStatus` | `0x21D` | 100 ms | EVSE status and current limit |
| `CP_chargeStatus` | `0x23D` | 100 ms | Charge status |
| `charge_power` | `0x2B2` | 100 ms | Charge power request |
| `VCFRONT_vehicleStatus` | `0x3A1` | 100 ms | Vehicle status with DC-DC voltage target |

## IVT-S shunt support

If an IVT-S current/voltage transducer is on the bus, its frames are decoded automatically:

| Signal | CAN ID | Description |
|---|---|---|
| `IVT_I` | `0x521` | DC link current (A) |
| `IVT_U1` | `0x522` | 12V bus voltage (V) |
| `IVT_U2` | `0x523` | HV bus voltage (V) — used by `precharge()` |

## Typical startup sequence

```bash
python scripts/pcs/pcs.py --channel can0

# In the shell — DC-DC only first to verify 12V comes up
pcs_mode("dcdc")
status()

# Full charge session
pcs_mode("off")
current_limits(15)
precharge(400.0)       # waits for DC link to reach 380 V, then closes contactors
pcs_mode("charge")
status()
```
