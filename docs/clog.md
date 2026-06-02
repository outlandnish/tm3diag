# clog.py — gateway cluster-log parser

Parses the `CL/DATA/<n>.CLH` + `<n>.CLB` log pairs found on Tesla service /
ConfigLoader SD cards. The gateway writes these during a session as a
black-box-style record of decoded vehicle signals.

```
python clog.py info    <n>.CLH
python clog.py varints  <n>.CLH [--segment N] [--limit N]
```

- **info** — print the firmware git SHA and the segment index (time window and
  CLB byte range per segment).
- **varints** — decode the raw LEB128 varint stream of one or all segments.

## What it is (and isn't)

It is a **decoded-signal log, not a raw CAN dump.** Each record is keyed by an
internal enumerated signal id that lives in a dense, contiguous band well above
the 11-bit CAN arbitration range (`> 0x7FF`), so these are post-decode signals
(the kind the DBC names), not CAN frames. Values are delta-encoded.

Mapping signal ids to names needs the firmware's signal table, which is **not**
on the card — it comes from a full computer dump (the same definitions behind
`Model3_ETH.compact.json`). `clog.py` parses the container and exposes the raw
varints so that name-resolution layer can be added later without re-reversing
the framing.

## Format

### `<n>.CLH` — index (224 B for a 5-segment log)

| Offset | Size | Field                                        |
| ------ | ---- | -------------------------------------------- |
| `0x00` | 2    | version / flags (`00 00`)                    |
| `0x02` | 10   | magic `"Poppyseed\0"`                        |
| `0x0C` | 20   | firmware git SHA                             |
| `0x20` | 32   | reserved / header tail                       |
| `0x40` | …    | segment records, 32 bytes each               |

Each 32-byte segment record:

| Offset | Size | Field                                            |
| ------ | ---- | ------------------------------------------------ |
| `+0x00`| 4    | record magic `BA DD CA FE`                       |
| `+0x04`| 4    | reserved (`0`)                                   |
| `+0x08`| 3    | segment byte length (== `end_offset - start`)    |
| `+0x0B`| 4    | start time (unix epoch, BE)                      |
| `+0x0F`| 4    | end time (unix epoch, BE)                        |
| `+0x13`| 4    | sequence number (BE, 0-based)                    |
| `+0x17`| 4    | start offset in the CLB (BE)                     |
| `+0x1B`| 4    | end offset in the CLB (BE)                       |

Records chain contiguously: `start_offset[0] == 0`, each `end_offset` is the
next `start_offset`, and the final `end_offset` equals the CLB file size. The
time windows likewise chain (`end_time[i] == start_time[i+1]`).

### `<n>.CLB` — body

One segment per index record, in order. Each segment is an 8-byte header
(length + a hash/CRC, not yet fully characterised) followed by a LEB128 varint
stream that decodes exactly to the segment boundary.

## Library usage

```python
import clog

idx = clog.parse_clh("0.CLH")
clb = open("0.CLB", "rb").read()
for seg in idx.segments:
    values = clog.iter_segment_varints(clb, seg)   # list[int]
    print(seg.seq, seg.start_time, len(values))
```
