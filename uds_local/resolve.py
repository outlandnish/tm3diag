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


def _resolve_entry(cfg, attr: str, label: str, arg: str) -> tuple[int, str]:
    """Return (hex_id, name) for an entry in cfg.<attr> by name or 0xHEX."""
    registry: dict = getattr(cfg, attr)
    if arg.startswith(("0x", "0X")):
        hex_id = int(arg, 16)
        name = next((n for n, e in registry.items() if e.hex_id == hex_id), f"0x{hex_id:04X}")
        return hex_id, name
    if arg not in registry:
        print(f"Error: {label} {arg!r} not found for node {cfg.name}.")
        print(f"Known {attr}: {', '.join(sorted(registry))}")
        sys.exit(1)
    return registry[arg].hex_id, arg


def resolve_did(cfg, did_arg: str) -> tuple[int, str]:
    """Return (did_id, did_name) for a DID specified by name or 0xHEX."""
    return _resolve_entry(cfg, "dids", "DID", did_arg)


def resolve_routine(cfg, routine_arg: str) -> tuple[int, str]:
    """Return (routine_id, routine_name) for a routine specified by name or 0xHEX."""
    return _resolve_entry(cfg, "routines", "routine", routine_arg)


def resolve_io_control(cfg, ctrl_arg: str) -> tuple[int, str, int]:
    """Return (ctrl_id, ctrl_name, control_param) for an IO control by name or 0xHEX.

    control_param is the UDS controlParameter byte inferred from the name suffix,
    defaulting to 0x03 (shortTermAdjustment).
    """
    ctrl_id, ctrl_name = _resolve_entry(cfg, "io_controls", "io_control", ctrl_arg)
    if ctrl_arg.startswith(("0x", "0X")):
        return ctrl_id, ctrl_name, 0x03
    control_param, _ = next(
        ((v, d) for sfx, (v, d) in IOCP_SUFFIX_MAP.items() if ctrl_arg.endswith(sfx)),
        (0x03, "shortTermAdjustment"),
    )
    return ctrl_id, ctrl_arg, control_param
