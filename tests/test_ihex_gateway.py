"""Decode the real Highland gateway image (GW.HGZ) with the project's tooling.

GW.HGZ is the gzip'd Intel HEX gateway app (gtw3/221/gwapp.img) captured from a
Tesla service SD card. It is a dual-bank image: two type-05 Start Linear Address
records (one entry point per bank), which intelhex's loader rejects as a
DuplicateStartAddressRecordError. ihex.parse_* strips the start-address records
and keeps all data, so both banks decode. These tests lock in that behaviour and
the exact bank layout of this build (git SHA 067a1dfc...).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from firmware_report import _load_image
from ihex import decode_to_hex, load_intelhex, parse_bytes, parse_file, to_hex_text

_HGZ = Path(__file__).parent / "fixtures" / "highland" / "GW.HGZ"

# Bank layout of this gateway build, verified against the decoded image.
_BANK_A = (0x00FB0000, 113504, "e776ce6d")
_BANK_B = (0x00FD0000, 113504, "1e9456e4")


@pytest.fixture(scope="module")
def hgz_bytes() -> bytes:
    return gzip.decompress(_HGZ.read_bytes())


class TestGatewayDualBankDecode:
    def test_fixture_present(self):
        assert _HGZ.exists()

    def test_parse_bytes_yields_two_banks(self, hgz_bytes):
        img = parse_bytes(hgz_bytes)
        assert len(img.segments) == 2

    def test_bank_addresses_and_sizes(self, hgz_bytes):
        img = parse_bytes(hgz_bytes)
        banks = sorted(img.segments, key=lambda s: s.start_address)
        for seg, (addr, length, _crc) in zip(banks, (_BANK_A, _BANK_B), strict=True):
            assert seg.start_address == addr
            assert seg.length == length

    def test_bank_crc32s(self, hgz_bytes):
        img = parse_bytes(hgz_bytes)
        banks = sorted(img.segments, key=lambda s: s.start_address)
        crcs = [f"{s.compute_crc32():08x}" for s in banks]
        assert crcs == [_BANK_A[2], _BANK_B[2]]

    def test_banks_are_distinct(self, hgz_bytes):
        # Two genuinely different banks, not a duplicate of one.
        img = parse_bytes(hgz_bytes)
        a, b = sorted(img.segments, key=lambda s: s.start_address)
        assert a.data != b.data
        assert a.compute_crc32() != b.compute_crc32()

    def test_total_payload(self, hgz_bytes):
        img = parse_bytes(hgz_bytes)
        assert sum(s.length for s in img.segments) == _BANK_A[1] + _BANK_B[1]


class TestLoadImageHandlesHgz:
    """firmware_report._load_image must decode this .hgz without raising."""

    def test_load_image_kind_and_segments(self):
        img, kind = _load_image(_HGZ)
        assert kind == "hgz(ihex)"
        assert len(img.segments) == 2


class TestDecodeToHex:
    """`ihex decode` gunzips a .hgz and emits canonical, re-parseable Intel HEX."""

    def test_decode_writes_hex_and_round_trips(self, tmp_path):
        out = decode_to_hex(_HGZ, tmp_path / "gw.hex")
        assert out.exists() and out.suffix == ".hex"
        text = out.read_text()
        # Canonical Intel HEX: ends with the EOF record.
        assert text.rstrip().endswith(":00000001FF")
        # The duplicate Start Linear Address records are gone on the round trip.
        dup_starts = [
            ln for ln in text.splitlines()
            if ln.startswith(":") and ln[7:9] in ("03", "05")
        ]
        assert len(dup_starts) <= 1

    def test_round_trip_preserves_banks(self, tmp_path):
        out = decode_to_hex(_HGZ, tmp_path / "gw.hex")
        reparsed = parse_file(out)
        banks = sorted(reparsed.segments, key=lambda s: s.start_address)
        assert [(s.start_address, s.length, f"{s.compute_crc32():08x}") for s in banks] == [
            _BANK_A, _BANK_B,
        ]

    def test_default_dest_is_hex_suffix(self, tmp_path):
        src = tmp_path / "copy.hgz"
        src.write_bytes(_HGZ.read_bytes())
        out = decode_to_hex(src)
        assert out == src.with_suffix(".hex")
        assert out.exists()

    def test_load_intelhex_and_to_hex_text_agree(self, tmp_path):
        ih = load_intelhex(_HGZ)
        text = to_hex_text(ih)
        rt = tmp_path / "rt.hex"
        rt.write_text(text)
        assert sum(s.length for s in parse_file(rt).segments) == _BANK_A[1] + _BANK_B[1]


class TestParseFileAcceptsPathAndStr:
    def test_path_and_str_agree(self, hgz_bytes, tmp_path):
        # parse_file takes decoded HEX text (the .hgz is gzip; _load_image gunzips).
        hex_path = tmp_path / "gw.hex"
        hex_path.write_bytes(hgz_bytes)
        from_path = parse_file(hex_path)
        from_str = parse_file(str(hex_path))
        assert [s.start_address for s in from_path.segments] == [
            s.start_address for s in from_str.segments
        ]
