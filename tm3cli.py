#!/usr/bin/env python3
"""Interactive diagnostic terminal for Tesla Model 3 ECUs.

Usage:
  python tm3cli.py --channel vcan0
  python tm3cli.py --node PCS --channel vcan0
  python tm3cli.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
"""

from __future__ import annotations

import json
import logging
import readline
import warnings
from pathlib import Path

import config as _cfg
from uds_local import colors as _c
from uds_local.client import (
    _SESSION_DEFAULT,
    _SESSION_EXTENDED,
    _SESSION_PROGRAMMING,
    _SESSION_SAFETY,
    BusUnavailableError,
)
from uds_local.node_config import NodeConfig
from uds_local.odj import FieldSpec, IoControlEntry, OdjEntry, RoutineEntry
from uds_local.resolve import IOCP_SUFFIX_MAP as _IOCP_SUFFIX_MAP

_log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=RuntimeWarning,
                        module=r"uds\.packet\.abstract_packet")
warnings.filterwarnings(
    "ignore",
    message="A CAN packet that does not start UDS message transmission")
warnings.filterwarnings(
    "ignore", module=r"uds\.can\.transport_interface\.common")
# Silence all warnings from the uds python_can transport (e.g. the Notifier
# timeout UserWarning) and from the python-can library itself.
warnings.filterwarnings(
    "ignore", module=r"uds\.can\.transport_interface\.python_can")
warnings.filterwarnings("ignore", module=r"can(\..*)?")


# ---------------------------------------------------------------------------
# Product selection
# ---------------------------------------------------------------------------

def _select_product() -> _cfg.FwPaths:
    """Prompt the user to choose a device/product when multiple are available.

    Returns a FwPaths for the chosen product. Falls back to the default
    (TM3_PRODUCT env var, or the first available product) without prompting
    when there is only one choice or TM3_PRODUCT is explicitly set.
    """
    products = _cfg.available_products()

    import os
    explicit = os.environ.get("TM3_PRODUCT")

    if explicit:
        return _cfg.FwPaths(explicit)

    if len(products) <= 1:
        product = products[0] if products else _cfg.PRODUCT
        return _cfg.FwPaths(product)

    _hdr("Select device")
    for i, name in enumerate(products, 1):
        print(f"  {i}.  {name}")
    print()

    _setup_completion(products)
    while True:
        try:
            raw = input("  Device> ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0) from None
        if not raw:
            continue
        # Accept a number or the name directly
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(products):
                return _cfg.FwPaths(products[idx])
            print(f"  Enter a number between 1 and {len(products)}")
        elif raw in products:
            return _cfg.FwPaths(raw)
        else:
            print(
                f"  Unknown product {raw!r}. "
                f"Choose from: {', '.join(products)}"
            )


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

def _decode_fields(data: bytes, fields: dict[str, FieldSpec]) -> list[tuple[str, str]]:
    """Decode response bytes into (field_name, value_str) pairs using ODJ field specs."""
    results = []
    for name, spec in sorted(fields.items(), key=lambda x: x[1].byte_position):
        dtype = spec.data_type
        byte_pos = spec.byte_position
        bit_len = spec.bit_length
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
    rule = "─" * 60
    print(f"\n{_c.dim(rule)}")
    print(f"  {_c.bold(text)}")
    print(_c.dim(rule))


def _menu_item(cmd: str, desc: str) -> None:
    """Print one menu row: bold command, dim description."""
    print(f"  {_c.bold(f'{cmd:<11}')} {_c.dim(desc)}")


# Prompt string shared by every input() — bold so the cursor line stands out
# from the command output above it.
_PROMPT = _c.bold("  > ")


def _err(msg: str) -> None:
    """Print an error line (red), indented two spaces like the rest of the UI."""
    print(_c.error(f"  {msg}"))


def _warn(msg: str) -> None:
    """Print a warning / hint line (yellow), indented two spaces."""
    print(_c.warning(f"  {msg}"))


def _print_did_response(name: str, did_id: int, data: bytes, fields: dict[str, FieldSpec]) -> None:
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

def _pre_connection_menu(nodes: dict, channel: str, interface: str, fw: _cfg.FwPaths) -> str | None:
    """Top-level menu shown before connecting. Returns a node name or None to quit."""
    from uds_local.scanner import print_scan_table, scan_network

    node_names = sorted(nodes.keys())
    _setup_completion(["scan", "connect", "quit"] + node_names)

    while True:
        _hdr(f"tm3diag  —  not connected  [{fw.product}]")
        _menu_item("scan", "Probe all known nodes on the bus")
        _menu_item("connect", "<node>  Connect to a node by name")
        _menu_item("quit", "Exit")

        try:
            raw = input(f"\n{_PROMPT}").strip()
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
                    channel, fw.nodes_json, fw.eth_compact, interface=interface)
                print()
                print_scan_table(results)
                print()
            except Exception as e:
                print(_c.error(f"  Scan error: {e}") + "\n")

        elif cmd == "connect":
            if len(parts) < 2:
                print(_c.warning("  Usage: connect <node>"))
                continue
            name = parts[1].upper()
            if name not in nodes:
                print(_c.error(
                    f"  Unknown node {name!r}. Known: {', '.join(node_names)}"))
                continue
            return name

        else:
            # bare node name shorthand
            name = raw.upper()
            if name in nodes:
                return name
            print(_c.error(f"  Unknown command or node: {raw!r}"))


# ---------------------------------------------------------------------------
# Identity banner (0xF180)
# ---------------------------------------------------------------------------

def _show_identity(sess, cfg: NodeConfig) -> bool:
    """Read the identity DID (0xF180) to confirm the ECU is responding.

    Returns True if the ECU answered (we're really connected), False if the
    read failed — in which case the caller should not proceed to the menu.
    """
    from uds_local.client import UdsError
    from uds_local.identity import parse_f180
    try:
        data = sess.read_did(0xF180)
    except UdsError as e:
        _err(f"Could not read 0xF180: {e}")
        return False

    print(f"\n  Connected to {cfg.name}")

    f180_entry = next(
        (e for e in cfg.dids.values() if e.hex_id == 0xF180), None
    )
    fields: dict[str, FieldSpec] = (
        f180_entry.read.output if f180_entry and f180_entry.read else {}
    )
    decoded = _decode_fields(data, fields)
    if decoded:
        for fname, val in decoded:
            print(f"    {fname:<36} {val}")

    # The firmware lookup key (the same one dfu.py / tm3uds.py identity report)
    try:
        ident = parse_f180(data, cfg.name)
    except ValueError:
        pass
    else:
        print(f"    {'lookup_key':<36} {ident.lookup_key}")
    return True


# ---------------------------------------------------------------------------
# DID menu
# ---------------------------------------------------------------------------

def _did_menu(sess, cfg: NodeConfig, dids: dict[str, OdjEntry]) -> None:
    from uds_local.client import UdsError

    readable = {name: entry for name, entry in dids.items() if entry.read is not None}
    if not readable:
        _warn("No readable DIDs found for this node.")
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
                entry = readable[n]
                size = entry.read.output_size if entry.read else "?"
                sl = entry.read.security_level if entry.read else 0
                sl_str = f"  sl={sl}" if sl else ""
                print(f"    0x{entry.hex_id:04X}  {n:<40} {size}B{sl_str}")
            continue

        # Resolve name or hex
        if raw in readable:
            name = raw
            entry = readable[name]
        elif raw.lower().startswith("0x"):
            try:
                did_id = int(raw, 16)
            except ValueError:
                print(f"  Invalid hex: {raw!r}")
                continue
            match = next((n for n, e in readable.items() if e.hex_id == did_id), None)
            if match:
                name, entry = match, readable[match]
            else:
                print(f"  DID 0x{did_id:04X} not in ODJ — attempting raw read")
                name, entry = raw, None
        else:
            _err(f"Unknown DID: {raw!r}  (try 'list' or tab complete)")
            continue

        did_id = entry.hex_id if entry else int(raw, 16)
        sl = entry.read.security_level if entry and entry.read else 0

        if sl:
            print(f"  DID requires security level {sl} — running security access...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        try:
            data = sess.read_did(did_id)
            fields: dict[str, FieldSpec] = (
                entry.read.output if entry and entry.read else {}
            )
            _print_did_response(name, did_id, data, fields)
        except UdsError as e:
            _err(f"Error: {e}")


# ---------------------------------------------------------------------------
# Routine menu
# ---------------------------------------------------------------------------

def _prompt_routine_inputs(fields: dict[str, FieldSpec]) -> bytes | None:
    """Prompt for each input field and pack into bytes. Returns None on error."""
    if not fields:
        return b""
    total = max(
        f.byte_position + (f.bit_length + 7) // 8
        for f in fields.values()
    )
    buf = bytearray(total)
    for fname, fspec in fields.items():
        bit_len = fspec.bit_length
        byte_pos = fspec.byte_position
        bit_pos = fspec.bit_position
        dtype = fspec.data_type
        enum_map = fspec.enum_map
        if enum_map:
            opts = ", ".join(f"{k}={v}" for k, v in enum_map.items())
            raw = input(f"    {fname} ({opts}): ").strip()
            upper_map = {k.upper(): v for k, v in enum_map.items()}
            if raw.upper() in upper_map:
                val = upper_map[raw.upper()]
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


def _routine_menu(sess, cfg: NodeConfig, routines: dict[str, RoutineEntry]) -> None:
    from uds_local.client import UdsError

    named = list(_NAMED_ROUTINES.keys())
    odj_names = sorted(routines.keys())
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
                    entry = routines[name]
                    actions = [a for a in ("start", "stop", "results")
                               if getattr(entry, a) is not None]
                    sl = entry.start.security_level if entry.start else 0
                    sa_str = f"  [sl={sl}]" if sl else ""
                    print(f"    0x{entry.hex_id:04X}  {name:<40} {', '.join(actions)}{sa_str}")
            continue

        # Resolve to (routine_id, needs_sa, sl, entry | None)
        odj_entry: RoutineEntry | None = None
        needs_sa = False
        sl = 1
        if raw in routines:
            odj_entry = routines[raw]
            routine_id = odj_entry.hex_id
            sl = odj_entry.start.security_level if odj_entry.start else 0
            needs_sa = bool(sl)
            print(f"  → 0x{routine_id:04X}  {raw}")
        elif raw.lower() in _NAMED_ROUTINES:
            routine_id, desc, needs_sa = _NAMED_ROUTINES[raw.lower()]
            print(f"  → 0x{routine_id:04X}  {desc}")
        elif raw.lower().startswith("0x"):
            match = next(
                (n for n, e in routines.items() if e.hex_id == int(raw, 16)),
                None,
            )
            if match:
                odj_entry = routines[match]
                routine_id = odj_entry.hex_id
                sl = odj_entry.start.security_level if odj_entry.start else 0
                needs_sa = bool(sl)
                print(f"  → 0x{routine_id:04X}  {match}")
            else:
                try:
                    routine_id = int(raw, 16)
                except ValueError:
                    print(f"  Invalid hex: {raw!r}")
                    continue
        else:
            _err(f"Unknown routine: {raw!r}  (try 'list' or tab complete)")
            continue

        if needs_sa:
            print(f"  Routine requires security level {sl} — authenticating...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        if odj_entry:
            available = [a for a in ("start", "stop", "results")
                         if getattr(odj_entry, a) is not None]
            if len(available) == 1:
                action = available[0]
            else:
                action_raw = input(
                    f"  Action ({'/'.join(available)}): ").strip().lower()
                if action_raw not in available:
                    _err(f"Unknown action: {action_raw!r}")
                    continue
                action = action_raw
            sub = getattr(odj_entry, action)
            input_fields = sub.input if sub else {}
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
                arg = bytes.fromhex(arg_raw.replace(" ", "")) if arg_raw else b""
            except ValueError:
                print(f"  Invalid hex: {arg_raw!r}")
                continue
            subtype = 0x01

        try:
            result = sess.routine_control(routine_id, arg, subtype=subtype)
            print(f"  Result: {result.hex() if result else '(empty)'}")
        except UdsError as e:
            _err(f"Error: {e}")


def _io_control_menu(sess, cfg: NodeConfig, io_controls: dict[str, IoControlEntry]) -> None:
    from uds_local.client import UdsError

    names = sorted(io_controls.keys())
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
                entry = io_controls[name]
                sl = entry.security_level
                sa_str = f"  [sl={sl}]" if sl else ""
                _, cp_desc = next(
                    ((v, d) for sfx, (v, d) in _IOCP_SUFFIX_MAP.items() if name.endswith(sfx)),
                    (0x03, "shortTermAdjustment"),
                )
                print(f"    0x{entry.hex_id:04X}  {name:<52} {cp_desc}{sa_str}")
            continue

        # Resolve to (ctrl_id, sl, entry | None)
        io_entry: IoControlEntry | None = None
        if raw in io_controls:
            io_entry = io_controls[raw]
            ctrl_id = io_entry.hex_id
            sl = io_entry.security_level
            print(f"  → 0x{ctrl_id:04X}  {raw}")
        elif raw.lower().startswith("0x"):
            match = next(
                (n for n, e in io_controls.items() if e.hex_id == int(raw, 16)),
                None,
            )
            if match:
                io_entry = io_controls[match]
                ctrl_id = io_entry.hex_id
                sl = io_entry.security_level
                print(f"  → 0x{ctrl_id:04X}  {match}")
            else:
                try:
                    ctrl_id = int(raw, 16)
                    sl = 0
                except ValueError:
                    print(f"  Invalid hex: {raw!r}")
                    continue
        else:
            _err(f"Unknown control: {raw!r}  (try 'list' or tab complete)")
            continue

        if sl:
            print(f"  Control requires security level {sl} — authenticating...")
            try:
                sess.diagnostic_session(_SESSION_PROGRAMMING)
                sess.security_access(seed_level=sl)
            except UdsError as e:
                print(f"  Security access failed: {e}")
                continue

        ctrl_name = raw if io_entry else f"0x{ctrl_id:04X}"
        control_param, _ = next(
            ((v, d) for sfx, (v, d) in _IOCP_SUFFIX_MAP.items() if ctrl_name.endswith(sfx)),
            (0x03, "shortTermAdjustment"),
        )

        if io_entry and io_entry.input:
            data = _prompt_routine_inputs(io_entry.input)
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
            if result and io_entry and io_entry.output:
                decoded = _decode_fields(result, io_entry.output)
                for fname, fval in decoded:
                    print(f"  {fname}: {fval}")
            else:
                print(f"  Result: {result.hex() if result else '(empty)'}")
        except UdsError as e:
            _err(f"Error: {e}")


# ---------------------------------------------------------------------------
# DFU (firmware update via dfu.py phases)
# ---------------------------------------------------------------------------

def _dfu_menu(sess, cfg, artifacts_dir: Path | None, force: bool | None = None) -> None:
    from dfu import run_flash
    from flash_scripts._display import StatusDisplay
    from uds_local.client import UdsError

    _hdr(f"Firmware update (DFU) — {cfg.name}")

    if artifacts_dir is None or not artifacts_dir.is_dir():
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
        _err(f"Error: {e}")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def _main_menu(sess, cfg: NodeConfig, artifacts_dir: Path | None, force: bool = False) -> None:
    _setup_completion([
        "dids", "routine", "io-control", "board-parts", "clear-dtc",
        "dfu", "session", "reset", "quit",
    ])

    while True:
        _hdr(f"{cfg.name}  —  Main menu")
        _menu_item("dids", "Read DIDs interactively")
        _menu_item("routine", "Run a routine control")
        _menu_item("io-control", "InputOutputControlByIdentifier (0x2F)")
        _menu_item("board-parts", "Read board part/serial DIDs (0xF012–0xF015)")
        _menu_item("clear-dtc", "ClearDiagnosticInformation (0xFFFFFF)")
        _menu_item("dfu", "Firmware update")
        _menu_item("session", "Switch diagnostic session")
        _menu_item("reset", "ECU hard reset")
        _menu_item("quit", "Disconnect and exit")

        try:
            cmd = input(f"\n{_PROMPT}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "dids":
            _did_menu(sess, cfg, cfg.dids)
        elif cmd == "routine":
            _routine_menu(sess, cfg, cfg.routines)
        elif cmd == "io-control":
            _io_control_menu(sess, cfg, cfg.io_controls)
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
            _err(f"Unknown command: {cmd!r}")


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
            _err(f"Unknown session: {raw!r}")
            return
    try:
        sess.diagnostic_session(mode)
        print(f"  Entered session 0x{mode:02X}")
    except UdsError as e:
        _err(f"Error: {e}")


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
            print(f"  0x{did_id:04X}  {label:<32} " + _c.error(f"Error: {e}"))


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
        _err(f"Error: {e}")


def _reset_cmd(sess) -> None:
    from uds_local.client import UdsError
    confirm = input("  Send ECU hard reset? [y/N] ").strip().lower()
    if confirm != "y":
        return
    try:
        sess.ecu_reset(0x01)
        print("  Reset sent.")
    except UdsError as e:
        _err(f"Error: {e}")


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

    if _cfg._ROOT is None:
        print(_c.error(
            "Error: TM3_ROOT is not set. Point it at the squashfs-root of a "
            "firmware extraction (export TM3_ROOT=... or add it to .env)."
        ))
        return 1

    fw = _select_product()

    if fw.nodes_json is None or not fw.nodes_json.exists():
        print(_c.error(
            f"Error: nodes.json not found for product {fw.product!r} under "
            f"{_cfg._ROOT}. Check TM3_ROOT and TM3_PRODUCT."
        ))
        if fw.nodes_json is not None:
            print(f"  Looked for: {fw.nodes_json}")
        return 1

    nodes = json.loads(fw.nodes_json.read_text())

    node_name = args.node.upper() if args.node else None
    if node_name and node_name not in nodes:
        print(_c.error(f"Error: unknown node {node_name!r}"))
        return 1

    artifacts_dir = Path(args.artifacts).expanduser(
    ).resolve() if args.artifacts else None

    # Interactive: chosen from the pre-connection menu, so a failed connect
    # returns there. --node: a single attempt that exits with the result code.
    interactive = node_name is None

    while True:
        if interactive:
            node_name = _pre_connection_menu(nodes, args.channel, args.interface, fw)
            if not node_name:
                return 0

        try:
            cfg = load_node_config(node_name, fw.nodes_json, fw.eth_compact, fw.odj_dir)
        except Exception as e:
            print(_c.error(f"Error loading node config: {e}"))
            if interactive:
                continue
            return 1

        connected = _connect_and_run(
            cfg, node_name, args, artifacts_dir, fw=fw, UdsSession=UdsSession
        )
        # Interactive: only a failed connect returns to the node menu; a session
        # the user quit ("Disconnect and exit") ends the program as advertised.
        if interactive and not connected:
            continue
        return 0 if connected else 1


def _connect_and_run(cfg, node_name, args, artifacts_dir, *, fw, UdsSession) -> bool:
    """Connect to one node and run the menu. Returns False if the connect failed.

    A failed connect (no 0xF180 response, or a session-level error) returns
    False so an interactive caller can drop back to the node menu instead of
    showing the main menu as if we were connected. Returns True once we've had
    a live session, whether the user quit or hit Ctrl-C.
    """
    effective_artifacts = artifacts_dir or fw.artifacts_dir
    print(f"\nConnecting to {node_name} on {args.channel}...")

    try:
        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            if not _show_identity(sess, cfg):
                print(_c.warning(
                    f"\n  No response from {node_name} on {args.channel}. "
                    "Check that the ECU is powered, on the bus, and that the "
                    "channel/interface are correct."
                ))
                return False
            _main_menu(sess, cfg, effective_artifacts, force=args.force)
    except KeyboardInterrupt:
        pass
    except BusUnavailableError as e:
        print(_c.error(
            f"\nError: CAN bus {e.channel!r} is down or unavailable."
        ))
        print(_c.warning(
            f"  Bring it up, e.g.:\n"
            f"  sudo ip link set {e.channel} up type can bitrate 500000"
        ))
        return False
    except Exception as e:
        print(_c.error(f"\nError: {e}"))
        return False

    print("\nDisconnected.")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
