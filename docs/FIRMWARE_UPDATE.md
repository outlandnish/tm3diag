# Tesla ECU Firmware Update — UDS Protocol Reference

---

### ECU → Script Group Map

| Script group                     | ECUs                                                                                                                     |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| gtw3                             | gtw3                                                                                                                     |
| Standard                         | hvbms, cp, epas3p, epas3s, epbl, epbr, hvp, ocs1p, sccmk, vcsec, tas                                                     |
| vcfront / ibstcal                | vcfront, ibstcal                                                                                                         |
| vcright                          | vcright                                                                                                                  |
| vcleft                           | vcleft                                                                                                                   |
| pcs / di / pm family (multi-CPU) | pcs (mod=0x00), pcscpu2 (mod=0x0c), pm (mod=0x00), pms (mod=0x00), di (mod=0x0c), dis (mod=0x0c)                         |
| park (extended erase timeout)    | park                                                                                                                     |
| park / aps                       | park, aps                                                                                                                |
| RAM app (variant A)              | vcleftramapp (mod=0x06), vcrightramapp (mod=0x0f), vcfrontramapp (mod=0x0f), vcsecramapp (mod=0x0f), sccmksub (mod=0x06) |
| ibst                             | ibst                                                                                                                     |
| espcal / rcmcal                  | espcal (mod=0x07), rcmcal (mod=0x07)                                                                                     |
| esp                              | esp                                                                                                                      |
| ibstcal (bootloader path)        | ibstcal (bootloader path)                                                                                                |
| rcm                              | rcm                                                                                                                      |
| tpms                             | tpms                                                                                                                     |
| cmp                              | cmp                                                                                                                      |
| ptc                              | ptc                                                                                                                      |
| RAM app (variant B)              | vcrightramapp, vcfrontramapp, vcsecramapp, bleepcenter (mod=0x0f)                                                        |
| vcleftramapp (OTA paths)         | vcleftramapp (mod=0x0f)                                                                                                  |
| opc / opcs                       | opc (mod=0x0c), opcs (mod=0x0c)                                                                                          |
| ths / swc / lumbar* / bleep*     | ths (mod=0x0c), swc (mod=0x0c), lumbarl/lumbar/lumbarr (mod=0x0b), bleep\* (various)                                     |
| Bootloader updater (`*bu`)       | parkbu (mod=0x12), hvbmsbu (mod=0x02), hvpbu (mod=0x0e)                                                                  |
| vcfrontbu (OTA preamble)         | vcfrontbu (mod=0x0d)                                                                                                     |
| Bootloader image (`*bl`)         | parkbl (mod=0x12), hvbmsbl (mod=0x02), hvpbl (mod=0x0e), vcfrontbl (mod=0x0d)                                            |

---

### gtw3

No UDS flash sequence — stub only.

---

### Standard (hvbms, cp, epas3p/s, epbl/r, hvp, ocs1p, sccmk, vcsec, tas)

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
  initializeEraseModule        31 01 FF 00
  transferData                 RequestDownload + blocks + TransferExit
checkModuleProgrammed          31 01 02 01
checkCorrectComponentAndRev    31 01 02 02
reset(soft)
sleep(300ms)
```

---

### vcfront / ibstcal (power context variant)

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

### vcright

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

### vcleft (pre-flash vendor routine)

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

### pcs / pcscpu2 / di / dis / pm / pms (multi-CPU)

The `module` byte selects which CPU the bootloader programs (`2E 01 02 <module>`)

Prog 1 (single-session dual-CPU) is supported when the secondary shares the
same UDS endpoint as the primary (i.e. `pcscpu2` on the PCS node). When `di`
or `dis` is the secondary and is on its own CAN endpoint, each CPU is flashed
as a separate prog-0 session instead.

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

### park (extended erase timeout)

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

### park / aps

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

### RAM app scripts — variant A (vcleft/vcright/vcfront/vcsec ramapp, sccmksub)

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

### ibst

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

### espcal / rcmcal (calibration flash, security level 3)

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

### esp (flash count check + security level 3)

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

### ibstcal bootloader path (hard reset with retries)

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

### rcm (Pektron: flash count + hard reset + explicit erase)

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

### tpms (security level 4 — baolong_hash)

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

### cmp (security level 7 — pektron-style, non-standard erase)

```
[prog 0]
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(7)
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

### ptc (non-standard erase, timeout 10s)

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

### RAM app scripts — variant B (vcright/vcfront/vcsec ramapp, bleepcenter)

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

### vcleftramapp (pre-flash routine + OTA paths)

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

### opc / opcs (OTA state machine)

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

### ths / swc / lumbar* / bleep* (OTA + multi-CPU style)

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

### Bootloader updater — parkbu, hvbmsbu, hvpbu

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

`vcfrontbu` uses a different script — see below.

---

### vcfrontbu — vcfront-specific bootloader updater

VCFRONT can't be flashed without first putting the **VCRIGHT** ECU into a
coordinated OTA state (the front and right vehicle controllers share door-lock
and OTA-state machinery). The vcfrontbu script wraps the standard
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

#### `sub4` — VCRIGHT-side OTA prep + IOCBI lockout

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

`sub5` is identical to sub4 but with `vcFrontLockoutIOControl(0)`
instead of `(1)` — the "release" counterpart to sub4's "engage". Used in some other
VCFRONT/VCRIGHT scripts but **not** in the vcfrontbu script.

Module byte at `+0x20` for `vcfrontbu` is `0x00` (wire frame `2E 01 02 00`);
the `0x0D` at `+0x1C` is the VCFRONT `node_id`, not the module byte.

---

### Bootloader image — parkbl, hvbmsbl, hvpbl, vcfrontbl

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

1. **Flash `*bu`** (bootloader updater script) — replaces the app slot with the
   update agent. ECU resets and the agent boots.
2. **Flash `*bl`** (bootloader image script) — agent erases the bootloader
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

Some ECUs include a secondary chip that's flashed _through_ the main MCU's UDS
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

| ecu_type   | Script group | Module byte | File format | Target                 |
| ---------- | ------------ | ----------- | ----------- | ---------------------- |
| `cp`       | Standard     | `0x00`      | BHX         | CP MCU app slot        |
| `cpPlcFw`  | Standard     | `0x00`      | Intel HEX   | PLC modem firmware     |
| `cpPlcPib` | Standard     | `0x00`      | Intel HEX   | PLC modem PIB (config) |

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
any binary code that reads this offset. It is _not_ the module byte (which
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

**0a. Reset frame** (`reset(0)` in the script, mnemonic "reset(soft)"):

```
→ 11 81    ECUReset subfunction 0x01 with suppressPositiveResponse bit set
  (fire-and-forget — no response wait)
```

`reset(1)` and `reset(2)` send `11 01` instead and wait for `51 01`; `reset(2)`
additionally retries up to 3 times with 10 s between attempts.

**0b. Bootloader handover wait** (`enterBootloader(0)` — always emitted after `reset`):

1. **Phase 1 — keep-alive while watching for boot-ID change** (up to 334 × 10 ms = 3.34 s):
   sleep 10 ms, send `3E 80` (TesterPresent fire-and-forget, no response
   expected), check the boot-broadcast CAN ID for this node — break when its
   arrival counter increments (bootloader is announcing on the broadcast ID).
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

**HVP bootloader side (TMS570LS, confirmed from binary):** The bootloader's main
loop (`FUN_000038b4`) checks `if (4999 < current_tick - last_tick)` where ticks
are driven by the RTI peripheral at 10 MHz → 1 ms/tick. That gives a **4999 ms
S3server timeout**. The session timer is reset by any `3E xx` frame
(`FUN_00006374`). Phase 1's `3E 80` frames arrive every 10 ms — well within the
window — so the bootloader stays in programming session throughout phase 1.

**`boot_state` prerequisite:** The HVP bootloader reads `boot_state` @ `0x0800160C`
at startup. If it finds `0x0F` (app-launch mode) it immediately jumps to the
application — the TesterPresent window never opens. For the bootloader to remain
in programming mode, the application must write `0x00` to `boot_state` before
asserting the `11 81` reset (this is done by the app's own shutdown path, not
by the GTW3). The condition for staying in the bootloader is: `boot_state == 0x00`
**and** `stay_in_bootloader == 1` (flag at `0x080015C6`, set by boot config init).

Implementations without DBC-level boot-ID decoding can substitute phase 1
with a fixed-time keep-alive loop (e.g. spam `3E 80` for ~1.5 s while the
bootloader boots), then run phase 2 normally. Skip the entire handover wait
if the script's first opcode is `reset(1)` or `reset(2)`, which already wait
for the application's `51 01` ack — though `enterBootloader` is still
required to confirm bootloader mode before sending DSC.

---

### 1. Read Part / Serial Info _(logged only — failure does not abort)_

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

**Start TesterPresent keepalive here: send `3E 80` every 2 s for the duration.**
The HVP bootloader's S3server timeout is 4999 ms (4999 RTI ticks at 10 MHz = 1 ms/tick);
2 s provides comfortable margin. Any `3E xx` frame resets the timer.

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

Module byte values for the PCS/DI/PM family:

| ECU                | Module byte | Notes                                                         |
| ------------------ | ----------- | ------------------------------------------------------------- |
| `pcs`, `pm`, `pms` | `0x00`      | CPU1 / primary — confirmed                                    |
| `pcscpu2`          | `0x0C`      | CPU2 on shared PCS node (0x628) — confirmed; `0x04` rejected  |
| `di`, `dis`        | `0x04`      | CPU2 on dedicated node (0x606) — confirmed from wire captures |

Newer firmware variants may use different secondary module bytes. The tool tries
the primary value first and falls back to an alternate if NRC 0x10 or 0x31 is
returned.

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

---

### 5. Security Access

```
→ 27 <level>              RequestSeed
← 67 <level> <seed bytes>

→ 27 <level+1> <key bytes>    SendKey
← 67 <level+1>
```

The seed level and key algorithm vary by ECU:

| Security idx | Algorithm       | Level            | ECUs                               |
| ------------ | --------------- | ---------------- | ---------------------------------- |
| 0            | `tesla_hash`    | 0x05 (see below) | most ECUs                          |
| 3            | `tesla_hash`    | varies           | ibst, esp, espcal, rcmcal, rcm     |
| 4            | `baolong_hash`  | varies           | tpms                               |
| 7            | `FUN_0040be8e`  | varies           | cmp                                |
| 13           | OTA session key | varies           | opc, opcs, ths, swc, lumbar, bleep |

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
→ 31 01 FF 00    RoutineControl startRoutine 0xFF00 (no argument byte)
← 71 01 FF 00 <status>
```

`status` must be `0x00`. The frame is exactly 4 bytes — no data after the routine ID.
Earlier versions of this doc showed a trailing `0x01` byte; that was a misread.
The protocol_ver=5 PCS bootloader rejects the 5-byte form with NRC 0x31.

Timeout during erase — set before sending, restore after:

| Script group | Erase timeout     |
| ------------ | ----------------- |
| Standard     | P2=3s / P2\*=6s   |
| PCS family   | P2=5s / P2\*=10s  |
| park         | P2=1s / P2\*=2s   |
| ibst         | P2=4s / P2\*=8s   |
| rcm          | P2=4s / P2\*=8s   |
| tpms         | P2=3s / P2\*=6s   |
| ptc          | P2=10s / P2\*=20s |

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

| File                      | Description                                                                                                                                               |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version_map2.tsv`        | Unsigned lookup table — 6 tab-separated columns                                                                                                           |
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

#### Prog 1 — single authenticated session, both CPUs

When the lookup yields both a primary (`ecu_type ∈ {pcs, pm, pms}`) and a
secondary (`ecu_type ∈ {pcscpu2}`) entry **on the same UDS endpoint**, run
the PCS/DI/PM family script **prog 1** once with both files. CPU2/secondary is flashed
first, then CPU1/primary, in a single `securityAccess` window:

```
reset(soft) + enterBootloader(0)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
moduleToProgram(<secondary_byte>)  netSetTimeout(erase_timeout)   → secondary
initializeEraseModule  transferData  checkModuleProgrammed
moduleToProgram(<primary_byte>)   netSetTimeout(erase_timeout)   → primary
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

Module bytes come from the ECU script map (see section 3). For `pcscpu2` on
the shared PCS node: secondary byte = `0x0C`, primary byte = `0x00`. If the
bootloader rejects the secondary byte with NRC 0x10 or 0x31, the tool retries
with a firmware-version-specific fallback (`0x04`).

Prog 1 is only applicable when the secondary CPU shares the same UDS endpoint
as the primary. When `di`/`dis` is the secondary (dedicated node 0x606/0x616),
use prog 0 ×2 instead.

#### Prog 0 ×2 — separate sessions per CPU

When the secondary is on its own CAN endpoint (e.g. `di` at 0x606/0x616), or
when files arrive incrementally, run prog 0 twice. TSV row order is
authoritative — CPU1 (`ecu_type=pcs`) rows always appear before CPU2:

1. Flash `pcs.bhx` → `moduleToProgram` sends `2E 01 02 00` → CPU1 at `0x00088000`
2. Flash `pcscpu2.bhx` → `moduleToProgram` sends `2E 01 02 0C` → CPU2 at `0x00082000`

Each entry goes through its own full prog-0 sequence (reset → session → auth →
moduleToProgram → erase → transfer → verify → reset). The module byte comes
from the node table entry for each `ecu_type`, **not** from the BHX file. The
SHDR target address and payload size are the only BHX-derived values passed
to the ECU (via `RequestDownload`).

---

## Security Detail

### `tesla_hash` (most ECUs, security level idx 0 and 3)

Stateless, no secret key. The seed→key transform is **not shipped** — supply it through your
security provider ([SECURITY_PROVIDER.md](SECURITY_PROVIDER.md)).

16-byte seed from `27 xx` response. 16-byte key sent as `27 xx+1` payload.

### Other algorithms

- **`baolong_hash`** (tpms, security idx 4) and **`FUN_0040be8e`** (cmp, security idx 7) —
  distinct algorithms, likewise provider-supplied and not shipped.

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

`BOOTLOADER_VERSION` (DID `0xF180`) — 19 bytes.

```
Byte   Field
0      MODULES          (0x01 observed)
1–8    Part name        (ASCII, null-terminated; e.g. "PMS12-13")
9–16   Git hash         (8 bytes)
17–18  Build config ID
```

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
python bhx.py info    <file.bhx>       # segment target address + payload size
python bhx.py extract <file.bhx> out/  # write the SHDR payload to out/
```

See [bhx.md](bhx.md) for the container format and library API.
