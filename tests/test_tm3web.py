"""Tests for scripts/tm3web.py logic that stands on its own.

Two areas: the ODIN live cross-reference (/api/seen-signals), which filters the
arrival-timestamp map by a freshness window and expands the surviving CAN ids to
the signal names they carry; and the flusher's handling of ids the signal DB does
not carry. Both run with app state injected directly -- no CAN bus, no reader
thread.
"""
import asyncio
import contextlib
import json
import time
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tm3web
from can_decoder import CanDatabase

# A tiny DBC fixture (one message, id 0x100) so these tests never need the MCU
# firmware compact JSON -- tm3web decodes from a DBC just as well.
_MINI_DBC = Path(__file__).parent / "fixtures" / "mini.dbc"


def _test_db() -> CanDatabase:
    return CanDatabase.from_dbc(_MINI_DBC)


@contextlib.asynccontextmanager
async def _client(seen_msg, msg_signals):
    app = web.Application()
    app["seen_msg"] = seen_msg
    app["msg_signals"] = msg_signals
    app.router.add_get("/api/seen-signals", tm3web._api_seen_signals)
    async with TestClient(TestServer(app)) as client:
        yield client


class TestSeenSignals:
    def test_fresh_ids_expand_to_signals_stale_dropped(self):
        async def body():
            now = time.monotonic()
            seen = {0x100: now, 0x200: now - 100.0}          # 0x200 is stale
            sig = {0x100: ("GTW_drivetrainType", "GTW_epasControlType"),
                   0x200: ("DI_gear",)}
            async with _client(seen, sig) as client:
                r = await client.get("/api/seen-signals")
                assert r.status == 200
                data = await r.json()
                assert data["window"] == tm3web._SEEN_WINDOW_S
                assert "GTW_drivetrainType" in data["signals"]
                assert "GTW_epasControlType" in data["signals"]
                assert "DI_gear" not in data["signals"]      # stale id filtered out
                assert data["signals"] == sorted(data["signals"])
        asyncio.run(body())

    def test_window_override_widens_freshness(self):
        async def body():
            now = time.monotonic()
            async with _client({0x200: now - 100.0}, {0x200: ("DI_gear",)}) as client:
                r = await client.get("/api/seen-signals?window=1000")
                data = await r.json()
                assert data["signals"] == ["DI_gear"]        # now within the window
                assert data["window"] == 1000.0
        asyncio.run(body())

    def test_empty_when_nothing_seen(self):
        async def body():
            async with _client({}, {}) as client:
                data = await (await client.get("/api/seen-signals")).json()
                assert data["signals"] == []
        asyncio.run(body())


# --------------------------------------------------------------------- flusher
class _OneShotHub:
    """Stands in for _BusHub: yields one drain() worth of frames, then nothing."""

    def __init__(self, label, frames):
        self.label = label
        self.last_sent = {}
        self._frames = frames

    def drain(self):
        latest = {aid: (data, 1.0) for aid, data in self._frames}
        counts = dict.fromkeys((aid for aid, _ in self._frames), 1)
        self._frames = []
        return latest, counts


class _Chan:
    def __init__(self):
        self.node = ""       # "" == every node
        self.paused = False
        self.sent = []

    def offer(self, payload):
        self.sent.append(payload)


async def _flush_once(frames, *, show_unknown=True, node=""):
    """Run one flusher tick over `frames`, returning the decoded frames a client got."""
    tm3web._DB = _test_db()
    chan = _Chan()
    chan.node = node
    app = {
        "hubs": [_OneShotHub("vcan0", frames)],
        "flush_interval": 0.01,
        "dash_sig": {}, "faults": {}, "alert_ids": {}, "seen_msg": {},
        # alertLog capture off: these tests are about frame forwarding, and the
        # decoder would need the MCU firmware libs.
        "alert_log": {}, "alert_decoder": None, "alertlog_ids": frozenset(),
        "show_unknown": show_unknown,
        "clients": {chan}, "raw_clients": set(),
    }
    task = asyncio.create_task(tm3web._flusher(app))
    await asyncio.sleep(0.08)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    out = []
    for payload in chan.sent:
        out.extend(json.loads(payload).get("frames", []))
    return out


def _an_unknown_id() -> int:
    """An id the shipped DB genuinely does not carry (0x25C is the sim's APP frame)."""
    db = _test_db()
    assert 0x25C not in db.messages
    return 0x25C


class TestUnknownIds:
    """A frame the DB can't name must still reach the viewer. Dropping it is how a
    node that IS transmitting comes to look dead -- the regression that hid the
    sim's APP/PTC nodes once they started sending ids the compact JSON lacks."""

    def test_unknown_id_is_forwarded_undecoded(self):
        async def body():
            unknown = _an_unknown_id()
            got = await _flush_once([(unknown, b"\xde\xad\xbe\xef")])
            frames = {f["msg_id"]: f for f in got}
            assert unknown in frames, "unknown id was dropped"
            f = frames[unknown]
            assert f["node"] == tm3web._UNKNOWN_NODE
            assert f["unknown"] == 1
            assert f["data"] == [0xDE, 0xAD, 0xBE, 0xEF]
        asyncio.run(body())

    def test_known_ids_still_decode_normally(self):
        async def body():
            db = _test_db()
            known = next(iter(db.messages))
            got = await _flush_once([(known, bytes(8))])
            frames = {f["msg_id"]: f for f in got}
            assert known in frames
            assert "unknown" not in frames[known]
            assert frames[known]["signals"], "known id should carry decoded signals"
        asyncio.run(body())

    def test_hide_unknown_restores_the_drop(self):
        async def body():
            unknown = _an_unknown_id()
            got = await _flush_once([(unknown, b"\x01")], show_unknown=False)
            assert unknown not in {f["msg_id"] for f in got}
        asyncio.run(body())

    def test_unknown_skipped_for_node_filtered_clients(self):
        """An unknown id has no originNode, so it can't satisfy a node subscription."""
        async def body():
            unknown = _an_unknown_id()
            got = await _flush_once([(unknown, b"\x01")], node="di")
            assert unknown not in {f["msg_id"] for f in got}
        asyncio.run(body())

    def test_repeated_unknown_id_is_marked_same(self):
        """Byte-identical repeats come back as a bare liveness tick, no payload."""
        async def body():
            unknown = _an_unknown_id()
            payload = b"\x01\x02"
            got = await _flush_once([(unknown, payload), (unknown, payload)])
            assert [f["msg_id"] for f in got].count(unknown) >= 1
        asyncio.run(body())


# --------------------------------------------------------------------- filter
class _Msg:
    """Minimal stand-in for can.Message: only what _TxFilter.drop looks at."""

    def __init__(self, arbitration_id, is_rx=True):
        self.arbitration_id = arbitration_id
        self.is_rx = is_rx


def _filter(**kw):
    kw.setdefault("tx_ids", set())
    kw.setdefault("ignore_ids", set())
    kw.setdefault("hide_host_tx", False)
    kw.setdefault("tx_hidden", False)
    return tm3web._TxFilter(**kw)


class TestTxFilter:
    """python-can sets is_rx=False from MSG_DONTROUTE, which SocketCAN raises for
    anything sent by ANY local process -- not just this socket (that is
    MSG_CONFIRM). Dropping on it therefore hides everything vehicle_sim
    broadcasts, which is what made the sim's nodes look dead in the viewer."""

    def test_locally_originated_frames_are_kept_by_default(self):
        f = _filter()
        assert not f.drop(_Msg(0x118, is_rx=False)), (
            "a frame from another local process (vehicle_sim) must not be dropped"
        )
        assert not f.drop(_Msg(0x118, is_rx=True))

    def test_hide_host_tx_drops_every_local_frame_when_asked(self):
        f = _filter(hide_host_tx=True)
        assert f.drop(_Msg(0x118, is_rx=False))
        assert not f.drop(_Msg(0x118, is_rx=True))

    def test_tx_ids_hide_and_unhide_by_id(self):
        f = _filter(tx_ids={0x132, 0x212}, tx_hidden=True)
        assert f.drop(_Msg(0x132))
        assert not f.drop(_Msg(0x118))
        f.set_tx_hidden(False)
        assert not f.drop(_Msg(0x132)), "unhiding must bring the ids back"
        f.set_tx_hidden(True)
        assert f.drop(_Msg(0x132))

    def test_ignore_ids_are_permanent(self):
        f = _filter(ignore_ids={0x700}, tx_ids={0x700}, tx_hidden=True)
        f.set_tx_hidden(False)
        assert f.drop(_Msg(0x700)), "ignore_ids survive the UI toggle"


class TestAlertLogCapture:
    """The alert-log store folds repeats and skips the idle broadcast.

    Uses a stub decoder so the test needs no firmware libs -- what is under test
    is tm3web's bookkeeping (dedupe key, count, first/last, cap), not the
    alert_log field packing (tests/test_alert_log.py covers that).
    """

    class _Dec:
        def decode(self, can_id, data):
            from alert_log import AlertLogDecode
            return AlertLogDecode(can_id=can_id, node="DIR", alert_code=data[0],
                                  alert=f"DIR_a{data[0]:03d}_stub")

    def test_identical_payloads_fold_into_one_entry(self):
        store, dec = {}, self._Dec()
        payload = bytes([0x5E, 0x80, 0xA1, 0x33, 0x1C, 0, 0, 0])
        tm3web._record_alertlog(store, dec, 0x5A5, payload, 100.0)
        tm3web._record_alertlog(store, dec, 0x5A5, payload, 101.5)
        assert len(store) == 1
        ent = next(iter(store.values()))
        assert ent["count"] == 2
        assert (ent["first"], ent["last"]) == (100.0, 101.5)
        assert ent["key"] == "5A5:5e80a1331c000000"

    def test_different_payloads_are_separate_entries(self):
        store, dec = {}, self._Dec()
        tm3web._record_alertlog(store, dec, 0x5A5, bytes([0x5E, 0x80, 1, 0, 0, 0, 0, 0]), 1.0)
        tm3web._record_alertlog(store, dec, 0x5A5, bytes([0x5E, 0x80, 2, 0, 0, 0, 0, 0]), 2.0)
        assert len(store) == 2

    def test_idle_all_zero_frame_is_ignored(self):
        """Every ECU broadcasts an empty alert log continuously."""
        store, dec = {}, self._Dec()
        tm3web._record_alertlog(store, dec, 0x5A5, bytes(8), 1.0)
        assert store == {}

    def test_store_is_capped_evicting_the_stalest(self):
        store, dec = {}, self._Dec()
        for i in range(tm3web._ALERT_LOG_CAP + 5):
            tm3web._record_alertlog(
                store, dec, 0x5A5,
                bytes([0x5E, 0x80, i & 0xFF, i >> 8, 0, 0, 0, 0]), float(i))
        assert len(store) == tm3web._ALERT_LOG_CAP
        assert min(e["last"] for e in store.values()) > 0.0, "oldest entries evicted"
