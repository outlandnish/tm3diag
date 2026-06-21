#!/usr/bin/env python3
"""Firmware flashing tool for Tesla Model 3 ECUs.

Usage:
  python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
  python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
"""

from __future__ import annotations

import argparse
import gzip
import io
import sys
from pathlib import Path

import config as _cfg
from flash_scripts import (
    find_bootloader_entries,
    find_dual_cpu_pair,
    find_subcomponent_entries,
    get_script,
    is_bootloader_ecu_type,
    parent_node_for_bootloader,
    parent_node_for_subcomponent,
    run_pcs_dual_cpu,
)
from flash_scripts._display import StatusDisplay
from flash_scripts._prompt import prompt_confirm, prompt_select
from uds_local.client import UdsSession

_NODES_JSON  = _cfg.NODES_JSON
_ETH_COMPACT = _cfg.ETH_COMPACT
_ODJ_DIR     = _cfg.ODJ_DIR

_DID_BOOTLOADER_VERSION = 0xF180


def _abort(msg: str) -> None:
    print(f"\nABORT: {msg}")
    sys.exit(1)


def phase1_identity(sess: UdsSession, node_name: str, display: StatusDisplay) -> dict:
    """Read ECU identity via DID 0xF180. Returns a dict of parsed fields."""
    display.set_header("[1/4] Identity")
    display.set_detail("Reading DID 0xF180...")

    f180 = sess.read_did(_DID_BOOTLOADER_VERSION)
    if len(f180) < 7:
        _abort(
            f"DID 0xF180 response too short "
            f"({len(f180)} bytes, expected >=7)"
        )

    # Layout: [MODULES:1][COMPONENT_ID:2][PCBA_ID:1][ASSEMBLY_ID:1][USAGE_ID:2]
    component_id = (f180[1] << 8) | f180[2]
    pcba_id = f180[3]
    assembly_id = f180[4]
    usage_id = (f180[5] << 8) | f180[6]

    # version_map packed key: PPAA00UU
    packed = (pcba_id << 24) | (assembly_id << 16) | (usage_id & 0xFF)
    lookup_key = f"{node_name.lower()}:{packed}"

    identity = {
        "f180_raw": f180.hex(),
        "component_id": component_id,
        "pcba_id": pcba_id,
        "assembly_id": assembly_id,
        "usage_id": usage_id,
        "packed_key": packed,
        "lookup_key": lookup_key,
    }

    display.set_detail(
        f"Node: {node_name}  COMPONENT_ID: 0x{component_id:04X}"
        f"  PCBA_ID: {pcba_id}  ASSEMBLY_ID: {assembly_id}"
        f"  USAGE_ID: {usage_id}  key: {lookup_key}"
    )
    display.finalize()
    return identity


def _prompt_conditions(
    matches: list,
    display: StatusDisplay,
) -> list:
    """Narrow *matches* by prompting for each varying condition key in turn."""
    from uds_local.metadata import narrow_by_conditions, varying_condition_keys

    keys = varying_condition_keys(matches)
    for key in keys:
        values = sorted({e.conditions[key] for e in matches if key in e.conditions})
        if len(values) <= 1:
            continue
        choice = prompt_select(
            f"Select {key}",
            [f"{key}={v}" for v in values],
            default=0,
            display=display,
        )
        matches = narrow_by_conditions(matches, key, values[choice])
    return matches


def phase2_firmware_selection(
    artifacts_dir: Path,
    identity: dict,
    node_name: str,
    display: StatusDisplay,
) -> list:
    """Load metadata and return selected FirmwareEntry list."""
    from uds_local.metadata import find_firmware, load_metadata

    display.set_header("[2/4] Firmware Selection")
    display.set_detail("Loading metadata...")

    tsv_path = artifacts_dir / "signed_metadata_map.tsv"
    if not tsv_path.exists():
        _abort(f"signed_metadata_map.tsv not found in {artifacts_dir}")

    entries = load_metadata(tsv_path)
    matches = find_firmware(entries, node_name, identity["packed_key"])

    if not matches:
        _abort(
            f"No firmware found for {identity['lookup_key']} in {tsv_path}"
        )

    display.finalize()

    if len(matches) == 1:
        selected = matches
    else:
        matches = _prompt_conditions(matches, display)
        dest_names = {e.dest_name for e in matches}
        if len(dest_names) == len(matches):
            selected = matches
        else:
            def _entry_label(e) -> str:
                cond_str = ",".join(f"{k}={v}" for k, v in e.conditions.items()) or "*"
                return f"{e.src_path} → {e.dest_name}  crc={e.crc}  cond={cond_str}"

            labels = [_entry_label(e) for e in matches] + ["All"]
            choice = prompt_select(
                f"Select firmware ({len(matches)} options)",
                labels,
                default=0,
                display=display,
            )
            selected = matches if choice == len(matches) else [matches[choice]]

    selected = _prompt_bootloader_choice(selected, node_name, display)
    selected = _prompt_subcomponent_choice(selected, node_name, display)
    selected = _prompt_dual_cpu_choice(selected, display)

    for e in selected:
        src = artifacts_dir / e.src_path
        if not src.exists():
            _abort(f"Firmware file not found: {src}")
        print(f"  Selected: {e.src_path} → {e.dest_name}  crc={e.crc}")

    return selected


def _prompt_bootloader_choice(selected: list, node_name: str, display: StatusDisplay) -> list:
    """If `selected` includes bootloader updater/image entries, ask whether to flash them."""
    bus, bls, apps = find_bootloader_entries(selected)
    if not bus and not bls:
        return selected

    print("\n  Bootloader updates detected:")
    for e in bus:
        print(f"    [{e.component}] {e.dest_name} (updater — flashed into app slot first)")
    for e in bls:
        print(f"    [{e.component}] {e.dest_name} (bootloader image — written by updater agent)")
    print()
    print("  WARNING: Bootloader flashing can brick the ECU if interrupted.")
    print("  The app slot will hold the update agent after bu+bl — reflash")
    print("  the regular app to restore normal operation.")
    if apps:
        print(f"  (App entries that reflash after BL: {', '.join(e.dest_name for e in apps)})")
    else:
        print("  (No regular app entries selected — ECU will boot the update agent.)")
    print()

    if not prompt_confirm("Include bootloader updates?", default=False, display=display):
        return apps

    for e in bus + bls:
        parent = parent_node_for_bootloader(e.component)
        if parent and parent.lower() != node_name.lower():
            print(
                f"  Warning: --node {node_name} but bootloader entry"
                f" {e.component} expects parent {parent!r}"
            )
    return bus + bls + apps


def _prompt_subcomponent_choice(selected: list, node_name: str, display: StatusDisplay) -> list:
    """If `selected` includes subcomponent entries, ask whether to flash them."""
    subs, others = find_subcomponent_entries(selected)
    if not subs:
        return selected

    parents = sorted({parent_node_for_subcomponent(e.component) or "?" for e in subs})

    print("\n  Subcomponents detected:")
    for e in subs:
        parent = parent_node_for_subcomponent(e.component) or "?"
        print(f"    [{e.component}] {e.dest_name}  (flashed via parent: {parent})")
    print()
    print("  Subcomponents are co-versioned with the parent app — skipping")
    print("  them may leave the subcomponent on a mismatched firmware revision.")

    parents_lower = {p.lower() for p in parents}
    if node_name.lower() not in parents_lower:
        print(
            f"  Warning: --node {node_name} but subcomponents expect parent(s)"
            f" {sorted(parents)}"
        )
    print()

    if prompt_confirm("Include subcomponents?", default=True, display=display):
        return selected
    return others


def _prompt_dual_cpu_choice(selected: list, display: StatusDisplay) -> list:
    """If `selected` contains a PCS-family dual-CPU pair, ask which CPU(s) to flash."""
    pair = find_dual_cpu_pair(selected)
    if pair is None:
        return selected
    primary, secondary = pair
    print()
    labels = [
        f"Primary only   ({primary.dest_name}, ecu_type={primary.component})",
        f"Secondary only ({secondary.dest_name}, ecu_type={secondary.component})",
        "Both           — prog 1, single authenticated session",
    ]
    choice = prompt_select("Flash which CPU(s)?", labels, default=2, display=display)
    keep_primary   = choice in (0, 2)
    keep_secondary = choice in (1, 2)
    return [
        e for e in selected
        if not (e is primary and not keep_primary)
        and not (e is secondary and not keep_secondary)
    ]


def _decode_pcs_identity(data: bytes) -> dict | None:
    """Decode 32-byte Tesla C28x ECU identity header from segment data."""
    import struct
    if len(data) < 32:
        return None
    w0, w1, _, _, w4, w5 = struct.unpack_from(">IIIIII", data, 0)
    if (w4 & 0xFFFF) != 0xFFFF or (w5 & 0xFFFF) != 0xFFFF:
        return None
    return {
        "component_id": (w0 >> 16) & 0xFFFF,
        "assembly_id": (w0 >> 8) & 0xFF,
        "pcba_id": w0 & 0xFF,
        "usage_id": (w1 >> 16) & 0xFFFF,
    }


def phase3_preflight(
    artifacts_dir: Path,
    selected: list,
    identity: dict,
    force: bool,
    display: StatusDisplay,
) -> None:
    """Cross-check BHX identity header against DID reads."""
    import bhx

    display.set_header("[3/4] Pre-flight")

    for entry in selected:
        src = artifacts_dir / entry.src_path
        if src.suffix.lower() != ".bhx":
            display.set_detail(f"{entry.src_path}: not a BHX file, skipping")
            continue
        bhx_file = bhx.parse_file(src)
        found_identity = False
        for seg in bhx_file.segments:
            ident = _decode_pcs_identity(seg.data)
            if not ident:
                continue
            found_identity = True
            bhx_component_id = ident["component_id"]
            mismatches = []
            if ident["pcba_id"] != identity["pcba_id"]:
                mismatches.append(
                    f"PCBA_ID: BHX={ident['pcba_id']}"
                    f" vs ECU={identity['pcba_id']}"
                )
            if ident["assembly_id"] != identity["assembly_id"]:
                mismatches.append(
                    f"ASSEMBLY_ID: BHX={ident['assembly_id']}"
                    f" vs ECU={identity['assembly_id']}"
                )
            if ident["usage_id"] != identity["usage_id"]:
                mismatches.append(
                    f"USAGE_ID: BHX={ident['usage_id']}"
                    f" vs ECU={identity['usage_id']}"
                )

            if mismatches:
                display.finalize()
                for m in mismatches:
                    print(f"  MISMATCH: {m}")
                if not force:
                    _abort("BHX identity mismatch. Use --force to override.")
                else:
                    print("  (--force: proceeding despite mismatch)")
            else:
                display.set_detail(
                    f"{entry.src_path}: identity OK"
                    f" (component_id=0x{bhx_component_id:04X})"
                )
        if not found_identity:
            display.set_detail(
                f"{entry.src_path}: no identity header, skipping check"
            )

    display.finalize()


def _parse_firmware(src: Path):
    """Parse a firmware file into a bhx-compatible object based on extension."""
    import bhx
    import ihex
    ext = src.suffix.lower()
    if ext == ".bhx":
        return bhx.parse_file(src)
    if ext == ".hex":
        return ihex.parse_file(src)
    if ext == ".hgz":
        try:
            data = gzip.decompress(src.read_bytes())
        except OSError as e:
            _abort(f"gzip decompress failed for {src.name}: {e}")
        return ihex.parse_file(io.BytesIO(data))
    _abort(f"Unsupported firmware file type: {src.suffix!r} ({src.name})")


def phase4_dry_run(artifacts_dir: Path, selected: list, display: StatusDisplay) -> None:
    """Print what would be flashed without sending any UDS frames."""
    display.set_header("[4/4] Dry Run — Flash Plan")
    display.finalize()

    for entry in selected:
        _check_flash_supported(entry, artifacts_dir / entry.src_path)

    pair = find_dual_cpu_pair(selected)
    if pair is not None:
        primary_entry, secondary_entry = pair
        primary_bhx = _parse_firmware(artifacts_dir / primary_entry.src_path)
        secondary_bhx = _parse_firmware(artifacts_dir / secondary_entry.src_path)
        print("\n  PCS-family dual-CPU pairing detected — using prog 1 (single auth session)")
        print(f"    primary   ({primary_entry.dest_name}, ecu_type={primary_entry.component})"
              f"  → moduleToProgram(0x00)")
        for i, seg in enumerate(primary_bhx.segments):
            print(f"      [{i}] addr=0x{seg.start_address:08X}  size={seg.length} bytes")
        print(f"    secondary ({secondary_entry.dest_name}, ecu_type={secondary_entry.component})"
              f"  → moduleToProgram(0x04) [flashed first]")
        for i, seg in enumerate(secondary_bhx.segments):
            print(f"      [{i}] addr=0x{seg.start_address:08X}  size={seg.length} bytes")
        print("\n  (dry run complete — no frames sent)")
        return

    for entry in selected:
        ecu_type = entry.component.lower()
        try:
            script, module_byte = get_script(ecu_type)
        except KeyError:
            print(f"\n  {entry.dest_name}: no flash script for '{ecu_type}', skipping")
            continue

        src = artifacts_dir / entry.src_path
        bhx_file = _parse_firmware(src)

        step_names = [s.__name__ for s in script.steps]
        print(f"\n  {entry.dest_name}  ({entry.src_path})")
        print(f"    ecu_type:       {ecu_type}")
        print(f"    module_byte:    0x{module_byte:02X}")
        print(f"    security_level: {script.security_level}")
        print(f"    erase_timeout:  {script.erase_timeout}s")
        print(f"    steps:          {' → '.join(step_names)}")
        print("    segments:")
        for i, seg in enumerate(bhx_file.segments):
            print(
                f"      [{i}] addr=0x{seg.start_address:08X}"
                f"  size={seg.length} bytes"
            )

    print("\n  (dry run complete — no frames sent)")


_UNTESTED_NOTE = (
    "flashing this firmware type is currently untested and disabled — "
    "remove the guard in dfu._check_flash_supported() to override"
)


def _check_flash_supported(entry, src: Path) -> None:
    """Abort if the entry uses a flash path that hasn't been validated yet."""
    if src.suffix.lower() == ".hgz":
        _abort(
            f"{entry.dest_name} ({src.name}): "
            f".hgz firmware — {_UNTESTED_NOTE}"
        )
    if is_bootloader_ecu_type(entry.component):
        _abort(
            f"{entry.dest_name} (component={entry.component}): "
            f"bootloader (bu/bl) flash — {_UNTESTED_NOTE}"
        )


def phase4_flash(
    sess: UdsSession,
    artifacts_dir: Path,
    selected: list,
    display: StatusDisplay,
    channel: str | None = None,
    interface: str | None = None,
) -> None:
    """Execute the flash sequence for the selected firmware entries."""
    for entry in selected:
        _check_flash_supported(entry, artifacts_dir / entry.src_path)

    pair = find_dual_cpu_pair(selected)
    if pair is not None:
        primary_entry, secondary_entry = pair
        display.set_header(
            f"[4/4] Flash (dual-CPU) — "
            f"{primary_entry.dest_name} + {secondary_entry.dest_name}"
        )
        primary_bhx = _parse_firmware(artifacts_dir / primary_entry.src_path)
        secondary_bhx = _parse_firmware(artifacts_dir / secondary_entry.src_path)
        run_pcs_dual_cpu(
            sess,
            primary_bhx, primary_entry,
            secondary_bhx, secondary_entry,
        )
        display.set_detail("Flash complete")
        display.finalize()
        return

    for fw_index, entry in enumerate(selected):
        display.set_header(f"[4/4] Flash — {entry.dest_name}")

        ecu_type = entry.component.lower()
        try:
            script, module_byte = get_script(ecu_type)
        except KeyError:
            display.finalize()
            print(f"  Skipping {entry.dest_name}: no flash script for '{ecu_type}'")
            continue

        src = artifacts_dir / entry.src_path
        bhx_file = _parse_firmware(src)

        script.module_byte = module_byte
        script.run(sess, bhx_file, entry, channel=channel, interface=interface, display=display)

        if fw_index == len(selected) - 1:
            display.set_detail("Flash complete")
            display.finalize()


def run_flash(
    sess: UdsSession,
    artifacts_dir: Path,
    node_name: str,
    display: StatusDisplay,
    force: bool = False,
    channel: str | None = None,
    interface: str | None = None,
) -> None:
    """Run all four flash phases against an already-open UdsSession."""
    identity = phase1_identity(sess, node_name, display)
    selected = phase2_firmware_selection(artifacts_dir, identity, node_name, display)
    phase3_preflight(artifacts_dir, selected, identity, force, display)
    print()
    if not prompt_confirm("Proceed with flashing?", default=False, display=display):
        print("Aborted.")
        return
    phase4_flash(sess, artifacts_dir, selected, display, channel=channel, interface=interface)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tesla Model 3 ECU firmware flashing tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node", "-n", required=True,
                        help="ECU node name (e.g. PCS)")
    parser.add_argument("--channel", "-c",
                        help="CAN interface channel")
    parser.add_argument("--interface", "-i",
                        help="python-can interface type")
    parser.add_argument(
        "--artifacts", "-a",
        help="Path to seed_artifacts_v2 directory",
    )
    _cfg.apply_defaults(parser)
    parser.add_argument("--force", action="store_true",
                        help="Skip pre-flight identity mismatch abort")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print flash plan without sending any UDS frames")
    parser.add_argument(
        "--packed-key", type=lambda s: int(s, 0),
        help=(
            "Offline identity override (decimal or 0x hex). "
            "Skips live ECU reads — implies --dry-run. "
            "Use the packed_key printed by a previous run "
            "(PPAA00UU from DID 0xF180)."
        ),
    )
    args = parser.parse_args()

    if args.packed_key is not None and not args.dry_run:
        parser.error("--packed-key requires --dry-run")

    if not args.artifacts:
        parser.error("--artifacts is required (or set TM3_ARTIFACTS_DIR in .env)")

    artifacts_dir = Path(args.artifacts).expanduser().resolve()
    if not artifacts_dir.is_dir():
        print(f"Error: artifacts directory not found: {artifacts_dir}")
        return 1

    from uds_local.node_config import load_node_config

    display = StatusDisplay()

    try:
        if args.packed_key is not None:
            identity = {
                "f180_raw": "(offline)",
                "component_id": 0,
                "pcba_id": (args.packed_key >> 24) & 0xFF,
                "assembly_id": (args.packed_key >> 16) & 0xFF,
                "usage_id": args.packed_key & 0xFF,
                "packed_key": args.packed_key,
                "lookup_key": f"{args.node.lower()}:{args.packed_key}",
            }
            display.set_header("[1/4] Identity (offline)")
            display.set_detail(
                f"Node: {args.node}  packed_key: {args.packed_key}"
                f"  lookup_key: {identity['lookup_key']}"
            )
            display.finalize()

            selected = phase2_firmware_selection(
                artifacts_dir, identity, args.node, display
            )
            phase3_preflight(artifacts_dir, selected, identity, args.force, display)
            phase4_dry_run(artifacts_dir, selected, display)
            return 0

        cfg = load_node_config(args.node, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)

        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            sess.diagnostic_session(0x01)

            if args.dry_run:
                identity = phase1_identity(sess, args.node, display)
                selected = phase2_firmware_selection(artifacts_dir, identity, args.node, display)
                phase3_preflight(artifacts_dir, selected, identity, args.force, display)
                phase4_dry_run(artifacts_dir, selected, display)
                return 0

            run_flash(
                sess, artifacts_dir, args.node, display,
                force=args.force, channel=args.channel, interface=args.interface,
            )

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
