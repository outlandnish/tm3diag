"""vapi_probe: recovering layouts from a decoder by setting one payload bit at a time.

A hand-written decoder stands in for crackMessage, so these need no .so.
"""
import candata_to_dbc
import so_candata
import vapi_probe

MID, NAME = 0x2B2, "BMS_energyStatus"
KEYS = {1: (NAME, "mux"), 2: (NAME, "common"), 3: (NAME, "energy"), 4: (NAME, "current"),
        5: (NAME, "flag"), 9: (NAME, "neverStored"), 7: ("OTHER_msg", "x")}


def _field(v, st, w, signed=False):
    raw = v >> st & ((1 << w) - 1)
    return raw - (1 << w) if signed and raw >> (w - 1) else raw


def _decode(mid, payload):
    """Pages 0 and 1 of a 2-bit mux @0; 2 and 3 are not pages. ``common`` is stored for
    every selector value."""
    if mid != MID:
        return ()
    v = int.from_bytes(payload, "little")
    mux = v & 3
    hits = [(1, float(mux), 1), (7, 1.0, 1),  # another message's key is ignored
            (2, _field(v, 8, 4) * 1.0, 1)]
    if mux == 0:
        hits += [(3, _field(v, 16, 16) * 0.02, 1)]
    elif mux == 1:
        hits += [(4, _field(v, 16, 16, signed=True) * 0.1 - 5.0, 1),
                 (5, float(_field(v, 40, 1)), 1)]
    return tuple(hits)


def _run(items):
    return [_decode(mid, data) for mid, data in items]


def _probe():
    return vapi_probe.probe_messages({MID: NAME}, _run, KEYS)[MID]


def test_finds_the_mux_and_its_pages():
    p = _probe()
    assert p.muxer == "mux" and p.pages == [0, 1]
    assert p.signals["mux"] == {"start_position": 0, "width": 2, "endianness": "LITTLE",
                                "signedness": "UNSIGNED", "scale": 1, "offset": 0,
                                "is_muxer": True}


def test_recovers_scale_offset_sign_and_page():
    s = _probe().signals
    assert s["energy"] == {"start_position": 16, "width": 16, "endianness": "LITTLE",
                           "signedness": "UNSIGNED", "scale": 0.02, "offset": 0.0,
                           "mux_id": 0}
    assert s["current"] == {"start_position": 16, "width": 16, "endianness": "LITTLE",
                            "signedness": "SIGNED", "scale": 0.1, "offset": -5.0,
                            "mux_id": 1}
    assert s["flag"]["start_position"] == 40 and s["flag"]["mux_id"] == 1


def test_a_signal_on_every_selector_value_is_not_muxed():
    s = _probe().signals["common"]
    assert (s["start_position"], s["width"]) == (8, 4) and "mux_id" not in s


def test_a_signal_on_the_only_page_is_still_muxed():
    # SDCR_alertMatrix: alerts on page 0, its only page; values 1-15 store just the index.
    # "On every page" is not "on every selector value" -- unmuxed, a DBC would decode them
    # on all sixteen.
    def run(items):
        out = []
        for _mid, d in items:
            v = int.from_bytes(d, "little")
            hits = [(1, float(v & 0xF), 1)]
            if v & 0xF == 0:
                hits.append((5, float(_field(v, 8, 1)), 1))
            out.append(tuple(hits))
        return out
    p = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID]
    assert p.pages == [0] and p.signals["flag"]["mux_id"] == 0


def test_a_catalog_name_the_decoder_never_stores_is_known_as_such():
    p = _probe()
    assert "neverStored" not in p.stored and "energy" in p.stored


def test_an_unmuxed_message_is_read_from_the_single_bit_pass():
    def run(items):
        return [((3, _field(int.from_bytes(d, "little"), 4, 8) * 0.5 + 1.0, 1),)
                for _mid, d in items]
    p = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID]
    assert p.muxer is None
    assert p.signals["energy"] == {"start_position": 4, "width": 8, "endianness": "LITTLE",
                                   "signedness": "UNSIGNED", "scale": 0.5, "offset": 1.0}


def test_sparse_pages_still_find_the_whole_selector():
    # VCLEFT_thermalLogging10Hz: 3-bit mux, pages 2-4 only. From zero, bit 0 goes page 0 -> 1,
    # neither of which exists, so it switches no page -- but it still moves the muxer.
    def run(items):
        out = []
        for _mid, d in items:
            v = int.from_bytes(d, "little")
            mux = v & 7
            hits = [(1, float(mux), 1)]
            if mux in (2, 3, 4):
                hits.append((3 if mux == 2 else 4, _field(v, 8, 8) * 1.0, 1))
            out.append(tuple(hits))
        return out
    p = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID]
    assert p.pages == [2, 3, 4]
    assert (p.signals["mux"]["start_position"], p.signals["mux"]["width"]) == (0, 3)
    assert p.signals["energy"]["mux_id"] == 2 and p.signals["current"]["mux_id"] == 3


def test_pages_no_single_bit_reaches_are_found_from_a_lone_page0_signal():
    # PMR_info: 8-bit index, pages 10/11/13 -- each two bits from zero, and page 0 holds only
    # the index. No donor hint.
    def run(items):
        out = []
        for _mid, d in items:
            v = int.from_bytes(d, "little")
            idx = v & 0xFF
            hits = [(1, float(idx), 1)]
            if idx in (10, 11, 13):
                hits.append(({10: 3, 11: 4, 13: 5}[idx], _field(v, 16, 8) * 1.0, 1))
            out.append(tuple(hits))
        return out
    p = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID]
    assert p.muxer == "mux" and p.pages == [10, 11, 13]
    assert p.signals["flag"]["mux_id"] == 13


def test_a_lone_signal_that_switches_nothing_is_read_flat():
    def run(items):
        return [((3, _field(int.from_bytes(d, "little"), 0, 8) * 2.0, 1),) for _m, d in items]
    p = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID]
    assert p.muxer is None and p.pages == []
    assert p.signals["energy"]["width"] == 8 and p.signals["energy"]["scale"] == 2.0


def test_apply_drops_an_unconfirmed_donor_layout_that_overlaps():
    cat = so_candata.SoCatalog(lib="t.so", bus="ETH", messages={NAME: {
        "message_id": MID, "length_bytes": 8, "cycle_time": 2000, "originNode": "BMS",
        "signals": {n: {} for n in ("mux", "common", "energy", "current", "flag", "stale")}}})
    stale = {"start_position": 20, "width": 8, "endianness": "LITTLE",
             "signedness": "UNSIGNED", "scale": 1, "offset": 0}  # flat, over energy's bits
    donor = {"_label": "vapi", "messages": {NAME: {"message_id": MID,
                                                   "signals": {"stale": stale}}}}
    db, report = candata_to_dbc.enrich(cat, [donor])
    n = candata_to_dbc.apply_probes(db, report, cat, {MID: _probe()})
    assert n["dropped"] == 1 and "stale" not in db["messages"][NAME]["signals"]


def test_a_big_endian_signal_is_recovered():
    # GTW_hrlTriggerDuration: 16-bit Motorola, MSB at bit 15 (byte 1 bit 7 -> byte 2 bit 0).
    def run(items):
        out = []
        for _mid, d in items:
            raw = d[1] << 8 | d[2]                       # byte 1 is the high byte
            raw = raw - (1 << 16) if raw >> 15 else raw  # signed
            out.append(((3, raw * 0.5, 1),))
        return out
    s = vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID].signals["energy"]
    assert s == {"start_position": 15, "width": 16, "endianness": "BIG",
                 "signedness": "SIGNED", "scale": 0.5, "offset": 0.0}


def test_nonlinear_bits_are_left_to_the_donors():
    def run(items):  # bit 1 weighs 3x bit 0: not a binary field
        return [((3, float((d[0] & 1) + 3 * (d[0] >> 1 & 1)), 1),) for _m, d in items]
    assert "energy" not in vapi_probe.probe_messages({MID: NAME}, run, KEYS)[MID].signals


def test_a_faulting_message_is_skipped():
    assert vapi_probe.probe_messages({MID: NAME}, lambda items: [None] * len(items),
                                     KEYS) == {}


def test_apply_fills_gaps_corrects_donors_and_tags_never_stored():
    cat = so_candata.SoCatalog(lib="t.so", bus="ETH", messages={NAME: {
        "message_id": MID, "length_bytes": 8, "cycle_time": 2000, "originNode": "BMS",
        "signals": {n: {"units": "kWh" if n == "energy" else ""}
                    for n in ("mux", "common", "energy", "current", "flag", "neverStored")}}})
    flat = {"start_position": 16, "width": 16, "endianness": "LITTLE",
            "signedness": "UNSIGNED", "scale": 1, "offset": 0}
    donor = {"_label": "vapi", "messages": {NAME: {"message_id": MID, "signals": {
        "energy": flat}}}}  # donor has energy flat (wrong), knows nothing else
    db, report = candata_to_dbc.enrich(cat, [donor])
    assert report["signals_gapped"] == 5
    n = candata_to_dbc.apply_probes(db, report, cat, {MID: _probe()})
    assert n == {"messages": 1, "filled": 4, "corrected": 1, "dropped": 0, "not_stored": 1}
    sigs = db["messages"][NAME]["signals"]
    assert sigs["energy"]["mux_id"] == 0 and sigs["energy"]["scale"] == 0.02
    assert sigs["energy"]["units"] == "kWh"  # catalog enrichment survives
    assert sigs["mux"]["is_muxer"]
    assert [(g["signal"], g.get("not_stored")) for g in report["gaps"]] == [
        ("neverStored", True)]
