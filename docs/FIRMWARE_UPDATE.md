# Tesla ECU Firmware Update — UDS Protocol Reference

Reverse engineered from `hashpicker_sim`. All frames are ISO-TP over CAN.
See [UDS_VM_OPCODES.md](UDS_VM_OPCODES.md) for the VM layer and
[README.md](README.md) for the BHX parser tool.

---

## Known errata (2026-05-04)

Several earlier claims in this document have been falsified or qualified by
direct reading of the binary in `hashpicker_sim_2` and by two CAN-bus
captures from a third-party flash tool — one successful DI
flash, one successful PM flash. Headline corrections, with details inline at
the affected sections:

1. **PCS prog 1's module bytes are `0x04` then `0x00` on the wire — not the
   `EcuNodeEntry+0x20` overrides.** The simulator's `vm_op_module_to_program`
   has a `context+0x29` override mechanism that rewrites the bytecode literal
   `0x04` to `0x0C` (the value at +0x20 for di/dis/pcscpu2). On real hardware
   this override does NOT happen — both third-party captures send the
   bytecode literals as-is. The doc previously implied node-table values
   were the wire bytes; that was wrong for the secondary side. See section
   on script `0x00651070`.

2. **Prog 1's "single auth, both CPUs" pattern can only work on CAN-topology
   revisions where both CPUs share one UDS endpoint.** In the binary's vintage,
   "pm" and "di" both have `EcuNodeEntry+0x1C = 20` — they were aliases for
   one UDS endpoint at `0x604/0x614`. In current production (per
   `Model3_ETH.compact.json` and observed wire traffic), the DI is split out
   onto `0x606/0x616` while PM stays at `0x604/0x614`. Prog 1's structure
   cannot span two UDS endpoints; flashing each CPU as its own prog-0 session
   is the right pattern in the split era. The 0x05F4 broadcast (payload
   `"PMS12-13"`) is shared because the underlying physical PMS module is the
   same, but the flash sessions are separate.

3. **The "PCS BHX has a single SHDR" claim is not generalizable.** DI's BHX
   has at least 2 SHDRs (observed at addresses `0x00009000` and `0x000091F0`,
   each 992 bytes). PM's BHX likewise has multiple SHDRs (e.g. `0x00003E80`,
   `0x00003E81F0`). The binary's BHX state machine (`bhx_transfer_state_machine`
   at `0x00407b46`) loops back to read more SHDRs after each section
   completes; the doc's previous "PCS = one section" was an artifact of
   whichever PCS sample was inspected, not a structural rule.

4. **DID `0xF180` does not match the structured layout this doc previously
   claimed (MODULES, COMPONENT_ID big-endian, PCBA_ID, ASSEMBLY_ID, USAGE_ID,
   FIRMWARE_TYPE, GIT_HASH, BUILD_CONFIG_ID).** Observed on PM:
   `01 50 4D 53 31 32 2D 31 33 00 01 11 EC 5E 94 B1 E2 00 4F`. Bytes 1-8 are
   the ASCII part name `"PMS12-13"` (printable, null-terminated), not packed
   integer fields. The 19-byte length and the leading `0x01` (likely "modules")
   are right; the middle is part-name ASCII; bytes 9-16 plausibly are a
   git-hash; bytes 17-18 plausibly are a build-config integer. Past byte 0,
   trust the bytes you observe over this doc.

5. **DID `0x0101` bootloader response varies per ECU and may or may not match
   the application ODJ layout.** PM's bootloader returns `05 01 04` (key,
   fw_type, protocol_ver — matches the documented layout). DI's returns
   `05 01 04` likewise. PCS variant 411 (per earlier note further down) was
   reported as `1B 00 05` which doesn't fit the layout. Don't abort on
   `fw_type != 1`; warn and continue.

6. **Phase 1 of `enterBootloader(0)` watches an arrival counter, not a
   payload pattern.** The binary's `FUN_00402764` returns the u16 at +0x04
   of the per-node broadcast entry, which is incremented by the CAN-receive
   layer (`FUN_00402d21`) on every matching frame. So Phase 1's exit
   condition is "any frame on this ECU's broadcast CAN ID arrives," not
   "this CAN ID's content changes." The 3.34 s ceiling exists because some
   ECUs broadcast at very low cadence (DI: ~50-75 s observed); for those
   the counter never bumps and Phase 1 falls through to Phase 2 (`3E 00` →
   `7E 00`) on its own. PM does emit a broadcast within ~28 ms of each
   `11 81` reset, so for PM the mechanic actually fires.

7. **Several "scripts" listed below have zero xrefs in the binary and are
   never executed.** Specifically: PCS prog 1 (the dual-CPU path at
   `0x006510a0`), all bootloader-update scripts (`0x00651300`, `0x00651320`,
   `0x00651340`), and various other prog-N entries beyond the first prog of
   each script slot. These exist as bytecode but no caller hands them to
   `uds_vm_run`. Treat them as design intent / specification, not as
   "what the simulator actually runs."

The doc below has not been edited section-by-section to reflect every
correction above — it remains useful as a UDS-layer spec but should be
cross-checked against `hashpicker_sim_2` and live captures before acting on
specifics. Items marked with **[ERRATA]** point back to this preamble.

---

## BHX File Format

BHX is a big-endian container. Headers are parsed locally — **never sent over UDS**.
Only the raw SHDR payload bytes are transmitted.

### GHDR — Global Header

| Offset | Size | Field              | Notes                                                       |
| ------ | ---- | ------------------ | ----------------------------------------------------------- |
| `0x00` | 4    | Magic `"GHDR"`     |                                                             |
| `0x04` | 4    | Version            | `1` or `2`, big-endian                                      |
| `0x08` | 4    | Total payload size | Sum of all SHDR payload sizes, not headers                  |
| `0x0C` | 4    | Total size         | **v2 only** — alternate total (redundant with `0x08`)       |

### SHDR — Section Header (20 bytes) + payload

| Offset  | Size | Field          | Notes                                                  |
| ------- | ---- | -------------- | ------------------------------------------------------ |
| `+0x00` | 4    | Magic `"SHDR"` |                                                        |
| `+0x04` | 4    | Version        | `1`, big-endian                                        |
| `+0x08` | 4    | Target address | Big-endian — used verbatim in `RequestDownload`        |
| `+0x0C` | 4    | Payload size   | Big-endian — used verbatim in `RequestDownload`        |
| `+0x10` | 4    | CRC32          | Of this section's payload; validated by ECU bootloader |
| `+0x14` | N    | **Payload**    | The only bytes sent over UDS                           |

Payload offset in file:

- GHDR v1: `0x20` (12-byte GHDR + 20-byte SHDR header)
- GHDR v2: `0x24` (16-byte GHDR + 20-byte SHDR header)

---

## Flash Scripts

17 unique scripts exist in the binary at address `0x00650fa0`–`0x006512e0`. Each
script contains one or more 48-byte program slots at `+0x00`, `+0x30`, `+0x60`, etc.
The orchestration layer selects which slot to run based on context.

The `module` byte in the ECU node table (`+0x20`, 1 byte) is loaded into
`context+0x29` before the VM runs. `moduleToProgram` reads and consumes it,
sending `2E 01 02 <module>` to select the target CPU/region in the
bootloader. For single-CPU ECUs this is `0x00`. **Almost every ECU has
`0x00` here**; the only non-zero values in the binary are for PCS-family
CPU2 nodes (`pcscpu2`, `di`, `dis`) which carry `0x0C`.

> **Don't confuse `+0x20` with `+0x1C`.** The byte at `+0x1C` (u16) is the
> ECU's `node_id`, used as the operand to the `udsContextSwitch` opcode and
> printed as `node=%d` by the binary's flash logger (`FUN_0040fb0a`).
> Many ECUs have small non-zero values there (cp=5, hvbms=2, vcsec=27,
> vcfront=13, vcright=25) — those are NOT module bytes.

> **Implementation note — every script flow below shows `reset(soft)` as a single
> step, but the VM emits two opcodes: `reset` followed by `enterBootloader(0)`.**
> Skipping the `enterBootloader(0)` wait is the most common cause of `moduleToProgram`
> being rejected — DSC / RDBI / SecurityAccess succeed against the still-running
> application, but WDBI 0x0102 only exists in the bootloader. See section 0 of the
> frame-by-frame reference for the handover protocol.

### ECU → Script Map

| Script       | ECUs                                                                                  |
| ------------ | ------------------------------------------------------------------------------------- |
| `0x00650fa0` | gtw3                                                                                  |
| `0x00650fb0` | hvbms, cp, epas3p, epas3s, epbl, epbr, hvp, ocs1p, sccmk, vcsec, tas                |
| `0x00651000` | vcfront, ibstcal                                                                      |
| `0x00651030` | vcright                                                                               |
| `0x00651050` | vcleft                                                                                |
| `0x00651070` | pcs (mod=0x00), pcscpu2 (mod=0x0c), pm (mod=0x00), pms (mod=0x00), di (mod=0x0c), dis (mod=0x0c) |
| `0x006510d0` | park                                                                                  |
| `0x006510f0` | park, aps                                                                             |
| `0x00651110` | vcleftramapp (mod=0x06), vcrightramapp (mod=0x0f), vcfrontramapp (mod=0x0f), vcsecrumapp (mod=0x0f), sccmksub (mod=0x06) |
| `0x00651140` | ibst                                                                                  |
| `0x00651170` | espcal (mod=0x07), rcmcal (mod=0x07)                                                  |
| `0x00651190` | esp                                                                                   |
| `0x006511b0` | ibstcal (bootloader path)                                                             |
| `0x006511d0` | rcm                                                                                   |
| `0x006511f0` | tpms                                                                                  |
| `0x00651230` | cmp                                                                                   |
| `0x00651270` | ptc                                                                                   |
| `0x00651290` | vcrightramapp, vcfrontramapp, vcsecrumapp, bleepcenter (mod=0x0f)                     |
| `0x006512b0` | vcleftramapp (mod=0x0f)                                                               |
| `0x006512d0` | opc (mod=0x0c), opcs (mod=0x0c)                                                       |
| `0x006512e0` | ths (mod=0x0c), swc (mod=0x0c), lumbarl/lumbar/lumbarr (mod=0x0b), bleep* (various)  |
| `0x00651300` | parkbu (mod=0x12), hvbmsbu (mod=0x02), hvpbu (mod=0x0e) — bootloader updater (`*bu`) |
| `0x00651320` | vcfrontbu (mod=0x0d) — vcfront-specific bootloader updater (OTA preamble)            |
| `0x00651340` | parkbl (mod=0x12), hvbmsbl (mod=0x02), hvpbl (mod=0x0e), vcfrontbl (mod=0x0d) — bootloader image (`*bl`) |

---

### `0x00650fa0` — gtw3

Single unrecognized opcode. No UDS flash sequence — stub only.

---

### `0x00650fb0` — Standard (hvbms, cp, epas3p/s, epbl/r, hvp, ocs1p, sccmk, vcsec, tas)

```
[prog 0]
reset(soft)
orFlags(4)                     suppress-error flag on
boardPartSerialGet             22 F012/F013/F014/F015  (logged, not validated)
andNotFlags(4)                 suppress-error flag off
diagnosticSession(2)           10 02
varifyCompAndFirmware(1)       22 01 01
securityAccess(0)              27 01/02  (tesla_hash, level idx 0)
netSetTimeout(5)               P2=5s P2*=10s
CALL sub1:
  moduleToProgram              2E 01 02 <module>
  initializeEraseModule        31 01 FF 00 01
  transferData                 RequestDownload + blocks + TransferExit
checkModuleProgrammed          31 01 02 01
checkCorrectComponentAndRev    31 01 02 02
reset(soft)
sleep(300ms)
```

---

### `0x00651000` — vcfront / ibstcal (power context variant)

```
[prog 0]  — power rail on, flash, power rail off
orFlags(4)
CALL sub2              setSecurityAccessLevel(3) + securityAccess + vcWaitForOTAMode + ibstPowerControl(1)
andNotFlags(4)
CALL sub4              setSecurityAccessLevel(3) + securityAccess + ibstPowerControl(2)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware(1)  securityAccess(0)
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
orFlags(4)
CALL sub2
CALL sub5              OTA mode + contextSwitch + erase variant
vcFrontLockIOControl(0)

[prog 1]  — standard flash only
reset(soft)  orFlags(4) / andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware(1)  securityAccess(0)
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651030` — vcright

```
[prog 0]  — standard flash
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)

[prog 1]  — resume: auth + flash only (no reset or part read)
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651050` — vcleft (pre-flash vendor routine)

```
[prog 0]
netSetTimeout(5)
orFlags(4)
routineControl0601(0)          31 01 06 01 — vendor pre-flash routine
andNotFlags(4)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651070` — pcs / pcscpu2 / di / dis / pm / pms (multi-CPU)

The `module` byte selects which CPU the bootloader programs (`2E 01 02 <module>`).

> **[ERRATA #1, #2, #7]** The `module` byte values listed for di/dis/pcscpu2
> elsewhere in this doc (`0x0C`) are the `EcuNodeEntry+0x20` values that the
> simulator's `vm_op_module_to_program` would override into. Two third-party
> captures show the **real bootloader expects the prog-1 bytecode literals**:
> `0x04` for the secondary side (di/dis/pcscpu2), `0x00` for the primary
> (pcs/pm/pms). The simulator's override mechanism is sim-only behavior.
>
> Prog 1 below is **dead code in the binary** (zero xrefs to `0x006510a0`).
> It also can't run on CAN-topology revisions where the two CPUs are at
> separate UDS endpoints (`0x604/0x614` for PM, `0x606/0x616` for DI in the
> current production revision); a single auth window can't span two CAN
> ID pairs. In the split era, flash each CPU as its own prog-0 session.

```
[prog 0]  — standard flash with extended erase timeout
reset(soft)
orFlags(4)  boardPartSerialGet  andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
netSetTimeout(5)               P2=5s P2*=10s
CALL sub1                      moduleToProgram(2E 01 02 <module>) + erase + transfer
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(300ms)

[prog 1]  — dual-CPU in-sequence (hardcoded subfunction, no context+0x29 override)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
moduleToProgram(4)             2E 01 02 04 — CPU2 flash region
netSetTimeout(30)              P2=30s P2*=60s
initializeEraseModule(0)
netSetTimeout(1)
transferData
checkModuleProgrammed
moduleToProgram(0)             2E 01 02 00 — CPU1 flash region
netSetTimeout(30)
initializeEraseModule(0)
transferData
netSetTimeout(4)
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 2]  — quick re-flash (5s post-reset sleep, no part read)
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(5000ms)

[prog 3]  — auth + flash only (no reset preamble)
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x006510d0` — park (extended erase timeout)

```
[prog 0]
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(5000ms)

[prog 1]  — auth + flash only
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x006510f0` — park / aps

```
[prog 0]
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — reset only (stub)
reset(soft)
```

---

### `0x00651110` — RAM app scripts (vcleft/vcright/vcfront/vcsec ramapp, sccmksub)

```
[prog 0]  — RAM app flash (no boardPartSerialGet)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — flash count check + DTC clear + security level 3
checkFlashCount(2)
orFlags(4)  clearDTC(0)  andNotFlags(4)
reset(soft)
orFlags(4)  boardPartSerialGet  andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
orFlags(4)  checkCorrectComponentAndRev  andNotFlags(4)
reset(soft)
```

---

### `0x00651140` — ibst

```
[prog 0]  — flash count check + DTC clear + security level 3
checkFlashCount(2)
orFlags  clearDTC  andNotFlags
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
orFlags  checkCorrectComponentAndRev  andNotFlags
reset(soft)

[prog 1]  — direct explicit erase (no sub1)
reset(soft)
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651170` — espcal / rcmcal (calibration flash, security level 3)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — reset only (stub)
reset(soft)
```

---

### `0x00651190` — esp (flash count check + security level 3)

```
[prog 0]
checkFlashCount(1)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
reset(soft)
```

---

### `0x006511b0` — ibstcal bootloader path (hard reset with retries)

```
[prog 0]
checkFlashCount(0)
reset(2)               hard reset, 3 retries + 10s delay each
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
CALL sub1
checkModuleProgrammed
reset(2)
sleep(100ms)
```

---

### `0x006511d0` — rcm (Pektron: flash count + hard reset + explicit erase)

```
[prog 0]
checkFlashCount(0)
reset(2)               hard reset with retries
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
sleep(100ms)
reset(2)
```

---

### `0x006511f0` — tpms (security level 4 — baolong_hash)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware
securityAccess(4)      baolong_hash algorithm
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651230` — cmp (security level 7 — pektron-style, non-standard erase)

```
[prog 0]
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(7)      FUN_0040be8e algorithm
moduleToProgram(0)
initializeEraseModule(1)   accepts non-standard response count
transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — transfer-only resume (no re-auth or re-erase)
transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651270` — ptc (non-standard erase, timeout 10s)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(10)
varifyCompAndFirmware  securityAccess(0)
moduleToProgram(0)
initializeEraseModule(1)   accepts non-standard response
transferData
checkModuleProgrammed(1)
checkCorrectComponentAndRev(1)
reset(soft)

[prog 1]  — empty
```

---

### `0x00651290` — vcright/vcfront/vcsec ramapp, bleepcenter

```
[prog 0]  — flash without boardPartSerialGet
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — resume: auth + flash only
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 2]  — OTA session with securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 3]  — same, no reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
```

---

### `0x006512b0` — vcleftramapp (pre-flash routine + OTA paths)

```
[prog 0]  — pre-flash routineControl0601 + standard flash
netSetTimeout(5)  orFlags  routineControl0601  andNotFlags
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — OTA: securityAccess(13) + reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 2]  — OTA: same, no reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 3]  — OTA: sleep(5000ms) + same
sleep(5000ms)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 4]  — standard flash, timeout 3s
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

---

### `0x006512d0` — opc / opcs (OTA state machine)

```
[prog 0]  — OTA: securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 1]  — standard flash, timeout 3s
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

---

### `0x006512e0` — ths / swc / lumbar* / bleep* (OTA + multi-CPU style)

```
[prog 0]  — OTA: securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — verify only + reset
checkCorrectComponentAndRev  reset(soft)

[prog 2]  — standard flash, timeout 3s, sleep preamble
sleep(1000ms)
diagnosticSession(2)  varifyCompAndFirmware(2)
securityAccess(0)  netSetTimeout(3)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 3]  — CALL sub5 only
CALL sub5
```

---

### `0x00651300` — bootloader-updater (parkbu, hvbmsbu, hvpbu)

The "bu" file (e.g. `parkbu.hex`, `hvpbu.hex`) is a **bootloader update agent**
that gets installed into the regular application slot first. The script is a
plain prog-0 flash with `fw_type = 1`:

```
[prog 0]
reset(soft) + enterBootloader(0)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware(1)             ← fw_type = 1 (regular firmware)
securityAccess(0)
CALL sub1                             moduleToProgram + erase + transfer
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)                           ← agent boots after this reset
```

After this script's trailing reset, the ECU comes back up running the bu agent
in place of the original application. The CAN endpoint is unchanged — same
`UDS_<parent>Request` / `<PARENT>_udsResponse` IDs as the parent ECU.

Module byte at `+0x20` is `0x00` for all `*bu` nodes; the wire frame is
`2E 01 02 00`. (The non-zero values at `+0x1C` — `0x12` for parkbu,
`0x02` for hvbmsbu, `0x0E` for hvpbu — are `node_id`s, not module bytes.)

`vcfrontbu` uses a different script (`0x00651320`) — see below.

---

### `0x00651320` — vcfront-specific bootloader-updater (vcfrontbu)

VCFRONT can't be flashed without first putting the **VCRIGHT** ECU into a
coordinated OTA state (the front and right vehicle controllers share door-lock
and OTA-state machinery). The `0x00651320` script wraps the standard
`SCRIPT_BL_UPDATER` body with a leading `CALL sub4` that opens a transient UDS
handle to VCRIGHT, runs the prep, then closes it:

```
[prog 0]
CALL sub4                            ← VCRIGHT-side OTA prep (see below)
reset(soft) + enterBootloader(0)
diagnosticSession(2)
varifyCompAndFirmware(1)             ← fw_type = 1 (the bu agent is "regular firmware")
securityAccess(0)
CALL sub1                             moduleToProgram + erase + transfer
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

#### `sub4` (`0x006513a0`) — VCRIGHT-side OTA prep + IOCBI lockout

Decoded VM bytecode `1A 19 0D 03 03 03 02 00 18 00 17 01 1B 00 2C 00`:

```
udsContextSwitch(25)                 ← open VCRIGHT (request 0x608, response 0x609)
diagnosticSession(3)                 ← extended session
setSecurityAccessLevel(3)            ← internal: writes 3 to context+0x02
securityAccess(0)                    ← seed level 0x05 (override doesn't fire because
                                       ctx+0x02 is now 3, not <3)
VCWaitForOTAMode(0)                  ← RC 0x540 start, then poll until response[0]==2
vcFrontLockoutIOControl(1)           ← IOCBI 0x218 controlParam=3, control byte 1
restoreUdsContext(0)                 ← close VCRIGHT, restore VCFRONT
RET
```

> **Operational prerequisite:** RC `0x540` returning `byte == 2` (OTA mode active)
> requires the **vehicle to actively be in OTA state** — initiated by the vehicle's
> overall state machine, not by the flash tool. On a bench setup with VCFRONT and
> VCRIGHT alone on a test bus, this routine will time out and the bu flash will not
> proceed. Bootloader updates for VCFRONT are practical only against a live, OTA-
> capable vehicle.

A flash tool implementing this needs:

1. **A second UDS session to VCRIGHT** (CAN IDs from `nodes.json`/ETH compact, sharing
   the same physical CAN channel as the VCFRONT session).
2. The sub-4 sequence applied to that VCRIGHT session.
3. After RET, the VCRIGHT session is closed and the standard bu flash continues
   against VCFRONT on its normal CAN IDs.

`sub5` (`0x006513b0`) is identical to sub4 but with `vcFrontLockoutIOControl(0)`
instead of `(1)` — the "release" counterpart to sub4's "engage". Used in some other
VCFRONT/VCRIGHT scripts but **not** in `0x00651320`.

Module byte at `+0x20` for `vcfrontbu` is `0x00` (wire frame `2E 01 02 00`);
the `0x0D` at `+0x1C` is the VCFRONT `node_id`, not the module byte.

---

### `0x00651340` — bootloader image (parkbl, hvbmsbl, hvpbl, vcfrontbl)

The "bl" file is the actual bootloader being installed. This script runs
**immediately after** the bu's trailing reset, with no opening reset of its
own — it relies on the bu agent already booting:

```
[prog 0]
sleep(1000ms)                         ← wait for bu agent to come up
diagnosticSession(2)
varifyCompAndFirmware(2)             ← fw_type = 2 (BOOTLOADER)
securityAccess(0)  netSetTimeout(3)
CALL sub1                             moduleToProgram + erase + transfer
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

The bu agent recognizes `fw_type=2` as a bootloader file and erases/rewrites
the bootloader sector instead of the app slot. After the trailing reset the
ECU boots into the new bootloader.

Module byte at `+0x20` is `0x00` for all `*bl` nodes (same as the
corresponding `*bu`).

#### Complete bootloader-update sequence

For an ECU with both bu and bl artifacts, the full update flow is:

1. **Flash `*bu`** via script `0x00651300` — replaces the app slot with the
   update agent. ECU resets and the agent boots.
2. **Flash `*bl`** via script `0x00651340` — agent erases the bootloader
   sector and writes the new bootloader. ECU resets into the new bootloader.
3. **Re-flash the regular `*` (app)** via the parent ECU's normal script —
   restores the application to the app slot. **Without this step the ECU
   continues to run the update agent in place of its application** and may
   appear non-functional. Skipping it is dangerous.

The bu→bl→app order is mandatory. CAN IDs throughout the entire sequence are
the parent ECU's standard UDS request/response IDs; no separate addressing is
needed for the bootloader endpoints.

---

## Subcomponent flashes (CP PLC modem)

Some ECUs include a secondary chip that's flashed *through* the main MCU's UDS
endpoint. The CP (charge port) MCU has a PLC modem (Powerline Communication
chip) on board, used for high-bandwidth communication during charging. The PLC
modem doesn't have its own CAN connection — its firmware is delivered to the
CP MCU via the regular UDS flash flow, and the CP MCU's bootloader forwards
the data over an internal interconnect (SPI or UART) based on the embedded
file addresses.

### TSV layout for CP

```
cp:201392129  cp/14/CP_..._CRC.bhx              cp.bhx          cp          ...
cp:201392129  cp/14/cpPlcFw_1.2.5-BE0A291A.hex  cpPlcFw.hex     cpPlcFw     ...
cp:201392129  cp/14/cpPlcPib-98FA4A87.hex       cpPlcPib.hex    cpPlcPib    ...
```

All three rows share the same `cp:<key>` lookup. Older CP variants (≤ 13)
ship only `cp.bhx`; the PLC modem firmware was added at variant 14.

### Flash properties

| ecu_type    | Script | Module byte | File format | Target              |
| ----------- | ------ | ----------- | ----------- | ------------------- |
| `cp`        | `0x00650fb0` (Standard) | `0x00` | BHX | CP MCU app slot |
| `cpPlcFw`   | `0x00650fb0` (Standard) | `0x00` | Intel HEX | PLC modem firmware |
| `cpPlcPib`  | `0x00650fb0` (Standard) | `0x00` | Intel HEX | PLC modem PIB (config) |

All three use the **same script**, the same wire frame for `moduleToProgram`
(`2E 01 02 00`), and the **same UDS CAN IDs** (`UDS_cpRequest` /
`CP_udsResponse`). The CP MCU bootloader distinguishes them by the address
ranges in the transferred records — the HEX files target memory regions on
the PLC modem die, not the CP MCU's flash.

(All three node entries have `0x05` at `+0x1C`, which is the CP `node_id`
used by `udsContextSwitch`, not the module byte.)

`fw_type` returned by DID `0x0101` during `varifyCompAndFirmwareType` is `1`
in all three cases — the bootloader doesn't differentiate the file's eventual
destination at the verify-type level.

### Order

TSV order is authoritative: **`cp` (main app) first**, then `cpPlcFw`, then
`cpPlcPib`. Each runs through its own complete prog-0 sequence (reset →
session → auth → moduleToProgram → erase → transfer → verify → reset). The
CP MCU must be running its new app before it can hand off PLC firmware over
the internal interconnect — flashing the PLC firmware first against an old
CP MCU app may fail or write to the wrong region.

### What's at node-table offset `+0x24`?

The CP, cpPlcFw, and cpPlcPib node-table entries differ at offset `+0x24`
(values `0`, `8`, `6` respectively). **Meaning unknown** — I have not traced
any binary code that reads this offset. It is *not* the module byte (which
is at `+0x20` per `FUN_0040fb0a`) and it does not appear in any UDS frame
we observed. The values don't match obvious candidates (DID offsets, sub-
function bytes, security indices) cleanly. Earlier versions of this doc
called this "an internal subcomponent identifier" — that was speculation
without backing evidence and has been retracted.

### Implementation note

A flash tool with `cp`, `cpPlcFw`, `cpPlcPib` mapped to the standard script
flow (module `0x05`) and the same `UdsSession` works for all three —
`bhx.parse_file` for the `.bhx`, `ihex.parse_file` for the `.hex`, both
producing a `Segment(start_address, data)` interface that `RequestDownload`
uses verbatim.

This pattern is **only used by CP** in the seed artifacts surveyed — no
other ECU in the TSV has subcomponent files with the `<parent><suffix>`
naming convention.

---

## Frame-by-Frame Reference

### 0. ECUReset and Bootloader Handover (before session)

This is two VM opcodes (`reset` + `enterBootloader`), not one. **A flash tool that
sends only the reset frame and skips the handover wait will silently end up talking
to the application instead of the bootloader** — DSC, RDBI 0x0101, and SecurityAccess
will all succeed (apps support those), but `WDBI 0x0102` (`moduleToProgram`) will be
rejected because that DID exists only in the bootloader.

> **[ERRATA #6]** Phase 1 watches the **arrival counter** (`+0x04` in the
> per-node entry of `DAT_00651440`/`DAT_00651500`), not the boot-broadcast
> payload. The CAN-receive layer (`FUN_00402d21`) increments that counter on
> every matching frame. Phase 1's exit condition is "any frame on the watched
> CAN ID arrived since the loop started," not a content-change check. The
> 3.34 s ceiling matters because some ECUs broadcast at very low cadence
> (DI ~50-75 s observed); for those, Phase 1 always falls through to Phase 2
> on its own. PM broadcasts within ~28 ms of each `11 81`, so the mechanic
> actually fires for PM but rarely for DI.

**0a. Reset frame** (`reset(0)` in the script, mnemonic "reset(soft)"):

```
→ 11 81    ECUReset subfunction 0x01 with suppressPositiveResponse bit set
  (fire-and-forget — no response wait)
```

`reset(1)` and `reset(2)` send `11 01` instead and wait for `51 01`; `reset(2)`
additionally retries up to 3 times with 10 s between attempts.

**0b. Bootloader handover wait** (`enterBootloader(0)` — always emitted after `reset`):

The VM's two-phase implementation (`FUN_00402120` + `FUN_00401f8c`):

1. **Phase 1 — keep-alive while watching for boot-ID change** (up to 3.34 s):
   sleep 10 ms, send `3E 80` (TesterPresent fire-and-forget, no response
   expected), check the boot-broadcast CAN ID for this node — break when its
   value changes (bootloader is announcing itself on a different ID/payload).
2. **Phase 2 — TP-with-response confirmation** (up to 14 retries × 40 ms):
   reduce P2 to 40 ms, send `3E 00` (TesterPresent zeroSubFunction, response
   required) and break on the first `7E 00` reply. Restore prior P2.

Note the two distinct TesterPresent variants:
- **`3E 80`** — sub-function `0x00` with `suppressPositiveResponse` bit set; no
  reply expected. Used in phase 1 (bus keep-alive).
- **`3E 00`** — sub-function `0x00`, response required; replies `7E 00`. Used
  in phase 2 (positive confirmation).

`3E 01` is **not** valid TesterPresent — only `0x00` is defined as a sub-function.
Strict bootloaders return NRC `0x12 subFunctionNotSupported` for `3E 01`.

Implementations without DBC-level boot-ID decoding can substitute phase 1
with a fixed-time keep-alive loop (e.g. spam `3E 80` for ~1.5 s while the
bootloader boots), then run phase 2 normally. Skip the entire handover wait
if the script's first opcode is `reset(1)` or `reset(2)`, which already wait
for the application's `51 01` ack — though `enterBootloader` is still
required to confirm bootloader mode before sending DSC.

---

### 1. Read Part / Serial Info *(logged only — failure does not abort)*

```
→ 22 F0 12    ReadDataByIdentifier — board part number
← 62 F0 12 <data>

→ 22 F0 13    ReadDataByIdentifier — serial number
← 62 F0 13 <data>

→ 22 F0 14    ReadDataByIdentifier — board revision
← 62 F0 14 <data>

→ 22 F0 15    ReadDataByIdentifier — assembly info
← 62 F0 15 <data>
```

Results written to `modinfo.log`.

---

### 2. Enter Programming Session

```
→ 10 02    DiagnosticSessionControl — programming session
← 50 02 <P2_hi> <P2_lo> <P2star_hi> <P2star_lo>
```

Response bytes 1–2: P2 timeout (ms). Bytes 3–4: P2\* enhanced timeout (×10 ms).
Applied to the CAN handle immediately.

**Start TesterPresent keepalive here: send `3E 80` every ~2 s for the duration.**

---

### 3. moduleToProgram — CPU/region selection

```
→ 2E 01 02 <module>    WriteDataByIdentifier DID 0x0102 — select CPU/region
← 6E 01 02
```

DID `0x0102` is **bootloader-only** — confirm the ECU is in the bootloader (see
section 0b) before sending. If you skip the handover, this is the first frame that
will fail (typically NRC `0x31 requestOutOfRange` or `0x22 conditionsNotCorrect`),
because the application accepts DSC/RDBI/SecurityAccess but not this WDBI.

The `module` byte is taken from the ECU node table entry (`+0x20`) and placed in
`context+0x29` before the VM runs. `moduleToProgram` reads and consumes it. For
single-CPU ECUs the module byte is `0x00`.

Module byte values for script `0x00651070` (PCS/DI/PM family):

| ECU | Module byte | Meaning |
| --- | ----------- | ------- |
| `pcs`, `pm`, `pms` | `0x00` | CPU1 / primary flash region |
| `di`, `dis`, `pcscpu2` | `0x0c` | CPU2 / secondary flash region |

---

### 4. Verify Component and Firmware Type

```
→ 22 01 01    ReadDataByIdentifier DID 0x0101
← 62 01 01 <component_key> <fw_type> <protocol_ver>
```

Three data bytes after the DID echo, in this order:
- byte[0] = `component_key` — logged only
- byte[1] = `fw_type` — must match the operand passed to `varifyCompAndFirmwareType`
  (always `1` for prog-0 flash flows). Mismatch → abort with error `0x10000 | fw_type`.
- byte[2] = `protocol_ver` — stored at `context+0x02` and consumed by the next
  `securityAccess` step to choose the seed level (see section 5).

> **Bootloader-mode caveat (observed on PCS, protocol_ver=5):** PCS variant
> 411 in bootloader mode returns `1B 00 05` for DID `0x0101`. Per the
> application ODJ that decodes as `COMPONENT_KEY=0x1B, FIRMWARE_TYPE=0x00,
> PROTOCOL_VER=0x05`. The `0x00` would fail `varifyCompAndFirmwareType(1)`'s
> equality check against operand `1` per the binary's
> `uds_varify_comp_and_firmware` (`0x0040973d`).
>
> **Inferred (not directly verified):** the values are also consistent with
> a layout of `[COMPONENT_ID_LO, COMPONENT_ID_HI, PROTOCOL_VER]` since
> `0x001B` matches the documented PCS CPU1 `COMPONENT_ID`. This is
> pattern-matching on the values; we have not seen the bootloader's actual
> DID 0x0101 decoder. Real Tesla flash behavior on this mismatch is
> unknown — tm3diag pragmatically downgrades the check to a warning so
> the rest of the flow can be observed.

---

### 5. Security Access

```
→ 27 <level>              RequestSeed
← 67 <level> <seed bytes>

→ 27 <level+1> <key bytes>    SendKey
← 67 <level+1>
```

The seed level and key algorithm vary by ECU:

| Security idx | Algorithm          | Level             | ECUs                              |
| ------------ | ------------------ | ----------------- | --------------------------------- |
| 0            | `tesla_hash`       | 0x05 (see below)  | most ECUs                         |
| 3            | `tesla_hash`       | varies            | ibst, esp, espcal, rcmcal, rcm    |
| 4            | `baolong_hash`     | varies            | tpms                              |
| 7            | `FUN_0040be8e`     | varies            | cmp                               |
| 13           | OTA session key    | varies            | opc, opcs, ths, swc, lumbar, bleep|

> **Protocol-version branch for idx 0** (`uds_security_access` at `0x0040c090`):
> the default seed level from the table (`DAT_00650e08[0]`) is `0x05`, but if
> `protocol_ver` (read in section 4 and stashed at `context+0x02`) is **less than 3**,
> the level is overridden to `0x01`. So a flash tool implementing idx 0 must:
>
> 1. read DID `0x0101` and remember `byte[2]` (`protocol_ver`),
> 2. send `27 01` / `27 02` if `protocol_ver < 3`, else `27 05` / `27 06`.
>
> NRC `0x35` (requestSequenceError) at the seed step means the ECU is already
> unlocked — silently treat as success.

`tesla_hash` — computed by your security provider (not shipped):

```python
# tesla_hash is supplied by your security provider; see docs/SECURITY_PROVIDER.md
    raise NotImplementedError  # not shipped
```

If ECU responds with NRC `0x35` (already unlocked), silently accepted.

---

### 6. Erase Flash Sectors

```
→ 31 01 FF 00 01    RoutineControl startRoutine 0xFF00, arg=0x01
← 71 01 FF 00 <status>
```

`status` must be `0x00`. The arg byte `0x01` is required.

Timeout during erase — set before sending, restore after:

| Script               | Erase timeout       |
| -------------------- | ------------------- |
| Standard             | P2=3s / P2\*=6s     |
| `0x00651070` (PCS)   | P2=5s / P2\*=10s    |
| `0x006510d0` (park)  | P2=1s / P2\*=2s     |
| `0x00651140` (ibst)  | P2=4s / P2\*=8s     |
| `0x006511d0` (rcm)   | P2=4s / P2\*=8s     |
| `0x006511f0` (tpms)  | P2=3s / P2\*=6s     |
| `0x00651270` (ptc)   | P2=10s / P2\*=20s   |

---

### 7. RequestDownload

```
→ 34 00 44 <addr:4BE> <size:4BE>
← 74 <lenFmt> <maxBlockSize:var>
```

- `addr` and `size` are taken verbatim from the SHDR target address and payload size fields
- `maxBlockSize` extracted from response; capped at 512 bytes

Failures: `uploadDownloadNotAccepted (0x70)`, `requestOutOfRange (0x31)`

---

### 8. TransferData

```
→ 36 <seq> <up to maxBlockSize bytes>
← 76 <seq> <crc_hi> <crc_lo>
```

- `seq` starts at `0x01`, increments per block, wraps `0xFF → 0x00`
- Send raw SHDR payload bytes in order
- ECU returns 2-byte CRC per block — verify it matches

Failures: `wrongBlockSequenceCounter (0x73)`, `transferDataSuspended (0x71)`

---

### 9. RequestTransferExit

```
→ 37
← 77
```

---

### 10. Verify Programming

```
→ 31 01 02 01    RoutineControl startRoutine 0x0201
← 71 01 02 01 <status>
```

ECU recomputes CRC of flashed image against trailing CRC word in payload.
`status` must be `0x00`.

---

### 11. Verify Component / Revision Match

```
→ 31 01 02 02    RoutineControl startRoutine 0x0202
← 71 01 02 02 <status>
```

Validates COMPONENT_ID, PCBA_ID, ASSEMBLY_ID, USAGE_ID against stored identity.
`status` must be `0x00`.

---

### 12. ECU Reset

```
→ 11 01    ECUReset
← 51 01
```

Bootloader re-validates CRC on next boot and jumps to application if valid.
Stop TesterPresent keepalive after receiving the positive response.

---

## Variant Lookup and Firmware File Selection

A flash tool must translate a boot ID read from the device into the correct BHX
file(s) using the lookup tables in `seed_artifacts_v2/`.

### Lookup table files

Two files provide the same mapping; use `signed_metadata_map.tsv` in production:

| File | Description |
| ---- | ----------- |
| `version_map2.tsv` | Unsigned lookup table — 6 tab-separated columns |
| `signed_metadata_map.tsv` | Signed version — same 6 columns plus a 7th base64 per-entry signature. First line is a manifest header (`<sha1>\t<entry_count>`) — skip it during lookup. |

### Column format

```
<ecu>:<variant_id>  <artifact_path>  <local_filename>  <ecu_type>  <crc>  <conditions>  [<signature>]
```

- **`ecu`** — node name that owns the boot ID (e.g. `pcs`, `pm`)
- **`variant_id`** — numeric boot ID reported by the ECU
- **`artifact_path`** — path within `seed_artifacts_v2/` to the BHX file
- **`local_filename`** — canonical staging name (e.g. `pcs.bhx`, `pcscpu2.bhx`)
- **`ecu_type`** — node name the file is actually flashed to; may differ from `ecu` (see DI below)
- **`crc`** — CRC32 of the SHDR payload, half-swapped: `((crc & 0xFFFF) << 16) | (crc >> 16)`
- **`conditions`** — vehicle option constraints (e.g. `drivetrainType=0,vdcType=1`); `*` = unconditional
- **`signature`** — (`signed_metadata_map.tsv` only) base64 per-entry signature

### Boot ID to node lookup

1. Connect to the ECU and read the boot ID (boot broadcast message or DID `0xF180`)
2. Resolve `<ecu>:<boot_id>` in the table — filter rows by matching `conditions` against vehicle options
3. Collect **all** matching rows — multi-file ECUs produce more than one

### Multi-file ECUs: multiple rows, one boot ID

Several ECUs map the same boot ID key to more than one BHX file. Each row carries a
different `ecu_type` and `local_filename`; each is a separate sequential flash operation.

**PCS** — two CPUs, same CAN IDs, same script (`0x00651070`), `moduleToProgram` selects the target:

| Row | `local_filename` | `ecu_type` | SHDR address | Module byte | Flash sectors |
| --- | ---------------- | ---------- | ------------ | ----------- | ------------- |
| 1   | `pcs.bhx`        | `pcs`      | `0x00088000` | `0x00`      | 4, 5, 6       |
| 2   | `pcscpu2.bhx`    | `pcscpu2`  | `0x00082000` | `0x0C`      | 1, 2, 3, 4    |

Component IDs: CPU1 = `0x001b`, CPU2 = `0x0096`. Bootloader in Sector 0 (`0x80000–0x81FFF`) is never erased.

**DI** — keyed by the PM (power module) boot ID, but `ecu_type=di`:

```
pm:390004738   di/12360/DI_23-63-2_M3_Single_crc.bhx   di.bhx   di   ...   drivetrainType=0,vdcType=0
```

The `di` node carries module byte `0x0c`, so it is flashed as a secondary CPU even though the
lookup key came from `pm`.

### Flash ordering for multi-file ECUs

PCS-family ECUs that resolve to **both** a primary and a secondary BHX file
have two valid execution paths. Either works; **prog 1 is preferred** when both
files are available up-front.

#### Prog 1 — single authenticated session, both CPUs (preferred)

When the lookup yields both a primary (`ecu_type ∈ {pcs, pm, pms}`) and a
secondary (`ecu_type ∈ {pcscpu2, di, dis}`) entry, run script `0x00651070`
**prog 1** once with both files. CPU2/secondary is flashed first using
bootloader-internal `moduleToProgram(4)`, then CPU1/primary using
`moduleToProgram(0)`, in a single `securityAccess` window:

```
reset(soft) + enterBootloader(0)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
moduleToProgram(4)   netSetTimeout(30)      → secondary
initializeEraseModule  netSetTimeout(1)  transferData  checkModuleProgrammed
moduleToProgram(0)   netSetTimeout(30)      → primary
initializeEraseModule  transferData  netSetTimeout(4)
checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

The `4` and `0` operands are **bootloader-internal region codes**, distinct
from the node table module bytes (`0x0C` / `0x00`) used by prog 0. Use these
literals when running prog 1; do not derive them from the script map.

#### Prog 0 ×2 — fallback when files arrive separately

If the firmware files arrive incrementally (e.g. streaming OTA) and you can
only stage one at a time, run prog 0 twice. TSV row order is authoritative —
CPU1 (`ecu_type=pcs`) rows always appear before CPU2 (`ecu_type=pcscpu2`):

1. Flash `pcs.bhx` → `moduleToProgram` sends `2E 01 02 00` → CPU1 at `0x00088000`
2. Flash `pcscpu2.bhx` → `moduleToProgram` sends `2E 01 02 0C` → CPU2 at `0x00082000`

Each entry goes through its own full prog-0 sequence (reset → session → auth →
moduleToProgram → erase → transfer → verify → reset). The module byte comes
from the node table entry for each `ecu_type`, **not** from the BHX file. The
SHDR target address and payload size are the only BHX-derived values passed
to the ECU (via `RequestDownload`).

> **Implementations should auto-switch to prog 1 when both files are present.**
> A primary/secondary pair detected at firmware-selection time is the trigger;
> running prog 0 twice for the same ECU update wastes one full
> reset / handover / auth round-trip.

> **[ERRATA #2, #7]** This recommendation is wrong for current production
> revisions. Prog 1 has zero xrefs in the binary (it's design intent, not
> an executed path), and on revisions where the PMS module's two CPUs are
> at separate UDS endpoints (`0x604/0x614` for PM, `0x606/0x616` for DI per
> `Model3_ETH.compact.json`), a single auth window cannot span both — they
> are physically different CAN ID pairs. The "prog 0 ×2" path (run the full
> sequence once per file) is correct for the split topology, and is what
> the third-party tool actually does on real hardware. Auto-switching to
> prog 1 on detecting a primary/secondary pair would break in this era.
> The "wasteful round-trip" critique is true but minor; the topology
> constraint dominates.

---

## Security Detail

### `tesla_hash` (most ECUs, security level idx 0 and 3)

Stateless, no secret key. Each seed byte XOR'd with `0x35`:

```
key[i] = seed[i] ^ 0x35   for i in 0..15
```

16-byte seed from `27 xx` response. 16-byte key sent as `27 xx+1` payload.

### Other algorithms

- **`baolong_hash`** (tpms, security idx 4): different algorithm, 2-byte buffer
- **`FUN_0040be8e`** (cmp, security idx 7): pektron-style, different algorithm

---

## Key DIDs

| DID      | Name                 | Bytes | Notes                                     |
| -------- | -------------------- | ----- | ----------------------------------------- |
| `0x0101` | `COMP_AND_FW_TYPE`   | 3     | `[component_key, fw_type, protocol_ver]`  |
| `0xF012` | Board part number    | var   | logged to `modinfo.log`                   |
| `0xF013` | Serial number        | var   | logged to `modinfo.log`                   |
| `0xF014` | Board revision       | var   | logged to `modinfo.log`                   |
| `0xF015` | Assembly info        | var   | logged to `modinfo.log`                   |
| `0xF100` | Flash count          | 4     | enforced per-ECU limit; exceeding → abort |
| `0xF180` | `BOOTLOADER_VERSION` | 19    | identity record (see below)               |
| `0xF01D` | `USAGE_ID`           | 2     |                                           |
| `0xF01E` | `SUB_USAGE_ID`       | 2     | secondary node                            |

`BOOTLOADER_VERSION` (DID `0xF180`) — 19 bytes:

```
Byte   Field
0      MODULES
1–2    COMPONENT_ID  (big-endian)
3      PCBA_ID
4      ASSEMBLY_ID
5–6    USAGE_ID
7      (unused)
8      FIRMWARE_TYPE
9–16   GIT_HASH  (8 bytes)
17–18  BUILD_CONFIG_ID
```

> **[ERRATA #4]** This packed-integer-field layout does not match observed
> bootloader responses. PM returns
> `01 50 4D 53 31 32 2D 31 33 00 01 11 EC 5E 94 B1 E2 00 4F` and DI returns
> a similar shape with `"DIS12-13"` in the same slot. Bytes 1-8 are the
> ASCII part name (printable, null-terminated), not COMPONENT_ID/PCBA_ID/
> ASSEMBLY_ID/USAGE_ID/(unused)/FIRMWARE_TYPE as integer fields.
> Confirmed structurally: byte 0 = `0x01` (modules), bytes 1-8 = ASCII
> identifier, bytes 9-16 = plausibly git-hash, bytes 17-18 = plausibly a
> build-config integer. Don't trust the doc layout for byte indices 1-8.

---

## Error Reference

| Code                            | Meaning                                               |
| ------------------------------- | ----------------------------------------------------- |
| `REQUEST_DOWNLOAD_FAILED`       | `34` rejected — wrong address, size, or session state |
| `NAK_uploadDownloadNotAccepted` | ECU not ready to receive download                     |
| `BHX_TRANSFER_DATA_ERROR`       | `36` block rejected                                   |
| `BLOCK_CHECKSUM_MISMATCH`       | CRC in `76` response doesn't match computed value     |
| `NAK_wrongBlockSequenceCounter` | Sequence byte out of order — transfer aborted         |
| `BHX_TRANSFER_exit_ERROR`       | `37` rejected                                         |
| `BHX_INVALID_GLOBAL_HEADER`     | File doesn't start with valid `GHDR` magic+version    |
| `BHX_INVALID_SEGMENT_HEADER`    | SHDR magic/version unrecognized                       |
| `BHX_READ_FILE_FAILED_A/B/C/D`  | Short read from BHX file                              |
| `INCORRECT_MODULE_PROGRAMMED`   | Routine `0x0201` returned non-zero status             |
| `INCORRECT_COMPONENT_AND_REV`   | Routine `0x0202` returned non-zero status             |
| `FLASH_COUNT_LIMIT_EXCEEDED`    | DID `0xF100` at or over per-ECU limit                 |

---

## Tools

```bash
python3 tools/bhx_parser.py <file.bhx>
python3 tools/bhx_parser.py --json <file.bhx>
python3 tools/bhx_parser.py --extract-dir /tmp/out/ <file.bhx>
```
