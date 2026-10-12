# dump_alerts.py — Dump the alert catalogue per firmware revision

Every firmware build ships `opt/odin/data/<model>/bus-alerts-map.json`: a
per-model map of which alert IDs are reachable on which vehicle bus. The IDs are
stored as **per-file salted hashes**, not in the clear. `dump_alerts.py` turns a
firmware build into a readable per-bus, per-node alert listing.

```
python dump_alerts.py                         # uses TM3_ROOT from .env
python dump_alerts.py /path/to/2026.8.3.ice.extracted
python dump_alerts.py <root> --no-scrape      # catalogue only, no image scrape
```

## Setup (.env)

Firmware root and paths come from `.env` (see `.env.example`), so a bare
`python dump_alerts.py` works once configured:

- `TM3_ROOT` — squashfs root of a firmware extraction (default firmware root).
- `TM3_ALERT_HASH_PROVIDER` — the alert-hash provider (module name or `.py`
  path; `--provider` overrides). This build ships no hash recipe; without a
  provider the tool exits. Interface: [SECURITY_PROVIDER.md](SECURITY_PROVIDER.md#alert-hash-provider).

## How it works

The plaintext alert catalogue is reusable across builds; the per-file salts are
not. The tool keeps a growing catalogue (`--catalog`) and reverses a build by
running every known alert string through the provider's `alert_hash` for that
file's salt. Anything still unresolved is recovered by scraping the firmware image for
alert-shaped strings (`--scrape`, default on; the `alertd` binary is the richest
source), and each newly-confirmed string is folded back into the catalogue.

Severity and service-UI panel info are merged in from
`service_ui/static/assets/alerts.json` when present (2025+ builds).

## Output

Dumps and the catalogue default to the gitignored `alerts/` dir (override with
`--out` / `--catalog`):

- `<rev>-<model>-alerts.json` — per bus: totals, per-node counts, and each
  alert's `id` / `name` / `node` / `severity` / `panels`, plus any unresolved
  hashes.
- `alert_catalog.txt` — the accumulated plaintext catalogue.
