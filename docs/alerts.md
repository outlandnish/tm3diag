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

The alert IDs in the map are stored as `sha256(alert_str + salt)` and the bus
buckets as `sha256(bus_name + salt)`, where the per-file salt is read from the
map itself. Because the alert strings are also present in the clear in the
firmware (the `alertd` binary and `libQtCarAlerts.so`), the tool reverses the
hashes by running known strings through the same recipe and matching — no key or
external recipe is needed.

## How it works

The key idea: **the plaintext alert catalogue is reusable across builds; the
per-file salts are not.** The tool keeps a growing plaintext catalogue
(`--catalog`); reversing a build is just running every known alert string
through `sha256(str + salt)` for that file's salt and matching. Whatever is
still unresolved is recovered by scraping the firmware image for alert-shaped
strings (`--scrape`, default on) — the `alertd` binary is the richest source —
and every newly-confirmed string is folded back into the catalogue, so coverage
improves each time a new revision is processed.

Severity and service-UI panel info are merged in from
`service_ui/static/assets/alerts.json` when present (2025+ builds).

## Output

Dumps and the catalogue default to the gitignored `alerts/` dir (override with
`--out` / `--catalog`):

- `<rev>-<model>-alerts.json` — per bus: totals, per-node counts, and each
  alert's `id` / `name` / `node` / `severity` / `panels`, plus any unresolved
  hashes.
- `alert_catalog.txt` — the accumulated plaintext catalogue.

These are generated from your firmware extraction; the default `alerts/` dir is
gitignored so they stay out of the repo.
