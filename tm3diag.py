#!/usr/bin/env python3
"""Interactive diagnostic terminal for Tesla Model 3 ECUs.

Usage:
  python tm3diag.py --channel vcan0
  python tm3diag.py --node PCS --channel vcan0
  python tm3diag.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
"""

from __future__ import annotations

import json
import logging
import readline
import warnings
from pathlib import Path
from typing import Any

import config as _cfg
from decode_bin import load_json as _load_json
from uds_local.client import (
    _SESSION_DEFAULT,
    _SESSION_EXTENDED,
    _SESSION_PROGRAMMING,
    _SESSION_SAFETY,
)
from uds_local.resolve import IOCP_SUFFIX_MAP as _IOCP_SUFFIX_MAP

_log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=RuntimeWarning,
                        module=r"uds\.packet\.abstract_packet")
warnings.filterwarnings(
    "ignore", message="A CAN packet that does not start UDS message transmission")
warnings.filterwarnings(
    "ignore", module=r"uds\.can\.transport_interface\.common")


_NODES_JSON = _cfg.NODES_JSON
_ETH_COMPACT = _cfg.ETH_COMPACT
_ODJ_DIR = _cfg.ODJ_DIR

_NAMED_ROUTINES: dict[str, tuple[int, str, bool]] = {
    "erase":              (0xFF00, "initializeEraseModule — EraseMemory", True),
    "verify-crc":         (0x0201, "checkModuleProgrammedCorrectly — CRC verify", False),
    "check-component":    (0x0202, "checkCorrectComponentAndRev", False),
    "ota-wait":           (0x0540, "vcWaitForOTAMode / otaStateRoutineControl", False),
    "ibst-power":         (0x0543, "ibstPowerControl", True),
    "bms-contactor-close":     (0x0204, "bmsContactorControl — close contactor", False),
    "bms-contactor-open":      (0x0304, "bmsContactorControl — open contactor", False),
    "disable-intrusion-sensor": (0x0601, "disableIntrusionSensor", False),
}

# DIDs read by opcode 14 (boardPartSerialNumberGet) → modinfo fields
_BOARD_PART_DIDS: list[tuple[int, str]] = [
    (0xF012, "BoardPartNumber"),
    (0xF013, "BoardSerialNumber"),
    (0xF014, "BoardHardwareRevision"),
    (0xF015, "BoardSoftwareRevision"),
    (0xF030, "BoardPartNumber2"),
    (0xF031, "BoardSerialNumber2"),
]


# ---------------------------------------------------------------------------
# ODJ field decode
# ---------------------------------------------------------------------------

def _decode_fields(data: bytes, fields: dict[str, Any]) -> list[tuple[str, str]]:
    """Decode response bytes into (field_name, value_str) pairs using ODJ field specs."""
    results = []
    for name, spec in sorted(fields.items(), key=lambda x: x[1].get("byte_position", 0)):
        dtype = spec.get("data_type", "bytes")
        byte_pos = spec.get("byte_position", 0)
        bit_len = spec.get("bit_length", 8)
        byte_len = (bit_len + 7) // 8
        chunk = data[byte_pos:byte_pos + byte_len]
        if not chunk:
            continue
        if dtype == "ascii":
            val = chunk.decode("ascii", errors="replace").rstrip("\x00")
            results.append((name, repr(val)))
        elif dtype in ("uint", "int"):
            n = int.from_bytes(chunk, "big")
            if dtype == "int" and chunk[0] & 0x80:
                n -= 1 << (byte_len * 8)
            results.append(
                (name, f"{n}  (0x{int.from_bytes(chunk, 'big'):0{byte_len*2}X})"))
        else:
            results.append((name, chunk.hex()))
    return results


def _load_odj_fields(odj_path: Path) -> dict[str, Any]:
    try:
        return _load_json(odj_path).get("data", {})
    except Exception:
        _log.warning("Failed to load ODJ fields from %s", odj_path)
        return {}


def _load_odj_routines(odj_path: Path) -> dict[str, Any]:
    try:
        return _load_json(odj_path).get("routines", {})
    except Exception:
        _log.warning("Failed to load ODJ routines from %s", odj_path)
        return {}


def _load_odj_io_controls(odj_path: Path) -> dict[str, Any]:
    try:
        return _load_json(odj_path).get("io_controls", {})
    except Exception:
        _log.warning("Failed to load ODJ io_controls from %s", odj_path)
        return {}


# ---------------------------------------------------------------------------
# Tab completion
# ---------------------------------------------------------------------------

class _Completer:
    def __init__(self, options: list[str]):
        self._options = options
        self._matches: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            self._matches = [
                o for o in self._options if o.lower().startswith(text.lower())]
        return self._matches[state] if state < len(self._matches) else None


def _setup_completion(options: list[str]) -> None:
    completer = _Completer(options)
    readline.set_completer(completer.complete)
    readline.parse_and_bind("tab: complete")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _hdr(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def _print_did_response(name: str, did_id: int, data: bytes, fields: dict[str, Any]) -> None:
    print(f"\n  {name} (0x{did_id:04X})  [{len(data)} bytes]")
    decoded = _decode_fields(data, fields)
    if decoded:
        for fname, val in decoded:
            print(f"    {fname:<36} {val}")
    else:
        print(f"    {data.hex()}")


# ---------------------------------------------------------------------------
# Node selection
# ---------------------------------------------------------------------------

def _pre_connection_menu(nodes: dict, channel: str, interface: str) -> str | None:
    """Top-level menu shown before connecting. Returns a node name or None to quit."""
    from uds_local.scanner import print_scan_table, scan_network

    node_names = sorted(nodes.keys())
    _setup_completion(["scan", "connect", "quit"] + node_names)

    while True:
        _hdr("tm3diag  —  not connected")
        print("  scan            Probe all known nodes on the bus")
        print("  connect <node>  Connect to a node by name")
        print("  quit            Exit")

        try:
            raw = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        if cmd in ("q", "quit", "exit"):
            return None

        elif cmd == "scan":
            print(f"\n  Scanning on {channel}...")
            try:
                results = scan_network(
                    channel, _NODES_JSON, _ETH_COMPACT, interface=interface)
                print()
                print_scan_table(results)
                print()
            except Exception as e:
                print(f"  Scan error: {e}\n")

        elif cmd == "connect":
            if len(parts) < 2:
                print("  Usage: connect <node>")
                continue
            name = parts[1].upper()
            if name not in nodes:
                print(
                    f"  Unknown node {name!r}. Known: {', '.join(node_names)}")
                continue
            return name

        else:
            # bare node name shorthand
            name = raw.upper()
            if name in nodes:
                return name
            print(f"  Unknown command or node: {raw!r}")


# ---------------------------------------------------------------------------
# Identity banner (0xF180)
# ---------------------------------------------------------------------------

def _show_identity(sess, cfg) -> None:
    from uds_local.client import UdsError
    try:
        data = sess.read_did(0xF180)
    except UdsError as e:
        print(f"  Could not read 0xF180: {e}")
        return

    print(f"\n  Connected to {cfg.name}")

    # Load field spec from the node's ODJ
    f180_fields: dict[str, Any] = {}
    for odj_name in json.loads(_NODES_JSON.read_text()).get(cfg.name, {}).get("odj_sources", []):
        odj_data = _load_odj_fields(_ODJ_DIR / odj_name)
        for did_spec in odj_data.values():
            if int(did_spec.get("hex_id", "0"), 16) == 0xF180:
                f180_fields = did_spec.get("read", {}).get("output", {})
                break
        if f180_fields:
            break

    decoded = _decode_fields(data, f180_fields)
    if decoded:
        for fname, val in decoded:
            print(f"    {fname:<36} {val}")


# ---------------------------------------------------------------------------
# DID menu
# ---------------------------------------------------------------------------

def _did_menu(sess, cfg, odj_fields: dict[str, Any]) -> None:
    from uds_local.client import UdsError

    # Build readable DID list
    readable = {
        name: spec for name, spec in odj_fields.items()
        if "read" in spec
    }
    if not readable:
        print("  No readable DIDs found for this node.")
        return

    names = sorted(readable.keys())
    _setup_completion(names + ["back", "list"])

    _hdr(f"DID read — {cfg.name}  ({len(readable)} readable DIDs)")
    print("  Type a DID name (tab to complete), hex ID (0xNNNN), 'list', or 'back'")

    while True:
        try:
            raw = input("\n  DID> ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not raw:
            continue
        if raw.lower() in ("back", "q", "quit"):
            return
        if raw.lower() == "list":
            print()
            for n in names:
                spec = readable[n]
                did_id = int(spec.get("hex_id", "0"), 16)
                size = spec.get("read", {}).get("output_size", "?")
                sl = spec.get("read", {}).get("security_level", 0)
                sl_str = f"  sl={sl}" if sl else ""
                print(f"    0x{did_id:04X}  {n:<40} {size}B{sl_str}")
            continue

        # Resolve name or hex
        if raw in readable:
            name = raw
            spec = readable[name]
        elif raw.lower().startswith("0x"):
            try:
                did_id = int(raw, 16)
            except ValueError:
                print(f"  Invalid hex: {raw!r}")
                continue
            match = next((n for n, s in readable.items() if int(
                s.get("hex_id", "0"), 16) == did_id), None)
            if match:
                name, spec = match, readable[match]
            else:
                print(f"  DID 0x{did_id:04X} not in ODJ — attempting raw read")
                name, spec = raw, {}
        else:
            print(f"  Unknown DID: {raw!r}  (try 'list' or tab complete)")
            continue

        did_id = int(spec.get("hex_id", "0"), 16) if spec else int(raw, 16)
        sl = spec.get("read", {}).get("security_level", 0) if spec else 0

        if sl:
            print(
                f"  DID requires security level {sl} — running security access...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        try:
            data = sess.read_did(did_id)
            fields = spec.get("read", {}).get("output", {}) if spec else {}
            _print_did_response(name, did_id, data, fields)
        except UdsError as e:
            print(f"  Error: {e}")


# ---------------------------------------------------------------------------
# Routine menu
# ---------------------------------------------------------------------------

def _prompt_routine_inputs(fields: dict[str, Any]) -> bytes | None:
    """Prompt for each input field and pack into bytes. Returns None on error."""
    if not fields:
        return b""
    # Determine total byte length from the highest byte_position + ceil(bit_length/8)
    total = max(
        f["byte_position"] + (f["bit_length"] + 7) // 8
        for f in fields.values()
    )
    buf = bytearray(total)
    for fname, fspec in fields.items():
        bit_len = fspec["bit_length"]
        byte_pos = fspec["byte_position"]
        bit_pos = fspec["bit_position"]
        dtype = fspec.get("data_type", "uint")
        enum_map = fspec.get("map", {}).get("enum", {})
        if enum_map:
            opts = ", ".join(f"{k}={v}" for k, v in enum_map.items())
            raw = input(f"    {fname} ({opts}): ").strip()
            rev = {str(v): k for k, v in enum_map.items()}
            rev.update({k.upper(): v for k, v in enum_map.items()})
            if raw.upper() in {k.upper(): v for k, v in enum_map.items()}:
                val = {k.upper(): v for k, v in enum_map.items()}[raw.upper()]
            else:
                try:
                    val = int(raw, 0)
                except ValueError:
                    print(f"  Invalid value: {raw!r}")
                    return None
        else:
            prompt = f"    {fname} ({'signed' if dtype == 'int' else 'uint'}, {bit_len}b): "
            raw = input(prompt).strip()
            try:
                val = int(raw, 0)
            except ValueError:
                print(f"  Invalid value: {raw!r}")
                return None
        # Pack bits into buf
        mask = (1 << bit_len) - 1
        val &= mask
        for i in range(bit_len):
            bit = (val >> i) & 1
            b = byte_pos + (bit_pos + i) // 8
            bp = (bit_pos + i) % 8
            if bit:
                buf[b] |= (1 << bp)
            else:
                buf[b] &= ~(1 << bp)
    return bytes(buf)


def _routine_menu(sess, cfg, odj_routines: dict[str, Any]) -> None:
    from uds_local.client import UdsError

    named = list(_NAMED_ROUTINES.keys())
    odj_names = sorted(odj_routines.keys())
    all_names = named + odj_names
    _setup_completion(all_names + ["back", "list"])

    _hdr(f"Routine control — {cfg.name}")
    print("  Type a routine name (tab to complete), hex ID (0xNNNN), 'list', or 'back'")

    while True:
        try:
            raw = input("\n  Routine> ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not raw:
            continue
        if raw.lower() in ("back", "q", "quit"):
            return
        if raw.lower() == "list":
            print()
            if named:
                print("  — built-in —")
                for name, (rid, desc, needs_sa) in _NAMED_ROUTINES.items():
                    sa_str = "  [sa]" if needs_sa else ""
                    print(f"    0x{rid:04X}  {name:<40} {desc}{sa_str}")
            if odj_names:
                print("  — node ODJ —")
                for name in odj_names:
                    spec = odj_routines[name]
                    rid = int(spec.get("hex_id", "0"), 16)
                    sl = spec.get("start", {}).get("security_level", 0)
                    sa_str = f"  [sl={sl}]" if sl else ""
                    actions = [a for a in (
                        "start", "stop", "results") if spec.get(a)]
                    print(
                        f"    0x{rid:04X}  {name:<40} {', '.join(actions)}{sa_str}")
            continue

        # Resolve to (routine_id, needs_sa, sl, odj_spec | None)
        odj_spec = None
        needs_sa = False
        sl = 1
        if raw in odj_routines:
            odj_spec = odj_routines[raw]
            routine_id = int(odj_spec.get("hex_id", "0"), 16)
            sl = odj_spec.get("start", {}).get("security_level", 0)
            needs_sa = bool(sl)
            print(f"  → 0x{routine_id:04X}  {raw}")
        elif raw.lower() in _NAMED_ROUTINES:
            routine_id, desc, needs_sa = _NAMED_ROUTINES[raw.lower()]
            print(f"  → 0x{routine_id:04X}  {desc}")
        elif raw.lower().startswith("0x"):
            # Check odj by hex id
            match = next(
                (n for n, s in odj_routines.items()
                 if int(s.get("hex_id", "0"), 16) == int(raw, 16)),
                None,
            )
            if match:
                odj_spec = odj_routines[match]
                routine_id = int(odj_spec.get("hex_id", "0"), 16)
                sl = odj_spec.get("start", {}).get("security_level", 0)
                needs_sa = bool(sl)
                print(f"  → 0x{routine_id:04X}  {match}")
            else:
                try:
                    routine_id = int(raw, 16)
                except ValueError:
                    print(f"  Invalid hex: {raw!r}")
                    continue
        else:
            print(f"  Unknown routine: {raw!r}  (try 'list' or tab complete)")
            continue

        if needs_sa:
            print(
                f"  Routine requires security level {sl} — authenticating...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        if odj_spec:
            # Prompt for action
            available = [a for a in (
                "start", "stop", "results") if odj_spec.get(a)]
            if len(available) == 1:
                action = available[0]
            else:
                action_raw = input(
                    f"  Action ({'/'.join(available)}): ").strip().lower()
                if action_raw not in available:
                    print(f"  Unknown action: {action_raw!r}")
                    continue
                action = action_raw
            sub = odj_spec[action]
            input_fields = sub.get("input", {})
            if input_fields:
                print(f"  Inputs for {action}:")
                arg = _prompt_routine_inputs(input_fields)
                if arg is None:
                    continue
            else:
                arg = b""
            subtype = {"start": 0x01, "stop": 0x02, "results": 0x03}[action]
        else:
            arg_raw = input("  Arg bytes (hex, empty for none): ").strip()
            try:
                arg = bytes.fromhex(arg_raw.replace(
                    " ", "")) if arg_raw else b""
            except ValueError:
                print(f"  Invalid hex: {arg_raw!r}")
                continue
            subtype = 0x01

        try:
            result = sess.routine_control(routine_id, arg, subtype=subtype)
            print(f"  Result: {result.hex() if result else '(empty)'}")
        except UdsError as e:
            print(f"  Error: {e}")


def _io_control_menu(sess, cfg, odj_io_controls: dict[str, Any]) -> None:
    from uds_local.client import UdsError

    names = sorted(odj_io_controls.keys())
    _setup_completion(names + ["back", "list"])

    _hdr(f"IO control — {cfg.name}")
    print("  Type a control name (tab to complete), hex ID (0xNNNN), 'list', or 'back'")

    while True:
        try:
            raw = input("\n  IO> ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not raw:
            continue
        if raw.lower() in ("back", "q", "quit"):
            return
        if raw.lower() == "list":
            print()
            for name in names:
                spec = odj_io_controls[name]
                ctrl_id = int(spec.get("hex_id", "0"), 16)
                sl = spec.get("security_level", 0)
                sa_str = f"  [sl={sl}]" if sl else ""
                cp, cp_desc = next(
                    ((v, d) for sfx, (v, d) in _IOCP_SUFFIX_MAP.items() if name.endswith(sfx)),
                    (0x03, "shortTermAdjustment"),
                )
                print(f"    0x{ctrl_id:04X}  {name:<52} {cp_desc}{sa_str}")
            continue

        # Resolve to (ctrl_id, sl, spec)
        spec = None
        if raw in odj_io_controls:
            spec = odj_io_controls[raw]
            ctrl_id = int(spec.get("hex_id", "0"), 16)
            sl = spec.get("security_level", 0)
            print(f"  → 0x{ctrl_id:04X}  {raw}")
        elif raw.lower().startswith("0x"):
            match = next(
                (n for n, s in odj_io_controls.items()
                 if int(s.get("hex_id", "0"), 16) == int(raw, 16)),
                None,
            )
            if match:
                spec = odj_io_controls[match]
                ctrl_id = int(spec.get("hex_id", "0"), 16)
                sl = spec.get("security_level", 0)
                print(f"  → 0x{ctrl_id:04X}  {match}")
            else:
                try:
                    ctrl_id = int(raw, 16)
                    sl = 0
                except ValueError:
                    print(f"  Invalid hex: {raw!r}")
                    continue
        else:
            print(f"  Unknown control: {raw!r}  (try 'list' or tab complete)")
            continue

        if sl:
            print(f"  Control requires security level {sl} — authenticating...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        # Determine control parameter from name suffix
        ctrl_name = raw if spec else f"0x{ctrl_id:04X}"
        control_param, _ = next(
            ((v, d) for sfx, (v, d) in _IOCP_SUFFIX_MAP.items() if ctrl_name.endswith(sfx)),
            (0x03, "shortTermAdjustment"),
        )

        # Prompt for input data if spec has input fields
        if spec and spec.get("input"):
            input_fields = spec["input"]
            data = _prompt_routine_inputs(input_fields)
            if data is None:
                continue
        else:
            arg_raw = input("  Data bytes (hex, empty for none): ").strip()
            try:
                data = bytes.fromhex(arg_raw.replace(" ", "")) if arg_raw else b""
            except ValueError:
                print(f"  Invalid hex: {arg_raw!r}")
                continue

        try:
            result = sess.io_control(ctrl_id, control_param, data)
            if result and spec and spec.get("output"):
                decoded = _decode_fields(result, spec["output"])
                for fname, fval in decoded:
                    print(f"  {fname}: {fval}")
            else:
                print(f"  Result: {result.hex() if result else '(empty)'}")
        except UdsError as e:
            print(f"  Error: {e}")


# ---------------------------------------------------------------------------
# DFU (firmware update via dfu.py phases)
# ---------------------------------------------------------------------------

def _dfu_menu(sess, cfg, artifacts_dir: Path | None, force: bool | None = None) -> None:
    from dfu import run_flash
    from flash_scripts._display import StatusDisplay
    from uds_local.client import UdsError

    _hdr(f"Firmware update (DFU) — {cfg.name}")

    if artifacts_dir is None:
        artifacts_dir = _cfg.ARTIFACTS_DIR

    if not artifacts_dir.is_dir():
        print(f"  Artifacts directory not found: {artifacts_dir}")
        print("  Set TM3_ARTIFACTS_DIR in .env or pass --artifacts")
        return

    if force is None:
        force = _cfg.DFU_FORCE or False

    try:
        run_flash(sess, artifacts_dir, cfg.name, StatusDisplay(), force=force)
    except SystemExit:
        print("  DFU aborted.")
    except UdsError as e:
        print(f"  UDS error: {e}")
    except Exception as e:
        print(f"  Error: {e}")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def _main_menu(sess, cfg, odj_fields: dict[str, Any], odj_routines: dict[str, Any], odj_io_controls: dict[str, Any], artifacts_dir: Path | None, force: bool = False) -> None:
    _setup_completion([
        "dids", "routine", "io-control", "board-parts", "clear-dtc",
        "dfu", "session", "reset", "quit",
    ])

    while True:
        _hdr(f"{cfg.name}  —  Main menu")
        print("  dids        Read DIDs interactively")
        print("  routine     Run a routine control")
        print("  io-control  InputOutputControlByIdentifier (0x2F)")
        print("  board-parts Read board part/serial DIDs (0xF012–0xF015)")
        print("  clear-dtc   ClearDiagnosticInformation (0xFFFFFF)")
        print("  dfu         Firmware update")
        print("  session     Switch diagnostic session")
        print("  reset       ECU hard reset")
        print("  quit        Disconnect and exit")

        try:
            cmd = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "dids":
            _did_menu(sess, cfg, odj_fields)
        elif cmd == "routine":
            _routine_menu(sess, cfg, odj_routines)
        elif cmd == "io-control":
            _io_control_menu(sess, cfg, odj_io_controls)
        elif cmd == "board-parts":
            _board_parts_cmd(sess)
        elif cmd == "clear-dtc":
            _clear_dtc_cmd(sess)
        elif cmd == "dfu":
            _dfu_menu(sess, cfg, artifacts_dir, force=force)
        elif cmd == "session":
            _session_cmd(sess)
        elif cmd == "reset":
            _reset_cmd(sess)
        elif cmd:
            print(f"  Unknown command: {cmd!r}")


def _session_cmd(sess) -> None:
    from uds_local.client import UdsError
    mode_map = {
        "default": _SESSION_DEFAULT, "programming": _SESSION_PROGRAMMING,
        "extended": _SESSION_EXTENDED, "safety": _SESSION_SAFETY,
    }
    raw = input(
        "  Session (default/programming/extended/safety or 0xNN): ").strip().lower()
    mode = mode_map.get(raw)
    if mode is None:
        try:
            mode = int(raw, 0)
        except ValueError:
            print(f"  Unknown session: {raw!r}")
            return
    try:
        sess.diagnostic_session(mode)
        print(f"  Entered session 0x{mode:02X}")
    except UdsError as e:
        print(f"  Error: {e}")


def _board_parts_cmd(sess) -> None:
    """Read board part/serial DIDs (opcode 14 — boardPartSerialNumberGet)."""
    from uds_local.client import UdsError
    print()
    for did_id, label in _BOARD_PART_DIDS:
        try:
            data = sess.read_did(did_id)
            text = data.decode("ascii", errors="replace").rstrip("\x00")
            print(f"  0x{did_id:04X}  {label:<32} {text!r}  [{data.hex()}]")
        except UdsError as e:
            print(f"  0x{did_id:04X}  {label:<32} Error: {e}")


def _clear_dtc_cmd(sess) -> None:
    from uds_local.client import UdsError
    confirm = input(
        "  ClearDiagnosticInformation (group 0xFFFFFF)? [y/N] "
    ).strip().lower()
    if confirm != "y":
        return
    try:
        sess.clear_dtc(0xFFFFFF)
        print("  DTCs cleared.")
    except UdsError as e:
        print(f"  Error: {e}")


def _reset_cmd(sess) -> None:
    from uds_local.client import UdsError
    confirm = input("  Send ECU hard reset? [y/N] ").strip().lower()
    if confirm != "y":
        return
    try:
        sess.ecu_reset(0x01)
        print("  Reset sent.")
    except UdsError as e:
        print(f"  Error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    from uds_local.client import UdsSession
    from uds_local.node_config import load_node_config

    parser = argparse.ArgumentParser(
        description="Interactive Tesla Model 3 ECU diagnostic terminal")
    parser.add_argument(
        "--node", "-n", help="ECU node name (e.g. PCS, CP). Prompts if omitted.")
    parser.add_argument("--channel", "-c", help="CAN interface channel")
    parser.add_argument("--interface", "-i", help="python-can interface type")
    parser.add_argument("--artifacts", "-a",
                        help="Path to seed_artifacts_v2 (for DFU)")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Skip identity mismatch check during DFU (also TM3_DFU_FORCE)")
    _cfg.apply_defaults(parser)
    args = parser.parse_args()

    nodes = json.loads(_NODES_JSON.read_text())

    node_name = args.node.upper() if args.node else None
    if node_name and node_name not in nodes:
        print(f"Error: unknown node {node_name!r}")
        return 1

    artifacts_dir = Path(args.artifacts).expanduser(
    ).resolve() if args.artifacts else None

    if not node_name:
        node_name = _pre_connection_menu(nodes, args.channel, args.interface)
    if not node_name:
        return 0

    try:
        cfg = load_node_config(node_name, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
    except Exception as e:
        print(f"Error loading node config: {e}")
        return 1

    odj_fields: dict[str, Any] = {}
    odj_routines: dict[str, Any] = {}
    odj_io_controls: dict[str, Any] = {}
    for odj_name in nodes[node_name].get("odj_sources", []):
        odj_fields.update(_load_odj_fields(_ODJ_DIR / odj_name))
        odj_routines.update(_load_odj_routines(_ODJ_DIR / odj_name))
        odj_io_controls.update(_load_odj_io_controls(_ODJ_DIR / odj_name))

    print(f"\nConnecting to {node_name} on {args.channel}...")

    try:
        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            _show_identity(sess, cfg)
            _main_menu(sess, cfg, odj_fields, odj_routines, odj_io_controls,
                       artifacts_dir, force=args.force)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError: {e}")
        return 1

    print("\nDisconnected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
