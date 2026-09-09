# Preparing a TMS320 firmware image for Ghidra

Tesla C28x images (inverter **DIR**/**PMR**, **PCS**, any other TMS320F28377D module) ship as
`.bhx` containers holding byte-swapped flat code. Extract, swap, then hand off to the
processor module's setup guide.

## 1. Pick the `.bhx`

`deploy/seed_artifacts_v2/signed_metadata_map.tsv` indexes every artifact:
col 0 `ecu:partnum`, col 1 path, col 3 module type, col 5 config tags.

```bash
cd deploy/seed_artifacts_v2
grep -P '^pm:' signed_metadata_map.tsv | grep -P '\tpm\t' | cut -f1,2,6
grep -P '^di:<partnum>\b' signed_metadata_map.tsv | cut -f1,2,6   # the DIR half
```

- Module type with no `bl`/`bu` suffix is the app body; `*bl`/`*bu` are the bootloaders.
- Config tags discriminate variants — `drivetrainType=0` RWD, `=1` AWD, plus
  `performancePackage`.
- **The PMR and DIR of one inverter share a part number** — that's how you pair the halves.
- Don't know the part number? Read it off a live ECU: `python tm3cli.py --node PCS`, then
  `board-parts` ([tm3cli.md](tm3cli.md)).

## 2. Extract the payload

```bash
python bhx.py info    <file>.bhx      # segment target address = your Ghidra load base
python bhx.py extract <file>.bhx out/
```

Bootloader artifacts (`pmrbl`/`pmrbu`) are flat byte-swapped C28x code too — same recipe as the
app bodies. See [bhx.md](bhx.md).

## 3. Byte-swap

```bash
python scripts/c28x/c28x_loadimg.py --mode swap out/firmware.bin --out fw_swapped.bin
```

Every 16-bit word is stored with its bytes reversed relative to the instruction stream. Applies to every Tesla F28377D image, all firmware eras.

## 4. Import + analyze

Follow
[C28X_IMAGE_SETUP.md](https://github.com/outlandnish/ghidra-tms320c28x/blob/main/docs/C28X_IMAGE_SETUP.md)
in `ghidra-tms320c28x` for the Ghidra side (import, `SetupF28377D`, `SeedFunctions`, the
materializers, cleanup). Import `fw_swapped.bin` as Raw Binary,
language `TMS320C28x:LE:32:default`.

Load bases are **word addresses**, confirmed against `bhx info`.

Pass `CPU1` for PMR/PCS, `CPU2` for DIR when running `SetupF28377D`.
