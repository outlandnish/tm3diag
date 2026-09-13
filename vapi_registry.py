"""vapi_registry.py -- the firmware's own VAPI DataValue alias table.

Most ``VAPI_*`` CID data-values are not computed: the MCU registers each as a
``DataValue`` sourced from ONE ETH CAN signal and rendered through a typed
holder -- a bool, an integer, or an enum with a ``NameMap``. This module reads
that table straight out of ``libQtCarVAPI``'s static initializers, so the bench
can answer a CID data-value query the way the car does -- decode the source
signal (``crackMessage`` via ``vapi_emu`` / ``can_read``) and render it -- rather
than hand-writing each mapping.

Two layers of the VAPI stack, split by how a value is produced:

  * ALIAS     ``VAPI_shiftState <- ETH_DI_gear``, rendered by ``ShiftStateNameMap``
              (``{1:P, 2:R, 3:N, 4:D}``). A pure table -- this module, extracted
              once per firmware rev.
  * COMPUTED  ``VAPI_driveRailOn`` is set by ``LowVoltagePowerStateMessage`` from
              the 0x221 power-state byte -- code, not a table. Not here; those
              stay explicit in ``BenchBackend._derive_cid`` (or a handler-emulation
              layer on top of this one).

Extraction runs the lib's ``.init_array`` under Unicorn -- the same "run the real
thing instead of modelling it" approach as ``vapi_emu`` -- with every
``DataValue`` and ``QString`` constructor intercepted and nothing outside the
image mapped. The registered name and source arrive as ``QString``s (captured at
the ``fromAscii``/``fromUtf8`` factory), and the holder TYPE is read from the
``_ZTV<Type>`` relocation the code references right after each constructor -- a
relocation names its symbol even when the vtable itself is imported (the plain
``DataValue`` and ``BoolDataValue`` vtables live in another library), which is
why the type cannot be read from the constructed object's vptr.
"""

from __future__ import annotations

import contextlib
import json
import logging
import struct
from collections.abc import Callable
from pathlib import Path

import capstone

from so_candata import ElfImage

log = logging.getLogger(__name__)

_PAGE = 0x1000
# Disjoint scratch regions, well clear of the image's own address range.
_STACK = 0x0000_3000_0000_0000
_STACK_SZ = 0x80000
_RET = 0x0000_5000_0000_0000
_FOREIGN = 0x0000_6000_0000_0000       # placeholders for imported symbols
_DUMMY = 0x0000_7000_0000_0000         # every non-QString PLT call returns this
_HEAP = 0x0000_7100_0000_0000          # fake QString allocations
_HEAP_SZ = 0x0400_0000

# The generic DataValue ctor and the typed ones we still want to see. A name is a
# QString built by one of the factories below; both are matched by substring.
_DV_CTOR = ("DataValueC1", "DataValueC2")
_QSTRING_FACTORY = ("_ZN7QString16fromAscii_helper", "_ZN7QString8fromUtf8",
                    "_ZN7QString10fromLatin1", "_ZN7QString13fromUtf8_helper")

# ETH_ prefixes the source signal name in the registration; the catalog / DBC
# spell the same signal without it (ETH_DI_gear -> DI_gear).
_SRC_PREFIX = "ETH_"


def _align(a: int) -> int:
    return a & ~(_PAGE - 1)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

class _InitEmulator:
    """Runs libQtCarVAPI's ``.init_array`` and records every DataValue built.

    Each record is ``(obj, ctor, [qstrings], ret)``: the object pointer, the
    constructor's mangled name, the QString arguments in call order (arg 0 is the
    registered name, arg 1 -- when present -- the ETH source signal), and the
    return address, from which the holder type is read statically.
    """

    def __init__(self, path: str | Path) -> None:
        import unicorn as U

        self.U = U
        self.X = __import__("unicorn.x86_const", fromlist=["x"])
        self.elf = ElfImage(path)
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.regs: list[tuple[int, str, list[str], int]] = []
        self._qstr: dict[int, str] = {}      # fake QArrayData* -> text
        self._heap = _HEAP
        self._plt = self._plt_names()
        self._foreign: dict[str, int] = {}
        self._local_name = self._first_symbol_by_addr()
        self._map_image()

    # -- static tables -------------------------------------------------------
    def _plt_names(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for sec in (".plt", ".plt.sec"):
            s = self.elf.by_name.get(sec)
            if not s:
                continue
            code = self.elf.d[s["offset"]:s["offset"] + s["size"]]
            for ins in self.md.disasm(code, s["addr"]):
                if ins.mnemonic == "jmp" and "rip" in ins.op_str:
                    t = ins.address + ins.size + int(ins.op_str.split("+")[1].rstrip("]"), 16)
                    r = self.elf.reloc.get(t)
                    if r and r[0] == "sym":
                        out[ins.address] = r[1]
        return out

    def _first_symbol_by_addr(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for n, s in self.elf.syms.items():
            if s["value"]:
                out.setdefault(s["value"], n)
        return out

    # -- image + relocations -------------------------------------------------
    def _map_image(self) -> None:
        U, X, uc = self.U, self.X, self.U.Uc(self.U.UC_ARCH_X86, self.U.UC_MODE_64)
        self.uc = uc
        alloc = [s for s in self.elf.sections if s["addr"] and s["size"]]
        lo = _align(min(s["addr"] for s in alloc))
        hi = (max(s["addr"] + s["size"] for s in alloc) + _PAGE) & ~(_PAGE - 1)
        uc.mem_map(lo, hi - lo, U.UC_PROT_ALL)
        for s in alloc:
            if s["type"] != 8:               # skip SHT_NOBITS (.bss)
                uc.mem_write(s["addr"], self.elf.d[s["offset"]:s["offset"] + s["size"]])
        # Apply every relocation: a defined symbol resolves to its address, an
        # imported one to a per-name placeholder in _FOREIGN (so a stored vtable
        # ptr is still traceable back to its symbol name).
        for off, r in self.elf.reloc.items():
            if r[0] == "rel":
                v = r[1]
            else:
                base = self.elf.syms.get(r[1], {}).get("value")
                if base:
                    v = base + r[2]
                else:
                    self._foreign.setdefault(r[1], _FOREIGN + len(self._foreign) * 0x100)
                    v = self._foreign[r[1]] + r[2]
            uc.mem_write(off, struct.pack("<Q", v))
        uc.mem_map(_FOREIGN, (len(self._foreign) * 0x100 + _PAGE) & ~(_PAGE - 1), U.UC_PROT_ALL)
        for base, size in ((_STACK, _STACK_SZ), (_RET, _PAGE), (_DUMMY, 0x10000),
                           (_HEAP, _HEAP_SZ)):
            uc.mem_map(base, size, U.UC_PROT_ALL)
        # Signal values never appear in init, but ctors touch SSE registers.
        uc.reg_write(X.UC_X86_REG_CR0, (uc.reg_read(X.UC_X86_REG_CR0) & ~4) | 2)
        uc.reg_write(X.UC_X86_REG_CR4, uc.reg_read(X.UC_X86_REG_CR4) | (1 << 9) | (1 << 10))
        pl = [s for s in self.elf.sections
              if s["n"] in (".plt", ".plt.sec", ".plt.got", ".iplt")]
        uc.hook_add(U.UC_HOOK_CODE, self._on_plt,
                    begin=min(s["addr"] for s in pl),
                    end=max(s["addr"] + s["size"] for s in pl) - 1)

    # -- the PLT interception ------------------------------------------------
    def _cstr(self, ptr: int, n: int) -> str:
        raw = bytes(self.uc.mem_read(ptr, min(max(n, 0), 4096)))
        return raw.decode("latin1", "replace")

    def _as_qstring(self, ptr: int) -> str | None:
        try:
            d = struct.unpack("<Q", self.uc.mem_read(ptr, 8))[0]
        except self.U.UcError:
            return None
        return self._qstr.get(d)

    def _on_plt(self, uc, address, size, _user) -> None:
        X = self.X
        name = self._plt.get(address, "")
        rax = _DUMMY
        if name.startswith(_QSTRING_FACTORY):
            n = uc.reg_read(X.UC_X86_REG_ESI)
            n = n - (1 << 32) if n & 0x8000_0000 else n     # -1 => NUL-terminated
            rdi = uc.reg_read(X.UC_X86_REG_RDI)
            text = self._cstr(rdi, n if n >= 0 else self._cstr(rdi, 4096).find("\0"))
            rax = self._heap
            self._heap += 32
            uc.mem_write(rax, struct.pack("<iiIq", 1, len(text), 0, 24))
            self._qstr[rax] = text
        elif any(k in name for k in _DV_CTOR):
            obj = uc.reg_read(X.UC_X86_REG_RDI)
            args = [uc.reg_read(r) for r in (X.UC_X86_REG_RSI, X.UC_X86_REG_RDX,
                                             X.UC_X86_REG_RCX, X.UC_X86_REG_R8,
                                             X.UC_X86_REG_R9)]
            strs = [s for s in (self._as_qstring(a) for a in args) if s is not None]
            ret = struct.unpack("<Q", uc.mem_read(uc.reg_read(X.UC_X86_REG_RSP), 8))[0]
            self.regs.append((obj, name, strs, ret))
        uc.reg_write(X.UC_X86_REG_RAX, rax)
        rsp = uc.reg_read(X.UC_X86_REG_RSP)              # return without executing
        uc.reg_write(X.UC_X86_REG_RSP, rsp + 8)
        uc.reg_write(X.UC_X86_REG_RIP, struct.unpack("<Q", uc.mem_read(rsp, 8))[0])

    # -- run -----------------------------------------------------------------
    def run(self) -> list[tuple[int, str, list[str], int]]:
        U, X, uc, elf = self.U, self.X, self.uc, self.elf
        ia = elf.by_name.get(".init_array")
        if not ia:
            return []
        inits = [elf.ptr_target(ia["addr"] + k) for k in range(0, ia["size"], 8)]
        for fn in inits:
            if not fn:
                continue
            uc.mem_write(_STACK + _STACK_SZ - 0x100, struct.pack("<Q", _RET))
            uc.reg_write(X.UC_X86_REG_RSP, _STACK + _STACK_SZ - 0x100)
            uc.reg_write(X.UC_X86_REG_RBP, 0)
            try:
                uc.emu_start(fn, _RET, 20_000_000, 20_000_000)
            except U.UcError as exc:                     # a bad init just drops out
                log.debug("init %#x faulted: %s", fn, exc)
        return self.regs

    # -- type from the post-ctor vtable reference ----------------------------
    def type_at(self, ret: int) -> str | None:
        """The ``_ZTV<Type>`` a registration installs, read from the first vtable
        GOT/PC-relative reference after its ctor returns. Returns the demangled
        holder type (``ShiftStateDataValue``), or None if none is found nearby."""
        off = self.elf.v2o(ret)
        if off is None:
            return None
        for ins in self.md.disasm(self.elf.d[off:off + 0x40], ret):
            if ins.mnemonic in ("mov", "lea") and "rip + " in ins.op_str:
                t = ins.address + ins.size + int(ins.op_str.split("rip + ")[1].split("]")[0], 16)
                r = self.elf.reloc.get(t)
                sym = r[1] if r and r[0] == "sym" else self._local_name.get(t)
                if sym and sym.startswith("_ZTV"):
                    return sym[4:].lstrip("0123456789")
            if ins.mnemonic == "call":                   # next ctor -- stop
                break
        return None


def _namemaps(elf: ElfImage) -> dict[str, dict[int, str]]:
    """``<Base>NameMap`` tables in .data: 16 bytes/entry -- value(+0), label*(+8),
    terminated by ``(0, NULL)``. Keyed by the enum base (``ShiftState``)."""
    out: dict[str, dict[int, str]] = {}
    for name, s in elf.syms.items():
        if not name.endswith("NameMap") or not s["value"] or not s["size"]:
            continue
        table: dict[int, str] = {}
        for k in range(0, s["size"], 16):
            a = s["value"] + k
            o = elf.v2o(a)
            if o is None:
                break
            val = struct.unpack_from("<i", elf.d, o)[0]
            ptr = elf.ptr_target(a + 8)
            if ptr is None:
                continue                                 # the (0, NULL) terminator
            table[val] = elf.cstr(ptr, 80)
        if table:
            out[name[:-len("NameMap")]] = table
    return out


def _render_kind(type_name: str | None, enums: dict[str, dict[int, str]]):
    """(kind, enum) for a holder type: 'enum' with its table, 'bool', or 'num'."""
    if not type_name or not type_name.endswith("DataValue"):
        return "num", None
    base = type_name[:-len("DataValue")]
    if base in enums:
        return "enum", enums[base]
    if base in ("Bool",) or type_name == "BoolDataValue":
        return "bool", None
    return "num", None


def extract(vapi_lib: str | Path) -> dict:
    """Build the alias table for one ``libQtCarVAPI``.

    Returns a JSON-friendly dict::

        {"lib": <name>,
         "aliases": {name: {"source": sig, "render": "enum"|"bool"|"num",
                            "enum": {int: label}?}},
         "unresolved": [names whose holder type could not be read]}

    An alias is a registration with a second QString argument -- the ETH source
    signal. Registrations with no source (computed handlers set those) are left
    out; they are not aliases.
    """
    emu = _InitEmulator(vapi_lib)
    records = emu.run()
    enums = _namemaps(emu.elf)
    aliases: dict[str, dict] = {}
    unresolved: list[str] = []
    for _obj, _ctor, strs, ret in records:
        if len(strs) < 2 or not strs[1].startswith(_SRC_PREFIX):
            continue                                     # not a single-signal alias
        name, source = strs[0], strs[1][len(_SRC_PREFIX):]
        if not name or name in aliases:
            continue
        type_name = emu.type_at(ret)
        kind, enum = _render_kind(type_name, enums)
        if type_name is None:
            unresolved.append(name)
        entry: dict = {"source": source, "render": kind}
        if enum is not None:
            entry["enum"] = {str(k): v for k, v in enum.items()}
        aliases[name] = entry
    return {"lib": Path(vapi_lib).name, "aliases": aliases,
            "unresolved": sorted(unresolved)}


# ---------------------------------------------------------------------------
# the runtime table
# ---------------------------------------------------------------------------

_MEMO: dict[str, Registry] = {}


class Registry:
    """A firmware-derived ``VAPI_*`` alias table with the rendering the car uses.

    ``value(name, read)`` answers a CID data-value query: look up the alias, read
    its source signal off the bus via ``read(signal)`` (a number, or None when the
    bus has not carried it), and render -- an enum to its ``NameMap`` label, a
    bool to ``"true"``/``"false"``, anything else to the number. Returns None for
    a name that is not an alias, or whose source is absent or whose enum value is
    unlisted, so a caller falls back to a seed/stored value exactly as before.
    """

    def __init__(self, aliases: dict[str, dict], lib: str = "") -> None:
        self.lib = lib
        self._aliases: dict[str, dict] = {}
        for name, a in aliases.items():
            entry = {"source": a["source"], "render": a.get("render", "num")}
            if "enum" in a:
                entry["enum"] = {int(k): v for k, v in a["enum"].items()}
            self._aliases[name] = entry

    # -- lookup --------------------------------------------------------------
    def is_alias(self, name: str) -> bool:
        return name in self._aliases

    def source(self, name: str) -> str | None:
        a = self._aliases.get(name)
        return a["source"] if a else None

    def value(self, name: str, read: Callable[[str], float | int | None]):
        a = self._aliases.get(name)
        if a is None:
            return None
        raw = read(a["source"])
        if raw is None:
            return None
        if a["render"] == "enum":
            try:
                return a["enum"].get(int(raw))
            except (TypeError, ValueError):
                return None
        if a["render"] == "bool":
            return "true" if raw else "false"
        return raw

    def __len__(self) -> int:
        return len(self._aliases)

    # -- (de)serialization ---------------------------------------------------
    def to_dict(self) -> dict:
        out = {}
        for name, a in self._aliases.items():
            e = {"source": a["source"], "render": a["render"]}
            if "enum" in a:
                e["enum"] = {str(k): v for k, v in a["enum"].items()}
            out[name] = e
        return {"lib": self.lib, "aliases": out}

    @classmethod
    def from_dict(cls, data: dict) -> Registry:
        return cls(data.get("aliases", {}), data.get("lib", ""))

    @classmethod
    def empty(cls) -> Registry:
        return cls({})


def load_or_build(vapi_lib: str | Path | None, cache_path: str | Path | None = None) -> Registry:
    """A ``Registry`` for ``vapi_lib``, from ``cache_path`` when it is present and
    newer than the lib, else extracted (and cached if a path is given).

    Never raises: a missing lib, an unreadable cache, or an extraction failure
    (no Unicorn, say) all yield an empty registry, which simply derives no
    aliases -- the caller then falls back to its seeds and hand-coded values, as
    it did before this table existed.
    """
    if vapi_lib is None:
        return Registry.empty()
    key = str(vapi_lib)
    if key in _MEMO:
        return _MEMO[key]
    lib = Path(vapi_lib)
    cache = Path(cache_path) if cache_path else None
    if cache and cache.exists() and lib.exists() and cache.stat().st_mtime >= lib.stat().st_mtime:
        try:
            reg = Registry.from_dict(json.loads(cache.read_text()))
            _MEMO[key] = reg
            return reg
        except Exception as exc:                         # noqa: BLE001
            log.warning("vapi registry cache %s unreadable (%s); rebuilding",
                        cache.name, exc)
    try:
        data = extract(lib)
        reg = Registry.from_dict(data)
        if cache is not None:
            with contextlib.suppress(OSError):
                cache.write_text(json.dumps(data, indent=1))
    except Exception as exc:                             # noqa: BLE001
        log.warning("vapi registry unavailable (%s); aliases will not be derived", exc)
        reg = Registry.empty()
    _MEMO[key] = reg
    return reg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> None:
    import argparse

    import config as _cfg

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fw", help="firmware extraction root (default: TM3_ROOT)")
    ap.add_argument("--out", help="write the registry JSON here")
    ap.add_argument("--show", type=int, default=20, help="sample aliases to print")
    args = ap.parse_args(argv)

    libs = _cfg.vapi_libs(Path(args.fw).expanduser() if args.fw else None)
    if libs is None:
        raise SystemExit("no libQtCarVAPI found (pass --fw, or set TM3_ROOT)")
    data = extract(libs[0])
    aliases = data["aliases"]
    kinds: dict[str, int] = {}
    for a in aliases.values():
        kinds[a["render"]] = kinds.get(a["render"], 0) + 1
    print(f"{libs[0].name}: {len(aliases)} aliases  {kinds}  "
          f"unresolved-type={len(data['unresolved'])}")
    if data["unresolved"]:
        print("  unresolved:", ", ".join(data["unresolved"][:30]))
    for name in list(aliases)[:args.show]:
        a = aliases[name]
        extra = f" enum[{len(a['enum'])}]" if "enum" in a else ""
        print(f"  {name:40s} <- {a['source']:34s} {a['render']}{extra}")
    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=1))
        print("wrote", args.out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _cli()
