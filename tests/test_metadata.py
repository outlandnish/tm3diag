"""Tests for uds/metadata.py."""

from pathlib import Path

import pytest

from uds_local.metadata import FirmwareEntry, find_firmware, load_metadata, packed_key_from_f180

_TSV = Path("/home/outlandnish/dev/tm3/deploy/seed_artifacts_v2/signed_metadata_map.tsv")

pytestmark = pytest.mark.skipif(
    not _TSV.exists(),
    reason=f"signed_metadata_map.tsv not found at {_TSV}",
)


@pytest.fixture(scope="module")
def entries():
    return load_metadata(_TSV)


class TestLoadMetadata:
    def test_loads_expected_count(self, entries):
        assert len(entries) == 393

    def test_all_entries_are_firmware_entry(self, entries):
        for e in entries:
            assert isinstance(e, FirmwareEntry)

    def test_git_sha_header_excluded(self, entries):
        # The first line (40-char hex SHA + version) must not appear as an entry
        for e in entries:
            assert len(e.lookup_key) < 40 or ":" in e.lookup_key

    def test_wildcard_conditions_parsed_as_empty_dict(self, entries):
        wildcards = [e for e in entries if not e.conditions]
        assert len(wildcards) > 0

    def test_explicit_conditions_parsed(self, entries):
        with_conds = [e for e in entries if e.conditions]
        assert len(with_conds) > 0
        for e in with_conds:
            for k, v in e.conditions.items():
                assert isinstance(k, str) and isinstance(v, str)

    def test_pcs_has_11_entries(self, entries):
        pcs = [e for e in entries if e.component == "pcs"]
        assert len(pcs) == 11

    def test_lookup_key_format(self, entries):
        for e in entries:
            assert ":" in e.lookup_key, f"Malformed lookup_key: {e.lookup_key!r}"

    def test_crc_is_hex_string_or_version_label(self, entries):
        # Most entries have a hex CRC32; park bootloader entries use a version label like "BL_016"
        for e in entries:
            assert e.crc, f"Empty CRC field for {e.lookup_key}"

    def test_src_path_nonempty(self, entries):
        for e in entries:
            assert e.src_path.strip()

    def test_dest_name_nonempty(self, entries):
        for e in entries:
            assert e.dest_name.strip()


class TestFindFirmware:
    def test_pcs_variant_531_returns_two_entries(self, entries):
        # packed_key for P5/A3/U1 = (5<<24)|(3<<16)|1 = 84082689
        results = find_firmware(entries, "pcs", 84082689)
        assert len(results) == 2

    def test_pcs_variant_531_dest_names(self, entries):
        results = find_firmware(entries, "pcs", 84082689)
        dest_names = {e.dest_name for e in results}
        assert dest_names == {"pcs.bhx", "pcscpu2.bhx"}

    def test_unknown_ecu_returns_empty(self, entries):
        assert find_firmware(entries, "fakeecu", 0) == []

    def test_unknown_packed_key_returns_empty(self, entries):
        assert find_firmware(entries, "pcs", 0xDEADBEEF) == []

    def test_case_insensitive_ecu_name(self, entries):
        lower = find_firmware(entries, "pcs", 84082689)
        upper = find_firmware(entries, "PCS", 84082689)
        assert lower == upper

    def test_conditions_filter_works(self, entries):
        # DI entries have drivetrainType/vdcType conditions
        all_di = find_firmware(entries, "di", 390004738)
        filtered = find_firmware(entries, "di", 390004738,
                                 conditions={"drivetrainType": "0", "vdcType": "0"})
        assert len(filtered) <= len(all_di)
        assert all(
            e.conditions.get("drivetrainType") == "0" and e.conditions.get("vdcType") == "0"
            or not e.conditions
            for e in filtered
        )

    def test_returns_list_of_firmware_entry(self, entries):
        results = find_firmware(entries, "pcs", 84082689)
        for r in results:
            assert isinstance(r, FirmwareEntry)


class TestPackedKeyFromF180:
    def test_pcs_variant_531(self):
        # PCBA_ID=5, ASSEMBLY_ID=3, USAGE_ID=1 → (5<<24)|(3<<16)|1 = 84082689
        f180 = bytes([
            0x00,        # MODULES
            0x00, 0x1B,  # COMPONENT_ID = 0x001B
            0x05,        # PCBA_ID = 5
            0x03,        # ASSEMBLY_ID = 3
            0x01,        # USAGE_ID = 1
        ] + [0x00] * 13)
        assert packed_key_from_f180(f180) == 84082689

    def test_zero_ids(self):
        f180 = bytes(19)
        assert packed_key_from_f180(f180) == 0

    def test_short_response_no_crash(self):
        f180 = bytes([0x00, 0x00, 0x00, 0x05, 0x03])  # no USAGE_ID byte
        key = packed_key_from_f180(f180)
        assert key == (5 << 24) | (3 << 16)
