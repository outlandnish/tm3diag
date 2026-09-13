"""Decode CAN by RUNNING the MCU's own decoder, instead of modelling its layout.

``vapi_layout`` recovers bit positions by reading ``GUICanCracker::crackMessage``
out of ``libQtCarVAPI.so`` -- a static model that is now within a few dozen
signals of the whole catalog, but that is still a model. A wrong layout is
silent: it decodes, it just decodes the wrong thing.

This module runs the real function instead. The trick that makes it cheap is
that the only callee we care about, ``CANDataManager::storeSignalValue(key,
double, valid, bus)``, is an **UND import** -- it goes out through the PLT. So
the whole decode can be observed by mapping libQtCarVAPI's own sections into
Unicorn and stubbing every PLT slot, with none of the hundred-odd libraries it
links (Qt, gRPC, protobuf, abseil, ICU, ...) present. Nothing outside the image
is mapped, so a decode cannot reach anything real.

What comes back is ``key -> value``; the catalog in ``libQtCarCANData`` turns
keys into names. No bit positions are involved at runtime at all -- which is
exactly why this sees things the layout path cannot, in both directions:

    $ python vapi_emu.py parity            # 2026.8.3, 3 payloads per message
    same 19784   different 4   only-emu 755   only-dbc 212

The 4 are real DBC bugs (DAS_controlDistance decodes at 2x its true scale); the
212 are DBC signals the firmware does NOT store for that payload -- false
positives on a mux page, which no static check can see.

Layout still matters for the other direction: ``crackMessage`` only decodes, so
``VapiDatabase`` subclasses the DBC-backed ``CanDatabase`` and overrides decode
alone. ``encode_frame`` and everything vehicle_sim/ecu_bench rely on is
inherited untouched.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import logging
import math
import multiprocessing
import random
import struct
import sys
import threading
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any

import capstone
import unicorn as U
import unicorn.x86_const as X

import config as _cfg
import so_candata
import vapi_layout as V
from can_decoder import CanDatabase, _int_to_hex, _is_hex_signal
from so_candata import ElfImage

# _int_to_hex / _is_hex_signal are borrowed from the layout decoder rather than
# reimplemented, so a hash or CRC signal renders the same whichever path
# produced its value.

log = logging.getLogger(__name__)

_PAGE = 0x1000
_PAYLOAD = 0x0000_2000_0000
_STACK = 0x0000_3000_0000
_STACK_SZ = 0x40000
_THIS = 0x0000_4000_0000
_MAGIC_RET = 0x0000_5000_0000

# crackMessage decodes for bus 3 (the vehicle backbone) and nothing else: buses
# 0-2 and 4-7 store not one signal (verified across all 580 ids on 2026.8.3).
# The bus a frame physically arrived on is therefore irrelevant here, exactly as
# it is on the DBC path.
_BUS = 3

# Generous enough for the largest mux tree in the catalog, small enough that a
# runaway decode fails instead of hanging a flush tick.
_TIMEOUT_US = 5_000_000
_MAX_INSNS = 400_000

# Ceiling on how long a worker waits for its siblings to finish warming up.
_WARMUP_S = 60.0

# forkserver over spawn wherever it exists (everywhere but Windows). Both give a
# worker a clean interpreter, which plain `fork` would not -- and that matters,
# because the pool is built lazily and quite possibly after tm3web has started
# its reader threads. forkserver forks each worker from a warm server instead of
# exec'ing a fresh interpreter, which is why it warms faster (0.84s vs 1.00s for
# four workers); steady-state throughput is a wash.
#
# It does NOT change what a worker imports: both methods run spawn.prepare() in
# the child, which re-imports the caller's __main__ as __mp_main__ -- so a
# caller's module-level code runs once per worker either way, and an
# `if __name__ == "__main__"` guard only spares the guarded block, not the
# module body. Anything with a side effect (opening a CAN bus, say) belongs
# inside a function, not at module level.
_START_METHOD = ("forkserver" if "forkserver" in multiprocessing.get_all_start_methods()
                 else "spawn")


def _align(addr: int) -> int:
    return addr & ~(_PAGE - 1)


class Cracker:
    """``GUICanCracker::crackMessage`` under Unicorn, with the PLT stubbed out.

    ``storeSignalValue`` is recorded; every other PLT slot returns a dummy
    pointer, which is all the prologue's manager accessor needs. Each ``run``
    resets the argument registers and the stack but keeps the image, so an
    instance is reusable -- and provably reusable: 3,000 interleaved decodes
    leave no residue, and a fresh instance agrees with a hammered one exactly.
    """

    def __init__(self, path: str | Path, trace: bool = False) -> None:
        self.elf = ElfImage(path)
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.store_plt, _ = V.discover_store_plt(self.elf, self.md)
        self.entry = self.elf.syms[V.CRACK_SYM]["value"]
        self.uc = U.Uc(U.UC_ARCH_X86, U.UC_MODE_64)
        self.hits: list[tuple[int, float, int]] = []
        self.fault: str | None = None
        self.log: list[str] = []
        self._map_image()
        self._enable_sse()

        plt = [s for s in self.elf.sections
               if s["n"] in (".plt", ".plt.sec", ".plt.got", ".iplt")]
        self.uc.hook_add(U.UC_HOOK_CODE, self._on_plt,
                         begin=min(s["addr"] for s in plt),
                         end=max(s["addr"] + s["size"] for s in plt) - 1)
        self.uc.hook_add(U.UC_HOOK_MEM_INVALID, self._on_bad_mem)
        # A whole-image code hook fires a Python callback per instruction and
        # costs ~14% of decode time, so it is only wired up when asked for.
        self.trace = 0
        if trace:
            self.uc.hook_add(U.UC_HOOK_CODE, self._on_code)

    # -- setup ---------------------------------------------------------------
    def _map_image(self) -> None:
        alloc = [s for s in self.elf.sections if s["addr"] and s["size"]]
        lo = _align(min(s["addr"] for s in alloc))
        hi = (max(s["addr"] + s["size"] for s in alloc) + _PAGE) & ~(_PAGE - 1)
        self.uc.mem_map(lo, hi - lo, U.UC_PROT_ALL)
        for s in alloc:
            if s["type"] == 8:                       # SHT_NOBITS (.bss)
                continue
            self.uc.mem_write(s["addr"],
                              self.elf.d[s["offset"]:s["offset"] + s["size"]])
        for base, size in ((_PAYLOAD, _PAGE), (_STACK, _STACK_SZ),
                           (_THIS, _PAGE), (_align(_MAGIC_RET), _PAGE)):
            self.uc.mem_map(base, size, U.UC_PROT_ALL)

    def _enable_sse(self) -> None:
        """Signal values are doubles, so the decode is full of SSE."""
        uc = self.uc
        cr0 = uc.reg_read(X.UC_X86_REG_CR0)
        uc.reg_write(X.UC_X86_REG_CR0, (cr0 & ~(1 << 2)) | (1 << 1))   # EM=0 MP=1
        cr4 = uc.reg_read(X.UC_X86_REG_CR4)
        uc.reg_write(X.UC_X86_REG_CR4, cr4 | (1 << 9) | (1 << 10))     # OSFXSR

    # -- hooks ---------------------------------------------------------------
    def _on_plt(self, uc, address, size, _user) -> None:
        if address == self.store_plt:
            raw = uc.reg_read(X.UC_X86_REG_XMM0) & ((1 << 64) - 1)
            self.hits.append((uc.reg_read(X.UC_X86_REG_ESI),
                              struct.unpack("<d", struct.pack("<Q", raw))[0],
                              uc.reg_read(X.UC_X86_REG_EDX) & 0xFF))
        else:
            uc.reg_write(X.UC_X86_REG_RAX, _THIS)    # e.g. the manager pointer
        rsp = uc.reg_read(X.UC_X86_REG_RSP)          # return without executing
        uc.reg_write(X.UC_X86_REG_RSP, rsp + 8)
        uc.reg_write(X.UC_X86_REG_RIP, struct.unpack("<Q", uc.mem_read(rsp, 8))[0])

    def _on_bad_mem(self, uc, access, address, size, value, _user) -> bool:
        self.fault = f"bad mem {access} @ {address:#x}"
        return False

    def _on_code(self, uc, address, size, _user) -> None:
        if self.trace <= 0:
            return
        self.trace -= 1
        self.log.append(f"{address:#x}  {self._text(address)}")

    def _text(self, addr: int, n: int = 1) -> str:
        off = self.elf.v2o(addr)
        if off is None or not 0 <= off < len(self.elf.d):
            return ""
        ins = list(self.md.disasm(self.elf.d[off:off + 16 * n], addr))[:n]
        return " | ".join(f"{i.mnemonic} {i.op_str}" for i in ins)

    # -- the decode ----------------------------------------------------------
    def run(self, msg_id: int, payload: bytes) -> tuple[tuple[int, float, int], ...] | None:
        """Signals stored for one frame, or None if the emulation faulted.

        None (rather than a partial list) so a caller can tell "the firmware
        stored nothing for this payload" -- a real answer, and the usual one for
        an unmatched mux page -- apart from "the decode broke".
        """
        uc = self.uc
        self.hits, self.fault = [], None
        uc.mem_write(_PAYLOAD, payload.ljust(8, b"\0"))
        uc.mem_write(_STACK + _STACK_SZ - 0x100, struct.pack("<Q", _MAGIC_RET))
        uc.reg_write(X.UC_X86_REG_RSP, _STACK + _STACK_SZ - 0x100)
        uc.reg_write(X.UC_X86_REG_RDI, _THIS)        # this
        uc.reg_write(X.UC_X86_REG_RSI, _BUS)
        uc.reg_write(X.UC_X86_REG_RDX, msg_id)
        uc.reg_write(X.UC_X86_REG_RCX, _PAYLOAD)
        try:
            uc.emu_start(self.entry, _MAGIC_RET, _TIMEOUT_US, _MAX_INSNS)
        except U.UcError as exc:
            rip = uc.reg_read(X.UC_X86_REG_RIP)
            self.fault = f"{exc} @ {rip:#x}: {self._text(rip, 3)}"
        return None if self.fault else tuple(self.hits)


# ---------------------------------------------------------------------------
# Worker processes
# ---------------------------------------------------------------------------

_WORKER: Cracker | None = None


def _worker_init(path: str, barrier: Any = None) -> None:
    global _WORKER
    _WORKER = Cracker(path)
    if barrier is not None:
        # Hold every worker here until they have ALL built a Cracker. A pool
        # spawns processes only as work arrives, so without this the second and
        # later workers pay their ~0.9 s warmup inside the first decode batch
        # that reaches them -- which made 4 workers no faster than 1.
        with contextlib.suppress(Exception):         # a slow start is not fatal
            barrier.wait(timeout=_WARMUP_S)


def _worker_run(batch: list[tuple[int, bytes]]) -> list[tuple | None]:
    if _WORKER is None:                              # pragma: no cover
        raise RuntimeError("worker has no Cracker")
    return [_WORKER.run(mid, data) for mid, data in batch]


class VapiPool:
    """Lazily-started worker processes, one ``Cracker`` each.

    Processes rather than threads because the emulator calls back into Python
    on every PLT hit, so threads serialise on the GIL and get *slower* the more
    you add -- measured 1341 -> 735 -> 165 decodes/s at 1/2/4 threads. Each
    worker costs ~0.9 s to warm up, so the pool is built on the first decode:
    a tool that only ENCODES (vehicle_sim, ecu_bench) never pays for it.
    """

    def __init__(self, path: str | Path, workers: int = 4) -> None:
        self.path = str(path)
        self.workers = max(1, workers)
        self._pool: ProcessPoolExecutor | None = None
        self._local: Cracker | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._pool is not None or self._local is not None:
            return
        with self._lock:
            if self._pool is not None or self._local is not None:
                return
            try:
                ctx = multiprocessing.get_context(_START_METHOD)
                if _START_METHOD == "forkserver":
                    # Preload this module in the server so every forked worker
                    # inherits it (with capstone and unicorn) instead of
                    # importing it itself. The default, ['__main__'], preloads
                    # the caller's script instead -- which is of no use here.
                    ctx.set_forkserver_preload([__name__])
                pool = ProcessPoolExecutor(
                    max_workers=self.workers, mp_context=ctx,
                    initializer=_worker_init,
                    initargs=(self.path, ctx.Barrier(self.workers)))
                # One no-op per worker, so every process actually spawns and the
                # whole warmup lands here rather than inside the first decode.
                # Also surfaces a broken pool while there is still a fallback.
                for fut in [pool.submit(_worker_run, []) for _ in range(self.workers)]:
                    fut.result()
                self._pool = pool
                log.info("VAPI decode: %d worker process(es) on %s",
                         self.workers, Path(self.path).name)
            except Exception as exc:
                log.warning("VAPI worker pool unavailable (%s); "
                            "decoding in-process", exc)
                self._local = Cracker(self.path)
            atexit.register(self.close)

    def submit(self, items: list[tuple[int, bytes]]) -> list[Future]:
        """Futures covering ``items``, one chunk per worker."""
        self._ensure()
        if not items:
            return []
        if self._local is not None:
            done: Future = Future()
            done.set_result([self._local.run(mid, data) for mid, data in items])
            return [done]
        n = -(-len(items) // self.workers)
        return [self._pool.submit(_worker_run, items[i:i + n])
                for i in range(0, len(items), n)]

    def run_many(self, items: list[tuple[int, bytes]]) -> list[tuple | None]:
        out: list[tuple | None] = []
        for fut in self.submit(items):
            out.extend(fut.result())
        return out

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------

_MISS = object()

# What a layout source contributes that the catalog cannot: where a signal sits
# and how to scale it. Everything else (units, enums, node) stays the catalog's.
_LAYOUT_KEYS = ("start_position", "width", "endianness", "signedness",
                "scale", "offset", "min", "max", "is_muxer", "mux_id")


def _int_value(v: float) -> int | None:
    """``v`` as an int when it is exactly one -- enum and hash lookups only."""
    return int(v) if math.isfinite(v) and v == int(v) else None


class VapiDatabase(CanDatabase):
    """A ``CanDatabase`` whose DECODE comes from the firmware, not from layouts.

    Names, units, enum tables, originNode and -- crucially -- ``encode_frame``
    still come from the generated DBC, because ``crackMessage`` only runs one
    way. Overriding decode alone is what keeps vehicle_sim, ecu_bench and
    tesla_frames on exactly the code they use today.
    """

    _CACHE_MAX = 8192

    @classmethod
    def build(cls, vapi: str | Path, candata: str | Path,
              layouts: str | Path | None = None,
              workers: int | None = None) -> VapiDatabase:
        """Build from the firmware's own catalog, with layouts only if given.

        Decoding needs no layout at all -- names, units, enum tables, DLC,
        cycle time and originNode all come out of ``libQtCarCANData`` -- so the
        catalog IS the database. ``layouts`` (a generated DBC, else
        compact.json) is layered on top purely for the two things running the
        decoder cannot do: ``encode_frame``, and decoding a frame whose
        emulation faulted.
        """
        candata = Path(candata)
        db = cls.__new__(cls)
        db._ingest(so_candata.to_compact_dict(
            so_candata.extract_catalog(candata), product=_cfg.PRODUCT))
        db.layout_source = None
        if layouts is not None:
            try:
                db._merge_layouts(Path(layouts))
            except Exception as exc:
                # Layouts are an optional overlay, so an unreadable one (an
                # encrypted compact.json with no TM3_BIN_KEY, say) must not cost
                # the decode path -- which never needed them.
                log.warning("no layouts from %s (%s); decode is unaffected, "
                            "but encode_frame has nothing to work from",
                            Path(layouts).name, exc)
        db._attach(vapi, candata, workers)
        return db

    def _merge_layouts(self, path: Path) -> None:
        """Overlay bit layouts from a DBC or compact.json onto the catalog.

        The catalog wins on everything it knows (its node names are the
        canonical spelling, its units and enums come from the same build), so
        only the layout keys are taken. A message the catalog does not carry --
        the *_udsRequest/*_udsResponse pairs ship only in compact.json -- is
        added whole, so coverage never shrinks by preferring the catalog.
        """
        src = (CanDatabase.from_dbc(path) if path.suffix == ".dbc"
               else CanDatabase(path))
        self.layout_source = path
        for mid, msg in src.messages.items():
            mine = self.messages.get(mid)
            if mine is None:
                self.messages[mid] = msg
                self._by_node.setdefault(msg.get("originNode", "unknown"), []).append(mid)
                continue
            for sname, sig in msg.get("signals", {}).items():
                target = mine["signals"].get(sname)
                if target is None:
                    mine["signals"][sname] = sig
                else:
                    target.update({k: v for k, v in sig.items()
                                   if k in _LAYOUT_KEYS})

    def _attach(self, vapi: str | Path, candata: str | Path,
                workers: int | None) -> None:
        self.vapi_lib = Path(vapi)
        self._pool = VapiPool(self.vapi_lib, workers or _cfg.VAPI_WORKERS)
        # The catalog is read here, in the parent, so the workers stay tiny and
        # stateless -- all they ever return is (key, value, valid).
        self._index(*V.catalog_index(candata))

    def _index(self, keys: dict[int, tuple[str, str]], cat: dict[str, dict]) -> None:
        """Join the catalog's signal KEYS onto the DBC's names, units and enums.

        The emulator reports a u32 key per store and nothing else, so this is
        the whole of the naming: key -> (message id, signal), plus the enum and
        hash tables needed to render a value the way the layout decoder would.
        """
        mid_of = {name: m["message_id"] for name, m in cat.items()}
        self._key_sig: dict[int, tuple[int, str]] = {
            k: (mid_of[mname], sname) for k, (mname, sname) in keys.items()
            if mname in mid_of}
        self._enums: dict[tuple[int, str], dict[int, str]] = {}
        self._hex: set[tuple[int, str]] = set()
        for mid, msg in self.messages.items():
            for name, sig in msg["signals"].items():
                vd = sig.get("value_description")
                if vd:
                    self._enums[(mid, name)] = {int(v): lbl for lbl, v in vd.items()}
                # A hash/CRC renders as hex bytes, which needs to know how many
                # -- so it takes a width, and without a layout source there is
                # none. _is_hex_signal would say yes anyway (0 % 8 == 0).
                if sig.get("width") and _is_hex_signal(sig, name):
                    self._hex.add((mid, name))
        self._cache: OrderedDict[tuple[int, bytes], tuple | None] = OrderedDict()
        self._cache_lock = threading.Lock()

    # -- cache ---------------------------------------------------------------
    def _plan(self, items: list[tuple[int, bytes]]):
        """(slots, work, where) -- cached hits, the decodes still owed, and the
        slots each owed decode fills. Repeats within one batch collapse."""
        slots: list[Any] = [_MISS] * len(items)
        work: list[tuple[int, bytes]] = []
        where: dict[tuple[int, bytes], list[int]] = {}
        with self._cache_lock:
            for i, key in enumerate(items):
                if key in self._cache:
                    self._cache.move_to_end(key)
                    slots[i] = self._cache[key]
                elif key in where:
                    where[key].append(i)
                else:
                    where[key] = [i]
                    work.append(key)
        return slots, work, where

    def _store(self, slots: list[Any], work: list[tuple[int, bytes]],
               where: dict[tuple[int, bytes], list[int]],
               results: list[tuple | None]) -> None:
        with self._cache_lock:
            for key, hits in zip(work, results, strict=True):
                for i in where[key]:
                    slots[i] = hits
                self._cache[key] = hits
                if len(self._cache) > self._CACHE_MAX:
                    self._cache.popitem(last=False)

    # -- rendering -----------------------------------------------------------
    def _rows(self, mid: int, hits: tuple) -> list[dict[str, Any]]:
        sigs = self.messages.get(mid, {}).get("signals", {})
        rows = []
        for key, value, valid in hits:
            ent = self._key_sig.get(key)
            if ent is None or ent[0] != mid:
                continue                             # another message's signal
            name = ent[1]
            sig = sigs.get(name, {})
            raw = _int_value(value)
            if (mid, name) in self._hex:
                label = _int_to_hex(raw, sig["width"]) if raw is not None else None
            else:
                enum = self._enums.get((mid, name))
                label = enum.get(raw) if enum is not None and raw is not None else None
            rows.append({"signal": name, "value": value, "label": label,
                         "units": sig.get("units", ""), "valid": bool(valid)})
        return rows

    def _finish(self, mid: int, data: bytes, hits: Any) -> list[dict[str, Any]] | None:
        if hits is None or hits is _MISS:
            # The emulation broke. Fall back to the recovered layouts rather
            # than showing the frame as carrying nothing.
            return super().decode_frame(mid, data)
        rows = self._decode_alertlog(mid, data) + self._rows(mid, hits)
        return rows or None

    # -- public API ----------------------------------------------------------
    def decode_frames(self, items: list[tuple[int, bytes]]) -> list[list | None]:
        """Decode a whole batch -- one pool round trip instead of len(items)."""
        norm = [(mid, bytes(data)) for mid, data in items]
        slots, work, where = self._plan(norm)
        if work:
            self._store(slots, work, where, self._pool.run_many(work))
        return [self._finish(mid, data, h)
                for (mid, data), h in zip(norm, slots, strict=True)]

    async def decode_frames_async(self, items: list[tuple[int, bytes]]) -> list[list | None]:
        """``decode_frames`` without blocking the event loop."""
        import asyncio

        norm = [(mid, bytes(data)) for mid, data in items]
        slots, work, where = self._plan(norm)
        if work:
            chunks = await asyncio.gather(
                *(asyncio.wrap_future(f) for f in self._pool.submit(work)))
            self._store(slots, work, where, [h for c in chunks for h in c])
        return [self._finish(mid, data, h)
                for (mid, data), h in zip(norm, slots, strict=True)]

    def decode_frame(self, msg_id: int, data: bytes) -> list[dict[str, Any]] | None:
        return self.decode_frames([(msg_id, data)])[0]

    def close(self) -> None:
        self._pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_libs(root: str | None) -> tuple[Path, Path]:
    libs = _cfg.vapi_libs(Path(root).expanduser() if root else None)
    if libs is None:
        sys.exit("no libQtCarVAPI/libQtCarCANData found "
                 "(pass --fw <extraction root>, or set TM3_ROOT in .env)")
    return libs


def cmd_parity(args) -> None:
    """Decode the same payloads both ways and report where they disagree.

    The emulator is ground truth by construction, so every difference is a bug
    in the recovered layout -- and 'only-dbc' is the interesting column: signals
    the DBC emits that the firmware does not store for that payload, which no
    static check can see.
    """
    vapi, candata = _resolve_libs(args.fw)
    dbc = Path(args.dbc) if args.dbc else _cfg.ETH_DBC
    if not dbc:
        sys.exit("no DBC to compare against (build one with candata_to_dbc.py)")
    print(f"emulator: {vapi.name}\ncatalog : {candata.name}\ndbc     : {dbc}",
          file=sys.stderr)

    cracker = Cracker(vapi)
    keys, cat = V.catalog_index(candata)
    name_of = {k: v[1] for k, v in keys.items()}
    db = CanDatabase.from_dbc(dbc)
    rng = random.Random(args.seed)

    same = diff = only_emu = only_dbc = 0
    examples: list[tuple] = []
    for meta in cat.values():
        mid = meta["message_id"]
        for _ in range(args.samples):
            payload = bytes(rng.randbytes(8))
            hits = cracker.run(mid, payload) or ()
            emu = {name_of[k]: v for k, v, _valid in hits if k in name_of}
            dbc_v = {s["signal"]: s["value"] for s in (db.decode_frame(mid, payload) or [])}
            for name, value in emu.items():
                if name not in dbc_v:
                    only_emu += 1
                elif abs(value - dbc_v[name]) <= 1e-6 * max(1.0, abs(value)):
                    same += 1
                else:
                    diff += 1
                    if len(examples) < args.show:
                        examples.append((mid, name, value, dbc_v[name]))
            only_dbc += len(set(dbc_v) - set(emu))

    print(f"same {same}   different {diff}   only-emu {only_emu}   "
          f"only-dbc {only_dbc}")
    for mid, name, got, want in examples:
        print(f"  0x{mid:03X} {name:40s} emu={got!r:24s} dbc={want!r}")


def cmd_bench(args) -> None:
    vapi, candata = _resolve_libs(args.fw)
    _keys, cat = V.catalog_index(candata)
    ids = sorted({m["message_id"] for m in cat.values()})
    rng = random.Random(args.seed)
    work = [(ids[i % len(ids)], bytes(rng.randbytes(8))) for i in range(args.n)]

    import time
    pool = VapiPool(vapi, args.workers)
    pool.run_many(work[:1])                          # warm up outside the clock
    t0 = time.time()
    out = pool.run_many(work)
    dt = time.time() - t0
    faults = sum(1 for h in out if h is None)
    print(f"{args.n} decodes / {args.workers} worker(s): {dt:.2f}s "
          f"= {args.n / dt:.0f} decodes/s, {faults} faults")
    pool.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fw", help="firmware extraction root (default: TM3_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parity", help="compare emulator decode against the DBC")
    p.add_argument("--dbc", help="DBC to compare against (default: config.ETH_DBC)")
    p.add_argument("--samples", type=int, default=3, help="payloads per message")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--show", type=int, default=8, help="example differences to print")
    p.set_defaults(func=cmd_parity)

    p = sub.add_parser("bench", help="measure decode throughput")
    p.add_argument("-n", type=int, default=2000)
    p.add_argument("--workers", type=int, default=_cfg.VAPI_WORKERS)
    p.add_argument("--seed", type=int, default=9)
    p.set_defaults(func=cmd_bench)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
