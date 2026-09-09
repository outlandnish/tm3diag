# bhx.py — BHX firmware image library

Parser and builder for the Tesla BHX firmware container format. Can be used as a library or run directly.

```
python bhx.py info    firmware.bhx
python bhx.py extract firmware.bhx [output_dir]
python bhx.py create  out.bhx 0x88000 segment.bin
```

## Library usage

```python
import bhx

# Parse
bhx_file = bhx.parse_file("firmware.bhx")
for seg in bhx_file.segments:
    print(f"addr=0x{seg.start_address:08X} len={seg.length}")

# Build
bhx_file = bhx.from_binary_segments([(0x88000, data)])
bhx.build_file(bhx_file, "out.bhx")
```

## File format

BHX is a big-endian container. Headers are parsed locally — **never sent over UDS**.
Only the raw SHDR payload bytes are transmitted.

### GHDR — Global Header

| Offset | Size | Field              | Notes                                                 |
| ------ | ---- | ------------------ | ----------------------------------------------------- |
| `0x00` | 4    | Magic `"GHDR"`     |                                                       |
| `0x04` | 4    | Version            | `1` or `2`, big-endian                                |
| `0x08` | 4    | Total payload size | Sum of all SHDR payload sizes, not headers            |
| `0x0C` | 4    | Total size         | **v2 only** — alternate total (redundant with `0x08`) |

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
