#!/usr/bin/env python3
"""Generate a Markdown report of firmware images in signed_metadata_map.tsv.

For each entry, resolves the BHX file under TM3_ARTIFACTS_DIR, parses it, and
emits per-segment start addresses, lengths, and CRC32s.
"""

from __future__ import annotations

import argparse
import gzip
from collections import defaultdict
from pathlib import Path

import config
from bhx import BhxParseError
from bhx import parse_file as parse_bhx
from ihex import parse_bytes as parse_ihex_bytes
from ihex import parse_file as parse_ihex
from uds_local.metadata import FirmwareEntry, load_metadata


def _load_image(bhx_path: Path):
    """Parse a firmware image, dispatching by extension.

    Returns (image, kind) where image exposes .segments (BhxFile or IHexFile),
    or raises BhxParseError / ValueError for unsupported formats.
    """
    suffix = bhx_path.suffix.lower()
    if suffix == ".bhx":
        return parse_bhx(bhx_path), "bhx"
    if suffix == ".hex":
        try:
            return parse_ihex(bhx_path), "ihex"
        except Exception as e:
            raise ValueError(f"ihex parse failed: {e}") from e
    if suffix == ".hgz":
        try:
            data = gzip.decompress(bhx_path.read_bytes())
        except OSError as e:
            raise ValueError(f"gzip decompress failed: {e}") from e
        try:
            return parse_ihex_bytes(data), "hgz(ihex)"
        except Exception as e:
            raise ValueError(f"ihex parse failed: {e}") from e
    raise ValueError(f"unsupported format `{suffix}`")


def _summarize_entry(
    entry: FirmwareEntry,
    artifacts_dir: Path,
) -> tuple[str, str]:
    """Return (start_addrs_cell, segment_crcs_cell) for the summary table."""
    bhx_path = artifacts_dir / entry.src_path
    if not bhx_path.exists():
        return ("_missing_", "_missing_")
    try:
        img, _ = _load_image(bhx_path)
    except (BhxParseError, ValueError) as e:
        return (f"_{e}_", f"_{e}_")
    addrs = "<br>".join(
        f"`{s.start_address:#010x}` ({s.length}B)" for s in img.segments
    )
    crcs = "<br>".join(
        f"`{(s.checksum or s.compute_crc32()):08x}`"
        for s in img.segments
    )
    return (addrs or "_no segments_", crcs or "_no segments_")


def _render_summary(
    by_component: dict[str, list[FirmwareEntry]],
    artifacts_dir: Path,
    lines: list[str],
) -> None:
    lines.append("## Summary")
    lines.append("")
    for component in sorted(by_component):
        comp_entries = by_component[component]
        lines.append(f"### `{component}`")
        lines.append("")
        lines.append(
            "| lookup_key | file | conditions "
            "| start address(es) | segment CRC32(s) |"
        )
        lines.append(
            "|------------|------|------------"
            "|-------------------|------------------|"
        )
        for entry in comp_entries:
            cond_str = ", ".join(
                f"{k}={v}" for k, v in entry.conditions.items()
            ) or "*"
            addrs, crcs = _summarize_entry(entry, artifacts_dir)
            lines.append(
                f"| `{entry.lookup_key}` | `{entry.src_path}` "
                f"| {cond_str} | {addrs} | {crcs} |"
            )
        lines.append("")


def render_markdown(
    entries: list[FirmwareEntry],
    artifacts_dir: Path,
) -> str:
    by_component: dict[str, list[FirmwareEntry]] = defaultdict(list)
    for e in entries:
        by_component[e.component].append(e)

    lines: list[str] = []
    lines.append("# Firmware Report")
    lines.append("")
    lines.append(f"- **Artifacts dir:** `{artifacts_dir}`")
    lines.append(
        f"- **Entries:** {len(entries)} "
        f"across {len(by_component)} component(s)"
    )
    lines.append("")

    _render_summary(by_component, artifacts_dir, lines)

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-dir", type=Path, default=config.ARTIFACTS_DIR,
        help="Override TM3_ARTIFACTS_DIR (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("firmware_report.md"),
        help="Output Markdown file (default: %(default)s)",
    )
    parser.add_argument(
        "--component", action="append",
        help="Restrict report to one or more component names (repeatable)",
    )
    args = parser.parse_args()

    tsv_path = args.artifacts_dir / "signed_metadata_map.tsv"
    if not tsv_path.exists():
        parser.error(f"signed_metadata_map.tsv not found at {tsv_path}")

    entries = load_metadata(tsv_path)
    if args.component:
        wanted = {c.lower() for c in args.component}
        entries = [e for e in entries if e.component.lower() in wanted]
        if not entries:
            parser.error(
                f"No entries matched components {sorted(wanted)}"
            )

    md = render_markdown(entries, args.artifacts_dir)
    args.output.write_text(md)
    print(
        f"Wrote {args.output} ({len(entries)} entries, "
        f"{args.output.stat().st_size} bytes)"
    )


if __name__ == "__main__":
    main()
