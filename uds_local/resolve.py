"""Shared helpers for resolving DID / routine / IO-control names or hex IDs."""

from __future__ import annotations

import sys

# Maps IO-control name suffix → (control_parameter_byte, description)
IOCP_SUFFIX_MAP: dict[str, tuple[int, str]] = {
    "_RETURN_TO_ECU": (0x00, "returnToECU"),
    "_RESET":         (0x01, "resetToDefault"),
    "_FREEZE":        (0x02, "freezeCurrentState"),
    "_ADJUST":        (0x03, "shortTermAdjustment"),
}


def resolve_did(cfg, did_arg: str) -> tuple[int, str]:
    """Return (did_id, did_name) for a DID specified by name or 0xHEX."""
    if did_arg.startswith("0x") or did_arg.startswith("0X"):
        did_id = int(did_arg, 16)
        for name, entry in cfg.dids.items():
            if entry.hex_id == did_id:
                return did_id, name
        return did_id, f"0x{did_id:04X}"
    if did_arg not in cfg.dids:
        print(f"Error: DID {did_arg!r} not found for node {cfg.name}.")
        print(f"Known DIDs: {', '.join(sorted(cfg.dids))}")
        sys.exit(1)
    entry = cfg.dids[did_arg]
    return entry.hex_id, did_arg


def resolve_routine(cfg, routine_arg: str) -> tuple[int, str]:
    """Return (routine_id, routine_name) for a routine specified by name or 0xHEX."""
    if routine_arg.startswith("0x") or routine_arg.startswith("0X"):
        routine_id = int(routine_arg, 16)
        for name, entry in cfg.routines.items():
            if entry.hex_id == routine_id:
                return routine_id, name
        return routine_id, f"0x{routine_id:04X}"
    if routine_arg not in cfg.routines:
        print(f"Error: routine {routine_arg!r} not found for node {cfg.name}.")
        print(f"Known routines: {', '.join(sorted(cfg.routines))}")
        sys.exit(1)
    entry = cfg.routines[routine_arg]
    return entry.hex_id, routine_arg


def resolve_io_control(cfg, ctrl_arg: str) -> tuple[int, str, int]:
    """Return (ctrl_id, ctrl_name, control_param) for an IO control by name or 0xHEX.

    control_param is the UDS controlParameter byte inferred from the name suffix,
    defaulting to 0x03 (shortTermAdjustment).
    """
    if ctrl_arg.startswith("0x") or ctrl_arg.startswith("0X"):
        ctrl_id = int(ctrl_arg, 16)
        ctrl_name = next(
            (n for n, e in cfg.io_controls.items() if e.hex_id == ctrl_id),
            f"0x{ctrl_id:04X}",
        )
        return ctrl_id, ctrl_name, 0x03
    if ctrl_arg not in cfg.io_controls:
        print(f"Error: io_control {ctrl_arg!r} not found for node {cfg.name}.")
        print(f"Known io_controls: {', '.join(sorted(cfg.io_controls))}")
        sys.exit(1)
    ctrl_id = cfg.io_controls[ctrl_arg].hex_id
    control_param, _ = next(
        ((v, d) for sfx, (v, d) in IOCP_SUFFIX_MAP.items() if ctrl_arg.endswith(sfx)),
        (0x03, "shortTermAdjustment"),
    )
    return ctrl_id, ctrl_arg, control_param
