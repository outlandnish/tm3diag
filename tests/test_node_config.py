"""Tests for uds/node_config.py."""

import pytest

import config as _cfg
from uds_local.node_config import NodeConfig, load_all_nodes, load_node_config
from uds_local.odj import OdjEntry

_NODES_JSON  = _cfg.NODES_JSON
_ETH_COMPACT = _cfg.ETH_COMPACT
_ODJ_DIR     = _cfg.ODJ_DIR

if _NODES_JSON is None or not _NODES_JSON.exists():
    pytest.skip("TM3_ROOT firmware data not available — skipping node config tests", allow_module_level=True)


class TestLoadNodeConfig:
    def test_cp_can_ids(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert cfg.request_can_id == 0x60E
        assert cfg.response_can_id == 0x61E

    def test_cp_security_algorithm(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert cfg.security_algorithm == "tesla_hash"
        assert cfg.security_buffer_size == 16

    def test_cp_has_dids(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert len(cfg.dids) > 0

    def test_cp_dids_are_named_and_well_formed(self):
        # Don't pin to a specific DID name/id — they vary by firmware dump.
        # Assert every loaded DID is named and carries a plausible hex id.
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert cfg.dids, "CP loaded no DIDs"
        for name, entry in cfg.dids.items():
            assert isinstance(name, str) and name
            assert 0 <= entry.hex_id <= 0xFFFF

    def test_rcm_pektron_algorithm(self):
        cfg = load_node_config("RCM", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert cfg.security_algorithm == "pektron_hash"
        assert cfg.security_buffer_size == 3

    def test_rcm_has_fixed_bytes_kw(self):
        cfg = load_node_config("RCM", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert "fixed_bytes" in cfg.security_kw
        assert cfg.security_kw["fixed_bytes"] == "6E6164616D"

    def test_unknown_node_raises(self):
        with pytest.raises(KeyError, match="FAKENODE"):
            load_node_config("FAKENODE", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)

    def test_returns_node_config_type(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert isinstance(cfg, NodeConfig)

    def test_did_entry_type(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        for entry in cfg.dids.values():
            assert isinstance(entry, OdjEntry)

    def test_pcs_can_ids(self):
        cfg = load_node_config("PCS", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        # Verify both CAN IDs are non-zero and distinct
        assert cfg.request_can_id != 0
        assert cfg.response_can_id != 0
        assert cfg.request_can_id != cfg.response_can_id

    def test_cp_writable_did_has_input_fields(self):
        # At least one CP DID should be writable with named input fields, and
        # every input field's enum_map should be a dict (empty when no enum).
        # The exact DID/field names differ across firmware dumps, so don't pin.
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        writable = [e for e in cfg.dids.values()
                    if e.write is not None and e.write.input]
        assert writable, "CP has no writable DID with input fields"
        for entry in writable:
            for fname, field in entry.write.input.items():
                assert isinstance(fname, str) and fname
                assert isinstance(field.enum_map, dict)


class TestLoadAllNodes:
    def test_returns_multiple_nodes(self):
        # Node count varies by firmware dump (e.g. 28 in 2020.8.1, 37 in
        # 2026.8.3), so assert a plausible lower bound rather than an exact
        # count. The remaining tests cover the shape of each entry.
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        assert len(nodes) >= 10

    def test_each_entry_is_3_tuple(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        for item in nodes:
            assert len(item) == 3
            name, tx_id, rx_id = item
            assert isinstance(name, str)
            assert isinstance(tx_id, int)
            assert isinstance(rx_id, int)

    def test_all_can_ids_nonzero(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        for name, tx_id, rx_id in nodes:
            assert tx_id != 0, f"{name} has zero TX id"
            assert rx_id != 0, f"{name} has zero RX id"

    def test_tx_rx_ids_differ_per_node(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        for name, tx_id, rx_id in nodes:
            assert tx_id != rx_id, f"{name}: TX and RX ids are the same ({tx_id:#x})"

    def test_cp_present(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        names = [n for n, _, _ in nodes]
        assert "CP" in names

    def test_rcm_present(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        names = [n for n, _, _ in nodes]
        assert "RCM" in names
