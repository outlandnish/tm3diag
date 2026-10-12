"""Recover signal layouts by probing the MCU's own decoder one payload bit at a time.

``vapi_layout`` reads ``crackMessage`` statically and misses some signals (2024.8.9: 5879 of
29221 catalog signals had no layout). ``vapi_emu`` runs it. Setting one bit at a time and
watching which stored value moves gives each signal's start and width; the value at its top bit
gives the scale and sign, the all-zero value the offset. Bits that change WHICH signals get
stored are the multiplexer, and every value it can take is tried as a page.

What comes back is the firmware's truth, so it outranks every donor -- and it can say a catalog
signal is never stored at all, which no layout source can. Only linearly scaled signals are
recovered (either byte order); anything else is left to the donors.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

# A wider selector (2^n pages to try) is left to the donors.
MAX_MUX_BITS = 10

Runner = Callable[[list[tuple[int, bytes]]], list[tuple | None]]
KeyMap = dict[int, tuple[str, str]]


@dataclass
class Probe:
    """What one message decodes to. ``signals`` maps name -> compact-schema layout."""

    signals: dict[str, dict] = field(default_factory=dict)
    stored: set[str] = field(default_factory=set)  # every signal the decoder ever stored
    muxer: str | None = None
    pages: list[int] = field(default_factory=list)


def _f32(x: float) -> float:
    """Shortest decimal that is the same float32 -- the firmware's constants are floats."""
    try:
        target = struct.pack("<f", x)
    except OverflowError:
        return x
    for d in range(1, 10):
        s = float(f"{x:.{d}g}")
        if struct.pack("<f", s) == target:
            return s
    return x


def _payload(v: int) -> bytes:
    return v.to_bytes(8, "little")


def _values(hits: tuple | None, keymap: KeyMap, mname: str) -> dict[str, float] | None:
    if hits is None:
        return None
    return {keymap[k][1]: val for k, val, _ok in hits
            if k in keymap and keymap[k][0] == mname}


def _motorola(msb: int, width: int) -> list[int]:
    """Bits of a big-endian signal, MSB first: down within a byte, then bit 7 of the next."""
    out, byte, bit = [], msb // 8, msb % 8
    for _ in range(width):
        out.append(byte * 8 + bit)
        byte, bit = (byte + 1, 7) if bit == 0 else (byte, bit - 1)
    return out


def _layout(bits: list[int], v0: float, at: dict[int, dict[str, float]],
            name: str) -> dict | None:
    """A linear layout, either byte order, from the bits that move ``name``; else None.

    Each bit's value delta is its weight: the weights must be lsb * 2^0 .. 2^(w-1), only the
    top one may be negative (two's complement), and the bits in weight order must run the
    Intel or the Motorola way.
    """
    w = len(bits)
    delta = {b: at[b][name] - v0 for b in bits}
    lsb = min(abs(d) for d in delta.values())
    if lsb == 0:
        return None
    exp = {}
    for b, d in delta.items():
        e = round(math.log2(abs(d) / lsb))
        if not math.isclose(abs(d), lsb * 2 ** e, rel_tol=1e-3):
            return None
        exp[b] = e
    if sorted(exp.values()) != list(range(w)):
        return None
    seq = sorted(bits, key=exp.__getitem__)          # LSB first
    top = seq[-1]
    if any(delta[b] < 0 for b in seq[:-1]):
        return None
    signed = delta[top] < 0 and w > 1
    scale = abs(delta[top]) / 2 ** (w - 1)           # widest delta: least rounding
    if seq == list(range(seq[0], seq[0] + w)):
        start, order = seq[0], "LITTLE"
    elif seq[::-1] == _motorola(top, w):
        start, order = top, "BIG"
    else:
        return None
    return {"start_position": start, "width": w, "endianness": order,
            "signedness": "SIGNED" if signed else "UNSIGNED",
            "scale": _f32(scale), "offset": _f32(v0)}


def _own(base: dict[str, float], at: dict[int, dict[str, float] | None],
         skip: set[int]) -> dict[str, list[int]]:
    """Signal -> the bits whose flip moves its value (key-set changes are ignored)."""
    owners: dict[str, list[int]] = {}
    for b in range(64):
        got = at.get(b)
        if b in skip or got is None:
            continue
        for n, v in base.items():
            if n in got and got[n] != v:
                owners.setdefault(n, []).append(b)
    return owners


def _page_layouts(base, at, skip) -> dict[str, dict]:
    """Layouts on one page. Owned bits are in bit order, which _layout re-sorts by weight."""
    out = {}
    for n, bits in _own(base, at, skip).items():
        lay = _layout(bits, base[n], at, n)
        if lay is not None:
            out[n] = lay
    return out


def probe_messages(msgs: dict[int, str], run: Runner, keymap: KeyMap,
                   progress: Callable[[str], None] | None = None,
                   hints: dict[int, tuple[int, int]] | None = None) -> dict[int, Probe]:
    """Probe each ``{message_id: catalog name}`` through ``run`` (a batch decoder).

    ``hints`` gives a donor's selector field ``(start, width)`` per message, tried when no
    single bit switches pages -- pages 10/11/13/20 are all two bits from zero.
    """
    hints = hints or {}
    say = progress or (lambda _s: None)
    ids = sorted(msgs)
    out: dict[int, Probe] = {}

    # Phase 1: all-zero payload + every single bit.
    say(f"probing {len(ids)} messages: single bits")
    items = [(mid, _payload(v)) for mid in ids for v in [0, *(1 << b for b in range(64))]]
    res = run(items)
    plan: dict[int, tuple[int, int]] = {}  # mid -> (mux start, mux width)
    first: dict[int, tuple[dict, dict]] = {}  # mid -> phase-1 (base, at), for a fallback
    for i, mid in enumerate(ids):
        name = msgs[mid]
        row = [_values(h, keymap, name) for h in res[i * 65:(i + 1) * 65]]
        base, at = row[0], dict(enumerate(row[1:]))
        if base is None:
            continue
        p = out[mid] = Probe()
        first[mid] = (base, at)
        p.stored |= set(base).union(*(set(a) for a in at.values() if a))
        switch = [b for b, a in at.items() if a is not None and set(a) != set(base)]
        if not switch:
            # No single bit reaches a page (PMR_info: pages 10/11/13/17-20). Try a donor's
            # selector, else a lone signal on page 0 -- a selector with no page at zero. Phase 2
            # confirms it; a candidate that switches nothing falls back to a flat read.
            owners = _own(base, at, set())
            field = hints.get(mid)
            if field is None and len(base) == 1:
                bits = owners.get(next(iter(base)), [])
                field = (bits[0], len(bits)) if bits else None
            if field is not None:
                hs, hw = field
                owner = [n for n, bits in owners.items() if bits == list(range(hs, hs + hw))]
                if len(owner) == 1 and 0 < hw <= MAX_MUX_BITS:
                    p.muxer = owner[0]
                    plan[mid] = (hs, hw)
                    continue
            p.signals = _page_layouts(base, at, set())
            continue
        # The muxer is the one signal every page-switching bit moves. Its whole field is every
        # bit that moves it: a selector bit only switches pages where a neighbour page exists
        # (pages 2-4 of a 3-bit mux: bit 0 from zero goes 0 -> 1, neither of which is a page).
        movers = [n for n in base if all(at[b].get(n, base[n]) != base[n] for b in switch)]
        if len(movers) != 1:
            continue
        mux = _own(base, at, set()).get(movers[0], [])
        st, w = (mux[0], len(mux)) if mux else (0, 0)
        if not mux or mux != list(range(st, st + w)) or w > MAX_MUX_BITS \
                or not set(switch) <= set(mux):
            continue
        p.muxer = movers[0]
        plan[mid] = (st, w)

    # Phase 2: which selector values are pages.
    say(f"probing {len(plan)} multiplexed messages: pages")
    items = [(mid, _payload(v << st)) for mid, (st, w) in plan.items() for v in range(1 << w)]
    res = iter(run(items))
    pages: dict[int, dict[int, dict]] = {}
    everywhere: dict[int, set[str]] = {}  # stored for EVERY selector value, page or not
    for mid, (_st, w) in plan.items():
        name, p = msgs[mid], out[mid]
        by_v = {v: _values(next(res), keymap, name) or {} for v in range(1 << w)}
        everywhere[mid] = set.intersection(*(set(vals) for vals in by_v.values()))
        for v, vals in by_v.items():
            p.stored |= set(vals)
            if set(vals) - everywhere[mid] - {p.muxer}:
                pages.setdefault(mid, {})[v] = vals

    # Phase 3: every page, one bit at a time (selector bits held).
    work = [(mid, v) for mid in pages for v in pages[mid]]
    say(f"probing {len(work)} pages: single bits")
    items = []
    for mid, v in work:
        st, w = plan[mid]
        items += [(mid, _payload((v << st) | (1 << b))) for b in range(64)]
    res = run(items)
    per_sig: dict[int, dict[str, list[tuple[int, dict]]]] = {}
    for j, (mid, v) in enumerate(work):
        st, w = plan[mid]
        name, p = msgs[mid], out[mid]
        at = {b: _values(h, keymap, name) for b, h in enumerate(res[j * 64:(j + 1) * 64])}
        for a in at.values():
            if a:
                p.stored |= set(a)
        skip = set(range(st, st + w))
        for n, lay in _page_layouts(pages[mid][v], at, skip).items():
            per_sig.setdefault(mid, {}).setdefault(n, []).append((v, lay))

    for mid, (st, w) in plan.items():
        p = out[mid]
        p.pages = sorted(pages.get(mid, {}))
        if not p.pages:
            p.muxer = None
            p.signals = _page_layouts(*first[mid], set())
            continue
        p.signals[p.muxer] = {"start_position": st, "width": w, "endianness": "LITTLE",
                              "signedness": "UNSIGNED", "scale": 1, "offset": 0,
                              "is_muxer": True}
        for n, seen in per_sig.get(mid, {}).items():
            if n == p.muxer:
                continue
            first_page, lay = seen[0]
            # Unmuxed only if stored for every selector value -- a DBC decodes an unmuxed
            # signal on all of them, not just the pages (SDCR_alertMatrix: alerts on page 0,
            # its only page, and nothing on 1-15).
            if all(s == lay for _v, s in seen) and n in everywhere.get(mid, set()):
                p.signals[n] = lay
            else:
                p.signals[n] = {**lay, "mux_id": first_page}
    return out
