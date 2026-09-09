# di.py — Drive Inverter Bench

Interactive bench emulator for a standalone Tesla Model 3 Drive Inverter (DI / rear drive unit). Continuously transmits the control frames the inverter expects, answers the immobilizer challenge, and gives you a live shell to command gear, system mode, and regen.

> **Requires a firmware dump** (`TM3_ROOT` set in `.env`) to look up the DI node's UDS addresses. The immobilizer responder needs a key-derivation provider — this repo ships none, so supply one (or pass `--no-immo` for liveness only). See [SECURITY_PROVIDER.md](SECURITY_PROVIDER.md).

```
python scripts/di/di.py
python scripts/di/di.py --no-immo
```

## What it does

On startup the script:

1. Opens the CAN bus and starts transmitting two control frames at 100 ms:
   - `0x64` `PRND_command_for_control` — gear, drive mode, regen, rolling counter
   - `0x54` `System_for_control` — system mode (off / drive / charge)
2. Starts the immobilizer responder (unless `--no-immo`) — answers the runtime handshake on `0x3D9` using a key from your key-derivation provider ([SECURITY_PROVIDER.md](SECURITY_PROVIDER.md))
3. Listens for `0x118` `DI_systemStatus` from the inverter and caches its signals
4. Opens an interactive shell where you can call control functions

## 2020 firmware note

On 2020 firmware the DU has **hardwired analog inputs** for the accelerator pedal and brake switch. There is no CAN torque command — the pedal position comes directly from the analog pins on the DU connector. Gear and system mode are still commanded over CAN as described below.

## Options

| Flag | Default | Description |
|---|---|---|
| `--channel` | from `.env` or `vcan0` | CAN interface name |
| `--interface` | from `.env` or `socketcan` | python-can driver |
| `--node` | `DIR` | DI node name in `nodes.json` |
| `--no-immo` | off | Skip immobilizer responder |
| `--no-interactive` | off | Run headless (no shell), Ctrl-C to stop |

## Shell functions

Once the bench is running, these functions are available in the shell:

### `gear(which)`

Set the drive direction. The frame is sent immediately on the next 100 ms tick.

```python
gear("P")   # Park
gear("R")   # Reverse
gear("N")   # Neutral
gear("D")   # Drive
```

### `system(mode)`

Set the `System_mode` field in frame `0x54`.

```python
system("off")     # system off
system("drive")   # enable drive
system("charge")  # charge mode
```

### `drivemode(mode)`

Set the pedal response curve.

```python
drivemode("chill")
drivemode("sport")
```

### `stopping(mode)`

Set the regen stopping behavior.

```python
stopping("standard")
stopping("creep")
stopping("hold")
```

### `regen(pct)`

Set maximum regen torque as a percentage (0–100).

```python
regen(100)   # full regen
regen(50)    # 50%
regen(0)     # coast
```

### `status()`

Print both what the bench is commanding and what the DI reports back on `0x118`.

```
  commanded: gear=P system=off drive_mode=0 regen=100%
  DI report: immo=DISARMED sys=IDLE gear=P pedal=—% brake=OFF
```

### `watch(seconds)`

Live-print the DI's `0x118` report for a number of seconds. Useful to observe the immobilizer handshake sequence:

```python
watch(10)
# DI report: immo=REQUEST sys=IDLE gear=P pedal=—% brake=OFF
# DI report: immo=AUTHENTICATING sys=IDLE gear=P pedal=—% brake=OFF
# DI report: immo=DISARMED sys=IDLE gear=P pedal=—% brake=OFF
```

## Typical startup sequence

```bash
# 1. Start the bench (immobilizer answered via your provider; --no-immo to skip)
python scripts/di/di.py

# 2. In the shell — wait for DISARMED, then enable drive
watch(15)
gear("D")
system("drive")
status()
```

## CAN frames transmitted

| Frame | CAN ID | Rate | Description |
|---|---|---|---|
| `PRND_command_for_control` | `0x64` | 100 ms | Gear, drive mode, regen, counter+checksum |
| `System_for_control` | `0x54` | 100 ms | System mode |

> **Note**: The checksum in `0x64` uses a placeholder algorithm (byte-sum mod 256). The exact constant used by Tesla/Ingenext is unconfirmed without a bench capture. If the DU ignores gear commands, a checksum mismatch is likely — compare against a real Ingenext-controlled bus capture and update `_prnd_checksum` in the script.

## Known limitations

The DU may require additional gating frames before it will arm (BMS HV-up signal, contactors-closed, VCFRONT power state). The full set has not been fully traced from vehicle-bus captures. If the inverter sits in `STANDBY` and will not transition to `ENABLE`, additional keepalive frames are likely needed.
