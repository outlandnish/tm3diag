"""Extract Tesla's CAN/ETH signal catalog from the MCU UI shared objects.

The infotainment UI stack ships ``libQtCarCANData.so``, which embeds the full
vehicle signal catalog as relocated data tables (no debug symbols needed --
the tables are *exported* objects):

    CANBusList          -> one "ETH" pseudo-bus descriptor, count + message array
    ETH_messages        -> array of 48-byte message descriptors
    ETH_<msg>_signals   -> per-message array of 40-byte signal descriptors
    Diag_<sig>_map      -> per-signal value->label enum tables

The catalog is *richer* than the per-revision ``Model3_ETH.compact.json`` (the
2022.45.15 build exposes 446 messages here vs 140 in compact.json), but it does
NOT carry the numeric bit-layout (start bit / width / scale / offset). Pair it
with :mod:`candata_to_dbc` to overlay layout from a compact.json donor.

Struct layouts (x86-64 LSB, reversed from the 2022.45.15 build and validated
against known Model 3 CAN IDs, e.g. BMS_kwhCounter == 0x3D2):

    message (48 bytes)              signal (40 bytes)
      +0x00  char*  name             +0x00  char*  name
      +0x08  u32    can_id           +0x08  u32    key (name hash)
      +0x0c  u32    dlc              +0x0c  u32    (pad)
      +0x10  u32    cycle_time_ms    +0x10  char*  units ("" if none)
      +0x14  u32    signal_count     +0x18  msg*   parent (into ETH_messages)
      +0x18  u32    mux/flags        +0x20  map*   value map (Diag_*, 0 if none)
      +0x20  sig*   signals
      +0x28  bus*   bus (CANBusList)

    value map: flat [u32 value][char* label] pairs, terminated by
               (0xffffffff, "").
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MSG_STRIDE = 48
SIG_STRIDE = 40
MAP_STRIDE = 16

# ELF relocation types we care about (x86-64)
_R_X86_64_64 = 1
_R_X86_64_GLOB_DAT = 6
_R_X86_64_JUMP_SLOT = 7
_R_X86_64_RELATIVE = 8


class ElfImage:
    """Minimal read-only ELF64 reader with relocation resolution.

    Pure-stdlib so it runs anywhere the rest of tm3diag does. Only the pieces
    needed to walk relocated data tables are implemented.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.d = self.path.read_bytes()
        d = self.d
        if d[:4] != b"\x7fELF" or d[4] != 2:
            raise ValueError(f"{path}: not an ELF64 image")
        (self.e_type, self.e_machine, _ev, _entry, _phoff, self.e_shoff,
         _flags, _ehsize, _phes, _phn, self.e_shentsize, self.e_shnum,
         self.e_shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", d, 16)

        self.sections: list[dict] = []
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            (name, typ, flags, addr, offset, size, link, info, _align,
             entsize) = struct.unpack_from("<IIQQQQIIQQ", d, off)
            self.sections.append({"name": name, "type": typ, "flags": flags,
                                  "addr": addr, "offset": offset, "size": size,
                                  "link": link, "info": info,
                                  "entsize": entsize})
        shstr = self.sections[self.e_shstrndx]
        for s in self.sections:
            s["n"] = self._cstr_at(shstr["offset"] + s["name"])
        self.by_name = {s["n"]: s for s in self.sections}
        # Sections that occupy virtual address space (for vaddr -> file off).
        self._alloc = [s for s in self.sections if s["addr"] and s["size"]]

        self.syms: dict[str, dict] = {}     # name -> {value, size}
        self._symlist: list[tuple[str, int]] = []  # dynsym order (for relocs)
        self._load_syms(".dynsym", ".dynstr")
        self._load_syms(".symtab", ".strtab")  # usually absent (stripped)
        # addr -> (name, size) for the first OBJECT/func symbol at each start.
        self._by_addr: dict[int, tuple[str, int]] = {}
        for nm, s in self.syms.items():
            self._by_addr.setdefault(s["value"], (nm, s["size"]))

        self.reloc: dict[int, tuple] = {}   # r_offset -> ('rel', t)|('sym', n, a)
        self._load_rela(".rela.dyn")
        self._load_rela(".rela.plt")

    # -- raw helpers ----------------------------------------------------------
    def _cstr_at(self, off: int, maxlen: int = 4096) -> str:
        e = self.d.find(b"\x00", off, off + maxlen)
        if e < 0:
            e = off + maxlen
        return self.d[off:e].decode("latin1")

    def _load_syms(self, symsec: str, strsec: str) -> None:
        if symsec not in self.by_name or strsec not in self.by_name:
            return
        ss, st, d = self.by_name[symsec], self.by_name[strsec], self.d
        for i in range(ss["size"] // 24):
            o = ss["offset"] + i * 24
            name, _info, _other, _shndx, value, size = struct.unpack_from(
                "<IBBHQQ", d, o)
            nm = self._cstr_at(st["offset"] + name)
            self._symlist.append((nm, value))
            if nm and nm not in self.syms:
                self.syms[nm] = {"value": value, "size": size}

    def _load_rela(self, sec: str) -> None:
        if sec not in self.by_name:
            return
        rs, d = self.by_name[sec], self.d
        for i in range(rs["size"] // 24):
            r_offset, r_info, r_addend = struct.unpack_from(
                "<QQq", d, rs["offset"] + i * 24)
            r_type = r_info & 0xFFFFFFFF
            r_sym = r_info >> 32
            if r_type == _R_X86_64_RELATIVE:
                self.reloc[r_offset] = ("rel", r_addend)
            elif r_type in (_R_X86_64_64, _R_X86_64_GLOB_DAT,
                            _R_X86_64_JUMP_SLOT):
                nm = self._symlist[r_sym][0] if r_sym < len(self._symlist) else ""
                self.reloc[r_offset] = ("sym", nm, r_addend)

    def v2o(self, addr: int) -> int | None:
        for s in self._alloc:
            if s["addr"] <= addr < s["addr"] + s["size"]:
                if s["type"] == 8:  # SHT_NOBITS (.bss) has no file bytes
                    return None
                return s["offset"] + (addr - s["addr"])
        return None

    # -- typed reads ----------------------------------------------------------
    def u32(self, addr: int) -> int:
        o = self.v2o(addr)
        return 0 if o is None else struct.unpack_from("<I", self.d, o)[0]

    def ptr_target(self, addr: int) -> int | None:
        """Resolve the pointer stored at *addr* to a virtual address.

        Prefers the relocation (position-independent objects store 0 in the
        slot and carry the real target as an addend), falling back to a literal
        qword for the rare non-relocated pointer.
        """
        r = self.reloc.get(addr)
        if r is not None:
            if r[0] == "rel":
                return r[1]
            base = self.syms.get(r[1], {}).get("value")
            return None if base is None else base + r[2]
        o = self.v2o(addr)
        if o is None:
            return None
        v = struct.unpack_from("<Q", self.d, o)[0]
        return v or None

    def cstr(self, addr: int | None, maxlen: int = 4096) -> str:
        if not addr:
            return ""
        o = self.v2o(addr)
        if o is None:
            return ""
        e = self.d.find(b"\x00", o, o + maxlen)
        if e < 0:
            e = o + maxlen
        try:
            return self.d[o:e].decode("utf-8")
        except UnicodeDecodeError:
            return self.d[o:e].decode("latin1")

    def sym(self, name: str) -> dict | None:
        return self.syms.get(name)

    def sym_at(self, addr: int) -> tuple[str, int] | None:
        """Return (name, size) of the OBJECT symbol starting at *addr*."""
        return self._by_addr.get(addr)


@dataclass
class SoCatalog:
    """Result of :func:`extract_catalog`."""
    lib: str
    bus: str
    messages: dict[str, dict] = field(default_factory=dict)
    # signals present in the .so but whose enum/units we captured; layout is
    # never known from the .so alone.
    signal_count: int = 0
    value_table_count: int = 0


def _value_map(elf: ElfImage, addr: int, size: int) -> dict[str, int]:
    """Parse a Diag_* value table into {label: raw_value}."""
    out: dict[str, int] = {}
    for k in range(size // MAP_STRIDE):
        base = addr + k * MAP_STRIDE
        val = elf.u32(base)
        label = elf.cstr(elf.ptr_target(base + 8))
        if not label:  # terminator / hole
            continue
        out.setdefault(label, val)
    return out


def extract_catalog(path: str | Path, bus: str | None = None) -> SoCatalog:
    """Extract the full ETH signal catalog from *path* (a libQtCarCANData .so).

    Returns messages keyed by name in (a subset of) the compact.json schema:
    each message carries ``message_id``, ``length_bytes``, ``cycle_time``,
    ``originNode``/``senders``/``bus`` and a ``signals`` dict; each signal
    carries ``units`` and ``value_description`` ({label: value}). Layout fields
    (start_position/width/scale/offset/...) are intentionally absent -- overlay
    them from a compact.json donor.
    """
    elf = ElfImage(path)
    if "ETH_messages" not in elf.syms:
        raise ValueError(f"{path}: no ETH_messages table (not a CANData lib?)")

    # Bus name from CANBusList if present, else default to "ETH".
    if bus is None:
        bus = "ETH"
        cbl = elf.sym("CANBusList")
        if cbl:
            nm = elf.cstr(elf.ptr_target(cbl["value"]))
            if nm:
                bus = nm

    cat = SoCatalog(lib=Path(path).name, bus=bus)
    msgsym = elf.syms["ETH_messages"]
    n_msgs = msgsym["size"] // MSG_STRIDE

    for i in range(n_msgs):
        b = msgsym["value"] + i * MSG_STRIDE
        mname = elf.cstr(elf.ptr_target(b + 0x00))
        if not mname:
            continue
        can_id = elf.u32(b + 0x08)
        dlc = elf.u32(b + 0x0C)
        cycle = elf.u32(b + 0x10)
        sig_count = elf.u32(b + 0x14)
        sig_target = elf.ptr_target(b + 0x20)

        signals: dict[str, dict] = {}
        if sig_target:
            info = elf.sym_at(sig_target)
            avail = (info[1] // SIG_STRIDE) if info else sig_count
            for j in range(max(sig_count, avail)):
                sb = sig_target + j * SIG_STRIDE
                sname = elf.cstr(elf.ptr_target(sb + 0x00))
                if not sname:
                    continue
                units = elf.cstr(elf.ptr_target(sb + 0x10))
                sig: dict = {}
                if units:
                    sig["units"] = units
                vm_target = elf.ptr_target(sb + 0x20)
                if vm_target:
                    vinfo = elf.sym_at(vm_target)
                    if vinfo:
                        vd = _value_map(elf, vm_target, vinfo[1])
                        if vd:
                            sig["value_description"] = vd
                            sig["value_table_name"] = vinfo[0].removeprefix(
                                "Diag_").removesuffix("_map")
                            cat.value_table_count += 1
                signals[sname] = sig
                cat.signal_count += 1

        prefix = mname.split("_", 1)[0]
        cat.messages[mname] = {
            "message_id": can_id,
            "length_bytes": dlc,
            "cycle_time": cycle,
            "originNode": prefix,
            "senders": [prefix],
            "bus": bus,
            "signals": signals,
        }
    return cat


def to_compact_dict(cat: SoCatalog, product: str = "Model3") -> dict:
    """Render a :class:`SoCatalog` as a compact.json-shaped dict."""
    return {
        "product": product,
        "version": f"from:{cat.lib}",
        "busMetadata": {cat.bus: {"messageCount": len(cat.messages)}},
        "messages": cat.messages,
    }


if __name__ == "__main__":  # tiny smoke test / CLI
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Dump ETH catalog from a .so")
    ap.add_argument("lib", type=Path, help="libQtCarCANData.so")
    ap.add_argument("-o", "--out", type=Path, help="write catalog JSON")
    a = ap.parse_args()
    c = extract_catalog(a.lib)
    print(f"{c.lib}: bus={c.bus} messages={len(c.messages)} "
          f"signals={c.signal_count} value_tables={c.value_table_count}")
    if a.out:
        a.out.write_text(json.dumps(to_compact_dict(c), indent=2))
        print(f"wrote {a.out}")
