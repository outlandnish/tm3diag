# pcs_send.py — Interactive PCS CAN scripting shell

Interactive Python REPL (IPython if available, otherwise stdlib) for experimenting with Tesla PCS CAN control messages. Runs alongside `can_live.py` on the same interface.

```
python pcs_send.py
python pcs_send.py --channel can0
```

## Options

| Flag          | Default     | Description               |
| ------------- | ----------- | ------------------------- |
| `--channel`   | `vcan0`     | CAN interface             |
| `--interface` | `socketcan` | python-can interface type |
| `--bitrate`   | —           | CAN bitrate (optional)    |

## Available functions

| Function                           | Frame   | Description                                                  |
| ---------------------------------- | ------- | ------------------------------------------------------------ |
| `pcs_mode(mode, hv_voltage)`       | `0x22A` | Set PCS mode: `'off'`, `'charge'`, `'dcdc'`, `'both'`        |
| `charger_enable(current_a)`        | `0x13D` | Enable charger with AC current limit                         |
| `charger_disable()`                | `0x13D` | Disable charger                                              |
| `charge_power(watts, on)`          | `0x2B2` | Charge power request in watts                                |
| `dcdc_voltage(volts)`              | `0x3A1` | DCDC output voltage setpoint                                 |
| `evse_limit(current_a)`            | `0x21D` | EVSE current limit                                           |
| `bms_heartbeat()`                  | `0x3B2` | One BMS keepalive frame                                      |
| `vcfront_heartbeat()`              | `0x545` | One VCFront keepalive frame (with counter + checksum)        |
| `raw(can_id, data)`                | any     | Send an arbitrary frame                                      |
| `start_heartbeats()`               | —       | Background BMS@10ms + VCFront@50ms keepalives                |
| `stop_heartbeats()`                | —       | Stop background heartbeats                                   |
| `start_listener(node)`             | —       | Print decoded incoming frames to terminal (default: `'PCS'`) |
| `stop_listener()`                  | —       | Stop listener                                                |
| `send_loop(name, fn, interval_ms)` | —       | Repeat any callable at a fixed interval                      |
| `stop_loop(name)`                  | —       | Stop a named loop                                            |
| `list_loops()`                     | —       | Show active loops                                            |

## Defaults

```python
DEFAULTS['hv_voltage']   = 400    # V
DEFAULTS['ac_limit']     = 32     # A
DEFAULTS['dcdc_voltage'] = 13.8   # V
DEFAULTS['charge_power'] = 0      # W
DEFAULTS['evse_limit']   = 32     # A
```

The PCS requires `0x3B2` (BMS) and `0x545` (VCFront) heartbeats at fixed rates or it raises `bmsMia`/`vcfrontMia` faults. Call `start_heartbeats()` before sending any control frames.
