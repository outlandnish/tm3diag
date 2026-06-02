# dump_odin.py — Extract and decompile the odin binary

`opt/odin/odin` in a firmware squashfs is a PyInstaller-frozen ELF. The CAN
signal/interface definitions, the `.bin` decryption constant, and the alert-name
hash recipe live inside it — the loose JSON (compact DB, `bus-alerts-map.json`)
is *derived* and may be redacted, so the binary is the authoritative source.

```
python dump_odin.py <firmware_root>
python dump_odin.py <firmware_root> --out ~/dev/odin-dumps/2026.8.3
python dump_odin.py <firmware_root> --no-decompile     # extract only
```

## Pipeline

1. **Extract** the PyInstaller archive with `pyinstxtractor.py`.
2. **Detect** the frozen Python version (from the shipped `libpython3.X.so`).
3. **Decompile** every `.pyc` to readable `.py` with `pycdc`.

Output lands under `--out` (default `~/dev/odin-dumps/<root name>/`):
`extracted/` (raw `.pyc` + PYZ) and `src/` (decompiled `.py`).

## Interpreter resolution (important)

`pyinstxtractor` must unmarshal the PYZ under the **same** Python major.minor as
the frozen build, or it silently skips PYZ extraction (leaving only bootstrap
stubs). The script resolves the extractor in this order:

1. the host interpreter, if it already matches;
2. a `uv`-managed interpreter (`uv python install <ver>`);
3. a Docker `python:<ver>-slim` image — covers EOL/arch-missing versions such as
   3.6 on aarch64, which `uv` cannot provide.

Decompilation always runs `pycdc` on the host (a version-agnostic C++ binary),
so only the extract step needs the matching interpreter.

## Prerequisites

- `~/dev/tools/pyinstxtractor.py` (single-file script)
- `~/dev/tools/pycdc/pycdc` — build once: `cmake . && make -j$(nproc)`
- `uv` (for 3.8+ builds) and/or Docker (for older builds). Override the tools
  dir with `--tools`.

## Notes

- A handful of `.pyc` fail to decompile (pycdc limitations on some bytecode);
  this is expected and the rest are unaffected.
- Verified on `2026.8.3` (Python 3.10, via uv) and `2022.44.30.2` (Python 3.6,
  via Docker).
