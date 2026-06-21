"""Reusable ECU bench-emulation core: drive a target ECU on a CAN bus.

A *bench* makes a single ECU (DI inverter, PCS charger, ...) believe it is in a
real car: it continuously transmits the periodic frames that ECU expects from
its peers, optionally answers an immobilizer challenge, and exposes interactive
control verbs. Each ECU supplies only its data (a ``BenchSpec``); this module
owns the bus, the periodic scheduler, signal encoding, and the shell.

Per-ECU modules (e.g. ``scripts/di/di.py``) declare:

    FRAMES   : list[Frame]            # what to transmit, and how often
    CONTROLS : dict[str, callable]    # interactive verbs (gear, pedal, ...)
    IMMO     : ImmoSpec | None        # optional 0x276->0x3D9 responder config

and call ``ecu_bench.run(spec, args)``.

Frame payloads are built two ways:
  * ``builder``: a callable returning bytes / list[int] (for hand-packed or
    stateful frames — counters, the immobilizer, etc.), or
  * ``signals``: a dict of signal-name -> value encoded via the CAN DB
    (``can_decoder.CanDatabase.encode_frame``), so you name signals, not bits.

Live state shared between control verbs and frame builders lives in
``BenchState.vars`` — a plain dict the builders read and the verbs mutate.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import can

from can_decoder import CanDatabase

# ---------------------------------------------------------------------------
# Spec types — what each ECU module declares
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """One periodic (or one-shot) CAN frame the bench transmits.

    Exactly one of ``builder`` or ``signals`` should be set. ``builder`` takes
    the BenchState and returns bytes/list[int]; ``signals`` is a static or
    state-derived signal map encoded via the DB.
    """
    name: str
    can_id: int
    interval_ms: int
    builder: Callable[[BenchState], bytes | list[int]] | None = None
    signals: dict[str, float | int] | Callable[[BenchState], dict[str, float | int]] | None = None
    enabled: bool = True


@dataclass
class ImmoSpec:
    """Immobilizer responder config: answer ``challenge_id`` with ``response_id``."""
    node: str                      # keystore node name (e.g. "DIR")
    challenge_id: int = 0x276
    response_id: int = 0x3D9
    keystore_path: str | None = None


@dataclass
class BenchSpec:
    """Everything an ECU module hands to ecu_bench.run().

    ``on_init(state)`` runs once after the BenchState is created, before the
    scheduler starts — use it to seed default vars and to return the control
    verbs (it may return a dict of {name: callable} that is merged into
    ``controls``). This lets control verbs close over the live BenchState
    instead of a placeholder.
    """
    name: str
    frames: list[Frame]
    controls: dict[str, Callable[..., Any]] = field(default_factory=dict)
    immo: ImmoSpec | None = None
    db_path: str | None = None     # override CAN DB path (else config default)
    on_init: Callable[[BenchState], dict[str, Callable[..., Any]] | None] | None = None
    # When True, run an RX listener that decodes every inbound frame via the CAN
    # DB into state.signals (for closed-loop control like precharge).
    listen: bool = True
    # Optional hook for non-DB raw frames (e.g. IVT-S 0x521-0x523 int32 fields):
    # rx_hook(state, can_id, data) -> may write into state.signals directly.
    rx_hook: Callable[[BenchState, int, bytes], None] | None = None


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

class BenchState:
    """Mutable runtime shared by frame builders and control verbs.

    ``vars`` holds named values (gear, pedal, counters, ...) that control verbs
    set and builders read. ``encode`` is the DB signal encoder bound to this
    bench. ``send`` transmits a raw frame.
    """

    def __init__(self, bus: can.BusABC, db: CanDatabase) -> None:
        self.bus = bus
        self.db = db
        self.vars: dict[str, Any] = {}
        # Latest decoded value per signal name, populated by the RX listener.
        # Builders/controls read this for closed-loop behavior (e.g. precharge
        # waiting on a measured voltage, watching DI_immobilizerState).
        self.signals: dict[str, float | int] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.vars[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.vars.get(key, default)

    def signal(self, name: str, default: Any = None) -> Any:
        """Latest RX-decoded value for a signal name (None until first seen)."""
        with self._lock:
            return self.signals.get(name, default)

    def encode(self, msg: int | str, **signals: float | int) -> bytes:
        return self.db.encode_frame(msg, signals)

    def send(self, can_id: int, data: bytes | list[int]) -> None:
        msg = can.Message(
            arbitration_id=can_id,
            data=bytes(data),
            is_extended_id=False,
        )
        with contextlib.suppress(can.CanError):
            self.bus.send(msg)


# ---------------------------------------------------------------------------
# Periodic scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """Runs each enabled Frame on its own interval in a single timer thread.

    A single thread ticks at the GCD-ish base interval and emits each frame when
    due, which keeps the TX cadence steady without one thread per frame.
    """

    def __init__(self, state: BenchState, frames: list[Frame]) -> None:
        self._state = state
        self._frames = [f for f in frames if f.enabled]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # base tick = min interval, floored at 5 ms
        intervals = [f.interval_ms for f in self._frames] or [10]
        self._tick_ms = max(5, min(intervals))

    def _payload(self, f: Frame) -> bytes | list[int] | None:
        if f.builder is not None:
            return f.builder(self._state)
        if f.signals is not None:
            sig = f.signals(self._state) if callable(f.signals) else f.signals
            return self._state.db.encode_frame(f.can_id, sig)
        return None

    def _run(self) -> None:
        next_due = {id(f): 0.0 for f in self._frames}
        tick = self._tick_ms / 1000.0
        while not self._stop.is_set():
            now = time.monotonic()
            for f in self._frames:
                if now >= next_due[id(f)]:
                    payload = self._payload(f)
                    if payload is not None:
                        self._state.send(f.can_id, payload)
                    next_due[id(f)] = now + f.interval_ms / 1000.0
            self._stop.wait(tick)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ecu-sched")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


# ---------------------------------------------------------------------------
# Immobilizer responder
# ---------------------------------------------------------------------------

class _ImmoResponder(can.Listener):
    """Answers 0x276 with 0x3D9 = the paired immobilizer response."""

    def __init__(self, state: BenchState, spec: ImmoSpec) -> None:
        from uds_local.immobilizer import Keystore, challenge_response
        self._state = state
        self._spec = spec
        self._challenge_response = challenge_response
        ks = Keystore(spec.keystore_path) if spec.keystore_path else Keystore()
        entry = ks.get(spec.node)
        if entry is None:
            raise SystemExit(
                f"No stored immobilizer key for node {spec.node} in {ks.path}. "
                f"Pair first: scripts/di/immobilizer_handshake.py pair"
            )
        self._key = entry.key_bytes
        self.challenges = 0
        print(f"  Immobilizer: keyed for {spec.node}, "
              f"answering 0x{spec.challenge_id:03X} -> 0x{spec.response_id:03X}")

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame or msg.is_remote_frame:
            return
        if msg.arbitration_id == self._spec.challenge_id:
            self.challenges += 1
            resp = self._challenge_response(self._key, bytes(msg.data))
            self._state.send(self._spec.response_id, resp + bytes(8 - len(resp)))

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# RX listener — decode inbound frames into state.signals
# ---------------------------------------------------------------------------

class _RxCacheListener(can.Listener):
    """Decode every inbound frame via the CAN DB into ``state.signals``.

    Runs an optional ``rx_hook`` first for raw/non-DB frames. Closed-loop control
    verbs (e.g. precharge waiting on a measured voltage) read state.signals.
    """

    def __init__(self, state: BenchState,
                 rx_hook: Callable[[BenchState, int, bytes], None] | None) -> None:
        self._state = state
        self._rx_hook = rx_hook

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame or msg.is_remote_frame:
            return
        data = bytes(msg.data)
        if self._rx_hook is not None:
            self._rx_hook(self._state, msg.arbitration_id, data)
        decoded = self._state.db.decode_frame(msg.arbitration_id, data)
        if decoded:
            with self._state._lock:
                for s in decoded:
                    self._state.signals[s["signal"]] = s["value"]

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Interactive shell
# ---------------------------------------------------------------------------

def _make_shell_namespace(state: BenchState, spec: BenchSpec,
                          sched: Scheduler) -> dict[str, Any]:
    ns: dict[str, Any] = {
        "state": state,
        "bus": state.bus,
        "db": state.db,
        "send": state.send,
        "encode": state.encode,
        "signals": state.signals,   # live RX-decoded cache
        "frames": spec.frames,
        "scheduler": sched,
    }
    # Bind each control verb so the user calls e.g. gear("D"), pedal(25).
    ns.update(spec.controls)

    def help_() -> None:
        print(f"\n{spec.name} bench — control verbs:")
        for name, fn in spec.controls.items():
            doc = (fn.__doc__ or "").strip().splitlines()
            print(f"  {name}{_sig(fn)}  — {doc[0] if doc else ''}")
        print("  send(can_id, data) / encode(msg, **signals) / scheduler.stop()")
        print()

    ns["help"] = help_
    return ns


def _sig(fn: Callable) -> str:
    import inspect
    try:
        return str(inspect.signature(fn))
    except (ValueError, TypeError):
        return "(...)"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(spec: BenchSpec, channel: str, interface: str = "socketcan",
        *, interactive: bool = True) -> None:
    """Open the bus, start the scheduler + immobilizer, then drop into a shell."""
    db_path = spec.db_path
    db = CanDatabase(db_path) if db_path else CanDatabase()
    bus = can.Bus(interface=interface, channel=channel)
    notifier = can.Notifier(bus, [])
    state = BenchState(bus, db)

    # Let the ECU module seed defaults and bind control verbs to the live state.
    if spec.on_init is not None:
        extra_controls = spec.on_init(state)
        if extra_controls:
            spec.controls.update(extra_controls)

    if spec.listen:
        notifier.add_listener(_RxCacheListener(state, spec.rx_hook))

    immo = None
    if spec.immo is not None:
        immo = _ImmoResponder(state, spec.immo)
        notifier.add_listener(immo)

    sched = Scheduler(state, spec.frames)
    sched.start()
    enabled = [f for f in spec.frames if f.enabled]
    print(f"  {spec.name}: transmitting {len(enabled)} frame(s); "
          f"base tick {sched._tick_ms} ms.")

    try:
        if interactive:
            import code
            ns = _make_shell_namespace(state, spec, sched)
            banner = (f"\n{spec.name} bench ready. Type help() for verbs, "
                      f"Ctrl-D to stop.\n")
            try:
                from IPython import start_ipython  # type: ignore
                print(banner)
                start_ipython(argv=[], user_ns=ns)
            except ImportError:
                code.interact(banner=banner, local=ns, exitmsg="Stopping bench.")
        else:
            print("  Non-interactive: running until Ctrl-C.")
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        notifier.stop()
        bus.shutdown()
        if immo is not None:
            print(f"  Immobilizer challenges answered: {immo.challenges}")
