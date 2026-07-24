# Loading a TMS320 firmware image into Ghidra

How to take a Tesla C28x firmware image — the inverter halves (**DIR** / **PMR**), the
**PCS** (Power Conversion System), or any other TMS320F28377D module — and get a clean,
analyzable program in Ghidra.

These images do **not** disassemble out of the box. Two things have to be right first:

1. **The bytes are byte-swapped.** Every 16-bit word is stored with its two bytes reversed
   relative to how the C28x core fetches them. Loaded raw, the disassembler desyncs and
   produces valid-but-incoherent garbage. You must swap before importing.
2. **The C28x needs a custom processor module.** Stock Ghidra has no TMS320C28x. We maintain
   a SLEIGH module (`ghidra-tms320c28x`) that you install once.

After those, the image still has **no header, no symbols, and no usable entry point** (the
reset stub lives in a flash sector the OTA images don't ship — see
[Why there's no entry point](#why-theres-no-entry-point)). So loading is: import flat at the
right base, map the chip's peripherals, then *seed* functions from the bytes themselves.

The whole flow:

```
pick firmware ──▶ .bhx ──extract──▶ .bin ──byte-swap──▶ swapped.bin ──import──▶ Ghidra
(metadata map)                                                            │
                                       SetupF28377D (map peripherals + RAM)
                                       SeedFunctions (recover function entries)
                                       auto-analyze
```

If you don't yet know *which* firmware file to open, start at
[Step 0](#step-0--find-the-right-firmware-file). If you already have the `.bhx`/`.bin` in hand,
skip to [Step 1](#step-1--get-a-flat-bin-payload).

---

## Prerequisites (one-time)

- **Ghidra 12.x** (12.1.2 is what we use) with a working Java 21+/25 runtime.
- **The `ghidra-tms320c28x` SLEIGH module installed.** Build it and drop it into
  `<ghidra_install>/Ghidra/Processors/`, or install the packaged extension; see that repo's
  `docs/BUILDING.machine-specific.md`. Verify it's live: in the language picker you should see
  **`TMS320C28x:LE:32:default`**. (Quick smoke test: disassemble bytes `01 00` → `ABORTI`,
  `21 76` → `IDLE`.)
- **`scripts/c28x/c28x_loadimg.py`** (in this repo) — the byte-swapper.
- **`bhx.py`** (in this repo) — to extract a `.bin` payload from a `.bhx` container, if you're
  starting from an OTA file. See [bhx.md](bhx.md).
- **The two Ghidra scripts** from the module repo (`ghidra-tms320c28x/ghidra_scripts/`):
  `SetupF28377D.java` and `SeedFunctions.java`. Point Ghidra's Script Manager at that directory
  (Script Manager → *Manage Script Directories* → add it).

---

## Step 0 — Find the right firmware file

Firmware lives under `deploy/seed_artifacts_v2/`. Inside it there are *many* builds of every
module — different hardware revisions, RWD vs AWD, performance vs standard — so the first job
is picking the **one** `.bhx` that matches the module you want to investigate. The index that
tells you which file is which is **`deploy/seed_artifacts_v2/signed_metadata_map.tsv`**.

### What's in the metadata map

It's a tab-separated file, one row per signed firmware artifact:

| Col | Field | Example | What it tells you |
|---|---|---|---|
| 0 | `ecu:partnum` | `pm:117440512` | which ECU, and the **part number** (decimal) |
| 1 | **path** | `db/30/PM_7-0-0_Poppyseed_RevB-C_VCRIGHT_AM2634C_RWD_crc.bhx` | the file to open (relative to `seed_artifacts_v2/`) |
| 2 | filename | `pm.bhx` | generic role name |
| 3 | module type | `pm`, `pmbl`, `pmbu`, `pcs`, `pcsbl` | app body vs bootloader (`*bl`) vs boot-updater (`*bu`) |
| 4 | version/hash | `31ba4310` | build id |
| 5 | **config tags** | `chassisType=2,drivetrainType=0,performancePackage=0` | the variant discriminators |
| 6 | signature | … | (ignore for RE) |

Two columns do the disambiguating work:

- **`ecu:partnum` (col 0).** The prefix is the ECU (`pm` = inverter power module, `di` =
  inverter drive module, `pcs` = charger/DC-DC, `cp` = charge port, …). The number after the
  colon is the **part number** — and the **PMR and DIR halves of one physical inverter share the
  same part number**, which is exactly how you pair them. Find the `pm:` row for your inverter,
  note the part number, then grab the `di:` row with the matching number for the motor-control
  half.
- **config tags (col 5).** This is how you tell RWD from AWD and standard from performance —
  `drivetrainType=0` is RWD, `drivetrainType=1` is AWD; `performancePackage` distinguishes
  perf builds. This matters: e.g. some AWD inverter builds offload work to a different chip than
  their RWD siblings, so opening the wrong variant can send you down the wrong path.

> Want the app body, not the bootloader. For RE you almost always want the module-type in col 3
> with **no** `bl`/`bu` suffix (`pm`, `pcs`, the `PMR_*`/`DIR_*`/`PCS_*` app `.bhx`), not
> `pmbl`/`pmbu`/`pcsbl` (those are the flash-programming bootloaders).

### Browsing the map

Filter to the module you care about and eyeball the candidates. From the repo root:

```bash
cd deploy/seed_artifacts_v2

# all inverter power-module (PMR) app builds, with their part# and config tags
grep -P '^pm:' signed_metadata_map.tsv | grep -P '\tpm\t' | cut -f1,2,6

# narrow to one drivetrain (RWD = drivetrainType=0)
grep -P '^pm:' signed_metadata_map.tsv | grep 'drivetrainType=0' | cut -f1,2,6

# the PCS (charger) app builds
grep -P '^pcs:' signed_metadata_map.tsv | grep -P '\tpcs\t' | cut -f1,2,6
```

Pick the row whose name + config tags match your target; **col 1 is the file you'll open in
Step 1.** Then, for an inverter, grab the matching `di:` row by part number for the DIR half:

```bash
# say the PMR you chose is part 117440512 — find its DIR mate
grep -P '^di:117440512\b' signed_metadata_map.tsv | cut -f1,2,6
```

### Don't know the part number? Ask the car with tm3diag

If you have a live ECU on the bench and want to know *which* build it's running (so you open the
matching firmware), read its board part/serial DIDs over CAN with
[tm3diag.py](tm3diag.md) and use `board-parts`:

```bash
python tm3diag.py --node PCS
# at the connected prompt:
#   board-parts     → reads part/serial DIDs 0xF012–0xF015, 0xF030/0xF031
```

(The CAN channel and artifacts directory come from `TM3_CHANNEL` and `TM3_ARTIFACTS_DIR` in
your `.env`, so you don't need to pass `--channel` or `--artifacts`.)

`board-parts` returns the ECU's hardware part/assembly numbers; match those against the
`ecu:partnum` and the `PM_PCBA_*` revision in the metadata map to land on the exact `.bhx` the
car is running. (`scan` from the pre-connection menu first if you're not sure the node is alive.)

Once you've identified the file (col 1 path), continue to Step 1.

---

## Step 1 — Get a flat `.bin` payload

If you already have a raw `.bin`, skip to Step 2.

OTA images ship as `.bhx` containers. Extract the code payload:

```bash
python bhx.py info    PMR_32-67-0_..._crc_lithium-signed.bhx     # shows segment addr + length
python bhx.py extract PMR_32-67-0_..._crc_lithium-signed.bhx out/
```

`info` prints the segment's **target address** — that is your load base for Step 3. For the
inverter halves and PCS it is typically one of the values in the table below; trust what `bhx
info` reports for your specific file over the table.

> **Note:** a `.bhx` is a *container* (`GHDR`/`SHDR`, big-endian). The `0x0900`-headered
> **bootloader artifacts** (`pmrbl`/`pmrbu`) carry an extra inner header but ARE flat
> byte-swapped C28x code too — these same steps DO apply (validated 2026-06-25:
> `swap16(bu .bhx payload)` == the coherent Ghidra image byte-for-byte, 49152/49152; the
> full UDS/flash handlers decompile cleanly). They are NOT signed packages — image
> acceptance is CRC + fw_type + SecurityAccess + rev only. The earlier "signed container /
> not flat code" note was wrong. App bodies (`DIR_*`, `PMR_*`, `PCS_*`) are likewise flat code.

---

## Step 2 — Byte-swap the image

```bash
python scripts/c28x/c28x_loadimg.py --mode swap out/PMR_32-67-0_...bin --out pmr_swapped.bin
```

This swaps the two bytes of every 16-bit word. The output is what Ghidra imports. The tool
prints the import recipe on success.

**Why this is necessary (and how we know):** F28377D flash is 16-bit, and these images store
each word with its bytes reversed relative to the instruction stream. Read raw, the canonical
compiled-function prologue `MOVL *SP++` ×3 (`b2bd aabd a2bd`) appears **0** times; read
byte-swapped it appears 67–329× per image, and `LRETR` density jumps to ~7/KB (real-code
density). This is a property of the Tesla F28377D image format, so it applies to **every**
F28377D-based module — DIR, PMR, PCS, and others — across all firmware eras. (See
`docs/private/c28x-ghidra-tesla/BYTESWAP-BREAKTHROUGH.md` for the full evidence.)

---

## Step 3 — Import flat at the correct base

In Ghidra: **File → Import File**, select your `*_swapped.bin`, then:

| Field | Value |
|---|---|
| **Format** | Raw Binary |
| **Language** | `TMS320C28x:LE:32:default` |
| **Options → Base Address** | the load base for this image (see below) |

> **Addresses are WORD addresses.** The C28x is word-addressable (1 address = 16 bits). All
> the bases below, and every address in our RE notes, are word addresses — type them directly
> into the Base Address field.

Common load bases (confirm against `bhx info` for your file):

| Image | Folder / part | Load base (word) | Core |
|---|---|---|---|
| **PMR** (rear inverter, CAN half) | `pm/<id>/PMR_*` | `0x88000` | CPU1 |
| **DIR** (rear inverter, motor-control half) | `di/<id>/DIR_*` | `0x80800` | CPU2 |
| **PMF / DIF** (front inverter) | `pm/`,`di/` `*F_*` | as PMR / DIR | CPU1 / CPU2 |
| **PCS** (charger / DC-DC) | `PCS_*` | per `bhx info` | CPU1 |

The DIR and PMR of one physical unit **share a part number** (see
`deploy/seed_artifacts_v2/signed_metadata_map.tsv`, column 0 = `ecu:partnum`); that's how you
pair the two halves of a single inverter. The PMR is the CAN-facing CPU1 half; the DIR is the
motor-control CPU2 half and has **no CAN** of its own.

After import, do **not** run auto-analysis yet — run the two setup scripts first (next steps),
then analyze. Running analysis before the peripherals and RAM are mapped wastes time and makes
MMIO accesses look like dangling references.

---

## Step 4 — Map peripherals + RAM (`SetupF28377D`)

Open the Script Manager, find **`SetupF28377D`** (category *TMS320C28x*), and run it. It:

- Creates and labels every F28377D peripheral frame (D_CAN, ePWM, eCAP, eQEP, ADC, SCI, SPI,
  I2C, GPIO, timers, PIE, sysctl, **IPC**, DMA) so every MMIO access resolves to a named
  register (e.g. a write to `CANA_IF1ARB` is self-evident).
- Maps the dual-core RAM regions, including the inter-core message RAM:
  `MSGRAM_CPU1_TO_CPU2 @ 0x3FC00` and `MSGRAM_CPU2_TO_CPU1 @ 0x3F800` — the link the PMR uses
  to hand commands to the DIR.
- Labels the reset vector at `0x3FFFC0`.

It **prompts for the CPU core** — this matters:

- **PMR / PCS → choose `CPU1`** (gets the CPU1-only frames: DEV_CFG, GPIO_CTRL, INPUT_XBAR; and
  the CPU1 IPC SEND/RECV register order).
- **DIR → choose `CPU2`**.

(If you ever need to re-run it or tweak the tables, the register maps are extracted from the
F2837xD TRM, SPRUHM8K.)

---

## Step 5 — Seed functions (`SeedFunctions`)

A headerless image has no entry points, so Ghidra's analyzer finds almost nothing on its own.
Run **`SeedFunctions`** (category *TMS320C28x*) to recover function entries from the bytes:

- **Call/branch targets** (high confidence): anything reached by `LCR`/`LC`/`FFC`/`LB` is, by
  definition, a real code entry.
- **Frame-save prologues** (medium confidence): C-compiled functions open with
  `MOVL *SP++,XARn` runs + `ADDB SP,#N`.
- A **data filter** (byte-entropy + opcode-plausibility) rejects prologue/call-like patterns
  that occur by chance inside string/calibration/crypto tables, so you don't litter the program
  with `halt_baddata` stubs.

It prints how many candidates it found, rejected, and seeded. Defaults are sensible; you can
tune via `-Dc28x.seed.*` properties (see the script header) — e.g.
`-Dc28x.seed.includeLoneProlog=true` to be more aggressive, or
`-Dc28x.seed.noDataFilter=true` to disable the gate.

---

## Step 6 — Auto-analyze

Now run **Analysis → Auto Analyze** (defaults are fine). With the seeds in place and
peripherals mapped, the decompiler has real functions to chew on, calls resolve into a sane
call graph, and MMIO/RAM references land on named symbols. Expect a few hundred to a few
thousand functions depending on the image.

If a region you care about still shows as undefined data, manually disassemble at a known
prologue. Search bytes for `bd b2 bd aa bd a2` (the swapped `MOVL *SP++` ×3 prologue), place
the cursor there, press **D**, then **F** to create a function. The auto-analyzer will follow
flow outward from it.

---

## Why there's no entry point

The OTA `.bhx` files contain only the **app/bootloader code bodies**. The **reset/entry stub
lives in flash sector A (`0x080000`–`0x081FFF`)**, which is factory-flashed separately and is
**not** in any shipped image. The boot ROM jumps into sector A → the stub jumps into the
bootloader → which jumps into the app — and you only have the last link.

So don't go hunting for a single "main" entry. Instead, work outward from what Step 5 already
gives you: call targets, prologue-seeded functions, and string/MMIO cross-references. For the
inverter, the CAN driver and its RX-filter mailbox tables are the most productive anchors
(CAN is PMR-side; the DIR receives commands over the `0x3FC00` IPC message RAM). Reading flash
sector A or a full live image needs hardware (JTAG via the DCSM, or chip-off) — see
`docs/private/c28x-ghidra-tesla/docs/GETTING-THE-ENTRY.md`.

---

## Quick reference

```bash
cd deploy/seed_artifacts_v2

# 0. pick the firmware: list a module's app builds with part# + config tags, then choose one
grep -P '^pm:' signed_metadata_map.tsv | grep -P '\tpm\t' | cut -f1,2,6
#   (RWD = drivetrainType=0, AWD = drivetrainType=1; col 1 = the .bhx to open)
#   pair the inverter halves by part number:
grep -P '^di:<partnum>\b' signed_metadata_map.tsv | cut -f1,2,6
#   don't know the part#? read it off a live ECU:
#   python tm3diag.py --node PCS  → board-parts

# 1. extract payload from the chosen .bhx (skip if you have a .bin)
python bhx.py info    <col1-path>.bhx        # note the segment target address = load base
python bhx.py extract <col1-path>.bhx out/

# 2. byte-swap for the C28x instruction stream
python scripts/c28x/c28x_loadimg.py --mode swap out/firmware.bin --out fw_swapped.bin
```

In Ghidra:

3. **Import** `fw_swapped.bin` as Raw Binary, language `TMS320C28x:LE:32:default`,
   base = load address (PMR `0x88000`, DIR `0x80800`, PCS per `bhx info`). **Word addresses.**
4. Run **`SetupF28377D`** — choose **CPU1** for PMR/PCS, **CPU2** for DIR.
5. Run **`SeedFunctions`**.
6. **Auto Analyze**.

Prologue byte-search for manual seeding: `bd b2 bd aa bd a2`.

## See also

- [tm3diag.md](tm3diag.md) — the interactive terminal; `board-parts` reads the part DIDs that key into the metadata map.
- [bhx.md](bhx.md) — the BHX container format and `bhx.py`.
- `deploy/seed_artifacts_v2/signed_metadata_map.tsv` — the firmware index (part# → file → config tags).
- `docs/private/c28x-ghidra-tesla/BYTESWAP-BREAKTHROUGH.md` — why the byte-swap is needed.
- `docs/private/c28x-ghidra-tesla/docs/GETTING-THE-ENTRY.md` — the missing sector-A entry stub.
- `ghidra-tms320c28x/docs/BUILDING.machine-specific.md` — building/installing the processor module.
