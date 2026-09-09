# Firmware Extraction Guide

This guide walks through getting from a raw Tesla firmware download to an extracted directory tree that tools like `tm3cli.py`, `dfu.py`, and `tm3uds.py` can read.

## Compatibility

| Image type | Status |
|---|---|
| `.model3` | Supported |
| `.ice` | Supported |
| `.mcu1` | Untested |
| `.mcu2` | Untested |

The `.model3` and `.ice` builds have been verified end-to-end. The `.mcu1` / `.mcu2` formats use the same squashfs container and the script will likely work, but the internal directory layout and ODJ paths may differ — `TM3_ROOT` dependent tools may not find what they need.

## Prerequisites

Install `squashfs-tools` (provides `unsquashfs`):

```bash
# Debian / Ubuntu / WSL
sudo apt install squashfs-tools

# Arch
sudo pacman -S squashfs-tools

# macOS (Homebrew)
brew install squashfs
```

Verify it is available:

```bash
unsquashfs -v
```

## What a Tesla firmware file is

A downloaded Tesla firmware blob is a squashfs filesystem. Inside it, sub-systems ship as additional squashfs images whose filenames encode the mount path: dots are directory separators and `%2E` is a literal dot. For example:

```
deploy.seed_artifacts_v2.dirsquashed   →   deploy/seed_artifacts_v2/
opt.odin.data.model3.dirsquashed       →   opt/odin/data/model3/
```

The on-car loader mounts each of these at boot. `unsquash_firmware.py` reproduces that as a one-shot extraction to disk.

## Step 1 — Extract the firmware

```bash
python unsquash_firmware.py ~/Downloads/2024.44.x.ice \
    --name 2024.44.x.ice.extracted \
    --out ~/dev/tesla-fw
```

This will:

1. Run `unsquashfs` on the top-level blob → `~/dev/tesla-fw/2024.44.x.ice.extracted/`
2. Find every `*.dirsquashed` inside and extract each to its decoded path, repeating until none remain
3. Delete the original downloaded blob to save space (these are 1–2 GB each)

To keep the original download:

```bash
python unsquash_firmware.py ~/Downloads/2024.44.x.ice \
    --name 2024.44.x.ice.extracted \
    --keep-download
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--name NAME` | file stem | Name for the extraction directory |
| `--out DIR` | `~/dev/tesla-fw` | Parent directory for the extraction |
| `--keep-squashed` | off | Keep `.dirsquashed` files after extracting |
| `--keep-download` | off | Keep the original downloaded blob |

## Step 2 — Point tm3diag at the extraction

Open (or create) `.env` in the project root and set `TM3_ROOT` to the `squashfs-root` directory produced in step 1:

```bash
TM3_ROOT=~/dev/tesla-fw/2024.44.x.ice.extracted
```

Tools that need the firmware dump (`tm3cli.py`, `dfu.py`, `tm3uds.py`) will automatically find `nodes.json`, the ETH compact DB, ODJ files, and seed artifacts from this path.

## Step 3 — Decryption key (if needed)

On some builds the compact DB and ODJ files are stored as encrypted `.bin` files alongside the plain `.json` versions. If you see only `.json.bin` files under `opt/odin/data/`, you need the decryption key.

The key is a base64-encoded constant stored inside the `odin` binary shipped with the firmware. To extract it:

1. Pull `opt/odin/odin` from the extraction
2. Decompile it with `pyinstxtractor` + `pycdc`
3. Find the constant `C` in `odin/platforms/binary_metadata_utils.py`

Then set it in `.env`:

```bash
TM3_BIN_KEY=<base64-encoded value>
```

`dump_odin.py` automates the pyinstxtractor + pycdc step — see [dump_odin.md](dump_odin.md).

## Verifying the extraction

After extraction, the root should contain at minimum:

```
opt/odin/data/<product>/nodes.json
opt/odin/data/<product>/dej/Model3_ETH.compact.json   (or .compact.json.bin)
opt/odin/data/<product>/odj/
deploy/seed_artifacts_v2/
```

where `<product>` is typically `model3` for `.model3` builds or the ICE variant name for `.ice` builds.

Quick check:

```bash
ls ~/dev/tesla-fw/2024.44.x.ice.extracted/opt/odin/data/
```

## Troubleshooting

**`unsquashfs` reports a fatal error or `xattr_ids is 0`**
The file is likely still downloading or was truncated. Verify the download is complete:
```bash
unsquashfs -s ~/Downloads/2024.44.x.ice
# look for a valid superblock and non-zero "Number of inodes"
```

**`command not found: unsquashfs`**
Install `squashfs-tools` (see Prerequisites above).

**`TM3_ROOT`-dependent tools report "no firmware dump"**
Check that `TM3_ROOT` in `.env` points to the directory that *contains* `opt/`, not to `opt/odin/data/` itself.
