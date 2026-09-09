#!/usr/bin/env python3
"""Vehicle-bus liveness simulator to bring a bench DIR/PMR out of MIA.

A standalone Model 3 rear drive unit on the bench asserts MIA alerts because the
BMS / VCFRONT / CP / brake liveness frames it expects never arrive. This transmits
the exact frames the DIR monitors, at their cycle rates, so those MIAs clear and
the unit can advance toward the immobilizer handshake and (later) drive.

ARCHITECTURE — node registry
----------------------------
Each peer ECU owns the CAN messages it *sources*, declared as a ``SimNode`` in its
own folder (``scripts/<node>/<node>.py``); ``sim_registry`` aggregates them and this
tool expands the selected nodes into one flat ``SimFrame`` list, then transmits each
on its logical bus.

BY FIRMWARE REVISION
--------------------
The message layouts are authored per firmware revision. ``--fw <rev>`` (or ``[firmware]
version`` in sim.toml / ``TM3_FW``) picks the target; each node transmits the newest set it
has authored at/below that revision, falling back to the last version for which it *has*
messages. Everything ships against the 2020.8.1 baseline today (only that extraction has a
decrypted compact DB); newer per-revision deltas get added to a node's ``fw_variants`` as RE
pins them. Omit ``--fw`` to use each node's newest authored set. See ``Node.fw_variants``.

WHICH FRAMES — firmware- + bench-confirmed
------------------------------------------
The 2020 DIR (TMS320C28x CPU2) has its OWN CAN-RX handler: the PMR (CPU1) relays
every received frame to the DIR over IPC unfiltered, and the DIR dispatches by
arbitration ID through binary-searched ID tables (dir32_67_2_rwd @ 0xb8278 = 48
IDs, 0xb8a30 = 18 IDs) — the definitive list of what it consumes. The per-message
rationale (which MIA each feeds, checksum/counter scheme, exact DLC) now lives as a
comment on that message's node builder; see each ``scripts/<node>/<node>.py``.

HOW MIA CLEARS — arrival for plain frames, checksum for validated frames
-----------------------------------------------------------------------
Handlers with no counter/checksum (BMS, CP) reset their MIA down-counter on any
DLC>=8 arrival regardless of payload. Handlers for counter+checksum messages
(0x221 confirmed; VCFRONT/ESP/IBST/RCM by convention) only reset the MIA when the
checksum AND the 4-bit rolling counter validate. So the validated frames must be
correct, not just present. Tesla vehicle-bus checksum:
    magic    = (id & 0xFF) + (id >> 8)            # id_low + id_high
    checksum = (magic + Sum(all bytes EXCEPT the checksum byte)) & 0xFF

IMMOBILIZER: this sim also ANSWERS the runtime handshake. The DIR emits a PARKED
0x276 challenge; we reply on 0x3D9 -> 0x118 immo = DISARMED. The response is
computed by a key-derivation provider you configure (see docs/SECURITY_PROVIDER.md)
— the framework ships no immobilizer algorithm. Enabled whenever a key is
available (via --immo-key, or a provider-backed keystore); disable with --no-immo.
The 0x3D9 response is VCSEC's message: the responder is the VCSEC node's (scripts/
vcsec/vcsec.py), which the bench core installs on the vehicle bus when VCSEC is
selected + a key resolved. TX runs on the shared ecu_bench engine (one Scheduler
+ a Notifier of listeners).

SAFETY: telling the DI "HV up / contactors closed / drive-ready, brakes not
applied" is fine on the bench -- with HVIL open nothing energizes; these frames
only let the readiness / immobilizer FSM advance. This does NOT command torque.

Usage:
  # liveness + immobilizer DISARM in one process (key from immo_keys.json or --immo-key):
  python scripts/vehicle_sim.py --channel can0 --party-channel can1
  python scripts/vehicle_sim.py --no-immo       # liveness only (no DISARM attempt)
  python scripts/vehicle_sim.py --fw 2024.8.9   # target a 2024.8.9 bench (falls back per node)
  python scripts/vehicle_sim.py --list-nodes --fw 2024.8.9   # inventory + resolved revision
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import can

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: config, can_decoder, uds
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))  # scripts/: tesla_frames, sim_*, nodes

import sim_core  # noqa: E402
import sim_registry  # noqa: E402
from tesla_frames import UI_SETTINGS  # noqa: E402
from tesla_frames import (  # noqa: E402
    VEHICLE_POWER_STATE as _VEHICLE_POWER_STATE,
)

import config as _cfg  # noqa: E402
import ecu_bench  # noqa: E402
from can_decoder import CanDatabase  # noqa: E402
from uds_local.security_provider import (  # noqa: E402
    Keystore,
    resolve_di_key,
)

IMMO_CHALLENGE_ID = 0x276  # DIR -> us: parked challenge (full counter+nonce on the wire)
IMMO_RESPONSE_ID = 0x3D9  # us -> DIR: answer (DLC 8) -> 0x118 immo = 3 DISARMED

# Named node-selection presets. A profile sets default --real nodes + handed-off IDs
# (excluded here because another transmitter sources them). Explicit --sim/--real/
# --exclude compose on top. 'exclude' IDs are also treated as covered for MIA checks.
_PROFILES: dict[str, dict] = {
    "full-car": {},  # simulate every node (same as no selection flags)
    # di.py owns the gear stalk (SCCM 0x229) + the pedal map (0x334); the other UI
    # members stay so uiMIA still clears. See scripts/di/di.py "RUNNING WITH vehicle_sim".
    "di-bench": {"real": ["SCCM"], "exclude": [0x334]},
}


# ---------------------------------------------------------------------------
# 0x118 DI status decode (to watch progress) -- bit positions from di.py
# ---------------------------------------------------------------------------
_DI_IMMO = {
    0: "INIT_SNA",
    1: "REQUEST",
    2: "AUTHENTICATING",
    3: "DISARMED",
    4: "IDLE",
    5: "RESET",
    6: "FAULT",
}
_DI_SYS = {0: "UNAVAIL", 1: "IDLE", 2: "STANDBY", 3: "FAULT", 4: "ABORT", 5: "ENABLE"}
_DI_GEAR = {0: "INVALID", 1: "P", 2: "R", 3: "N", 4: "D", 7: "SNA"}
_DI_HVIL = {0: "DISABLED", 1: "STG1", 2: "CLOSED", 3: "SNA"}


def _decode_0x118(data: bytes) -> str:
    v = int.from_bytes(data[:8], "little")
    gear = (v >> 21) & 0x7
    sysst = (v >> 16) & 0x7
    immo = (v >> 27) & 0x7
    hvil = (v >> 24) & 0x3
    return (
        f"immo={_DI_IMMO.get(immo, immo)} sys={_DI_SYS.get(sysst, sysst)} "
        f"gear={_DI_GEAR.get(gear, gear)} hvil={_DI_HVIL.get(hvil, hvil)}"
    )


# ---------------------------------------------------------------------------
# VDC / ESP status-field bisection helpers
# ---------------------------------------------------------------------------
# The DI VDC/ESP/AEB/hold alert batch (0x36B/0x36E) is gated on per-signal STATUS/QUALITY
# sub-fields, not liveness: the DIR counts a signal valid only when value==0 AND its
# status bit is SET. The ESP node's builders assert the known
# ones; the exact "valid" enum per field is not statically recoverable (stripped fw), so use
# --set to bench-bisect the rest. Candidate fields (start:width, standard little-endian bit):
#   0x105  13:1 14:1 15:1 + 33:1 34:1 35:1  (brake-torque a193/a194; gates wheel-speed a232)
#   0x155  byte4 four 2-bit fields (bits0-7) + 40:1 41:1  (MC-press/steering a198)
#   0x185  50:1 direction bit  (NB 52:4 = rolling counter, 56 = checksum -- don't --set them)
def _parse_set_overrides(spec: str | None) -> dict[int, list[tuple[int, int, int]]]:
    """Parse --set 'ID:start:width=value[,...]' into {can_id: [(start,width,value), ...]}."""
    out: dict[int, list[tuple[int, int, int]]] = {}
    if not spec:
        return out
    for item in spec.replace(" ", "").split(","):
        if not item:
            continue
        field_spec, _, value_s = item.partition("=")
        if not value_s:
            raise ValueError(f"--set entry '{item}' missing '=value'")
        cid_s, start_s, width_s = field_spec.split(":")
        out.setdefault(int(cid_s, 16), []).append(
            (int(start_s, 0), int(width_s, 0), int(value_s, 0))
        )
    return out


def _start_control_server(controller, port: int) -> ThreadingHTTPServer:
    """Tiny stdlib HTTP server so tm3web can push new command states into the sim.

    POST /cmd {"type":"gear"|"lv"|"ui"|"carconfig", ...}; GET /state (commanded state);
    GET /carconfig (the GTW schema for the dashboard editor). Runs in a daemon thread.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # keep the console quiet
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/state":
                self._send(200, controller.state())
            elif self.path == "/carconfig":
                self._send(200, controller.carcfg.schema())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                cmd = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                return self._send(400, {"error": "invalid JSON"})
            t = cmd.get("type")
            try:
                if t == "gear":
                    v = controller.gear(cmd["value"])
                elif t == "lv":
                    v = controller.set_lv(cmd["value"])
                elif t == "ui":
                    v = controller.set_ui(cmd["field"], cmd["value"])
                elif t == "carconfig":
                    v = controller.set_carconfig(cmd["signal"], cmd["value"])
                else:
                    return self._send(400, {"error": f"unknown command type {t!r}"})
            except (KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True, "value": v})

    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="ctrl-server").start()
    return srv


class _ControlFacade:
    """Routes control-server commands (from tm3web) to the node that owns each piece of
    state, so _start_control_server + tm3web don't need to know the command state was
    decentralized from one VehicleController into the nodes. A command for a deselected node
    raises KeyError -> the server returns 400."""

    def __init__(self, by_name: dict) -> None:
        self._n = by_name

    def gear(self, value):
        return self._n["SCCM"].gear(value)

    def set_lv(self, value):
        return self._n["VCFRONT"].set_lv(value)

    def set_ui(self, field, value):
        return self._n["UI"].set_ui(field, value)

    def set_carconfig(self, signal, value):
        return self._n["GTW"].set_carconfig(signal, value)

    @property
    def carcfg(self):
        return self._n["GTW"].carcfg

    def state(self) -> dict:
        n = self._n
        uicfg = n["UI"].uicfg if "UI" in n else None
        lv_vps = n["VCFRONT"].lv.vps if "VCFRONT" in n else None
        return {
            "commanded_gear": n["SCCM"].last_gear_cmd if "SCCM" in n else None,
            "lv_state": next((k for k, v in _VEHICLE_POWER_STATE.items() if v == lv_vps), None),
            "lv_options": list(_VEHICLE_POWER_STATE),
            "ui": {f: getattr(uicfg, f) for f in UI_SETTINGS} if uicfg else {},
            "ui_schema": {
                f: {"label": s["label"], "options": s["options"]} for f, s in UI_SETTINGS.items()
            },
            "epb_status": n["EPB"].epb.status if "EPB" in n else None,
        }


def _resolve_fw_target(cli_fw: str | None, config_fw: str | None):
    """Resolve the target firmware revision to a ``sim_core.FirmwareVersion`` (or None).

    Precedence: ``--fw`` > ``sim.toml [firmware] version`` > ``TM3_FW`` env; nothing set =>
    None, which makes each node emit its newest authored message set. Raises ValueError on an
    unparseable token.
    """
    raw = cli_fw or config_fw or _cfg.FW_VERSION
    return sim_core.FirmwareVersion(raw) if raw else None


def _fw_label(fw) -> str:
    return "newest authored per node (no --fw)" if fw is None else str(fw)


def main() -> None:
    p = argparse.ArgumentParser(description="Vehicle-bus liveness sim for a bench DIR/PMR")
    p.add_argument("--channel", default=None, help="bus A (vehicle CAN) channel")
    p.add_argument("--interface", default=None)
    p.add_argument(
        "--party-channel",
        dest="party_channel",
        default=None,
        help="bus B (party CAN) channel, e.g. can0. REQUIRED for the party ECUs "
        "(rcm/esp/ibst/epas3p/das) -- their frames are DROPPED if sent on bus A",
    )
    p.add_argument(
        "--party-interface",
        dest="party_interface",
        default=None,
        help="python-can interface for bus B (defaults to --interface)",
    )
    p.add_argument(
        "--charge-channel",
        dest="charge_channel",
        default=None,
        help="charge CAN channel (only needed if a message is reassigned to the 'charge' "
        "bus via the bus map; defaults to TM3_CHARGE_CHANNEL)",
    )
    p.add_argument(
        "--charge-interface",
        dest="charge_interface",
        default=None,
        help="python-can interface for the charge bus (defaults to --interface)",
    )
    p.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="bench config TOML: [nodes] sim/real selection + [bus] message->bus overrides. "
        "Defaults to ./sim.toml if present. CLI --sim/--real/--profile compose on top.",
    )
    p.add_argument(
        "--fw",
        dest="fw",
        default=None,
        help="target firmware revision for the message layouts to transmit (e.g. 2024.8.9). "
        "Each node emits the newest message set it has authored at/below this revision, "
        "falling back to the last version for which it has messages. Omit for the newest "
        "authored set per node. Precedence: --fw > sim.toml [firmware] > TM3_FW env.",
    )
    p.add_argument(
        "--allow-bridged",
        action="store_true",
        dest="allow_bridged",
        help="permit running without --party-channel (party frames ride bus A). "
        "They WILL be dropped by the DIR's group2 dispatch -- debug only",
    )
    p.add_argument(
        "--no-221",
        action="store_true",
        dest="no_221",
        help="don't TX 0x221 (leave it to another transmitter)",
    )
    p.add_argument(
        "--no-gtw",
        action="store_true",
        dest="no_gtw",
        help="don't TX GTW_carConfig 0x7FF (disable if configMismatch appears)",
    )
    p.add_argument(
        "--no-das",
        action="store_true",
        dest="no_das",
        help="don't TX DAS_control 0x2B9 (provisional dasMIA source)",
    )
    p.add_argument(
        "--vdc-esp",
        action="store_true",
        dest="vdc_esp",
        help="TX the ESP VDC signal-status inputs 0x1E5 + 0x240 (idle content from a known-good "
        "drive log). Firmware-confirmed NOT board-TX (PMR transmits neither) so no a066 collision; "
        "supplies the DIR VDC-readiness ESP inputs that are absent on a DU-only bench. If a066 "
        "canDataBusB appears, STOP -- something on the DU does transmit them.",
    )
    p.add_argument(
        "--no-ui",
        action="store_true",
        dest="no_ui",
        help="don't TX the UI message group (uiMIA aggregate: 0x82/213/284/293/313/334)",
    )
    p.add_argument(
        "--no-shifter",
        action="store_true",
        dest="no_shifter",
        help="don't TX SCCM_rightStalk 0x229 -- hand the gear stalk to di.py (avoids the "
        "0x229 counter/CRC collision when running the di.py alongside this sim). "
        "sccmMIA will re-assert unless di.py is transmitting 0x229. Alias for --real SCCM.",
    )
    # --- node selection ---------------------
    p.add_argument(
        "--sim",
        dest="sim_nodes",
        default=None,
        help="simulate ONLY these nodes (comma/space list of names; see --list-nodes). "
        "Default: every node. Case-insensitive.",
    )
    p.add_argument(
        "--real",
        "--provided",
        dest="real_nodes",
        default=None,
        help="nodes present on the bus for real (or owned by another process, e.g. di.py) -- "
        "do NOT simulate their frames. Comma/space list of node names.",
    )
    p.add_argument(
        "--profile",
        choices=sorted(_PROFILES),
        default=None,
        help="named selection preset: 'full-car' (all nodes), 'di-bench' (hand gear 0x229 + "
        "pedal 0x334 to di.py). Explicit --sim/--real/--exclude compose with it.",
    )
    p.add_argument(
        "--list-nodes",
        action="store_true",
        dest="list_nodes",
        help="print the node inventory (names + frame IDs + bus) and exit",
    )
    # --- bisection helpers ---
    p.add_argument(
        "--no-party",
        action="store_true",
        dest="no_party",
        help="drop every party (bus B) frame -- isolate bus A for canDataBusA bisect",
    )
    # NOTE: the old global --party-period flatten is GONE. Party-frame rates are now per-frame
    # CONSTANTS baked into the nodes: the MIA-owning nodes (rcm/esp/ibst/epas3p) transmit at
    # sim_core.PARTY_LIVENESS_S (the ~100Hz group2 CANB MIA-clear floor); non-MIA party frames
    # (das/unknown) stay native. Tune the one constant in sim_core, not a CLI knob per run.
    p.add_argument(
        "--only",
        default=None,
        help="send ONLY these arbitration IDs (comma CSV, hex e.g. '0x221,0x3a1')",
    )
    p.add_argument(
        "--exclude",
        default=None,
        help="send everything EXCEPT these arbitration IDs (comma CSV, hex)",
    )
    p.add_argument(
        "--set",
        dest="set_overrides",
        default=None,
        help="override payload bitfields for VDC/ESP status-field bench bisection. Format: "
        "'ID:start:width=value[,ID:start:width=value...]' (ID hex; start bit-index + width decimal; "
        "value dec or 0x). Layered onto the payload BEFORE counter/checksum so the checksum covers "
        "it. E.g. --set '0x185:52:4=0xF,0x105:13:1=1'. Candidate fields: see the ESP node.",
    )
    # UI-command options (packed into UI_powertrainControl / UI_chassisControl / UI_trackModeSettings)
    p.add_argument(
        "--pedal-map",
        choices=["chill", "sport", "performance"],
        default="chill",
        help="UI_pedalMap",
    )
    p.add_argument(
        "--stopping-mode",
        choices=["standard", "creep", "hold"],
        default="standard",
        help="UI_stoppingMode (regen stopping behavior)",
    )
    p.add_argument(
        "--motor-on-mode",
        choices=["normal", "front", "rear"],
        default="normal",
        help="UI_motorOnMode",
    )
    p.add_argument("--track-mode", choices=["off", "on"], default="off", help="UI_trackModeRequest")
    p.add_argument(
        "--winch-mode",
        choices=["idle", "enter", "exit"],
        default="idle",
        help="UI_winchModeRequest",
    )
    p.add_argument("--trailer-mode", action="store_true", help="UI_trailerMode ON")
    p.add_argument(
        "--traction-mode",
        choices=["normal", "slip_start", "rolls", "dyno"],
        default="normal",
        help="UI_tractionControlMode",
    )
    p.add_argument(
        "--immo-key",
        dest="immo_key",
        default=None,
        help="16-byte hex immobilizer key (override). If omitted, the DI board S/N is read "
        "over UDS and its key looked up in immo_keys.json; --no-immo disables the responder.",
    )
    p.add_argument(
        "--immo-node",
        dest="immo_node",
        default=None,
        help="board S/N to pick the immo key from immo_keys.json (skips the UDS board-ID read)",
    )
    p.add_argument(
        "--no-immo",
        action="store_true",
        dest="no_immo",
        help="don't answer the 0x276 immobilizer challenge (liveness only)",
    )
    p.add_argument(
        "--lv-power-state",
        dest="lv_power_state",
        choices=list(_VEHICLE_POWER_STATE),
        default="drive",
        help="VCFRONT_vehiclePowerState in 0x221 (default drive). Try 'accessory' -- it may let "
        "the immo reach REQUEST without the continuous DRIVE poke that reverts DISARMED.",
    )
    p.add_argument("--no-watch", action="store_true", help="don't print 0x118 DI status")
    p.add_argument(
        "--control-port",
        type=int,
        default=8770,
        help="HTTP port for the tm3web command server (gear/lv/pedal/car-config); 0 disables it",
    )
    _cfg.apply_defaults(p)
    args = p.parse_args()

    if args.list_nodes:
        # --list-nodes runs before the config file loads, so its fw comes from --fw / TM3_FW.
        try:
            list_fw = _resolve_fw_target(args.fw, None)
        except ValueError as e:
            p.error(str(e))
        print(f"firmware target: {_fw_label(list_fw)}")
        ctx = sim_core.NodeContext(db=CanDatabase())
        for node in sim_registry.instantiate(sim_registry.NODES, ctx):
            fr = node.frames_for(list_fw)
            ids = (
                ", ".join(f"0x{f.can_id:03X}({f.bus[0]},{1.0 / f.period_s:.0f}Hz)" for f in fr)
                or "(no periodic frames)"
            )
            print(f"{node.name:9s}[{node.resolved_fw(list_fw)}]: {ids}")
        return

    # Runtime immobilizer responder key (0x276 -> 0x3D9). Reads the DI board S/N over UDS to pick
    # the matching key from immo_keys.json (or use --immo-key / --immo-node). Off if --no-immo.
    immo_key: bytes | None = None
    immo_sn: str | None = None
    immo_note = ""
    if not args.no_immo:
        try:
            immo_key, immo_sn, immo_note = resolve_di_key(
                Keystore(),
                args.channel,
                args.interface,
                node=args.immo_node,
                explicit_key_hex=args.immo_key,
            )
        except Exception as e:  # noqa: BLE001
            immo_note = f"key resolution failed: {e}"

    # One CAN DB + node context. Each selected node OWNS its state (gear stalk, UI, LV, EPB,
    # car-config, immobilizer); the driver seeds the drive scenario + externalities below.
    db = CanDatabase()
    ctx = sim_core.NodeContext(db=db)

    # Top-level bench config (TOML): [nodes] sim/real selection + [bus] id->bus overrides.
    # Auto-load ./sim.toml if present; --config overrides the path. CLI flags compose.
    cfg_path = args.config_path or (
        str(sim_registry.DEFAULT_CONFIG_PATH) if sim_registry.DEFAULT_CONFIG_PATH.exists() else None
    )
    bench = sim_registry.BenchConfig()
    if cfg_path:
        try:
            bench = sim_registry.load_bench_config(cfg_path)  # ValueError covers TOMLDecodeError
        except (OSError, ValueError) as e:
            p.error(f"--config {cfg_path}: {e}")
        print(f"config: {cfg_path}")

    # Target firmware revision -> which authored message set each node transmits (newest set
    # <= target, clamped to the oldest; None => newest per node). --fw > [firmware] > TM3_FW.
    try:
        fw_target = _resolve_fw_target(args.fw, bench.fw)
    except ValueError as e:
        p.error(str(e))

    # ---- node selection: --profile preset + config [nodes], then --real / --sim (CLI wins).
    # Legacy whole-node flags become --real aliases; --no-das/--no-221 stay ID-level drops.
    prof = _PROFILES.get(args.profile, {})
    real_names: set[str] = set(prof.get("real", [])) | {x.upper() for x in bench.real}
    prof_exclude: set[int] = set(prof.get("exclude", []))
    if args.no_shifter:
        real_names.add("SCCM")  # hand the gear stalk to di.py
    if args.no_gtw:
        real_names.add("GTW")
    if args.no_ui:
        real_names.add("UI")
    if args.real_nodes:
        real_names |= {x.strip() for x in args.real_nodes.replace(",", " ").split()}
    if args.sim_nodes:
        sim_names = args.sim_nodes.replace(",", " ").split()
    else:
        sim_names = list(bench.sim) if bench.sim else None
    try:
        node_classes = sim_registry.select_nodes(sim=sim_names, real=sorted(real_names))
    except ValueError as e:
        p.error(str(e))

    # Instantiate the selected nodes and inject the DRIVE-scenario externalities the driver
    # owns: LV power state, UI knobs, immobilizer key. (Deselected nodes are simply absent.)
    nodes = sim_registry.instantiate(node_classes, ctx)
    # Tell every selected node its target firmware -> behavior (frame encode + rx decode/
    # response) resolves against node.fw. State (immo key, gear, LV, ...) is shared, not
    # versioned; only the on-wire projection + reactions do.
    for _n in nodes:
        _n.fw = fw_target
    by_name = {n.name: n for n in nodes}
    if "VCFRONT" in by_name:
        by_name["VCFRONT"].set_lv(args.lv_power_state)
    if "UI" in by_name:
        ui_node = by_name["UI"]
        ui_node.set_ui("pedal_map", args.pedal_map)
        ui_node.set_ui("stopping_mode", args.stopping_mode)
        ui_node.set_ui("motor_on_mode", args.motor_on_mode)
        ui_node.set_ui("traction_mode", args.traction_mode)
        ui_node.set_ui("winch_mode", args.winch_mode)
        ui_node.set_ui("track_mode", args.track_mode)
        ui_node.set_ui("trailer_mode", "on" if args.trailer_mode else "off")
    if "VCSEC" in by_name:
        by_name["VCSEC"].immo_key = immo_key
    immo_active = immo_key is not None and "VCSEC" in by_name

    # Orchestrator: apply the [scenario.<NODE>] initial state from the bench config. This is
    # how a charge (or any) scenario is driven -- it pokes each selected node's owned state via
    # node.configure() (EVSE-connect, charge limits, HVP mode, gear, ...), overriding the CLI
    # seeds above for the nodes it addresses. A scenario for a deselected node is skipped.
    for scen_node, settings in bench.scenario.items():
        node = by_name.get(scen_node)
        if node is None:
            print(f"  scenario: node {scen_node} not selected -- skipping its [scenario] block")
            continue
        try:
            node.configure(**settings)
        except (ValueError, KeyError, TypeError) as e:
            p.error(f"[scenario.{scen_node}]: {e}")

    # tm3web pushes commands via the control server; a facade routes each to the node that
    # owns that state (SCCM gear / VCFRONT LV / UI knobs / GTW car-config).
    ctrl_srv = (
        _start_control_server(_ControlFacade(by_name), args.control_port)
        if args.control_port
        else None
    )

    # BUS BINDING IS RIGID (firmware-confirmed, 12603 gen-26): each frame's bus is fixed by its
    # node, but the bench config's [bus] map can reassign a specific ID (different bench wiring).
    frames = sim_registry.collect_frames(nodes)  # each node inherits its own fw (set above)
    if args.vdc_esp:
        # ESP VDC signal-status inputs the DIR consumes (bits 0-4) but which nothing on a
        # DU-only bench transmits. These are EXTERNAL inputs, NOT board-TX: the PMR party-TX
        # set = {0x108,0x118,0x148,0x256,0x257,0x286}, no 0x1E5/0x240 TX or immediate, and the
        # DIR RXes both. Idle payloads reproduce a captured party-bus idle trace (0x1E5 ctr0
        # -> 00 0c..00 f2; 0x240 ctr0 -> 72 30). Opt-in until bench-confirmed.
        _rate = sim_core.PARTY_RATE_S["ESP"]
        frames.append(
            sim_core.SimFrame(
                "ESP_0x1E5_vdc", 0x1E5, _rate,
                lambda: bytearray([0x00, 0x0C, 0, 0, 0, 0, 0, 0]),
                53, 56, counter_width=2, bus="party",  # DIR reads (word3>>5)&3 = 2-bit @ byte6 b5-6
            )
        )
        frames.append(
            sim_core.SimFrame(
                "ESP_0x240_vdc", 0x240, _rate,
                lambda: bytearray([0x00, 0x30]),
                8, 0, counter_width=4, bus="party",
            )
        )
    for f in frames:
        if f.can_id in bench.bus:
            f.bus = bench.bus[f.can_id]

    only = {int(x, 16) for x in args.only.replace(" ", "").split(",") if x} if args.only else None
    excl = (
        {int(x, 16) for x in args.exclude.replace(" ", "").split(",") if x}
        if args.exclude
        else set()
    )

    # Legacy ID/bus drops that don't map to a whole node (kept for exact back-compat):
    # --no-das drops only the provisional DAS_control 0x2B9 (DAS_status2 0x389 stays);
    # --no-221 drops the VCFRONT LV frame; --no-party drops the whole party bus.
    drop: set[int] = set(prof_exclude)
    if args.no_das:
        drop.add(0x2B9)
    if args.no_221:
        drop.add(0x221)
    if drop:
        frames = [f for f in frames if f.can_id not in drop]
    if args.no_party:
        frames = [f for f in frames if f.bus != "party"]
    if only is not None:
        frames = [f for f in frames if f.can_id in only]
    if excl:
        frames = [f for f in frames if f.can_id not in excl]
    # Party-frame periods are per-frame constants (sim_core.PARTY_LIVENESS_S for MIA frames);
    # nothing rewrites them here -- the scheduler transmits each frame at its own fixed period.

    # MIA-aggregate coverage: an aggregate the DIR clears only when ALL members arrive can
    # never clear if we send only some. Count IDs we send + IDs owned by a --real node
    # (present on the bus for real) + profile handoffs (--exclude'd because di.py sends them).
    covered = {f.can_id for f in frames} | prof_exclude
    for name in real_names:
        cls = sim_registry.BY_NAME.get(name.upper())
        if cls is not None:
            covered |= {f.can_id for f in sim_registry.collect_frames([cls(ctx)], fw_target)}
    mia_warnings = sim_registry.mia_coverage_warnings(covered)

    # --set bitfield overrides (VDC/ESP status-field bench bisection) -- apply after selection so
    # they attach to whatever frames survived the drops/--only/--exclude/--no-party.
    overrides = _parse_set_overrides(args.set_overrides)
    for f in frames:
        if f.can_id in overrides:
            f.overrides = overrides[f.can_id]
    unmatched = sorted(set(overrides) - {f.can_id for f in frames})
    if unmatched:
        print(
            "  WARNING: --set targets not in the active frame list (dropped by selection or not "
            "sent): " + ", ".join(f"0x{c:03X}" for c in unmatched)
        )

    # Per-bus channels: CLI wins, else the configured defaults (config.py / .env).
    party_channel = args.party_channel or _cfg.PARTY_CHANNEL
    charge_channel = args.charge_channel or _cfg.CHARGE_CHANNEL

    # BRIDGING IS INVALID (firmware-confirmed): a party ID sent on bus A is tagged CANA and
    # DROPPED -- its MIA can never clear. Refuse by default rather than silently producing
    # "party MIAs never clear". (Skip the guard when selection left no party frames.)
    if any(f.bus == "party" for f in frames) and not party_channel and not args.allow_bridged:
        p.error(
            "--party-channel is required: the party ECUs (rcm 0x101/0x111, esp 0x145, "
            "ibst 0x39D, epas3p 0x3D1, das 0x2B9) are group2/CANB and are DROPPED by the DIR "
            "if transmitted on bus A. Pass --party-channel canX (or set TM3_PARTY_CHANNEL), "
            "or --allow-bridged to override for debugging."
        )

    # Open one python-can bus per LOGICAL bus in use. The vehicle bus is always opened (it
    # carries the immo RX + control loop). A party/charge frame with no channel falls back
    # to the vehicle bus (party => guarded above; charge => warned below).
    used_buses = {f.bus for f in frames}
    bus_a = can.Bus(interface=args.interface, channel=args.channel)
    buses = {"vehicle": bus_a}
    if "party" in used_buses:
        buses["party"] = (
            can.Bus(interface=args.party_interface or args.interface, channel=party_channel)
            if party_channel
            else bus_a
        )
    if "charge" in used_buses:
        if charge_channel:
            buses["charge"] = can.Bus(
                interface=args.charge_interface or args.interface, channel=charge_channel
            )
        else:
            buses["charge"] = bus_a
            print(
                "  WARNING: a message is mapped to the 'charge' bus but no --charge-channel/"
                "TM3_CHARGE_CHANNEL is set; it will ride the vehicle bus (DIR may drop it)"
            )
    # Run on the shared ecu_bench engine: ONE Scheduler (single timer thread, per-frame bus
    # routing) + a Notifier that fans every received frame out to the nodes' on_rx. Each
    # SimFrame becomes an ecu_bench.Frame whose builder calls sf.frame(); TX ok/err counting
    # lives in BenchState.send.
    state = ecu_bench.BenchState(bus_a, db, buses=buses)
    # Sends are NON-blocking: a stalled TX drops (counted in tx_err/tx_err_by_id) rather than
    # blocking the scheduler thread -- blocking would stall the whole party cadence during a DIR
    # blip and trip every group2 MIA. The rolling-counter rollback (SimFrame.note_send) keeps a
    # dropped frame from desyncing the on-wire counter, so drops stay cheap.
    ecu_frames = sim_registry.to_bench_frames(frames)

    # Global RX dispatch table: arbitration id -> [handler(data, send)], built ONCE from the
    # selected nodes' rx_handlers(). An inbound frame -- from the bus (real hardware) or an
    # internal sim-node broadcast -- goes straight to only its registered handlers (EPB on
    # DI_epbRequest, VCSEC on the 0x276 immo challenge, VCFRONT/BMS/HVP on the charge cascade);
    # a non-reactive ID is a dict miss (no per-frame scan of every node). One lock serializes
    # the Notifier thread (bus RX) and the scheduler thread (internal emit) against node-state
    # races. state.send is bound to the vehicle bus.
    rx_dispatch: dict[int, list] = {}
    for node in nodes:
        for cid, cb in node.rx_handlers().items():
            rx_dispatch.setdefault(cid, []).append(cb)
    rx_lock = threading.Lock()

    def _dispatch(can_id: int, data: bytes, blocking: bool = True) -> None:
        cbs = rx_dispatch.get(can_id)
        if not cbs:
            return
        # The scheduler (on_emit) path is NON-blocking: if the Notifier is mid-burst holding
        # the lock, skip -- the next periodic broadcast re-triggers -- so RX never stalls TX.
        if not rx_lock.acquire(blocking=blocking):
            return
        try:
            for cb in cbs:
                try:
                    cb(data, state.send)
                except Exception as e:  # noqa: BLE001 -- a handler bug must not kill the bus
                    print(f"  rx handler error on 0x{can_id:03X}: {e}")
        finally:
            rx_lock.release()

    class _NodeRx(can.Listener):
        def __init__(self) -> None:
            self._last: str | None = None

        def on_message_received(self, msg: can.Message) -> None:
            if msg.is_error_frame or msg.is_remote_frame:
                return
            data = bytes(msg.data)
            _dispatch(msg.arbitration_id, data)
            if not args.no_watch and msg.arbitration_id == 0x118 and len(data) >= 8:
                line = _decode_0x118(data)
                if line != self._last:
                    print(f"  0x118  {line}")
                    self._last = line

        def stop(self) -> None:
            pass

    notifier = can.Notifier(bus_a, [_NodeRx()])  # one RX listener dispatches to the handlers
    # ONE Scheduler PER BUS (each node registered its frames' bus): a busy bus -- party at
    # 100Hz -- runs on its own thread and can't stall another bus's TX cadence. Every sim-node
    # broadcast is fed (NON-BLOCKING) to the matching handlers so the charge session emerges
    # from the nodes, and an RX-processing burst on the Notifier can't stall the schedulers.
    frames_by_bus: dict[str, list] = {}
    for f in ecu_frames:
        frames_by_bus.setdefault(f.bus, []).append(f)
    scheds = [
        ecu_bench.Scheduler(
            state, frs, on_emit=lambda cid, d, _bus: _dispatch(cid, d, blocking=False)
        )
        for frs in frames_by_bus.values()
    ]
    for s in scheds:
        s.start()

    # Firmware target + which authored revision each selected node resolved to (surfaces the
    # fallback: a node with no set at/below the target uses the newest set that is <= it).
    resolved: dict[str, list[str]] = {}
    for node in nodes:
        resolved.setdefault(str(node.resolved_fw(fw_target)), []).append(node.name)
    rev_summary = "; ".join(
        f"{rev} ({len(names)} node{'s' if len(names) != 1 else ''})"
        for rev, names in sorted(resolved.items())
    )
    print(f"firmware target: {_fw_label(fw_target)} -> {rev_summary}")

    bus_a_ids = ", ".join(f"0x{sf.can_id:03X}" for sf in frames if sf.bus == "vehicle")
    bus_b_ids = ", ".join(f"0x{sf.can_id:03X}" for sf in frames if sf.bus == "party")
    charge_ids = ", ".join(f"0x{sf.can_id:03X}" for sf in frames if sf.bus == "charge")
    mode = "bus B = " + party_channel if party_channel else "bridged onto bus A"
    print(f"bus A ({args.channel}): {bus_a_ids}")
    print(f"bus B ({mode}): {bus_b_ids}")
    if charge_ids:
        print(f"charge ({charge_channel or 'bridged onto bus A'}): {charge_ids}")
    # Deterministic TX-rate summary: every frame's period is a fixed constant now (no
    # --party-period flatten), so the per-bus load is knowable up front and logged here.
    # Party MIA frames run at sim_core.PARTY_LIVENESS_S; --list-nodes shows every frame's Hz.
    for _bus in ("vehicle", "party", "charge"):
        _bfr = [f for f in ecu_frames if f.bus == _bus]
        if not _bfr:
            continue
        _fps = sum(1000.0 / f.interval_ms for f in _bfr)
        _extra = (
            f" (party liveness {1.0 / sim_core.PARTY_LIVENESS_S:.0f}Hz)" if _bus == "party" else ""
        )
        print(f"  rate {_bus:8s}: {len(_bfr)} frame(s), ~{_fps:.0f} fps aggregate{_extra}")
    # Warn on a small CAN TX queue: at ~1000 fps a default qlen (10) overflows on the slightest
    # USB drain hiccup -> send() ENOBUFS (counted as tx_err) even though the bus is error-free
    # (ip -s shows TX dropped 0 -- the frame bounced at the full qdisc, never reached the wire).
    # Those drops are what trip the group2 MIAs, so surface it up front.
    for _ch in {args.channel, party_channel, charge_channel}:
        if not _ch:
            continue
        try:
            with open(f"/sys/class/net/{_ch}/tx_queue_len") as _fh:
                _qlen = int(_fh.read().strip())
        except (OSError, ValueError):
            continue
        if _qlen < 128:
            print(
                f"  WARNING: {_ch} txqueuelen={_qlen} is small -- bursty TX overflows the qdisc "
                f"(ENOBUFS drops that trip group2 MIAs, NOT a DIR/bus fault). "
                f"Fix: sudo ip link set {_ch} txqueuelen 1000"
            )
    for w in mia_warnings:
        print(f"  MIA WARNING: {w} (unless another transmitter sources it)")
    simulated_inv = sorted(sim_registry.INVERTER_NODES & set(by_name))
    if simulated_inv:
        print(
            f"  WARNING: SIMULATING inverter node(s) {', '.join(simulated_inv)} -- if a real "
            f"DIR/PMR is connected this WILL collide (canDataBusB). Mark it present with "
            f"--config scenarios/drive.toml (or --real {','.join(simulated_inv)})."
        )
    if overrides:
        shown = "; ".join(
            f"0x{cid:03X}[" + ",".join(f"{s}:{w}={v:#x}" for s, w, v in flds) + "]"
            for cid, flds in sorted(overrides.items())
        )
        print(f"  --set overrides (VDC/ESP status bisection): {shown}")
    if not args.no_gtw:
        print("  NOTE: GTW 0x7FF is PROVISIONAL -- if configMismatch appears, rerun with --no-gtw")
    if not (args.no_gtw and args.no_das and args.no_ui):
        print("  NOTE: GTW/DAS/UI IDs+bus are unconfirmed vs 2020/12603 fw (pending RE)")
    if bus_b_ids and not party_channel:
        print(
            "  WARNING: running BRIDGED -- every party frame above will be DROPPED by the DIR; "
            "the party MIAs (rcm/esp/ibst/epas3p) CANNOT clear in this mode"
        )
    if immo_active:
        print(
            f"  immo responder ON: 0x{IMMO_CHALLENGE_ID:03X} -> 0x{IMMO_RESPONSE_ID:03X} "
            f"(key {immo_key.hex()[:8]}... via {immo_note}) -- watch 0x118 for immo=DISARMED"
        )
    elif immo_key is not None:
        print("  immo responder OFF: VCSEC node not selected (--real VCSEC / --sim without it)")
    elif not args.no_immo:
        print(
            f"  immo responder OFF ({immo_note or 'no key'}; --immo-key/--immo-node to enable DISARM)"
        )
    print("Ctrl-C to stop.\n")

    def _tx_report() -> str:
        # Diagnostic read of the scheduler's per-bus counters; all-fail on a bus == nothing
        # is ACKing it (no peer / miswired / unterminated). The per-ID breakdown attributes a
        # spike: a few IDs => something specific; every ID => a bus event / overrun.
        lines = []
        for name in ("vehicle", "party", "charge"):
            if name in buses:
                ok, err = state.tx_ok.get(name, 0), state.tx_err.get(name, 0)
                flag = "  <-- NO ACK?" if err and not ok else ""
                lines.append(f"  tx {name:8s} ok={ok} err={err}{flag}")
        with state._tx_lock:
            top = sorted(state.tx_err_by_id.items(), key=lambda kv: -kv[1])[:8]
            runs = sorted(state.tx_err_run_max.items(), key=lambda kv: -kv[1])
        if top:
            lines.append("  tx err by id: " + ", ".join(f"0x{c:03X}={n}" for c, n in top))
        # Worst CONSECUTIVE drop run per ID -- a run >1 is what trips a group2 MIA (a gap in that
        # member's stream); scattered single drops (run=1) are harmless, so only surface run>1.
        bursts = [(c, n) for c, n in runs if n > 1][:8]
        if bursts:
            lines.append("  worst drop run: " + ", ".join(f"0x{c:03X}={n}" for c, n in bursts))
        return "\n".join(lines)

    # Reactive work (immo, EPB, 0x118 watch) runs in the Notifier's listeners; the main thread
    # just prints periodic TX health until Ctrl-C.
    try:
        while True:
            time.sleep(10.0)
            print(_tx_report())
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        for s in scheds:
            s.stop()
        notifier.stop()
        if ctrl_srv is not None:
            ctrl_srv.shutdown()
        for b in set(buses.values()):  # distinct python-can buses (party/charge may alias vehicle)
            b.shutdown()


if __name__ == "__main__":
    main()
