#!/usr/bin/env python3
"""Generic UDS CLI for Tesla Model 3 ECUs.

Usage examples:
  python tm3uds.py scan --channel vcan0
  python tm3uds.py --node PCS --channel vcan0 read-did BOOTLOADER_VERSION
  python tm3uds.py --node PCS --channel vcan0 read-did 0xF180
  python tm3uds.py --node PCS --channel vcan0 write-did 0x0102 deadbeef
  python tm3uds.py --node PCS --channel vcan0 routine 0xFF00 01
  python tm3uds.py --node PCS --channel vcan0 routine FORCE_COPY_ALL_RAM_To_EEPROM
  python tm3uds.py --node PCS --channel vcan0 io-control DCDC_HV_LV_Bridge_MFG_IO_CONTROL_ADJUST 01
  python tm3uds.py --node CP  --channel vcan0 security-access
  python tm3uds.py --node PCS --channel vcan0 session programming
  python tm3uds.py --node PCS --channel vcan0 reset
"""

from __future__ import annotations

import argparse
import sys

import config as _cfg
from uds_local.client import (
    _SESSION_DEFAULT, _SESSION_PROGRAMMING, _SESSION_EXTENDED, _SESSION_SAFETY,
)
from uds_local.resolve import resolve_did, resolve_routine, resolve_io_control

_NODES_JSON  = _cfg.NODES_JSON
_ETH_COMPACT = _cfg.ETH_COMPACT
_ODJ_DIR     = _cfg.ODJ_DIR


def _load_config(node_name: str):
    from uds_local.node_config import load_node_config
    return load_node_config(node_name, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)


def _make_session(node_name: str, channel: str, interface: str):
    from uds_local.client import UdsSession
    cfg = _load_config(node_name)
    return UdsSession(cfg, channel, interface=interface)


def cmd_scan(args: argparse.Namespace) -> None:
    from uds_local.scanner import scan_network, print_scan_table
    print(f"Scanning {args.channel} ({args.interface})...")
    results = scan_network(
        args.channel, _NODES_JSON, _ETH_COMPACT,
        timeout_per_node=args.timeout,
        interface=args.interface,
    )
    print_scan_table(results)
    responding = sum(1 for r in results if r.responded)
    print(f"\n{responding}/{len(results)} nodes responded.")


def cmd_read_did(args: argparse.Namespace) -> None:
    cfg = _load_config(args.node)
    did_id, did_name = resolve_did(cfg, args.did)
    with _make_session(args.node, args.channel, args.interface) as sess:
        data = sess.read_did(did_id)
    print(f"{did_name} (0x{did_id:04X}): {data.hex()}")

    # Decode fields from ODJ if name is known
    entry = cfg.dids.get(did_name)
    if entry and entry.read_size:
        print(f"  size: {len(data)} bytes (expected {entry.read_size})")


def cmd_write_did(args: argparse.Namespace) -> None:
    cfg = _load_config(args.node)
    did_id, did_name = resolve_did(cfg, args.did)
    try:
        data = bytes.fromhex(args.data.replace(" ", ""))
    except ValueError:
        print(f"Error: invalid hex data: {args.data!r}")
        sys.exit(1)
    with _make_session(args.node, args.channel, args.interface) as sess:
        sess.write_did(did_id, data)
    print(f"Wrote {len(data)} bytes to {did_name} (0x{did_id:04X}).")


def cmd_routine(args: argparse.Namespace) -> None:
    cfg = _load_config(args.node)
    routine_id, routine_name = resolve_routine(cfg, args.routine_id)
    arg_bytes = bytes.fromhex(args.arg) if args.arg else b""
    with _make_session(args.node, args.channel, args.interface) as sess:
        result = sess.routine_control(routine_id, arg_bytes)
    result_str = result.hex() if result else "(empty)"
    print(f"Routine {routine_name} (0x{routine_id:04X}) result: {result_str}")


def cmd_io_control(args: argparse.Namespace) -> None:
    cfg = _load_config(args.node)
    ctrl_id, ctrl_name, control_param = resolve_io_control(cfg, args.control)
    try:
        data = bytes.fromhex(args.data.replace(" ", "")) if args.data else b""
    except ValueError:
        print(f"Error: invalid hex data: {args.data!r}")
        sys.exit(1)
    with _make_session(args.node, args.channel, args.interface) as sess:
        result = sess.io_control(ctrl_id, control_param, data)
    result_str = result.hex() if result else "(empty)"
    print(f"IO control {ctrl_name} (0x{ctrl_id:04X}) result: {result_str}")


def cmd_security_access(args: argparse.Namespace) -> None:
    cfg = _load_config(args.node)
    with _make_session(args.node, args.channel, args.interface) as sess:
        sess.diagnostic_session(_SESSION_PROGRAMMING)  # programming session required for SA
        sess.start_tester_present()
        sess.security_access()
        sess.stop_tester_present()
    print(
        f"Security access granted for {args.node}"
        f" (algorithm: {cfg.security_algorithm})."
    )


def cmd_session(args: argparse.Namespace) -> None:
    mode_map = {
        "default": _SESSION_DEFAULT, "programming": _SESSION_PROGRAMMING,
        "extended": _SESSION_EXTENDED, "safety": _SESSION_SAFETY,
    }
    mode_arg = args.mode.lower()
    if mode_arg in mode_map:
        mode = mode_map[mode_arg]
    else:
        try:
            mode = int(mode_arg, 0)
        except ValueError:
            print(f"Error: unknown session mode: {args.mode!r}")
            sys.exit(1)
    with _make_session(args.node, args.channel, args.interface) as sess:
        sess.diagnostic_session(mode)
    print(f"Entered session 0x{mode:02X} on {args.node}.")


def cmd_reset(args: argparse.Namespace) -> None:
    with _make_session(args.node, args.channel, args.interface) as sess:
        sess.ecu_reset(0x01)
    print(f"ECU reset sent to {args.node}.")


def cmd_clear_dtc(args: argparse.Namespace) -> None:
    with _make_session(args.node, args.channel, args.interface) as sess:
        sess.clear_dtc(0xFFFFFF)
    print(f"ClearDiagnosticInformation sent to {args.node} (group 0xFFFFFF).")


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description="Tesla Model 3 UDS diagnostic tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--channel", "-c",
        help="CAN interface channel")
    parser.add_argument(
        "--interface", "-i",
        help="python-can interface type")
    _cfg.apply_defaults(parser)

    parent_node = argparse.ArgumentParser(add_help=False)
    parent_node.add_argument(
        "--node", "-n", help="ECU node name (e.g. PCS, CP, RCM)")

    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser(
        "scan", help="Probe all nodes for TesterPresent response",
    )
    p_scan.add_argument(
        "--timeout", type=float, default=0.1, help="Per-node timeout (s)")
    p_scan.set_defaults(func=cmd_scan)

    # read-did
    p_rdid = sub.add_parser(
        "read-did", help="Read a DID (0x22)",
        parents=[parent_node],
    )
    p_rdid.add_argument("did", help="DID name or 0xHEX id")
    p_rdid.set_defaults(func=cmd_read_did)

    # write-did
    p_wdid = sub.add_parser(
        "write-did", help="Write a DID (0x2E)",
        parents=[parent_node],
    )
    p_wdid.add_argument("did", help="DID name or 0xHEX id")
    p_wdid.add_argument("data", help="Hex data to write (no spaces required)")
    p_wdid.set_defaults(func=cmd_write_did)

    # routine
    p_rc = sub.add_parser(
        "routine", help="Execute a RoutineControl (0x31 01)",
        parents=[parent_node],
    )
    p_rc.add_argument("routine_id", help="Routine name or 0xHEX id")
    p_rc.add_argument(
        "arg", nargs="?", default="", help="Optional argument hex bytes")
    p_rc.set_defaults(func=cmd_routine)

    # io-control
    p_ioc = sub.add_parser(
        "io-control", help="InputOutputControlByIdentifier (0x2F)",
        parents=[parent_node],
    )
    p_ioc.add_argument("control", help="IO control name or 0xHEX id")
    p_ioc.add_argument(
        "data", nargs="?", default="", help="Optional hex data bytes")
    p_ioc.set_defaults(func=cmd_io_control)

    # security-access
    p_sa = sub.add_parser(
        "security-access",
        help="Enter programming session + full seed/key exchange",
        parents=[parent_node],
    )
    p_sa.set_defaults(func=cmd_security_access)

    # session
    p_sess = sub.add_parser(
        "session", help="Switch diagnostic session",
        parents=[parent_node],
    )
    p_sess.add_argument(
        "mode",
        help="Session: default|programming|extended|safety or 0xNN")
    p_sess.set_defaults(func=cmd_session)

    # reset
    p_rst = sub.add_parser(
        "reset", help="ECU hard reset (0x11 01)",
        parents=[parent_node],
    )
    p_rst.set_defaults(func=cmd_reset)

    # clear-dtc
    p_cdtc = sub.add_parser(
        "clear-dtc",
        help="ClearDiagnosticInformation (0x14) — group 0xFFFFFF",
        parents=[parent_node],
    )
    p_cdtc.set_defaults(func=cmd_clear_dtc)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Commands that need --node
    if args.command != "scan" and not args.node:
        parser.error(f"--node is required for '{args.command}'")

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
