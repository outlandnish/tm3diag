# ihex.py — Intel HEX / HGZ parser

Parser for Intel HEX firmware, exposing the same `.segments` interface as
[`bhx.py`](bhx.md). Handles Tesla's gzip'd `.hgz` images and the gateway's
**dual-bank** layout, which stock Intel HEX tools reject.

```
python ihex.py info    <file.hex|file.hgz>
python ihex.py decode  <file.hgz> [out.hex]
python ihex.py extract <file.hex|file.hgz> [out_dir]
```

- **info** — print each segment's address range, length, and CRC32.
- **decode** — gunzip a `.hgz` and write canonical Intel HEX (defaults to the
  source path with a `.hex` suffix). Use this to turn `GW.HGZ` into a `.hex` that
  any standard tool can read.
- **extract** — write each segment to a raw `.bin` (`segment_NN_<addr>.bin`).

## The dual-bank quirk

Tesla's gateway app (`gtw3/.../gwapp.img`, distributed as `GW.HGZ`) is **two
firmware banks in one HEX file**, each with its own type-05 Start Linear Address
record:

| Bank | Address       | Entry point  | Size      |
| ---- | ------------- | ------------ | --------- |
| A    | `0x00FB0000`  | `0x00FB0054` | 113,504 B |
| B    | `0x00FD0000`  | `0x00FD0054` | 113,504 B |

Stock `intelhex` raises `DuplicateStartAddressRecordError` on the second start
record. `ihex` strips the type-03/05 records (they carry only the CPU entry
point, unused by the segment model) and keeps all data, so both banks decode.
`decode` re-serialises to a single canonical start/EOF structure.

## Library usage

```python
import ihex

# Parse a .hgz or .hex into segments
img = ihex.parse_file("GW.hex")              # or ihex.parse_bytes(raw)
for seg in img.segments:
    print(f"{seg.start_address:#010x} {seg.length}B crc32={seg.compute_crc32():08x}")

# Normalise a .hgz to canonical Intel HEX on disk
out_path = ihex.decode_to_hex("GW.HGZ")      # -> GW.hex

# Or get the text / IntelHex object directly
ih   = ihex.load_intelhex("GW.HGZ")
text = ihex.to_hex_text(ih)
```
