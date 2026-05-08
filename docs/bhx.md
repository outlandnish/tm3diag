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

See [FIRMWARE_UPDATE.md](FIRMWARE_UPDATE.md) for a full description of the GHDR/SHDR container format.
