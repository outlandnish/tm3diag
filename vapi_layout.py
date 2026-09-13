"""Recover per-signal CAN bit-layout from the MCU UI's own decoder.

Why this exists
---------------
``libQtCarCANData.so`` carries the *complete* signal catalog (names, units, enum
tables) but no bit-layout -- see :mod:`so_candata`. The layout was assumed to
live only in ``Model3_ETH.compact.json``, which Tesla strips down every release
(2022.45.15: 140 messages / 347 signals vs the catalog's 446 / 26227), forcing
``candata_to_dbc`` to borrow layout from *older* revisions. That is unsound:
layouts move between releases (0x118 ``DI_immobilizerState`` is 27|3 in 2020 but
13|3 in 2022, ``DI_driveBlocked`` 12|2 -> 24|2).

The layout does exist same-revision -- as *code*, in ``libQtCarVAPI.so``:

    CAN frame -> GUICanCracker::crackMessage(int bus, int msgId, uchar *payload)
              -> CANDataManager::storeSignalValue(key, double, valid, bus)

``crackMessage`` is one generated function (2022: ~1.1 MB) -- a jump table on
``msgId`` whose cases load payload bytes, shift/mask the field, scale it, and
call ``storeSignalValue`` with a 32-bit signal KEY. **That KEY is the u32 at
+0x08 in the catalog's signal struct**, which is what recovers the name. The
call count equals the catalog signal count exactly (2020: 10193, 2022: 26227),
so every catalogued signal has exactly one decode site.

Because the key identifies the signal outright, this walks the whole function
linearly and attributes by key -- no need to reason about case boundaries.

Correctness stance: emit nothing rather than a guess. Any instruction sequence
the register model does not fully understand marks the field unusable and is
reported as a gap, so a recovered layout is trustworthy.

Validated against the SAME-REVISION 2020 ``Model3_ETH.compact.json`` -- Tesla's
own layout *data*, shipped in the same image as the *code* being decoded here, so
the two are genuinely independent. Of the 10192 signals present in both:

    exact start|width|endianness : 10139  (99.48%)
    same BITS, i.e. decodes the same : 10192  (100.00%)
    genuinely different : 0

Every signal it emits that compact.json also carries decodes to exactly the same
bits. The gap between the two rows is notation: a field inside a single byte has
an equivalent Motorola spelling, `start + width - 1` with @0, and nothing in the
binary says which the DBC author would pick. That is 10192 of 2020's 10193
catalogued signals checked against Tesla's own data.

Coverage is 100.0% of the 2020 catalog (all 10193, no gaps of any kind),
100.0% of 2022.45.15 and 99.9% of 2026.8.3.

There is a second, independent check on the MUX model specifically. An
alertLog's page number is the alert number, and the signal names carry it
(``VCBATT2_a192_hvState`` is alert 192), so the catalog validates the pages we
assign: 2026.8.3 is 18586/18588 correct with none wrong, 2022 is 11974/11976.
(Confirmed against the ``mux_id`` compact.json does ship -- 21 signals across
five revisions, every one agreeing with its name -- before being relied on.)
:func:`check_alert_pages` runs it on every extraction.

Byte order comes from two places. A ``bswap`` of a whole register says it
outright. Otherwise it is inferred from which payload byte supplies the HIGH half
of a split field: a *lower* byte index means the field was assembled MSB-first,
i.e. Motorola. Either way the DBC start bit is the MSB rather than the LSB.

Multiplexed messages are the bulk of the catalog, and GCC compiles their page
switch five different ways -- a jump table, an if-else compare chain, a chain
that walks the page number down with ``sub``, a two-way branch on a single
selector BIT, and a search TREE of range splits with a table at each leaf. All
five are recognised; see :func:`find_dispatch` and :func:`find_compare_chain`.

Known gaps, in descending size (2026.8.3, 58 signals of 40455):
  * **Decode sequences the register model does not follow** (27 sites),
    reported as gaps rather than emitted.
  * **Pages never attributed** (24 signals), all in VCBATT2_alertLog, whose
    dispatch tree resolves only in part.
  * **One message whose branches still do not resolve** -- dropped wholesale so
    a compact.json donor can supply it (5 signals).

2022.45.15 is down to 7 unmodelled sites and nothing else; 2020.8.1 to none.

Usage::

    python vapi_layout.py <libQtCarVAPI.so> [--candata <libQtCarCANData.so>]
                          [-o layout.json] [--report gaps.txt]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import struct
import sys
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

from so_candata import MSG_STRIDE, SIG_STRIDE, ElfImage

CRACK_SYM = "_ZN13GUICanCracker12crackMessageEiiPh"

# x86-64 register families: every alias collapses to one canonical name so the
# model follows a value through al/ax/eax/rax.
_REG_FAMILY: dict[str, str] = {}
for _fam, _aliases in {
    "a": "rax eax ax al ah", "b": "rbx ebx bx bl bh",
    "c": "rcx ecx cx cl ch", "d": "rdx edx dx dl dh",
    "si": "rsi esi si sil", "di": "rdi edi di dil",
    "bp": "rbp ebp bp bpl", "sp": "rsp esp sp spl",
}.items():
    for _a in _aliases.split():
        _REG_FAMILY[_a] = _fam
for _n in range(8, 16):
    for _suf in ("", "d", "w", "b"):
        _REG_FAMILY[f"r{_n}{_suf}"] = f"r{_n}"

# How many bits a register name actually holds. A `movzx eax, al` after a field
# has been assembled is a TRUNCATION, and the only evidence of the real width.
_REG_BITS: dict[str, int] = {}
for _names, _bits in (("al ah bl bh cl ch dl dh sil dil bpl spl", 8),
                      ("ax bx cx dx si di bp sp", 16),
                      ("eax ebx ecx edx esi edi ebp esp", 32),
                      ("rax rbx rcx rdx rsi rdi rbp rsp", 64)):
    for _r in _names.split():
        _REG_BITS[_r] = _bits
for _n in range(8, 16):
    for _suf, _bits in (("b", 8), ("w", 16), ("d", 32), ("", 64)):
        _REG_BITS[f"r{_n}{_suf}"] = _bits

# Longest first: "word ptr" is a SUBSTRING of "dword ptr"/"qword ptr", so checking
# "word" first silently reads every 32/64-bit load as 16 bits.
_LOAD_SIZES = (("qword", 64), ("dword", 32), ("word", 16), ("byte", 8))
_RIP_RE = re.compile(r"\[rip \+ (0x[0-9a-f]+)\]")
_LEA_SCALE_RE = re.compile(r"^\[([a-z][a-z0-9]*)\*(\d+)\]$")
_LEA_BASE_RE = re.compile(r"^\[([a-z][a-z0-9]*)\s*\+\s*([a-z][a-z0-9]*)(?:\*(\d+))?\]$")


def _canon(reg: str) -> str | None:
    return _REG_FAMILY.get(reg.strip())


def _rbx_byte(operand: str) -> int | None:
    """Payload byte index a ``<size> ptr [rbx + N]`` operand starts at, else None.

    Any width: an alertLog selects on a 10-bit alert id read as a WORD, and a
    mask is always relative to this byte, so the index is what matters and the
    size does not.
    """
    if "ptr [rbx" not in operand:
        return None
    inner = operand.split("[rbx", 1)[1].split("]", 1)[0]
    try:
        return int(inner.replace("+", "").strip() or "0", 0)
    except ValueError:
        return None


def _bit_test(parts: list[str], sel_regs: set[str]) -> tuple[int | None, int] | None:
    """``test <payload byte>, <single bit>``: the two-way mux of a 1-bit selector.

    A message whose pages are chosen by ONE payload bit gets neither a jump
    table nor a compare chain, just a branch::

        test byte ptr [rbx + 6], 1      ; VC_pcsInterfaceMuxIndex, bit 48
        je   page0                      ; ... and page 1 is the fall-through

    which is how VC_pcsInterface, VCBATT_pcsInterface, TAS_axleData and
    UI_systemMonitor dispatch. VCFRONT_vehicleStatus loads the byte first and
    tests the register (``test al, 1``), so both operand forms count.

    Returns ``(payload byte or None when tested through a register, mask)``.
    Only a single-bit mask qualifies: ``test`` distinguishes zero from nonzero,
    which is an unambiguous two-way split only when the field is one bit wide.
    """
    try:
        imm = int(parts[1], 0)
    except ValueError:
        return None
    if imm <= 0 or imm.bit_count() != 1:
        return None
    b = _rbx_byte(parts[0])
    if b is not None:
        return b, imm
    return (None, imm) if _canon(parts[0]) in sel_regs else None


def _mask_test(parts: list[str], sel_regs: set[str]) -> tuple[int | None, int] | None:
    """``test <payload byte>, <field mask>``: a masked selector, tested in place.

    The ``and`` form writes the mask back and is followed already; this one does
    not, which is all GCC needs when the message has a single page::

        movzx eax, byte ptr [rbx]
        test  al, 0xf
        je    page0

    Only a LOW-CONTIGUOUS mask qualifies, so the value tested is the page number
    itself, and only a multi-bit one: a single bit is the two-way split that
    :func:`_bit_test` models with bounds of its own.

    Returns ``(payload byte or None when tested through a register, mask)``.
    """
    try:
        imm = int(parts[1], 0)
    except ValueError:
        return None
    if not _is_field_mask(imm) or imm.bit_count() < 2:
        return None
    b = _rbx_byte(parts[0])
    if b is not None:
        return b, imm
    return (None, imm) if _canon(parts[0]) in sel_regs else None


def _bare_exit(elf: ElfImage, md, addr: int, store_plt: int,
               limit: int = 12) -> bool:
    """Does straight-line code at ``addr`` leave the case without storing anything?

    Every message case signs off by telling the manager the frame arrived::

        mov  edx, 0x3e6                 ; the message id
        mov  esi, 3
        mov  rdi, rbp
        call CANDataManager::messageArrived
        jmp  <the end of crackMessage>

    and a mux dispatch with a SINGLE page is written as a branch over exactly
    that block::

        movzx eax, byte ptr [rbx]
        test  al, 0xf
        je    page0                     ; ...and the fall-through gives up

    So a branch whose other side reaches some call that is not
    ``storeSignalValue``, having stored nothing on the way, is a dispatch
    default rather than a page. Without this a one-page mux is
    indistinguishable from an ordinary plausibility check: 29 messages in
    2026.8.3 emitted their signals as unconditional when they are only valid on
    one selector value, and the twelve whose page body opens on a register
    loaded BEFORE the branch modelled nothing at all for want of a seed.
    """
    off = elf.v2o(addr)
    if off is None:
        return False
    for n, ins in enumerate(md.disasm(elf.d[off:off + limit * 16], addr)):
        if n >= limit:
            return False
        if ins.mnemonic == "call":
            return ins.op_str.startswith("0x") and int(ins.op_str, 0) != store_plt
        if ins.mnemonic.startswith("j"):
            return False        # branches on: more decoding, not the sign-off
    return False


def _pristine_step(f: Field, m: str, parts: list[str], dst: str) -> bool:
    """Carry a still-live payload load through one instruction. Mutates ``f``.

    A dispatch preamble often narrows the payload byte it just loaded and then
    leaves the RESULT live for the page bodies -- DAS_telemetryEvent computes
    its jump table in rdx/rcx precisely so that ``eax = byte0 >> 5`` survives
    the branch, and every one of its ten pages opens by using it. Treating the
    ``shr`` as destroying the load left those pages with no register state and
    their first signal modelled as nothing.

    Returns False when the instruction is not a refinement we can follow, in
    which case the caller drops the field rather than guess at it.
    """
    if m in ("shr", "sar", "shl") and len(parts) == 2:
        try:
            k = int(parts[1], 0)
        except ValueError:
            return False
        if m == "shl":
            return False                # a join, not a narrowing; too much state
        f.shift += k
        f.signed = f.signed and m == "sar"
        return True
    if m == "and" and len(parts) == 2:
        try:
            imm = int(parts[1], 0)
        except ValueError:
            return False
        if not _is_field_mask(imm):
            return False
        f.mask = imm if f.mask is None else f.mask & imm
        f.narrowed = True
        return True
    if m in ("mov", "movzx", "movsx") and len(parts) == 2 \
            and _canon(parts[1]) == dst:
        # `movzx eax, al` on itself: a truncation of the same field
        bits = _REG_BITS.get(parts[1])
        if bits and bits < 32:
            f.cap = bits if f.cap is None else min(f.cap, bits)
            if bits == 8:
                f.narrowed = True
        f.signed = f.signed or m == "movsx"
        return True
    return False


def _page(value: int, mask: int | None, bias: int) -> int:
    """A page number, taken modulo the selector mask ONLY past a rebase.

    ``add ax, 0x3c5`` is ``-0x3b mod 1024``, so the bias goes negative and only
    the mask brings a later comparison back to the page it stands for. Reducing
    unconditionally would instead fold every ordinary comparison in a page body
    onto a real page -- with a one-bit selector, ``cmp al, 5`` becomes page 1
    and claims a region belonging to something else.
    """
    return value & mask if mask is not None and bias < 0 else value


def _mask_field(sel: int, mask: int) -> tuple[int, int] | None:
    """``(start bit, width)`` of a contiguous mask inside payload byte ``sel``.

    The mask need not be low-aligned: a selector isolated with ``test byte
    ptr [rbx+2], 4`` is the one-bit field at bit ``2*8 + 2``.
    """
    if mask <= 0:
        return None
    shift = (mask & -mask).bit_length() - 1
    if ((mask >> shift) & ((mask >> shift) + 1)) != 0:
        return None                                   # not contiguous
    return sel * 8 + shift, mask.bit_count()


def _lea_shift(mem: str) -> tuple[str, int] | None:
    """``lea`` used as a left shift: ``(source register, shift)`` or None.

    GCC writes small shifts as address arithmetic -- ``lea edx,[rax+rax]`` is
    ``shl edx,1``, ``lea edx,[rax*4]`` is ``shl edx,2``. Reading them as opaque
    leaves the high half of a split field with no ``shl``, so the ``or`` join
    never fires and only the low half survives::

        movzx eax, word ptr [rbx + 6]   ; high half, 16 bits
        lea   edx, [rax + rax]          ; << 1
        movzx eax, byte ptr [rbx + 5]
        shr   al, 7                     ; low half, 1 bit at 47
        or    eax, edx
        movzx eax, ax                   ; -> VCFRONT_EPAS3PCurrent 47|16

    Only powers of two qualify: ``lea [rax+rax*2]`` is a multiply by 3, which is
    arithmetic on the value rather than a field move.
    """
    m = _LEA_SCALE_RE.match(mem)
    if m:
        reg, mult = m.group(1), int(m.group(2))
    else:
        m = _LEA_BASE_RE.match(mem)
        if not m or m.group(1) != m.group(2):
            return None
        reg, mult = m.group(1), int(m.group(3) or 1) + 1
    if mult < 2 or mult & (mult - 1):
        return None
    return reg, mult.bit_length() - 1


@dataclass
class Field:
    """A payload bit-field under construction, tracked per register."""

    byte: int
    avail: int = 8
    shift: int = 0            # right shift already applied
    mask: int | None = None   # low-contiguous field mask, if any
    signed: bool = False
    narrowed: bool = False    # value was truncated to 8 bits after shifting
    shl: int = 0              # left shift (marks the high half of a split field)
    fixed: int | None = None  # exact width, set when halves are joined by `or`
    big: bool = False         # halves joined MSB-first (Motorola)
    be_start: int | None = None   # Motorola start bit = position of the MSB
    cap: int | None = None    # assembled value later truncated to this many bits
    ok: bool = True           # cleared when we hit something we do not model

    @property
    def start(self) -> int:
        return self.byte * 8 + self.shift

    @property
    def _raw_width(self) -> int:
        # An explicit mask is the most direct evidence of width, so it outranks a
        # width inferred from a sign-extension or a joined split field.
        if self.mask is not None:
            return bin(self.mask).count("1")
        if self.fixed is not None:
            return self.fixed
        if self.narrowed:
            return max(8 - self.shift, 0)
        return max(self.avail - self.shift, 0)

    @property
    def width(self) -> int:
        # A narrowing register copy AFTER the field is assembled clamps it. The
        # 12-bit-looking join `shr al,4` | `shl edx,4` | `or eax,edx` is followed
        # by `movzx eax, al` precisely because the field is 8 bits: without the
        # clamp it runs into its neighbour and both get dropped as overlapping.
        w = self._raw_width
        return min(w, self.cap) if self.cap is not None else w

    @property
    def modelled(self) -> bool:
        """False when the width is a guess rather than something we actually saw.

        A 32/64-bit load is either consumed whole (a CRC/serial) or narrowed by a
        mask. If it was shifted but we never saw a mask, some narrowing step went
        unmodelled and `avail - shift` would invent a far-too-wide field -- which
        then collides with every real signal in the message.
        """
        if not self.ok:
            return False
        if self.mask is not None or self.fixed is not None or self.narrowed:
            return True
        return not (self.avail >= 32 and self.shift)


def _or_join(a: Field, b: Field) -> Field:
    """The field two ``or``-ed pieces assemble, or an unmodelled one.

    Each side occupies result bits ``[shl, shl+width)``. They join iff those runs
    are adjacent, and the result keeps the LOWER side's payload origin and its
    offset. Requiring the low side to sit at bit 0 only expresses a TWO-way
    join; a field assembled from three or more pieces (PMR_bootGitHash takes
    bytes 1, 2-3 and 4-7) carries a shift on both sides at every intermediate
    step, and was refused outright.
    """
    hi, lo = (a, b) if a.shl >= b.shl else (b, a)
    if not (hi.shl and lo.shl + lo.width == hi.shl):
        return Field(byte=lo.byte, ok=False)
    # A LOWER payload byte supplying the HIGH bits is Motorola.
    nf = Field(byte=lo.byte, avail=lo.avail, shift=lo.shift, signed=hi.signed,
               shl=lo.shl, fixed=hi.shl + hi.width - lo.shl,
               big=hi.byte < lo.byte, ok=a.ok and b.ok)
    if nf.big:
        nf.be_start = hi.byte * 8 + hi.shift + hi.width - 1
    return nf


@dataclass
class Store:
    """One recovered storeSignalValue site."""

    key: int
    start: int
    width: int
    signed: bool
    big: bool = False         # Motorola: `start` is the MSB position
    addr: int = 0             # call site, used to place it in a mux branch
    scale: float = 1.0
    offset: float = 0.0
    scale_known: bool = True
    constant: bool = False    # stored from a constant: the frame carries no bits


@dataclass
class Report:
    lib: str = ""
    calls: int = 0
    recovered: int = 0
    catalog_signals: int = 0
    unresolved_keys: int = 0
    gaps: list = field(default_factory=list)
    muxed: list = field(default_factory=list)   # messages deferred to a donor
    message_gaps: list = field(default_factory=list)
    # Self-check on the mux model -- see check_alert_pages.
    alert_pages_ok: int = 0
    alert_pages_unpaged: int = 0
    alert_pages_wrong: list = field(default_factory=list)


def _is_field_mask(m: int) -> bool:
    """A field mask is low-contiguous. The SNA test (`and eax,0xffffffe0`) is not."""
    return m > 0 and (m & (m + 1)) == 0


def _occupied_bits(sig: dict) -> list[int]:
    """Absolute bit indices a signal covers, honouring its byte order.

    Intel runs upward from the LSB. Motorola starts at the MSB and walks
    *down* within a byte, stepping to bit 7 of the next byte at each boundary --
    so the two orders cover different bits from the same start value.
    """
    start, width = sig["start_position"], sig["width"]
    if sig.get("endianness", "LITTLE") != "BIG":
        return list(range(start, start + width))
    out, byte, bit = [], start // 8, start % 8
    for _ in range(width):
        out.append(byte * 8 + bit)
        if bit == 0:
            byte, bit = byte + 1, 7
        else:
            bit -= 1
    return out


def _snap_float(x: float) -> float:
    """Undo float32->double widening of a scale/offset constant.

    Tesla authors these as C floats, so a scale of 0.2 reaches the binary as
    0.20000000298023224. If the value is exactly representable in float32, return
    the shortest decimal that still round-trips through float32 (0.2).

    A value whose EXACT decimal is already short is the authored constant and is
    left alone. 0.0732421875 is 75/1024 written out in full; shortening it to
    the 0.07324219 that float32 also accepts throws away digits Tesla's own
    compact.json still carries.
    """
    try:
        as32 = struct.unpack("<f", struct.pack("<f", x))[0]
    except (OverflowError, ValueError):
        return x
    if as32 != x:
        return x                      # a genuine double; leave it alone
    if len(f"{Decimal(x):f}".replace("-", "").replace(".", "").strip("0")) <= 12:
        return x
    for prec in range(1, 10):
        cand = float(f"{x:.{prec}g}")
        if struct.unpack("<f", struct.pack("<f", cand))[0] == x:
            return cand
    return x


def catalog_index(path: str | Path) -> tuple[dict, dict]:
    """(key -> (message, signal), message -> {message_id, dlc, cycle, signals})."""
    elf = ElfImage(path)
    ms = elf.syms["ETH_messages"]
    keys: dict[int, tuple[str, str]] = {}
    msgs: dict[str, dict] = {}
    for i in range(ms["size"] // MSG_STRIDE):
        b = ms["value"] + i * MSG_STRIDE
        mname = elf.cstr(elf.ptr_target(b + 0x00))
        tgt = elf.ptr_target(b + 0x20)
        if not mname:
            continue
        sigs: list[str] = []
        if tgt:
            info = elf.sym_at(tgt)
            n = (info[1] // SIG_STRIDE) if info else elf.u32(b + 0x14)
            for j in range(n):
                sb = tgt + j * SIG_STRIDE
                sname = elf.cstr(elf.ptr_target(sb + 0x00))
                if sname:
                    keys[elf.u32(sb + 0x08)] = (mname, sname)
                    sigs.append(sname)
        msgs[mname] = {"message_id": elf.u32(b + 0x08),
                       "length_bytes": elf.u32(b + 0x0C),
                       "cycle_time": elf.u32(b + 0x10),
                       "signals": sigs}
    return keys, msgs


def find_dispatch(elf: ElfImage, md, start: int, end: int):
    """Locate a jump-table dispatch in ``[start, end)``.

    Both the message switch at the top of crackMessage and the per-message mux
    switch inside an alertLog/alertMatrix case compile to the same shape::

        movzx eax, byte ptr [rbx + N]   ; selector byte (absent at top level)
        sub   eax, LO                   ; optional low bound
        cmp   al, COUNT-1
        ja    default
        lea   rax, [rip + DISP]         ; table of int32 self-relative offsets
        movsxd rdx, dword ptr [rax + rdx*4]
        add   rax, rdx
        jmp   rax

    When the selector is a sub-byte field the range check is redundant, so GCC
    drops the ``cmp`` and the mask itself bounds the table -- and it is free to
    hoist the ``lea`` above the mask::

        movzx eax, byte ptr [rbx]
        mov   rdx, rax
        lea   rax, [rip + DISP]
        and   edx, 0x1f                 ; 32 entries, no cmp anywhere
        movsxd rdx, dword ptr [rax + rdx*4]
        add   rax, rdx
        jmp   rax

    so a table seen before its bound is held back and resolved at the indirect
    ``jmp``, which is what makes it a dispatch at all.

    The low bound is what the table's first entry stands for, and GCC subtracts
    it two ways. On a full-width selector it is a plain ``sub``; on a masked one
    it rebases MODULO the mask, because the value is known to fit::

        movzx eax, word ptr [rbx]
        add   ax, 0x3f1                 ; TRCM_alertLog: -15 mod 1024
        and   ax, 0x3ff
        cmp   ax, 0x198                 ; table covers alerts 15..423

    so the bound is ``(-K) & MASK``. Missing it does not lose a page, it
    MISNUMBERS every one of them -- and a mux id is an alert number, so each
    alert's payload is then decoded as a different alert's.

    Returns ``(selector_byte | None, lo, count, table_va)`` or None.
    """
    sel = lo = count = None
    held = None
    rebase: tuple[str, int] | None = None
    for ins in md.disasm(elf.d[elf.v2o(start):elf.v2o(end)], start):
        m, ops = ins.mnemonic, ins.op_str
        parts = [p.strip() for p in ops.split(",")]
        dst = _canon(parts[0]) if parts else None
        # rbx is the payload and rsp/rbp the frame; arithmetic on those is not
        # the selector being rebased. (_canon yields family names: "b", "sp".)
        scratch = dst is not None and dst not in ("b", "sp", "bp")
        if m in ("movzx", "mov") and "ptr [rbx" in ops:
            inner = ops.split("[rbx", 1)[1].split("]", 1)[0]
            sel = int(inner.replace("+", "").strip() or "0", 0)
            lo, rebase = 0, None
        elif m in ("sub", "lea") and "- 0x" in ops:
            with contextlib.suppress(ValueError):
                lo = int(ops.split("- ")[1].rstrip("]"), 0)
        elif m == "sub" and len(parts) == 2 and scratch:
            with contextlib.suppress(ValueError):
                lo = int(parts[1], 0)
        elif m == "add" and len(parts) == 2 and scratch:
            with contextlib.suppress(ValueError):
                rebase = (dst, int(parts[1], 0))
        elif m == "cmp":
            try:
                count = int(ops.split(",")[1].strip(), 0) + 1
            except (IndexError, ValueError):
                count = None
            # A rebase needs no `and` when the bound is read off a SUB-register:
            # `add eax,0x6d` / `cmp al,9` truncates to 8 bits, so the modulus is
            # the sub-register's width (EPAS3S_alertLog, base alert 147).
            if rebase is not None and dst == rebase[0]:
                w = _REG_BITS.get(parts[0], 0)
                if w in (8, 16):
                    lo = (-rebase[1]) & ((1 << w) - 1)
                    rebase = None
        elif m == "and" and len(parts) == 2:
            with contextlib.suppress(ValueError):
                imm = int(parts[1], 0)
                if _is_field_mask(imm):
                    # The mask is the modulus of a rebase whether or not the
                    # selector load is in view -- a subtree of a search tree is
                    # entered below it, and falling back on the register width
                    # would read `add ax,0x2d4` as base 64812 rather than 300.
                    if rebase is not None and rebase[0] == dst:
                        lo = (-rebase[1]) & imm
                        rebase = None
                    if count is None and sel is not None:
                        count = imm + 1
        elif m == "call":
            rebase = None
        elif m == "lea" and "rip +" in ops:
            disp = int(ops.split("rip + ")[1].rstrip("]"), 0)
            addr = ins.address + ins.size + disp
            if count:
                return sel, (lo or 0), count, addr
            held = addr
        elif m == "jmp" and held is not None and count and _canon(ops):
            return sel, (lo or 0), count, held
    return None


def find_compare_chain(elf: ElfImage, md, start: int, fn_start: int, fn_end: int,
                       budget: int = 4000, store_plt: int | None = None):
    """Locate an if-else mux dispatch at ``start``, the non-jump-table form.

    GCC only builds a jump table when the case values are dense enough. A
    message with a handful of pages selected by a payload *nibble* -- the whole
    ``<NODE>_alertMatrix`` family -- gets a compare chain instead::

        movzx eax, byte ptr [rbx]     ; selector byte
        mov   edx, eax
        and   edx, 0xf                ; sub-byte selector
        cmp   dl, 1
        je    page1
        cmp   dl, 2
        je    page2
        ja    default                 ; range guard, reusing the same flags
        test  dl, dl
        je    page0

    Three wrinkles this has to model.

    ``and`` and ``sub`` set ZF themselves, so GCC walks the chain by decrementing
    the selector and never emits a ``cmp`` at all -- ``and dl, 0xf`` / ``je
    page0`` / ``sub dl, 1`` / ``jne next`` selects page 0 then page 1. A running
    ``bias`` therefore tracks how far the register has been walked down, and the
    real page number is ``bias`` plus whatever was compared.

    In the ``jne`` form the page body is the FALL-THROUGH while the chain resumes
    at the jump target, so this follows the chain rather than scanning linearly.

    And ``ja default`` is not the end of the chain: it is a BINARY SPLIT, and the
    pages above the bound are tested at the target. So the chain is walked as a
    worklist of runs rather than a single line, with each run carrying the page
    range it is responsible for -- ``jbe``/``jb`` split the same way downward.

    A large switch is a whole SEARCH TREE of those splits, with a jump table at
    the leaves rather than at the root. VCBATT2_alertLog tests alert 227 on its
    own, hands everything above it to one subtree and everything below 0x3b to
    another, and tables only the 0x3b..0xc0 run in between::

        cmp ax, 0xe3 / je  <case>       ; alert 227
                       ja  <subtree>    ; 228..    -> subtree
        cmp ax, 0x3a / jbe <subtree>    ; ..58     -> subtree
        add ax, 0x3c5 ... jmp rax       ; 59..192  -> table

    so a table reached mid-walk is expanded in place. Reading only the root
    table left 128 of that message's 209 signals with no page at all.

    Past a rebase the register no longer holds the page number, so comparisons
    are taken MODULO the selector mask: ``add ax,0x3c5`` makes ``cmp ax,0x85``
    mean page ``(0x85 - 0x3c5) & 0x3ff`` = 192, which is what bounds the run.

    Within one run the last page usually has no test of its own: ``cmp dl, 2`` /
    ``ja hi_pages`` bounds this run to 0..2, and once pages 0 and 2 have branched
    away, page 1 is simply what execution falls into. That page is recovered only
    when the bound leaves exactly one value unaccounted for, so nothing is
    assigned by guesswork.

    A page body can also open with ``shr al, 4`` on a byte loaded BEFORE the
    branch, so alongside the targets this tracks which payload loads are still
    pristine and snapshots them per page -- see ``state`` in the return value.
    Snapshotting once for the whole message is not good enough: the chain itself
    mutates registers between one page's branch and the next.

    Returns ``(selector_byte, selector_mask | None, {value: target},
    {value: live payload registers})`` or None. Requires two distinct targets, so
    an ordinary plausibility check on a payload byte cannot masquerade as a
    dispatch -- unless ``store_plt`` is given and the other side of the branch is
    the case's bare sign-off (see :func:`_bare_exit`), which is what a mux with a
    single page looks like and is evidence a comparison alone cannot give.
    """
    sel_byte: int | None = None
    mask: int | None = None
    solo = False                      # one page, the other side being the default
    # A bit-test dispatch pins the selector the moment it branches. The running
    # mask must not be reported instead: the walk continues through the page
    # body, where further `and`s on the same register are ordinary field
    # extraction and would otherwise be folded into it.
    pinned: tuple[int, int] | None = None
    targets: dict[int, int] = {}
    state: dict[int, dict] = {}
    seen: set[int] = set()
    steps = 0
    # (entry, selector regs, mask, bias, page range [lo, hi] this run owns, pristine)
    queue: list[tuple[int, set[str], int | None, int, int, int | None, dict]] = [
        (start, set(), None, 0, 0, None, {})]
    queued = {start}

    def record(value: int, target: int, live: dict) -> None:
        """Claim a page, pinning the selector the first time one resolves.

        The walk carries on into the page bodies, which load other payload
        bytes and mask other fields; whatever the running selector has become
        by the end is not the one this dispatch branched on.
        """
        nonlocal pinned
        # A page cannot exceed the field that selects it. Anything above the
        # mask means the value was rebased against the wrong modulus, and a
        # bogus page is worse than a missing one -- it claims a code region.
        # Judge against the PINNED mask: the running one drifts as the walk
        # carries on into the page bodies.
        limit = pinned[1] if pinned is not None else mask
        if value in targets or value < 0 or (limit is not None and value > limit):
            return
        targets[value] = target
        state[value] = {r: replace(f) for r, f in live.items()}
        if pinned is None and sel_byte is not None:
            pinned = (sel_byte, mask)

    while queue and steps < budget:
        entry, sel_regs, mask_in, bias, lo, hi, pristine = queue.pop(0)
        pc = entry
        sel_regs, pristine = set(sel_regs), {r: replace(f) for r, f in pristine.items()}
        if mask_in is not None:
            mask = mask_in
        pending: int | None = None   # real page number the current flags encode
        cmp_val: int | None = None   # last compared page, for the range guard
        fall: int | None = None      # where this run falls through: its last page
        fall_state: dict = {}
        quiet = 0
        pc = pc if fn_start <= pc < fn_end else None

        while pc is not None and steps < budget:
            redirect = stop = None
            for ins in md.disasm(elf.d[elf.v2o(pc):elf.v2o(fn_end)], pc):
                if ins.address in seen:
                    stop = True
                    break
                seen.add(ins.address)
                steps += 1
                quiet += 1
                if steps >= budget or (quiet > 96 and targets) or \
                        (quiet > 1200 and not targets):
                    stop = True
                    break
                m, ops = ins.mnemonic, ins.op_str
                parts = [p.strip() for p in ops.split(",")]
                dst = _canon(parts[0]) if parts else None

                # Which payload loads a page body could still be relying on.
                loaded = _payload_load(m, ops)
                if m == "call":
                    pristine.clear()
                elif loaded is not None and dst:
                    pristine[dst] = loaded
                elif dst and dst in pristine and m not in ("cmp", "test") \
                        and not _pristine_step(pristine[dst], m, parts, dst):
                    del pristine[dst]

                if m in ("movzx", "mov") and len(parts) == 2 and \
                        "ptr [rbx" in parts[1] and dst:
                    # Any width: an alertLog selects on a 10-bit alert id read
                    # as a word, not on a byte.
                    b = _rbx_byte(parts[1])
                    if b is None:
                        sel_regs.discard(dst)
                    elif sel_byte is not None and b != sel_byte and targets:
                        stop = True     # a second selector: a different chain
                        break
                    elif b == sel_byte and sel_regs:
                        # The same payload bytes in a SECOND register, not a new
                        # selector: PARK_pscEnvSlot reads byte 0 into eax and
                        # then word 0 into ecx, and goes on to compare `al`.
                        # Replacing the set here loses the register the chain
                        # actually branches on.
                        sel_regs.add(dst)
                    else:
                        sel_byte, sel_regs, mask = b, {dst}, None
                        pending = cmp_val = None
                        bias = 0
                elif m in ("mov", "movzx", "movsx", "movsxd") and len(parts) == 2 \
                        and _canon(parts[1]) in sel_regs and dst:
                    sel_regs.add(dst)
                elif m in ("and", "sub", "add") and len(parts) == 2 and dst in sel_regs:
                    try:
                        imm = int(parts[1], 0)
                    except ValueError:
                        sel_regs.discard(dst)
                        continue
                    if m == "and":
                        mask = imm if mask is None else mask & imm
                        # `add ax,0x3ff` / `and ax,0x3ff` is ONE modular rebase
                        # -- the `and` is the modulus of the bias, not a fresh
                        # field, so the bias has to survive it. Zeroing it made
                        # VCFRONT1_alertLog's `cmp ax,0x109 / ja default` mean
                        # page 265 rather than 266, and the single-value split
                        # then handed alert 266 to the sign-off block.
                        if hi is None and _is_field_mask(imm):
                            hi = imm
                    else:
                        bias += imm if m == "sub" else -imm
                    pending = _page(bias, mask, bias)  # set ZF: `je` is "== bias"
                    cmp_val = None
                elif m == "cmp" and len(parts) == 2 and dst in sel_regs:
                    try:
                        pending = cmp_val = _page(bias + int(parts[1], 0), mask, bias)
                    except ValueError:
                        pending = cmp_val = None
                elif m == "test" and len(parts) == 2 and dst in sel_regs \
                        and _canon(parts[1]) == dst:
                    pending, cmp_val = _page(bias, mask, bias), None
                elif m == "test" and len(parts) == 2 and not targets and \
                        (bt := _bit_test(parts, sel_regs)) is not None:
                    # A 1-bit selector: this REDEFINES the selector field, so
                    # any bound or bias a preceding `and` left behind no longer
                    # applies -- 0x441 masks byte 6 with 0xf to read an
                    # unrelated signal just before testing bit 0 of it.
                    b = bt[0] if bt[0] is not None else sel_byte
                    if b is None:
                        pending = cmp_val = None
                    else:
                        sel_byte, mask, bias = b, bt[1], 0
                        pinned = (b, bt[1])
                        hi, lo = 1, 0     # a one-bit field selects page 0 or 1
                        pending, cmp_val = 0, None   # `je`: the bit is clear
                elif m == "test" and len(parts) == 2 and not targets and \
                        (mt := _mask_test(parts, sel_regs)) is not None:
                    # `test al,0xf` is the `and` form without the write-back --
                    # all GCC needs when the message has one page. Every
                    # single-page alertMatrix dispatches this way.
                    b = mt[0] if mt[0] is not None else sel_byte
                    if b is None:
                        pending = cmp_val = None
                    else:
                        sel_byte, mask, bias = b, mt[1], 0
                        pinned = (b, mt[1])
                        lo, hi = 0, mt[1]
                        pending, cmp_val = 0, None   # `je`: the field is zero
                elif m == "jmp":
                    # An INDIRECT jmp is a jump table at a leaf of the tree.
                    # Expand it here, with the same low-bound handling the
                    # table walker uses, and go on to the other subtrees.
                    if not ops.startswith("0x"):
                        d = find_dispatch(elf, md, entry, ins.address + ins.size)
                        if d:
                            for v, t in dispatch_targets(elf, d[1], d[2],
                                                         d[3]).items():
                                if fn_start <= t < fn_end:
                                    record(v, t, pristine)
                    stop = True
                    break
                elif m in ("je", "jz") and pending is not None and ops.startswith("0x"):
                    t = int(ops, 0)
                    after = ins.address + ins.size
                    if fn_start <= t < fn_end:
                        record(pending, t, pristine)
                        quiet = 0
                        if store_plt is not None and \
                                _bare_exit(elf, md, after, store_plt):
                            # The other side gives up on the frame, so there is
                            # no fall-through page to infer -- and this branch
                            # alone is a whole dispatch.
                            solo = True
                        else:
                            fall = after
                            fall_state = {r: replace(f)
                                          for r, f in pristine.items()}
                    pending = None
                elif m in ("jne", "jnz") and pending is not None and ops.startswith("0x"):
                    t = int(ops, 0)
                    after = ins.address + ins.size
                    if store_plt is not None and _bare_exit(elf, md, after, store_plt):
                        # `test al,0x10 / jne page`: the page is at the TARGET
                        # and the fall-through gives up. Only a one-bit selector
                        # names it -- with a wider mask "nonzero" is several
                        # pages at once and none of them is known.
                        if mask is not None and mask.bit_count() == 1 \
                                and pending == 0 and fn_start <= t < fn_end:
                            record(1, t, pristine)
                            solo = True
                        pending = None
                        stop = True
                        break
                    record(pending, after, pristine)
                    pending = None
                    quiet = 0
                    if fn_start <= t < fn_end:
                        redirect = t    # the body fell through; the chain is there
                        break
                elif m.startswith("j"):
                    # An unsigned range test on the flags a `cmp` just set is a
                    # BINARY SPLIT: it bounds this run and hands the pages on
                    # the other side to a sibling run.
                    up = m in ("ja", "jnbe", "jae", "jnb")
                    down = m in ("jbe", "jna", "jb", "jnae")
                    if cmp_val is not None and (up or down):
                        t = int(ops, 0) if ops.startswith("0x") else None
                        if up:      # target takes the HIGH side
                            bound = cmp_val - 1 if m in ("jae", "jnb") else cmp_val
                            # the sibling inherits the bound in force BEFORE
                            # this split narrowed it
                            sub = (bound + 1, hi)
                            hi = bound if hi is None else min(hi, bound)
                        else:       # target takes the LOW side
                            bound = cmp_val if m in ("jbe", "jna") else cmp_val - 1
                            sub = (lo, bound)
                            lo = max(lo, bound + 1)
                        if t and fn_start <= t < fn_end:
                            if sub[1] is not None and sub[0] == sub[1]:
                                # The split leaves the sibling exactly ONE value,
                                # so the target IS that page -- no walking needed.
                                # `cmp dl,1` / `jb page0` is how every
                                # <NODE>_alertMatrix reaches page 0, and queueing
                                # it as a run to search found nothing: 287 of
                                # 2020's signals had no page for want of this.
                                record(sub[0], t, pristine)
                                quiet = 0
                            elif t not in queued and len(queued) < 64:
                                queued.add(t)
                                queue.append((t, set(sel_regs), mask, bias,
                                              sub[0], sub[1], {r: replace(f) for r, f in pristine.items()}))
                    pending = None
                elif m == "call":
                    sel_regs.clear()
                    pending = cmp_val = None
                elif dst in sel_regs:
                    sel_regs.discard(dst)
            pc = redirect if not stop else None

        # The page this run falls into, when its bound leaves exactly one open.
        # The RANGE is what has to stay small, not the page numbers: a subtree
        # of an alertLog owns ids in the hundreds.
        if hi is not None and fall is not None and lo <= hi and hi - lo < 256:
            missing = [v for v in range(lo, hi + 1) if v not in targets]
            if len(missing) == 1:
                record(missing[0], fall, fall_state)

    if pinned is not None:
        sel_byte, mask = pinned
    if sel_byte is None or not targets:
        return None
    if not solo and (len(targets) < 2 or len(set(targets.values())) < 2):
        return None
    return sel_byte, mask, targets, state


def _sel_step(regs: dict[str, Field], m: str, parts: list[str], dst: str) -> None:
    """Carry the selector's field model through one instruction. Mutates ``regs``.

    The pieces a joined selector is built from, and nothing else: a register
    copy, a mask, a shift either way (including the ``lea`` spelling), and the
    ``or`` that puts them together. Anything else writing the register drops it.
    """
    src = _canon(parts[1]) if len(parts) > 1 else None
    if m in ("mov", "movzx", "movsx", "movsxd") and src in regs:
        regs[dst] = replace(regs[src])
        return
    if m == "lea":
        got = _lea_shift(parts[1]) if len(parts) > 1 else None
        base = _canon(got[0]) if got else None
        if got and base in regs:
            regs[dst] = replace(regs[base], shl=regs[base].shl + got[1])
            return
    elif m == "or" and dst in regs and src in regs:
        regs[dst] = _or_join(regs[dst], regs[src])
        return
    elif dst in regs:
        f = regs[dst]
        try:
            imm = int(parts[1], 0) if len(parts) > 1 else None
        except ValueError:
            imm = None
        if imm is not None and m == "and" and _is_field_mask(imm):
            f.mask = imm
            return
        if imm is not None and m in ("shr", "sar"):
            f.shift += imm
            if parts[0] in ("al", "bl", "cl", "dl", "sil", "dil"):
                f.narrowed = True
            return
        if imm is not None and m == "shl":
            f.shl += imm
            return
    regs.pop(dst, None)


def find_joined_selector(elf: ElfImage, md, start: int, end: int, fn_start: int,
                         fn_end: int, store_plt: int | None, limit: int = 2048):
    """A dispatch whose selector is ASSEMBLED from two payload pieces.

    VCFRONT_lightStatus picks its page with a 3-bit index that straddles a byte
    boundary::

        movzx eax, byte ptr [rbx + 5]
        mov   edx, eax
        and   edx, 3                  ; byte 5 bits 0-1
        lea   ecx, [rdx + rdx]        ; ...shifted up one
        movzx edx, byte ptr [rbx + 4]
        shr   dl, 7                   ; byte 4 bit 7 -- the LSB
        or    edx, ecx
        sub   dl, 1                   ; ZF iff the index is 1
        je    page1                   ; ...and the fall-through gives up

    In Intel numbering that selector is perfectly contiguous -- bits 39..41,
    which is VCFRONT_lightStatusMuxIndex, a signal the extraction already
    recovers -- so the message is expressible; the compare chain simply tracks a
    selector as a byte plus a mask, and this one is neither. Rather than teach
    that walker a second model, read this shape on its own terms and offer it as
    another candidate.

    Deliberately narrow: the assembled field must SPAN A BYTE BOUNDARY, which is
    exactly the case the chain cannot express, and the branch's other side must
    be the case's bare sign-off, which is the evidence a lone comparison lacks.

    Returns the same 4-tuple as :func:`find_compare_chain`, or None.
    """
    off = elf.v2o(start)
    if store_plt is None or off is None:
        return None
    regs: dict[str, Field] = {}
    pending: int | None = None
    pend: Field | None = None
    for n, ins in enumerate(md.disasm(elf.d[off:elf.v2o(end)], start)):
        if n >= limit:
            break
        m, ops = ins.mnemonic, ins.op_str
        parts = [p.strip() for p in ops.split(",")]
        dst = _canon(parts[0]) if parts else None

        if m in ("je", "jz") and pending is not None and ops.startswith("0x"):
            t = int(ops, 0)
            spans = pend is not None and pend.modelled and pend.width > 0 \
                and not pend.big \
                and pend.start // 8 != (pend.start + pend.width - 1) // 8
            if fn_start <= t < fn_end and spans and \
                    _bare_exit(elf, md, ins.address + ins.size, store_plt):
                mask = ((1 << pend.width) - 1) << (pend.start % 8)
                # Only the untouched payload loads are any use to the page body.
                state = {r: replace(f) for r, f in regs.items()
                         if f.shl == 0 and f.shift == 0 and f.fixed is None
                         and f.mask is None and not f.narrowed}
                return pend.start // 8, mask, {pending: t}, {pending: state}
            pending = pend = None
            continue
        if m == "call":
            regs.clear()
            pending = pend = None
            continue
        if m.startswith("j"):
            pending = pend = None
            continue

        loaded = _payload_load(m, ops)
        if loaded is not None and dst:
            regs[dst] = loaded
        elif m in ("cmp", "sub") and dst in regs and len(parts) == 2:
            try:
                pending, pend = int(parts[1], 0), replace(regs[dst])
            except ValueError:
                pending = pend = None
        elif m == "test" and len(parts) == 2 and dst in regs \
                and _canon(parts[1]) == dst:
            pending, pend = 0, replace(regs[dst])
        elif dst:
            _sel_step(regs, m, parts, dst)
    return None


def dispatch_targets(elf: ElfImage, lo: int, count: int, table: int) -> dict[int, int]:
    """{selector value: branch address} for a resolved jump table."""
    toff = elf.v2o(table)
    out: dict[int, int] = {}
    for i in range(count):
        rel = struct.unpack_from("<i", elf.d, toff + i * 4)[0]
        out[lo + i] = table + rel
    return out


def discover_store_plt(elf: ElfImage, md) -> tuple[int, int]:
    """(PLT address of storeSignalValue, call count). It dominates crackMessage."""
    from collections import Counter
    s = elf.syms[CRACK_SYM]
    off = elf.v2o(s["value"])
    calls: Counter = Counter()
    for ins in md.disasm(elf.d[off:off + s["size"]], s["value"]):
        if ins.mnemonic == "call" and ins.op_str.startswith("0x"):
            calls[int(ins.op_str, 0)] += 1
    if not calls:
        raise ValueError("no calls in crackMessage -- wrong symbol?")
    return calls.most_common(1)[0]


def _double_at(elf: ElfImage, ins) -> float | None:
    m = _RIP_RE.search(ins.op_str)
    if not m:
        return None
    addr = ins.address + ins.size + int(m.group(1), 0)
    off = elf.v2o(addr)
    if off is None or off + 8 > len(elf.d):
        return None
    return struct.unpack_from("<d", elf.d, off)[0]


def _payload_load(m: str, ops: str) -> Field | None:
    """``movzx <reg>, <size> ptr [rbx + N]`` -> the pristine field it loads."""
    if m not in ("mov", "movzx", "movsx", "movsxd") or "ptr [rbx" not in ops:
        return None
    for tok, bits in _LOAD_SIZES:
        if f"{tok} ptr [rbx" in ops:
            inner = ops.split("[rbx", 1)[1].split("]", 1)[0]
            inner = inner.replace("+", "").strip() or "0"
            try:
                return Field(byte=int(inner, 0), avail=bits,
                             signed=m.startswith("movs"))
            except ValueError:
                return None                  # [rbx + rax]: not a constant offset
    return None


_VALUE_OPS = frozenset({
    "cvtsi2sd", "cvtsi2ss", "movsd", "movapd", "movaps", "movq", "movd",
    "pxor", "xorpd", "xorps", "mulsd", "addsd", "subsd", "divsd"})


def _bare_store(elf: ElfImage, md, addr: int, store_plt: int,
                limit: int = 6) -> bool:
    """Does the block at ``addr`` store a signal WITHOUT computing its value?

    GCC merges page bodies that decode the same field under different keys,
    leaving stubs that do nothing but name the signal::

        mov  rdi, rbp
        mov  esi, 0xb01e03f6
        call storeSignalValue

    UI_ventPanelControlRequest computes its value ONCE, above the branch that
    picks between two such stubs. The linear sweep arrives here long afterwards
    with nothing left to store, so the signal modelled as nothing at all -- and
    the only place its value can come from is the branch that reached it.
    """
    off = elf.v2o(addr)
    if off is None:
        return False
    named = False
    for n, ins in enumerate(md.disasm(elf.d[off:off + limit * 8], addr)):
        if n >= limit:
            return False
        m = ins.mnemonic
        if m == "call":
            return named and ins.op_str.startswith("0x") \
                and int(ins.op_str, 0) == store_plt
        if m in _VALUE_OPS or "ptr [rbx" in ins.op_str or m.startswith("j"):
            return False        # it computes its own value: not a bare stub
        if m == "mov" and ins.op_str.startswith("esi, 0x"):
            named = True
    return False


# SysV callee-saved. A zero parked in one of these survives a call, which is
# exactly why GCC zeroes r12 ONCE at the top of crackMessage and spends the next
# 1.2 MB moving it into an xmm register as the 0.0 it adds to scaled signals.
_CALLEE_SAVED = frozenset({"b", "bp", "sp", "r12", "r13", "r14", "r15"})


def extract_stores(elf: ElfImage, md, store_plt: int,
                   seeds: dict | None = None, progress=None) -> list[Store]:
    """Linear sweep of crackMessage, emitting one Store per decode site.

    ``seeds`` maps a mux page's entry address to the register state its dispatch
    preamble left live there; see :func:`_preamble_regs`.
    """
    s = elf.syms[CRACK_SYM]
    off = elf.v2o(s["value"])
    regs: dict[str, Field] = {}
    zeroed: set[str] = set()          # xmm regs known to hold 0.0
    zero_gpr: set[str] = set()        # integer regs known to hold 0
    consts: dict[str, float] = {}     # xmm regs holding a constant double
    carry: dict[int, tuple] = {}      # value state a merged store stub inherits
    val: Field | None = None          # field currently converted into xmm0
    scale, offset, scale_known = 1.0, 0.0, True
    key: int | None = None
    out: list[Store] = []

    def reset():
        nonlocal val, scale, offset, scale_known, key
        val, scale, offset, scale_known, key = None, 1.0, 0.0, True, None

    seen_ins = 0
    for ins in md.disasm(elf.d[off:off + s["size"]], s["value"]):
        seen_ins += 1
        if progress and seen_ins % 100_000 == 0:
            progress("reading decode sites", ins.address - s["value"], s["size"])
        m, ops = ins.mnemonic, ins.op_str
        parts = [p.strip() for p in ops.split(",")]
        dst = _canon(parts[0]) if parts else None

        # A mux page is entered by a branch, not by falling in, so the linear
        # sweep has the wrong register state here. Install what its dispatch
        # preamble left live. Copied: Field is mutated in place downstream.
        if seeds and ins.address in seeds:
            regs = {r: replace(f) for r, f in seeds[ins.address].items()}

        # A merged store stub has no value of its own; it inherits the one the
        # branch that reached it had already computed.
        if val is None and ins.address in carry:
            val, scale, offset, scale_known = carry.pop(ins.address)

        # --- payload load: movzx/mov/movsx <reg>, <size> ptr [rbx + N] ---
        if "ptr [rbx" in ops and m in ("mov", "movzx", "movsx", "movsxd"):
            loaded = _payload_load(m, ops)
            if loaded is not None and dst:
                regs[dst] = loaded
            continue

        # `movbe` is a load that byte-reverses on the way in -- the same
        # Motorola statement a `bswap` after a plain load makes, folded into one
        # instruction (SCCM_infoAppCrc, ESP_infoApplicationCRC, GTW_uptimeSeconds).
        if m == "movbe" and "ptr [rbx" in ops:
            loaded = _payload_load("mov", ops)
            if loaded is not None and dst:
                loaded.big = True
                loaded.be_start = loaded.byte * 8 + 7
                regs[dst] = loaded
            continue

        # Which integer registers hold zero, so `movq xmm6,r12` below can be
        # read as the 0.0 it is. Only the callee-saved ones survive a call.
        if dst and m not in ("cmp", "test", "call") and not m.startswith("j"):
            if len(parts) == 2 and (
                    (m == "xor" and parts[0] == parts[1]) or
                    (m == "mov" and parts[1] == "0")):
                zero_gpr.add(dst)
            else:
                zero_gpr.discard(dst)

        # `pxor xmm1,xmm1`, `xorpd xmm0,xmm0`, `xorps xmm0,xmm0` -- GCC picks
        # between them by which unit is free, and they all mean 0.0.
        if m in ("pxor", "xorpd", "xorps") and len(parts) == 2 \
                and parts[0] == parts[1]:
            zeroed.add(parts[0])
            consts.pop(parts[0], None)
            continue

        # An xmm register loaded from a zeroed integer register is that same
        # 0.0, written the other way round. 837 signals in 2026.8.3 reach their
        # `addsd` like this, and reading it as an unknown constant threw away a
        # scale the `mulsd` right above had already given us.
        if m in ("movq", "movd") and len(parts) == 2 and parts[0].startswith("xmm"):
            consts.pop(parts[0], None)
            if _canon(parts[1]) in zero_gpr:
                zeroed.add(parts[0])
            else:
                zeroed.discard(parts[0])
            continue

        # The scale need not be an operand of the multiply: GCC loads it into
        # its own register first and multiplies the VALUE into it --
        # `movsd xmm1,[rip+X]` / `mulsd xmm1,xmm0`. So remember which xmm holds
        # which constant, and let the arithmetic below look either operand up.
        if m in ("movsd", "movapd", "movaps") and len(parts) == 2 \
                and parts[0].startswith("xmm"):
            d = _double_at(elf, ins)
            if d is not None:
                consts[parts[0]] = d
                zeroed.discard(parts[0])
            elif parts[1].startswith("xmm"):
                src_c, src_z = consts.get(parts[1]), parts[1] in zeroed
                consts.pop(parts[0], None)
                zeroed.discard(parts[0])
                if src_c is not None:
                    consts[parts[0]] = src_c
                if src_z:
                    zeroed.add(parts[0])
            else:
                consts.pop(parts[0], None)
                zeroed.discard(parts[0])
            continue

        # --- field arithmetic ---
        if m in ("shr", "sar", "shl") and dst and dst in regs:
            f = regs[dst]
            try:
                k = int(parts[1], 0)
            except (IndexError, ValueError):
                f.ok = False
                continue
            if m == "shl":
                f.shl = k
                # Bits shifted off the top of the register are GONE, so the
                # shift is also a WIDTH bound: `shl eax,0x1f` on a byte load
                # keeps one bit, not eight. Without this the piece is read at
                # its full width and the join runs past the end of the field --
                # GTW_nmDebugWakeUp came out 39 bits instead of 32 and
                # swallowed the three signals above it.
                room = max(_REG_BITS.get(parts[0], 32) - k, 0)
                f.cap = room if f.cap is None else min(f.cap, room)
            elif m == "sar" and f.shl and k >= f.shl \
                    and 0 < (_REG_BITS.get(parts[0]) or 32) < 32:
                # `shl eax,K` then `sar ax,M`: the shl pushes the load's top bits
                # out of the SUB-register, so this is a net right shift of (M-K),
                # not of M, and the width is however much of the load still fits
                # above bit M. Reading it as a shift of M put the start (M-K) bits
                # too high -- BMS_cacMinUpdateAhError 39|9 instead of 37|9,
                # DAS_TE_vlC1 23|9 instead of 18|9. The full-register form is a
                # different animal and keeps its own rule above.
                w = _REG_BITS[parts[0]]
                f.shift += k - f.shl
                f.fixed = max(min(f.shl + f.avail - 1, w - 1) - k + 1, 0)
                f.signed = True
                f.shl = 0
            elif m == "sar" and f.shl and k < f.shl \
                    and 0 < (_REG_BITS.get(parts[0]) or 32) < 32:
                # `shl eax,K` then `sar al,M` with M < K is a net LEFT shift of
                # (K-M) inside the sub-register, sign-extended from its top bit.
                # It is how GCC places the high half of a split field: this is
                # DAS_TE_accMinJ, 3 bits of byte 4 under 2 bits of byte 5.
                # Read as a right shift of M it kept the stale `shl` and
                # modelled nothing at all.
                #
                # The width is set by the ORIGINAL shift, not the net one: the
                # `shl` pushes all but (w - K) bits of the load out of the
                # sub-register, and `sar` only moves what is left back down.
                # Sizing it by the net shift instead over-reads -- accMinJ came
                # out 8 bits where compact.json says 5.
                w = _REG_BITS[parts[0]]
                room = max(w - f.shl, 0)
                f.cap = room if f.cap is None else min(f.cap, room)
                f.shl -= k
                f.signed = True
            elif m == "sar" and f.shl == k and k:
                # `shl r,K` + `sar r,K` on the FULL register is sign extension of
                # a field K bits narrower than the load -- the shifts cancel, they
                # do not move the field. Treating the sar as a right shift would
                # push the start up by exactly (avail - width). Checked after the
                # sub-register form above, which is the more specific case.
                f.signed = True
                f.fixed = max(f.avail - k, 0)
                f.shl = 0
            else:
                f.shift += k
                if parts[0] in ("al", "bl", "cl", "dl", "sil", "dil"):
                    f.narrowed = True
                if m == "sar":
                    f.signed = True
        elif m == "lea" and dst and len(parts) > 1:
            got = _lea_shift(parts[1])
            src = _canon(got[0]) if got else None
            if got and src and src in regs:
                f = regs[src]
                regs[dst] = replace(f, shl=f.shl + got[1])
            elif dst in regs:
                del regs[dst]        # an address computation, not our field
        elif m == "add" and dst and dst in regs and len(parts) > 1 \
                and _canon(parts[1]) == dst:
            regs[dst].shl += 1       # `add eax,eax` is `shl eax,1`
        elif m == "bswap" and dst and dst in regs:
            # A whole-register byte reversal IS the Motorola case, stated outright
            # rather than inferred from which byte supplied the high half:
            #   mov eax, dword ptr [rbx+4] ; bswap eax  -> ESP_infoApplicationCRC
            # The DBC start of a Motorola field is its MSB, which after the swap
            # is bit 7 of the FIRST payload byte.
            f = regs[dst]
            bits = _REG_BITS.get(parts[0])
            if bits and f.avail == bits and not f.big and f.shift == 0 \
                    and f.mask is None and f.shl == 0 and f.fixed is None \
                    and f.cap is None:
                f.big = True
                f.be_start = f.byte * 8 + 7
            else:
                f.ok = False         # a swap of something we did not model whole
        elif m == "and" and dst and dst in regs:
            try:
                mask = int(parts[1], 0)
            except (IndexError, ValueError):
                continue
            if _is_field_mask(mask):
                regs[dst].mask = mask
            # a non-contiguous mask is the SNA test; it does not touch the field
        elif m == "or" and dst and dst in regs:
            src = _canon(parts[1]) if len(parts) > 1 else None
            if src and src in regs:
                regs[dst] = _or_join(regs[dst], regs[src])
            elif parts[1].startswith("0x"):
                pass  # `or ecx,-1` is the bus argument, not a field
        elif m in ("mov", "movzx", "movsx", "movsxd") and dst and len(parts) > 1:
            src = _canon(parts[1])
            if src and src in regs:
                f = regs[src]
                nf = Field(byte=f.byte, avail=f.avail, shift=f.shift, mask=f.mask,
                           signed=f.signed or m.startswith("movs"),
                           narrowed=f.narrowed, shl=f.shl, fixed=f.fixed,
                           big=f.big, be_start=f.be_start, cap=f.cap, ok=f.ok)
                bits = _REG_BITS.get(parts[1])
                if m in ("movzx", "movsx") and bits and bits < 32:
                    nf.cap = bits if nf.cap is None else min(nf.cap, bits)
                    if bits == 8:
                        nf.narrowed = True
                    # Truncating a Motorola field drops bits off its MSB end, so
                    # be_start no longer names the MSB. Emit nothing rather than
                    # a start bit we would be guessing at.
                    if nf.big and nf.cap < f._raw_width:
                        nf.ok = False
                regs[dst] = nf
            elif parts[1].startswith("0x") and dst == "si":
                key = int(parts[1], 0)          # the signal key argument
            elif dst in regs and not parts[1].startswith("0x"):
                del regs[dst]
        elif m in ("cwde", "cdqe"):
            # AX->EAX (or EAX->RAX) sign extension. NOT a no-op: it says the
            # assembled field is only that many bits wide and is signed. Treating
            # it as nothing left VCFRONT_PCSCurrent 17 bits wide instead of 16.
            f = regs.get("a")
            if f is not None:
                bits = 16 if m == "cwde" else 32
                f.cap = bits if f.cap is None else min(f.cap, bits)
                f.signed = True
        elif m == "cvtsi2sd" and len(parts) > 1:
            zeroed.discard(parts[0])
            consts.pop(parts[0], None)
            if "ptr [rbx" in parts[1]:
                # Straight from the payload to a double, no integer register in
                # between: `cvtsi2sd xmm1, dword ptr [rbx+4]` is TCU_mdmSINR and
                # TCU_w014_Reason. cvtsi2sd reads a SIGNED integer.
                val = _payload_load("movsxd", parts[1])
            else:
                src = _canon(parts[1])
                val = regs.get(src) if src else None
        elif m == "mulsd":
            # Either operand may be the constant: the value is multiplied INTO
            # the scale's register as often as the other way round.
            d = _double_at(elf, ins)
            if d is None and len(parts) > 1:
                d = consts.get(parts[1], consts.get(parts[0]))
            zeroed.discard(parts[0])
            consts.pop(parts[0], None)
            if d is None:
                scale_known = False
            else:
                scale *= d
        elif m == "addsd" and len(parts) > 1:
            if parts[1] == parts[0]:
                scale *= 2.0                     # addsd xmm0,xmm0 == *2
            elif parts[1] in zeroed or parts[0] in zeroed:
                # Adding a zeroed register, or adding INTO one: either way the
                # add is a move and the offset is 0. GCC writes the second form
                # as `xorpd xmm0,xmm0` / `cvtsi2sd xmm1,mem` / `addsd xmm0,xmm1`,
                # and reading it as an unknown constant threw the scale away.
                pass
            else:
                d = _double_at(elf, ins)
                if d is None:
                    d = consts.get(parts[1], consts.get(parts[0]))
                if d is None:
                    scale_known = False
                else:
                    offset += d
            zeroed.discard(parts[0])
            consts.pop(parts[0], None)
        elif m == "subsd" and len(parts) > 1:
            d = _double_at(elf, ins)
            if d is None:
                d = consts.get(parts[1])         # only the SUBTRAHEND: the
            zeroed.discard(parts[0])             # other way round negates the
            consts.pop(parts[0], None)           # value, which is not an offset
            if d is None:
                scale_known = False
            else:
                offset -= d
        elif m == "call":
            if ops.startswith("0x") and int(ops, 0) == store_plt:
                if key is not None and val is not None and val.modelled and val.width > 0:
                    _big = val.big and val.be_start is not None
                    out.append(Store(key=key,
                                     start=val.be_start if _big else val.start,
                                     width=val.width, signed=val.signed, big=_big,
                                     addr=ins.address, scale=_snap_float(scale),
                                     offset=_snap_float(offset),
                                     scale_known=scale_known))
                elif key is not None:
                    # The generator emits one store per CATALOGUED signal, so a
                    # field this revision's frame does not carry is still
                    # stored -- from a constant, with no payload read at all.
                    # That is a finished answer, not a decode we failed to
                    # follow, and saying so keeps the two apart in the report.
                    out.append(Store(key=key, start=-1, width=0, signed=False,
                                     addr=ins.address, scale_known=False,
                                     constant=val is None
                                     and ("xmm0" in zeroed or "xmm0" in consts)))
            reset()
            regs.clear()
            zeroed.clear()
            consts.clear()
            zero_gpr &= _CALLEE_SAVED
        elif m.startswith("j") and m != "jmp" and ops.startswith("0x") \
                and val is not None:
            t = int(ops, 0)
            if t > ins.address and t not in carry \
                    and _bare_store(elf, md, t, store_plt):
                carry[t] = (replace(val), scale, offset, scale_known)
        elif m in ("jmp", "ret"):
            # Nothing falls through an unconditional transfer, so whatever the
            # sweep has accumulated does NOT belong to the block that starts on
            # the next byte. GCC's unsigned-64-to-double idiom ends `addsd
            # xmm0,xmm0` / `movq rax,xmm0` / `jmp`, and reading that doubling as
            # a scale leaked it into every following block until the next store:
            # GTW_hrlPagesCount came out scaled by 2^24, DIR_Vsx by 2^26.
            #
            # The callee-saved zeros survive: a `jmp` clobbers nothing, and the
            # 0.0 in r12 is set once for the whole function.
            reset()
            regs.clear()
            zeroed.clear()
            consts.clear()
    return out


# An in-line case body is small -- the largest in 2026.8.3 is under 1.3 KB. The
# LAST case in address order has no following case to bound it, so without a cap
# find_dispatch scans to the end of the function (1.2 MB) and latches onto some
# other message's table.
_CASE_BODY_MAX = 4096


def _page_owner(elf: ElfImage, md, addr: int, store_plt: int,
                key_mid: dict[int, int], limit: int = 64) -> int | None:
    """Message id of the first signal stored at ``addr``, or None if unreadable.

    A mux page body decodes signals OF THE MESSAGE THAT DISPATCHED TO IT -- the
    keys are unique per signal, so two messages can never share a store, and a
    page whose first store belongs to somebody else was never this message's to
    claim. RCM_collision's compare chain walked out of its own case body and
    into GTW_info's, taking all four of GTW_info's pages with it; GTW_info was
    then left flat and every one of its signals overlapped every other.
    """
    off = elf.v2o(addr)
    if off is None:
        return None
    key = None
    for n, ins in enumerate(md.disasm(elf.d[off:off + limit * 16], addr)):
        if n >= limit:
            break
        if ins.mnemonic == "mov" and ins.op_str.startswith("esi, 0x"):
            key = int(ins.op_str.split(",")[1], 0)
        elif ins.mnemonic == "call":
            if ins.op_str.startswith("0x") and int(ins.op_str, 0) == store_plt \
                    and key is not None:
                return key_mid.get(key)
            return None         # the case's sign-off, or a call we cannot read
    return None


def build_regions(elf: ElfImage, md, fn_start: int, fn_end: int, progress=None,
                  sig_counts: dict[int, int] | None = None,
                  store_plt: int | None = None,
                  key_mid: dict[int, int] | None = None):
    """Map code addresses to ``(message_id, mux_id)``.

    The top-level switch gives one region per message. Inside a multiplexed
    message (every ``<NODE>_alertLog``, the alertMatrix pages) a *second* switch
    of the identical shape selects the signal set, so its branches become
    sub-regions carrying the mux value. Signals stored before that switch belong
    to every slot (mux_id None) -- that includes the selector byte itself.

    Returns ``(marks, muxsel, seeds)``: sorted [(addr, msg_id, mux_id)],
    ``{msg_id: (selector byte, selector mask | None)}``, and
    ``{page entry: register state its preamble left live}`` for extract_stores.
    """
    top = find_dispatch(elf, md, fn_start, fn_start + 4096)
    if not top:
        raise ValueError("could not find the crackMessage message switch")
    _sel, lo, count, table = top
    by_target: dict[int, list[int]] = {}
    for mid, va in dispatch_targets(elf, lo, count, table).items():
        by_target.setdefault(va, []).append(mid)
    default = max(by_target.items(), key=lambda kv: len(kv[1]))[0]
    cases = {ids[0]: va for va, ids in by_target.items() if va != default}

    marks: dict[int, tuple[int, int | None]] = {va: (mid, None)
                                                for mid, va in cases.items()}
    bounds = sorted(set(cases.values()) | {fn_end})
    muxsel: dict[int, tuple[int, int | None]] = {}
    seeds: dict[int, dict] = {}
    for done, (mid, va) in enumerate(cases.items()):
        if progress and done % 25 == 0:
            progress("mapping message cases", done, len(cases))
        nxt = next((b for b in bounds if b > va), fn_end)
        # Try both dispatch shapes and keep whichever resolves more branches.
        # find_dispatch scans the whole case body, so on a compare-chain message
        # it can latch onto an unrelated jump table further in and win with two
        # bogus slots; branch count is what actually decides which model fits.
        cands = []
        d = find_dispatch(elf, md, va, min(nxt, va + _CASE_BODY_MAX))
        if d and d[0] is not None:
            # This form carries no seeds: it reads the table outright rather
            # than walking to it, so the live register state is unknown. A table
            # with one entry is not a dispatch, hence the 2.
            cands.append((d[0], None, dispatch_targets(elf, d[1], d[2], d[3]),
                          {}, 2))
        chain = find_compare_chain(elf, md, va, fn_start, fn_end,
                                   store_plt=store_plt)
        if chain:
            # The chain refuses a single target unless the branch's other side
            # is the case's bare sign-off, so one page reaching here is already
            # evidence of a real dispatch rather than a stray comparison.
            cands.append((*chain, 1))
        joined = find_joined_selector(elf, md, va, min(nxt, va + _CASE_BODY_MAX),
                                      fn_start, fn_end, store_plt)
        if joined:
            cands.append((*joined, 1))
        if not cands:
            continue
        # Break a tie towards the model that DID walk the code: it resolved the
        # same pages and also knows which payload loads were still live at each
        # branch. Page bodies that open on a register loaded before the dispatch
        # (`shr al,4` with no load in the block) model as nothing without that.
        sel, smask, tvals, tstate, need = max(
            cands, key=lambda c: (len(set(c[2].values())), bool(c[3])))
        tby: dict[int, list[int]] = {}
        for v, t in tvals.items():
            tby.setdefault(t, []).append(v)
        if len(tby) < need:
            continue
        # A message cannot have more pages than it has signals to put on them.
        # BMS_kwhCounter has two signals and was claiming nine pages -- pages
        # that belonged to VCBATT2_alertLog, whose own stores then fell into a
        # region owned by the wrong message and were left unattributed.
        have = sig_counts.get(mid) if sig_counts else None
        if have is not None and len(tby) > max(have, 2):
            continue
        # ...and a page it claims must decode ITS OWN signals. A chain that
        # walks out of its case body and into a neighbour's dispatch resolves
        # perfectly good pages that belong to somebody else, and the message
        # they belong to is then left flat -- see _page_owner.
        if store_plt is not None and key_mid and any(
                (o := _page_owner(elf, md, t, store_plt, key_mid)) is not None
                and o != mid for t in tby):
            continue
        # The switch default is the branch several selector values share. A dense
        # table where every value has its own branch has no default at all -- and
        # a compare chain never lists one, since it is the fall-through. Dropping
        # the most-populated branch regardless would silently lose a real page.
        widest, wvals = max(tby.items(), key=lambda kv: len(kv[1]))
        mdefault = widest if len(wvals) > 1 else None
        muxsel[mid] = (sel, smask)
        for t, vals in tby.items():
            if t != mdefault and t not in marks:
                marks[t] = (mid, vals[0])
                live = tstate.get(vals[0])
                if live:
                    seeds[t] = live
    return sorted((a, m, x) for a, (m, x) in marks.items()), muxsel, seeds


def extract_layouts(vapi: str | Path, candata: str | Path, progress=None):
    """Build a compact.json-schema donor carrying same-rev layout.

    Returns ``(donor_db, Report)``. The donor slots straight into
    ``candata_to_dbc.enrich`` as the highest-priority layout source.

    ``progress`` is an optional ``callable(stage: str, done: int, total: int)``.
    This disassembles ~1.1 MB of generated code three times over, which is tens
    of seconds of silence otherwise.
    """
    def note(stage, done=0, total=0):
        if progress:
            progress(stage, done, total)
    try:
        import capstone
    except ImportError as e:  # pragma: no cover - environment dependent
        raise SystemExit(
            "vapi_layout needs capstone (pip install capstone)") from e

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    elf = ElfImage(vapi)
    if CRACK_SYM not in elf.syms:
        raise ValueError(f"{vapi}: no {CRACK_SYM} (not a libQtCarVAPI?)")

    note("reading the signal catalog")
    keys, cat_msgs = catalog_index(candata)
    note("locating storeSignalValue")
    store_plt, ncalls = discover_store_plt(elf, md)
    fn = elf.syms[CRACK_SYM]
    marks, muxsel, seeds = build_regions(
        elf, md, fn["value"], fn["value"] + fn["size"], progress,
        {m["message_id"]: len(m["signals"]) for m in cat_msgs.values()},
        store_plt,
        {k: cat_msgs[mn]["message_id"] for k, (mn, _) in keys.items()
         if mn in cat_msgs})
    stores = extract_stores(elf, md, store_plt, seeds, progress)
    note("attributing signals")
    mark_addrs = [a for a, _, _ in marks]
    id_to_name = {m["message_id"]: n for n, m in cat_msgs.items()}

    def region_for(addr: int):
        i = bisect_right(mark_addrs, addr) - 1
        return (None, None) if i < 0 else (marks[i][1], marks[i][2])

    rep = Report(lib=Path(vapi).name, calls=ncalls,
                 catalog_signals=sum(len(m["signals"]) for m in cat_msgs.values()))

    messages: dict[str, dict] = {}
    for st in stores:
        owner = keys.get(st.key)
        if owner is None:
            rep.unresolved_keys += 1
            continue
        mname, sname = owner
        if st.width <= 0 or st.start < 0 or (not st.big and st.start + st.width > 64):
            rep.gaps.append({
                "message": mname, "signal": sname,
                "reason": "stored as a constant, no payload bits"
                if st.constant else "unmodelled decode sequence"})
            continue
        meta = cat_msgs.get(mname, {})
        msg = messages.setdefault(mname, {
            "message_id": meta.get("message_id", 0),
            "length_bytes": meta.get("length_bytes") or 8,
            "cycle_time": meta.get("cycle_time", 0),
            "originNode": mname.split("_", 1)[0],
            "senders": [mname.split("_", 1)[0]],
            "signals": {},
        })
        sig = {
            "start_position": st.start,
            "width": st.width,
            "signedness": "SIGNED" if st.signed else "UNSIGNED",
            "endianness": "BIG" if st.big else "LITTLE",
            "scale": st.scale if st.scale_known else 1,
            "offset": st.offset if st.scale_known else 0,
        }
        # Only trust the mux branch when the code region agrees with the catalog
        # about which message we are in; the key is always authoritative.
        rmid, rmux = region_for(st.addr)
        if rmux is not None and id_to_name.get(rmid) == mname:
            sig["mux_id"] = rmux
        msg["signals"][sname] = sig
        rep.recovered += 1

    name_multiplexors(messages, muxsel)
    apply_guards(messages, rep)
    check_alert_pages(messages, rep)

    for mname, meta in cat_msgs.items():
        got = len(messages.get(mname, {}).get("signals", {}))
        if got != len(meta["signals"]):
            rep.message_gaps.append({"message": mname,
                                     "message_id": meta["message_id"],
                                     "recovered": got,
                                     "catalog": len(meta["signals"])})

    donor = {
        "product": "Model3",
        "version": f"vapi:{rep.lib}",
        "_label": f"vapi:{rep.lib}",
        "busMetadata": {"ETH": {"messageCount": len(messages)}},
        "messages": messages,
    }
    return donor, rep


def name_multiplexors(messages: dict, muxsel: dict) -> dict:
    """Mark each multiplexed message's selector signal. Mutates ``messages``.

    The dispatch told us which payload BYTE it switches on, and usually the MASK
    too; the signal occupying those bits is the multiplexor. Done as a pass over
    the finished message so it works whatever the selector's width, rather than
    assuming a full byte. An exact bit match wins -- a nibble selector shares its
    byte with a second nibble, and "widest signal in the byte" would pick between
    the two by coin toss, which costs the whole message at the guard.
    """
    for msg in messages.values():
        entry = muxsel.get(msg["message_id"])
        if entry is None or not any("mux_id" in s for s in msg["signals"].values()):
            continue
        sel, smask = entry
        free = [(n, s) for n, s in msg["signals"].items() if "mux_id" not in s]
        pick = None
        want = _mask_field(sel, smask) if smask is not None else None
        if want is not None:
            pick = next((n for n, s in free
                         if (s["start_position"], s["width"]) == want), None)
        if pick is None:
            cands = [(s["width"], n) for n, s in free
                     if s["start_position"] // 8 == sel]
            pick = max(cands)[1] if cands else None
        if pick is not None:
            msg["signals"][pick]["is_muxer"] = True
    return messages


def conflict_graph(sigs: dict) -> dict[str, set[str]]:
    """Which signals share a bit with which others in the same frame.

    Mux slots may legitimately reuse bits, so a signal is only compared against
    signals it can actually be received alongside: its own slot, plus every
    signal with no slot (those are read on every page).
    """
    claimed: dict[tuple, str] = {}
    graph: dict[str, set[str]] = {}
    slots = {s.get("mux_id") for s in sigs.values()} - {None}
    for sname, s in sorted(sigs.items(), key=lambda kv: kv[1]["start_position"]):
        own = s.get("mux_id")
        here = slots | {None} if own is None else {own}
        for bit in _occupied_bits(s):
            for slot in here:
                other = claimed.get((slot, bit))
                if other is not None and other != sname:
                    graph.setdefault(sname, set()).add(other)
                    graph.setdefault(other, set()).add(sname)
                claimed[(slot, bit)] = sname
    return graph


def find_conflicts(sigs: dict) -> set[str]:
    """Names of signals sharing a bit with another signal in the same frame."""
    return set(conflict_graph(sigs))


_ALERT_SIGNAL = re.compile(r"^[A-Za-z0-9]+_a(\d+)_")


def check_alert_pages(messages: dict, rep: Report) -> Report:
    """Check recovered mux pages against the alert numbers in the signal names.

    An ``<NODE>_alertLog`` selects on the alert code, so a page number IS an
    alert number -- and the catalog spells it out: ``VCBATT2_a192_hvState``
    belongs to page 192. That makes the catalog an oracle for the mux model,
    independent of the code we recovered it from, over ~18500 signals.

    It is worth running on every extraction because the failure it catches is
    silent. A dispatch whose low bound we misread still yields a full set of
    pages, all of them shifted by a constant, so coverage looks perfect while
    every alert decodes as a different alert. That is exactly what happened:
    13247 signals were wrong until the modular rebase forms were modelled.

    (Verified to be a real invariant before being relied on, against the
    ``mux_id`` compact.json ships: 21 signals across five revisions, all
    agreeing.) Records counts on ``rep``; mismatches are the ones that matter.
    """
    for mname, msg in messages.items():
        if not mname.endswith("alertLog"):
            continue
        for sname, sig in msg["signals"].items():
            m = _ALERT_SIGNAL.match(sname)
            if not m:
                continue
            want, got = int(m.group(1)), sig.get("mux_id")
            if got is None:
                rep.alert_pages_unpaged += 1
            elif got == want:
                rep.alert_pages_ok += 1
            else:
                rep.alert_pages_wrong.append(
                    {"message": mname, "signal": sname,
                     "page": got, "alert": want})
    return rep


def apply_guards(messages: dict, rep: Report) -> dict:
    """Drop anything that would make an unusable DBC. Mutates ``messages``.

    cantools rejects overlapping signals outright. Prune the least trustworthy
    signals first and only give up on a whole message if it is still
    inconsistent, because these messages are the big ones -- 2026.8.3's
    VCFRONT2_alertLog carries 458 signals, of which 444 were recovered
    perfectly. Trust runs, least to most:

    * A signal in a multiplexed message with NO page attributed. It is read on
      every page, so if we simply failed to attribute its page it collides with
      all of them. A genuinely mux-independent signal (a counter, a checksum)
      sits in bits no page uses and conflicts with nothing -- so conflict, not
      the mere absence of a page, is what tells the two apart.
    * An ordinary signal: drop the conflicting pair.
    * The multiplexor, which is the most corroborated signal in the message --
      the dispatch we read the pages from is a branch on exactly these bits.
      Never drop it to save an ordinary signal; without it the pages cannot be
      expressed at all and the whole message has to go.

    Only when conflicts survive all that is the layout model for the message
    considered broken, and the message handed to a compact.json donor.
    """
    def orphaned_pages(sigs: dict) -> bool:
        """Pages present but no selector -- cantools would read them flat."""
        return (any("mux_id" in s for s in sigs.values())
                and not any(s.get("is_muxer") for s in sigs.values()))

    def hand_to_donor(mname: str, reason: str) -> None:
        msg = messages.pop(mname)
        rep.recovered -= len(msg["signals"])
        rep.muxed.append({"message": mname, "message_id": msg["message_id"],
                          "signals": len(msg["signals"]), "reason": reason})

    def drop(mname: str, sigs: dict, names, reason: str) -> None:
        for sname in names:
            del sigs[sname]
            rep.recovered -= 1
            rep.gaps.append({"message": mname, "signal": sname,
                             "reason": reason})

    for mname in list(messages):
        msg = messages[mname]
        sigs = msg["signals"]
        # A message using mux_id must also name its multiplexor, or the DBC is
        # unusable. If we recovered slots but never identified the selector, the
        # mux model for this message is incomplete -- hand it to a donor.
        if orphaned_pages(sigs):
            hand_to_donor(mname, "no multiplexor found")
            continue
        conflicted = find_conflicts(sigs)
        if not conflicted:
            continue

        # A conflicting signal with no page, in a message that HAS pages: we
        # failed to attribute it, and it now collides with every page. In
        # 2026.8.3 fourteen of these were enough to condemn all 458 signals of
        # VCFRONT2_alertLog; without them the remaining 444 are consistent.
        if any(s.get("is_muxer") for s in sigs.values()):
            mispaged = {n for n in conflicted
                        if "mux_id" not in sigs[n] and not sigs[n].get("is_muxer")}
            if mispaged:
                drop(mname, sigs, mispaged, "page not attributed")
                conflicted = find_conflicts(sigs)

        # The multiplexor outranks whatever it collides with.
        if any(sigs[n].get("is_muxer") for n in conflicted):
            drop(mname, sigs, {n for n in conflicted if not sigs[n].get("is_muxer")},
                 "overlaps the multiplexor")
            conflicted = find_conflicts(sigs)

        # Prune the worst offender, then re-check. One over-wide field collides
        # with everything beneath it, so dropping the whole conflicted set
        # discards its victims along with it: GTW_nmDebugWakeUp took
        # OTAKeepAwake, smsPokeReceived and bdyBusAsleep down, and an 11-signal
        # message went to a donor over one bad signal. Highest degree, then
        # widest, is the one least likely to be right. Bound the DROPS rather
        # than the conflicts -- a single bad field can conflict with many
        # signals and still be one deletion away from consistent.
        limit = max(2, 0.25 * len(sigs))
        dropped = 0
        while True:
            graph = conflict_graph(sigs)
            if not graph:
                break
            cands = [n for n in graph if not sigs[n].get("is_muxer")]
            if dropped >= limit or not cands:
                hand_to_donor(mname, "unresolved mux branches")
                break
            worst = max(cands, key=lambda n: (len(graph[n]), sigs[n]["width"], n))
            drop(mname, sigs, [worst], "overlaps another signal")
            dropped += 1
        if mname not in messages:
            continue
        # Pruning can still take the multiplexor with it, and pages with no
        # selector are exactly what cantools refuses to load.
        if orphaned_pages(sigs):
            hand_to_donor(mname, "multiplexor lost to an overlap")
    return messages


def _sibling_candata(vapi: Path) -> Path:
    p = vapi.parent / "libQtCarCANData.so.1.0.0"
    if not p.exists():
        raise SystemExit(f"no libQtCarCANData.so.1.0.0 beside {vapi}; pass --candata")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vapi", type=Path, help="libQtCarVAPI.so(.1.0.0)")
    ap.add_argument("--candata", type=Path, help="libQtCarCANData.so (default: sibling)")
    ap.add_argument("-o", "--out", type=Path, help="write the donor JSON here")
    ap.add_argument("--report", type=Path, help="write a coverage/gap report here")
    args = ap.parse_args(argv)

    candata = args.candata or _sibling_candata(args.vapi)
    donor, rep = extract_layouts(args.vapi, candata)

    pct = 100.0 * rep.recovered / rep.catalog_signals if rep.catalog_signals else 0.0
    print(f"{rep.lib}: {len(donor['messages'])} messages, "
          f"{rep.recovered}/{rep.catalog_signals} signals ({pct:.1f}%) "
          f"[{rep.calls} decode sites]")
    # Account for every catalogued signal, so the numbers reconcile.
    from collections import Counter
    if rep.unresolved_keys:
        print(f"  unresolved keys: {rep.unresolved_keys}")
    for reason, n in Counter(g["reason"] for g in rep.gaps).items():
        print(f"  dropped signal   x{n:<6} {reason}")
    mux_sigs = sum(m["signals"] for m in rep.muxed)
    for reason, n in Counter(m.get("reason", "unresolved mux branches")
                             for m in rep.muxed).items():
        sigs = sum(m["signals"] for m in rep.muxed
                   if m.get("reason", "unresolved mux branches") == reason)
        print(f"  dropped message  x{n:<6} {reason} ({sigs} signals)")
    total = rep.recovered + len(rep.gaps) + mux_sigs
    if total != rep.catalog_signals:
        print(f"  WARNING: {total} accounted vs {rep.catalog_signals} catalogued")
    # The mux model checked against the alert numbers in the signal names.
    if rep.alert_pages_wrong:
        print(f"  WARNING: {len(rep.alert_pages_wrong)} alertLog signals are on "
              f"the WRONG page (of {rep.alert_pages_ok + len(rep.alert_pages_wrong)}"
              " checked)")
        for w in rep.alert_pages_wrong[:5]:
            print(f"      {w['signal']}: page {w['page']}, alert {w['alert']}")
    elif rep.alert_pages_ok:
        print(f"  alert pages     {rep.alert_pages_ok} correct, "
              f"{rep.alert_pages_unpaged} unpaged, 0 wrong")

    if args.out:
        args.out.write_text(json.dumps(donor, indent=1))
        print(f"wrote {args.out}")
    if args.report:
        lines = [f"lib: {rep.lib}",
                 f"decode sites: {rep.calls}",
                 f"recovered: {rep.recovered}/{rep.catalog_signals}",
                 f"unresolved keys: {rep.unresolved_keys}", "",
                 "== messages with missing signals =="]
        lines += [f"  {g['message_id']:#05x} {g['message']:34s} "
                  f"{g['recovered']}/{g['catalog']}" for g in rep.message_gaps]
        lines += ["", "== unmodelled decode sequences =="]
        lines += [f"  {g['message']}.{g['signal']}" for g in rep.gaps]
        args.report.write_text("\n".join(lines) + "\n")
        print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
