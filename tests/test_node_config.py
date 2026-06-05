"""Tests for uds/node_config.py."""

import pytest

import config as _cfg
from uds_local.node_config import NodeConfig, OdjEntry, load_all_nodes, load_node_config

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

    def test_cp_known_did_present(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        assert "Application_CRC" in cfg.dids

    def test_cp_did_hex_id(self):
        cfg = load_node_config("CP", _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)
        entry = cfg.dids["Application_CRC"]
        assert entry.hex_id == 0xF00

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


class TestLoadAllNodes:
    def test_returns_28_nodes(self):
        nodes = load_all_nodes(_NODES_JSON, _ETH_COMPACT)
        assert len(nodes) == 28

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
