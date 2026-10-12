"""ecu_bench multi-bus generalization (Phase 3a).

The bench engine gained per-frame bus routing so vehicle_sim can share it. These
lock the additive contract: single-ECU benches (default bus="vehicle") are
unchanged, and a frame's bus routes to the matching python-can bus.
"""
from __future__ import annotations

import ecu_bench


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[int] = []

    def send(self, msg) -> None:
        self.sent.append(msg.arbitration_id)


def test_frame_defaults_to_vehicle_bus():
    f = ecu_bench.Frame("x", 0x100, 10, builder=lambda st: b"\x00" * 8)
    assert f.bus == "vehicle"


def test_benchstate_send_routes_by_logical_bus():
    veh, party = _FakeBus(), _FakeBus()
    state = ecu_bench.BenchState(veh, db=None, buses={"vehicle": veh, "party": party})
    state.send(0x100, b"\x00" * 8)  # default -> vehicle
    state.send(0x200, b"\x00" * 8, bus="party")
    state.send(0x300, b"\x00" * 8, bus="charge")  # unopened -> falls back to vehicle
    assert veh.sent == [0x100, 0x300]
    assert party.sent == [0x200]


def test_benchstate_defaults_to_single_vehicle_bus():
    veh = _FakeBus()
    state = ecu_bench.BenchState(veh, db=None)  # no buses map -> {"vehicle": bus}
    assert state.buses == {"vehicle": veh}
    state.send(0x111, b"\x00" * 8, bus="party")  # no party bus -> vehicle
    assert veh.sent == [0x111]


def test_scheduler_emits_each_frame_on_its_bus():
    veh, party = _FakeBus(), _FakeBus()
    state = ecu_bench.BenchState(veh, db=None, buses={"vehicle": veh, "party": party})
    frames = [
        ecu_bench.Frame("v", 0x100, 10, builder=lambda st: b"\x00" * 8),
        ecu_bench.Frame("p", 0x200, 10, builder=lambda st: b"\x00" * 8, bus="party"),
    ]
    sched = ecu_bench.Scheduler(state, frames)
    # one manual tick of the scheduler's due logic (avoid thread timing flakiness)
    for f in frames:
        state.send(f.can_id, sched._payload(f), bus=f.bus)
    assert veh.sent == [0x100]
    assert party.sent == [0x200]


def test_send_returns_true_and_false_and_tallies_error():
    import can

    class _DeadBus:
        def send(self, msg, timeout=None):
            raise can.CanError("no ACK")

    state = ecu_bench.BenchState(_DeadBus(), db=None)
    assert state.send(0x123, b"\x00" * 8) is False
    assert state.tx_err.get("vehicle") == 1
    assert state.tx_err_by_id.get(0x123) == 1
    # a healthy bus returns True and does not touch the error tallies
    ok_state = ecu_bench.BenchState(_FakeBus(), db=None)
    assert ok_state.send(0x1, b"\x00") is True
    assert ok_state.tx_err == {}


def test_send_tracks_worst_consecutive_failure_run_per_id():
    import can

    class _FlakyBus:
        def __init__(self):
            self.fail = False

        def send(self, msg, timeout=None):
            if self.fail:
                raise can.CanError("blip")

    bus = _FlakyBus()
    state = ecu_bench.BenchState(bus, db=None)
    bus.fail = True
    state.send(0x101, b"\x00")
    state.send(0x101, b"\x00")           # 2 in a row
    bus.fail = False
    state.send(0x101, b"\x00")           # success resets the run
    bus.fail = True
    state.send(0x101, b"\x00")           # a shorter run of 1
    assert state.tx_err_by_id[0x101] == 3        # total drops
    assert state.tx_err_run_max[0x101] == 2      # worst consecutive run (what trips a MIA)


def test_scheduler_staggers_same_period_frames():
    # Frames sharing a (bus, period) must be phase-offset across that period so they don't
    # burst together and overrun the target ECU's RX (the party-bus no-ACK fix).
    import pytest

    veh = _FakeBus()
    state = ecu_bench.BenchState(veh, db=None)
    party = [ecu_bench.Frame(f"p{i}", 0x100 + i, 10, builder=lambda st: b"\x00", bus="party")
             for i in range(4)]
    vehicle = [ecu_bench.Frame(f"v{i}", 0x200 + i, 20, builder=lambda st: b"\x00")
               for i in range(2)]
    off = ecu_bench.Scheduler(state, party + vehicle)._stagger()
    # 4 party @ 10ms -> 0, 2.5, 5, 7.5 ms; 2 vehicle @ 20ms -> 0, 10 ms
    assert sorted(off[id(f)] for f in party) == pytest.approx([0.0, 0.0025, 0.005, 0.0075])
    assert sorted(off[id(f)] for f in vehicle) == pytest.approx([0.0, 0.010])
    # each offset stays within its own period (never >= interval)
    assert all(0 <= off[id(f)] < 0.010 for f in party)
    assert all(0 <= off[id(f)] < 0.020 for f in vehicle)
