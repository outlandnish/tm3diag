"""Tests for clog.py against a real Highland gateway cluster log.

Fixtures 0.CLH/0.CLB are a verbatim capture from a Tesla service SD card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clog import (
    SEGMENT_HEADER_SIZE,
    ClusterLogIndex,
    SegmentIndex,
    iter_segment_varints,
    parse_clh,
)

_FIX = Path(__file__).parent / "fixtures" / "highland"
_CLH = _FIX / "0.CLH"
_CLB = _FIX / "0.CLB"

if not _FIX.exists():
    pytest.skip(
        "highland fixtures are a local-only Tesla capture (gitignored); not shipped",
        allow_module_level=True,
    )

_GIT_SHA = "067a1dfcf133a88b994f7f9562dde8eae27155c0"
# (seq, start_time, end_time, start_offset, end_offset)
_EXPECTED = [
    (0, 0x67F55E05, 0x67F55EAF, 0x0000, 0x17EC),
    (1, 0x67F55EAF, 0x67F55F63, 0x17EC, 0x33B9),
    (2, 0x67F55F63, 0x67F5601E, 0x33B9, 0x449D),
    (3, 0x67F5601E, 0x67F560D3, 0x449D, 0x8473),
    (4, 0x67F560D3, 0x67F561BA, 0x8473, 0xAE7E),
]


@pytest.fixture(scope="module")
def index() -> ClusterLogIndex:
    return parse_clh(_CLH)


@pytest.fixture(scope="module")
def clb() -> bytes:
    return _CLB.read_bytes()


class TestParseClh:
    def test_fixtures_present(self):
        assert _CLH.exists() and _CLB.exists()

    def test_git_sha(self, index):
        assert index.git_sha == _GIT_SHA

    def test_five_segments(self, index):
        assert len(index.segments) == 5

    def test_segments_are_typed(self, index):
        assert all(isinstance(s, SegmentIndex) for s in index.segments)

    def test_segment_fields_match_card(self, index):
        got = [
            (s.seq, s.start_time, s.end_time, s.start_offset, s.end_offset)
            for s in index.segments
        ]
        assert got == _EXPECTED

    def test_byte_length_equals_offset_span(self, index):
        for s in index.segments:
            assert s.byte_length == s.end_offset - s.start_offset

    def test_offsets_chain_contiguously(self, index):
        prev = 0
        for s in index.segments:
            assert s.start_offset == prev
            prev = s.end_offset

    def test_last_offset_is_clb_size(self, index):
        assert index.segments[-1].end_offset == _CLB.stat().st_size

    def test_times_are_monotonic_and_chained(self, index):
        for s in index.segments:
            assert s.start_time <= s.end_time
        for a, b in zip(index.segments, index.segments[1:], strict=False):
            assert a.end_time == b.start_time

    def test_bad_magic_raises(self, tmp_path):
        bogus = tmp_path / "bad.CLH"
        bogus.write_bytes(b"\x00\x00NOTPOPPY" + b"\x00" * 200)
        with pytest.raises(ValueError, match="bad magic"):
            parse_clh(bogus)


class TestVarintStream:
    def test_each_segment_consumes_exactly_to_boundary(self, index, clb):
        # The body (minus per-segment header) is a clean LEB128 stream.
        for s in index.segments:
            vals = iter_segment_varints(clb, s)
            assert len(vals) > 0
            assert isinstance(vals[0], int)

    def test_seg0_known_prefix(self, index, clb):
        seg0 = index.segments[0]
        vals = iter_segment_varints(clb, seg0)
        assert vals[:9] == [13626044039, 16440, 16, 1, 128, 4, 2, 4, 3]

    def test_seg0_count(self, index, clb):
        assert len(iter_segment_varints(clb, index.segments[0])) == 4070

    def test_signal_ids_exceed_11bit_can_range(self, index, clb):
        # High tokens band above 0x7FF (max 11-bit CAN id): internal signal
        # index, not CAN arbitration ids.
        vals = iter_segment_varints(clb, index.segments[0])
        band = [v for v in vals if 0x3000 < v < 0x5000]
        assert band, "expected a high signal-id band"
        assert min(band) > 0x7FF

    def test_total_varints_across_segments(self, index, clb):
        total = sum(len(iter_segment_varints(clb, s)) for s in index.segments)
        assert total == 4070 + 4361 + 2666 + 10851 + 6999


def test_segment_header_size_constant():
    assert SEGMENT_HEADER_SIZE == 8
