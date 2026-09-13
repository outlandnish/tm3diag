"""Tests for vapi_emu: decoding by RUNNING the firmware's own decoder.

No firmware needed. The Cracker is driven against a hand-built ELF holding a
handful of real x86-64 instructions that stand in for crackMessage -- enough to
pin the calling convention, the SSE value register, the bus argument and the PLT
interception. Everything above it (the catalog join, the cache, the batch API,
the fallback to the recovered layouts) runs against a stub pool.
"""
import asyncio
import struct
from concurrent.futures import Future

import pytest

import vapi_emu
from vapi_emu import VapiDatabase, VapiPool, _int_value

unicorn = pytest.importorskip("unicorn")

# ---------------------------------------------------------------------------
# A synthetic libQtCarVAPI
# ---------------------------------------------------------------------------

_BASE = 0x1000                  # .text
_PLT = 0x2000
_OTHER_SLOT = _PLT + 0x00       # a PLT call that is NOT storeSignalValue
_STORE_SLOT = _PLT + 0x10

_SYM = "_ZN13GUICanCracker12crackMessageEiiPh"


def _elf_bytes(code: bytes, sym: str = _SYM) -> bytes:
    """A minimal ELF64 shared object: .text + .plt + .dynsym/.dynstr.

    Only the parts so_candata.ElfImage actually reads, so a Cracker can be
    pointed at real instructions without a 14 MB firmware library.
    """
    shstr = bytearray(b"\0")
    name_off = {"": 0}
    for n in (".shstrtab", ".text", ".plt", ".dynsym", ".dynstr"):
        name_off[n] = len(shstr)
        shstr += n.encode() + b"\0"

    dynstr = bytearray(b"\0")
    sym_off = len(dynstr)
    dynstr += sym.encode() + b"\0"
    # entry 0 is the mandatory null symbol; entry 1 is crackMessage itself
    dynsym = bytes(24) + struct.pack(
        "<IBBHQQ", sym_off, 0x12, 0, 2, _BASE, len(code))

    plt = b"\x90" * 0x20        # never executed: the hook redirects RIP first
    o_text = 0x40
    o_plt = o_text + len(code)
    o_dynsym = o_plt + len(plt)
    o_dynstr = o_dynsym + len(dynsym)
    o_shstr = o_dynstr + len(dynstr)
    o_shdr = (o_shstr + len(shstr) + 7) & ~7

    def shdr(name, typ, flags, addr, off, size, link=0):
        return struct.pack("<IIQQQQIIQQ", name_off[name], typ, flags, addr,
                           off, size, link, 0, 1, 0)

    shdrs = b"".join([
        bytes(64),
        shdr(".shstrtab", 3, 0, 0, o_shstr, len(shstr)),
        shdr(".text", 1, 6, _BASE, o_text, len(code)),
        shdr(".plt", 1, 6, _PLT, o_plt, len(plt)),
        shdr(".dynsym", 11, 0, 0, o_dynsym, len(dynsym), link=5),
        shdr(".dynstr", 3, 0, 0, o_dynstr, len(dynstr)),
    ])
    ehdr = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack(
        "<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, o_shdr, 0, 64, 56, 0, 64, 6, 1)

    out = bytearray(ehdr)
    assert len(out) == o_text
    out += code + plt + dynsym + dynstr + shstr
    out += bytes(o_shdr - len(out))
    return bytes(out + shdrs)


class _Asm:
    """Just the encodings these fixtures need, assembled at _BASE."""

    def __init__(self):
        self.b = bytearray()

    def mov_rbx_rcx(self):                       # the payload argument
        self.b += b"\x48\x89\xcb"
        return self

    def load_byte(self, n):                      # movzx eax, byte ptr [rbx+n]
        self.b += b"\x0f\xb6\x43" + bytes([n])
        return self

    def to_double(self, reg="eax"):              # cvtsi2sd xmm0, <reg>
        self.b += {"eax": b"\xf2\x0f\x2a\xc0",
                   "r8d": b"\xf2\x41\x0f\x2a\xc0",
                   "r9d": b"\xf2\x41\x0f\x2a\xc1"}[reg]
        return self

    def mov_esi(self, imm):                      # the signal key
        self.b += b"\xbe" + imm.to_bytes(4, "little")
        return self

    def mov_edx(self, imm):                      # the valid flag
        self.b += b"\xba" + imm.to_bytes(4, "little")
        return self

    def save_args(self):                         # r8d = bus, r9d = message id
        self.b += b"\x41\x89\xf0\x41\x89\xd1"
        return self

    def call(self, target):
        rel = target - (_BASE + len(self.b) + 5)
        self.b += b"\xe8" + rel.to_bytes(4, "little", signed=True)
        return self

    def ret(self):
        self.b += b"\xc3"
        return self


def _payload_code(stores):
    """Store payload byte N as a double, once per (byte, key, valid)."""
    a = _Asm().mov_rbx_rcx()
    for byte, key, valid in stores:
        a.load_byte(byte).to_double().mov_esi(key).mov_edx(valid).call(_STORE_SLOT)
    return a.call(_OTHER_SLOT).ret().b        # a non-store call, must be ignored


def _cracker(tmp_path, code, name="libQtCarVAPI.so.1.0.0"):
    p = tmp_path / name
    p.write_bytes(_elf_bytes(bytes(code)))
    return vapi_emu.Cracker(p)


_K1, _K2 = 0xAA01, 0xAA02


@pytest.fixture
def cracker(tmp_path):
    return _cracker(tmp_path, _payload_code([(2, _K1, 1), (5, _K2, 0)]))


class TestCracker:
    def test_a_decode_returns_key_value_and_valid_per_store(self, cracker):
        # One assertion covers the lot: the payload reached rbx from rcx, the
        # byte offsets are honoured, the value came out of xmm0 as a double, the
        # key out of esi and the valid flag out of edx -- and the trailing
        # non-store PLT call was stubbed rather than recorded.
        got = cracker.run(0x123, bytes([0, 0, 42, 0, 0, 7, 0, 0]))
        assert got == ((_K1, 42.0, 1), (_K2, 7.0, 0))

    def test_the_most_called_plt_slot_is_taken_as_storeSignalValue(self, cracker):
        assert cracker.store_plt == _STORE_SLOT

    def test_a_short_payload_is_zero_padded(self, cracker):
        # Real frames run from DLC 1 to 8; the firmware always reads 8 bytes, so
        # the shim pads deterministically rather than reading whatever is there.
        assert cracker.run(0x123, bytes([0, 0, 9])) == ((_K1, 9.0, 1), (_K2, 0.0, 0))

    def test_bus_3_and_the_message_id_arrive_in_the_right_registers(self, tmp_path):
        # crackMessage(this, bus, msgId, payload) -- and it decodes for bus 3
        # and nothing else, so the shim always passes 3 whatever bus the frame
        # was seen on.
        a = _Asm().mov_rbx_rcx().save_args()
        a.to_double("r8d").mov_esi(_K1).mov_edx(1).call(_STORE_SLOT)
        a.to_double("r9d").mov_esi(_K2).mov_edx(1).call(_STORE_SLOT)
        c = _cracker(tmp_path, a.ret().b)
        assert c.run(0x2A5, bytes(8)) == ((_K1, 3.0, 1), (_K2, 677.0, 1))

    def test_reuse_leaves_no_residue(self, cracker):
        # The cache and the worker pool both assume a decode is a pure function
        # of (id, payload). It is: an instance hammered with other traffic still
        # answers the first payload identically.
        first = cracker.run(0x123, bytes([0, 0, 1, 0, 0, 2, 0, 0]))
        for i in range(50):
            cracker.run(0x200 + i, bytes([i] * 8))
        assert cracker.run(0x123, bytes([0, 0, 1, 0, 0, 2, 0, 0])) == first

    def test_a_faulting_decode_reports_none_not_a_partial_answer(self, tmp_path):
        # Storing one signal and then running off into unmapped memory must not
        # look like "the firmware stored exactly one signal".
        a = _Asm().mov_rbx_rcx()
        a.load_byte(0).to_double().mov_esi(_K1).mov_edx(1).call(_STORE_SLOT)
        a.call(0x8000)                              # past the mapped image
        c = _cracker(tmp_path, a.ret().b)
        assert c.run(0x123, bytes(8)) is None
        assert c.fault


# ---------------------------------------------------------------------------
# The database on top
# ---------------------------------------------------------------------------

_DBC = """VERSION ""


BU_: TESTER

BO_ 256 TESTMSG: 8 TESTER
 SG_ SigA : 0|8@1+ (1,0) [0|255] "degC" TESTER
 SG_ SigB : 8|8@1+ (1,0) [0|255] "" TESTER
 SG_ SigHash : 16|32@1+ (1,0) [0|0] "" TESTER

BO_ 512 OTHER: 8 TESTER
 SG_ SigA : 0|8@1+ (1,0) [0|255] "" TESTER

VAL_ 256 SigB 1 "ON" 0 "OFF" ;
"""

_MID, _OTHER_MID = 0x100, 0x200
# catalog keys -> (message, signal), the join the emulator's u32 keys need
_KEYS = {1: ("TESTMSG", "SigA"), 2: ("TESTMSG", "SigB"),
         3: ("TESTMSG", "SigHash"), 9: ("OTHER", "SigA")}
_CAT = {"TESTMSG": {"message_id": _MID}, "OTHER": {"message_id": _OTHER_MID}}


class _StubPool:
    """Stands in for the worker processes: canned hits, and a record of asks."""

    def __init__(self, table=None):
        self.table = table or {}
        self.batches: list[list] = []

    def run_many(self, items):
        self.batches.append(list(items))
        return [self.table.get(k) for k in items]

    def submit(self, items):
        fut: Future = Future()
        fut.set_result(self.run_many(items))
        return [fut]

    @property
    def asked(self):
        return [k for batch in self.batches for k in batch]

    def close(self):
        pass


def _db(tmp_path, table=None):
    p = tmp_path / "t.dbc"
    p.write_text(_DBC)
    db = VapiDatabase.from_dbc(p)
    db._pool = _StubPool(table)
    db._index(_KEYS, _CAT)
    return db


class TestCatalogJoin:
    def test_a_key_becomes_a_named_signal_with_units_and_validity(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((1, 21.5, 1),)})
        assert db.decode_frame(_MID, bytes(8)) == [
            {"signal": "SigA", "value": 21.5, "label": None,
             "units": "degC", "valid": True}]

    def test_an_enum_value_gets_its_label(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((2, 1.0, 1),)})
        assert db.decode_frame(_MID, bytes(8))[0]["label"] == "ON"

    def test_a_non_integral_value_gets_no_enum_label(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((2, 1.5, 1),)})
        assert db.decode_frame(_MID, bytes(8))[0]["label"] is None

    def test_a_hash_signal_renders_as_hex(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((3, float(0x1234), 1),)})
        assert db.decode_frame(_MID, bytes(8))[0]["label"] == "34120000"

    def test_the_firmwares_invalid_flag_is_carried_through(self, tmp_path):
        # Information the layout decoder structurally cannot produce -- it is
        # what stops tm3web latching a fault from a bad store.
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((1, 3.0, 0),)})
        assert db.decode_frame(_MID, bytes(8))[0]["valid"] is False

    def test_a_key_belonging_to_another_message_is_not_attributed_here(self, tmp_path):
        # Key 9 is OTHER.SigA. Seeing it while decoding TESTMSG would mean the
        # emulation leaked across messages, so it is dropped rather than renamed.
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((9, 5.0, 1), (1, 6.0, 1))})
        assert [r["signal"] for r in db.decode_frame(_MID, bytes(8))] == ["SigA"]
        assert db.decode_frame(_MID, bytes(8))[0]["value"] == 6.0

    def test_a_key_absent_from_the_catalog_is_dropped(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((999, 1.0, 1),)})
        assert db.decode_frame(_MID, bytes(8)) is None


class TestFallback:
    def test_a_faulted_decode_falls_back_to_the_recovered_layout(self, tmp_path):
        # None from the pool means the emulation broke. Showing the frame as
        # carrying nothing would be worse than decoding it from the DBC.
        db = _db(tmp_path, {(_MID, bytes([7] + [0] * 7)): None})
        rows = db.decode_frame(_MID, bytes([7] + [0] * 7))
        assert {"signal": "SigA", "value": 7, "label": None,
                "units": "degC"} in [{k: r[k] for k in
                                      ("signal", "value", "label", "units")}
                                     for r in rows]

    def test_a_message_that_stores_nothing_decodes_to_nothing(self, tmp_path):
        # Distinct from a fault: an empty tuple is a real answer, and the usual
        # one for a payload whose mux page carries no signals.
        db = _db(tmp_path, {(_MID, b"\0" * 8): ()})
        assert db.decode_frame(_MID, bytes(8)) is None


class TestCache:
    def test_a_repeated_payload_is_not_decoded_twice(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((1, 1.0, 1),)})
        db.decode_frame(_MID, bytes(8))
        db.decode_frame(_MID, bytes(8))
        assert db._pool.asked == [(_MID, b"\0" * 8)]

    def test_a_different_payload_is_decoded_again(self, tmp_path):
        db = _db(tmp_path, {})
        db.decode_frame(_MID, bytes(8))
        db.decode_frame(_MID, bytes([1] + [0] * 7))
        assert len(db._pool.asked) == 2

    def test_a_repeat_inside_one_batch_collapses_to_one_decode(self, tmp_path):
        db = _db(tmp_path, {(_MID, b"\0" * 8): ((1, 4.0, 1),)})
        rows = db.decode_frames([(_MID, bytes(8))] * 3)
        assert db._pool.asked == [(_MID, b"\0" * 8)]
        assert [r[0]["value"] for r in rows] == [4.0, 4.0, 4.0]

    def test_the_cache_is_bounded(self, tmp_path):
        db = _db(tmp_path, {})
        db._CACHE_MAX = 4
        for i in range(10):
            db.decode_frame(_MID, bytes([i] + [0] * 7))
        assert len(db._cache) == 4


class TestBatch:
    def test_results_line_up_with_the_input_order(self, tmp_path):
        # The pool chunks work across processes, so order is a real risk: a
        # mis-zip would silently show one message's signals under another.
        table = {(_MID, bytes([i] + [0] * 7)): ((1, float(i), 1),) for i in range(5)}
        db = _db(tmp_path, table)
        items = [(_MID, bytes([i] + [0] * 7)) for i in (3, 0, 4, 1, 2)]
        assert [r[0]["value"] for r in db.decode_frames(items)] == [3.0, 0.0, 4.0, 1.0, 2.0]

    def test_the_async_path_agrees_with_the_sync_one(self, tmp_path):
        table = {(_MID, bytes([i] + [0] * 7)): ((1, float(i), 1),) for i in range(3)}
        items = [(_MID, bytes([i] + [0] * 7)) for i in range(3)]
        sync = _db(tmp_path, table).decode_frames(items)
        got = asyncio.run(_db(tmp_path, table).decode_frames_async(items))
        assert got == sync

    def test_an_empty_batch_never_reaches_the_pool(self, tmp_path):
        db = _db(tmp_path, {})
        assert db.decode_frames([]) == []
        assert db._pool.batches == []


_CATALOG = {"messages": {
    "TESTMSG": {"message_id": _MID, "length_bytes": 8, "cycle_time": 100,
                "originNode": "TESTER", "senders": ["TESTER"],
                "signals": {"SigA": {"units": "degC"},
                            "SigB": {"value_description": {"ON": 1, "OFF": 0}}}},
}}


def _catalog_db(tmp_path, layouts=True):
    """A VapiDatabase built the way build() does: catalog first, layouts over."""
    db = VapiDatabase.__new__(VapiDatabase)
    db._ingest({"messages": {k: dict(v, signals=dict(v["signals"]))
                             for k, v in _CATALOG["messages"].items()}})
    db.layout_source = None
    if layouts:
        p = tmp_path / "t.dbc"
        p.write_text(_DBC)
        db._merge_layouts(p)
    return db


class TestCatalogFirst:
    """The catalog IS the database; layouts are an overlay for encoding.

    Nothing in the decode path needs a bit position, so a host with firmware and
    no built DBC still gets every name, unit and enum -- and the catalog's node
    spelling, which is the canonical one.
    """

    def test_the_catalog_alone_names_units_and_enums(self, tmp_path):
        db = _catalog_db(tmp_path, layouts=False)
        db._pool = _StubPool({(_MID, b"\0" * 8): ((1, 7.0, 1), (2, 1.0, 1))})
        db._index(_KEYS, _CAT)
        rows = {r["signal"]: r for r in db.decode_frame(_MID, bytes(8))}
        assert rows["SigA"]["units"] == "degC"
        assert rows["SigB"]["label"] == "ON"

    def test_without_layouts_there_is_nothing_to_encode_from(self, tmp_path):
        db = _catalog_db(tmp_path, layouts=False)
        # No start_position anywhere -> a signal contributes no bits rather than
        # silently landing at bit 0.
        assert db.encode_frame(_MID, {}) == bytes(8)

    def test_layouts_are_overlaid_without_losing_the_catalog(self, tmp_path):
        db = _catalog_db(tmp_path)
        sig = db.messages[_MID]["signals"]["SigA"]
        assert sig["start_position"] == 0 and sig["width"] == 8   # from the DBC
        assert sig["units"] == "degC"                             # from the catalog
        assert db.messages[_MID]["originNode"] == "TESTER"

    def test_encoding_works_once_layouts_are_overlaid(self, tmp_path):
        db = _catalog_db(tmp_path)
        assert db.encode_frame(_MID, {"SigA": 7, "SigB": 1}) == bytes([7, 1, 0, 0, 0, 0, 0, 0])

    def test_a_message_only_the_layout_source_has_is_kept(self, tmp_path):
        # The *_udsRequest/*_udsResponse pairs ship only in compact.json, so
        # preferring the catalog must not shrink coverage.
        db = _catalog_db(tmp_path)
        assert _OTHER_MID in db.messages
        assert db.messages[_OTHER_MID]["name"] == "OTHER"

    def test_the_layout_source_is_recorded(self, tmp_path):
        assert _catalog_db(tmp_path).layout_source.name == "t.dbc"
        assert _catalog_db(tmp_path, layouts=False).layout_source is None


class TestInherited:
    def test_encode_still_uses_the_recovered_layout(self, tmp_path):
        # crackMessage only decodes, so encode_frame is inherited untouched --
        # this is what keeps vehicle_sim transmitting the same bytes as before.
        db = _db(tmp_path, {})
        assert db.encode_frame(_MID, {"SigA": 7, "SigB": 1}) == bytes([7, 1, 0, 0, 0, 0, 0, 0])

    def test_encoding_does_not_touch_the_decoder(self, tmp_path):
        db = _db(tmp_path, {})
        db.encode_frame(_MID, {"SigA": 1})
        assert db._pool.batches == []


class TestPool:
    def test_work_is_split_one_chunk_per_worker(self):
        class _FakeExecutor:
            def __init__(self):
                self.chunks = []

            def submit(self, _fn, batch):
                self.chunks.append(batch)
                fut: Future = Future()
                fut.set_result([None] * len(batch))
                return fut

        pool = VapiPool("x.so", workers=3)
        pool._pool = _FakeExecutor()
        pool.submit([(i, b"") for i in range(9)])
        assert [len(c) for c in pool._pool.chunks] == [3, 3, 3]

    def test_a_pool_that_will_not_start_falls_back_in_process(self, monkeypatch):
        # A machine that cannot spawn processes should still decode, slowly,
        # rather than losing the shim entirely.
        monkeypatch.setattr(vapi_emu, "ProcessPoolExecutor",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no fork")))
        monkeypatch.setattr(vapi_emu, "Cracker", lambda path: "in-process")
        pool = VapiPool("x.so", workers=2)
        pool._ensure()
        assert pool._local == "in-process" and pool._pool is None


class TestIntValue:
    @pytest.mark.parametrize("value,want", [
        (3.0, 3), (0.0, 0), (-2.0, -2), (1.5, None),
        (float("nan"), None), (float("inf"), None)])
    def test_only_exact_integers_are_looked_up_in_a_value_table(self, value, want):
        assert _int_value(value) == want
