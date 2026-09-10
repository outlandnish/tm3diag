"""Parametrized BHX parser tests across every firmware file in seed_artifacts_v2.

One test ID per BHX file; skipped when the seed_artifacts_v2 directory is absent.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

import bhx

_ARTIFACTS = Path("/home/outlandnish/dev/tm3/deploy/seed_artifacts_v2")
_ALL_BHX = sorted(_ARTIFACTS.rglob("*.bhx")) if _ARTIFACTS.is_dir() else []

pytestmark = pytest.mark.skipif(
    not _ARTIFACTS.is_dir(),
    reason=f"seed_artifacts_v2 not found at {_ARTIFACTS}",
)


def _rel(path: Path) -> str:
    return str(path.relative_to(_ARTIFACTS))


def _recompute_segment_crc(path: Path, seg_index: int) -> int:
    """Recompute CRC32 for a segment payload directly from raw bytes."""
    bhx_file = bhx.parse_file(path)
    seg = bhx_file.segments[seg_index]
    return zlib.crc32(seg.data) & 0xFFFFFFFF


@pytest.mark.parametrize("bhx_path", _ALL_BHX, ids=_rel)
class TestBhxFile:
    def test_parses_without_exception(self, bhx_path: Path):
        bhx.parse_file(bhx_path)

    def test_has_at_least_one_segment(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        assert len(bhx_file.segments) >= 1

    def test_all_segment_crcs_match(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        for i, seg in enumerate(bhx_file.segments):
            computed = seg.compute_crc32()
            assert seg.checksum == computed, (
                f"Segment {i} CRC mismatch in {_rel(bhx_path)}: "
                f"stored=0x{seg.checksum:08X} computed=0x{computed:08X}"
            )

    def test_total_payload_bytes_consistent(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        assert bhx_file.total_payload_bytes == sum(s.length for s in bhx_file.segments)

    def test_segment_crcs_match_independent_calculation(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        for i, seg in enumerate(bhx_file.segments):
            expected = _recompute_segment_crc(bhx_path, i)
            assert seg.checksum == expected, (
                f"Segment {i} CRC: stored=0x{seg.checksum:08X} "
                f"vs recomputed=0x{expected:08X} in {_rel(bhx_path)}"
            )

    def test_segment_addresses_fit_in_32_bits(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        for i, seg in enumerate(bhx_file.segments):
            assert 0 <= seg.start_address <= 0xFFFFFFFF, (
                f"Segment {i} start_address 0x{seg.start_address:X} out of 32-bit range "
                f"in {_rel(bhx_path)}"
            )

    def test_segment_sizes_are_positive(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        for i, seg in enumerate(bhx_file.segments):
            assert seg.length > 0, (
                f"Segment {i} has zero length in {_rel(bhx_path)}"
            )

    def test_roundtrip(self, bhx_path: Path):
        """build(parse(data)) == original bytes."""
        original = bhx_path.read_bytes()
        bhx_file = bhx.parse(original)
        rebuilt = bhx.build(bhx_file)
        assert rebuilt == original, f"Roundtrip mismatch in {_rel(bhx_path)}"


_PCS_BHX = [p for p in _ALL_BHX if p.parts[-3] == "pcs"]


@pytest.mark.parametrize("bhx_path", _PCS_BHX, ids=_rel)
class TestPcsBhx:
    def test_exactly_one_segment(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        assert len(bhx_file.segments) == 1

    def test_target_address_in_valid_range(self, bhx_path: Path):
        bhx_file = bhx.parse_file(bhx_path)
        addr = bhx_file.segments[0].start_address
        assert 0x080000 <= addr < 0x100000 and addr % 2 == 0, (
            f"Unexpected PCS start_address 0x{addr:08X} in {_rel(bhx_path)}"
        )


_PARK_BHX = [p for p in _ALL_BHX if p.parts[-3] == "park"]


@pytest.mark.parametrize("bhx_path", _PARK_BHX, ids=_rel)
def test_park_has_multiple_segments(bhx_path: Path):
    bhx_file = bhx.parse_file(bhx_path)
    assert len(bhx_file.segments) > 1, (
        f"Expected multiple segments in park BHX {_rel(bhx_path)}"
    )
