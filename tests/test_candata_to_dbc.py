"""Tests for candata_to_dbc: revision naming, and merging the catalog with a donor.

Self-contained -- a hand-built SoCatalog and donor stand in for the firmware, so
these need no .so and no compact.json.
"""
from pathlib import Path
from types import SimpleNamespace

import candata_to_dbc
import config as _cfg
import so_candata


def _sig(start=0, width=8):
    return {"start_position": start, "width": width, "endianness": "LITTLE",
            "signedness": "UNSIGNED", "scale": 1, "offset": 0, "units": ""}


def _cat(messages):
    return so_candata.SoCatalog(lib="test.so", bus="ETH", messages=messages)


def _donor(messages, label="compact"):
    return {"_label": label, "messages": messages}


class TestResolveRev:
    """What the written DBC is NAMED, which is how config finds it again.

    TM3_FW is the revision vehicle_sim transmits -- often deliberately older
    than the extraction being read. It must not name an artifact built from a
    different root, or config._resolve_eth_dbc loads a 2026-derived DBC as if it
    were the 2020 one.
    """

    ROOT = Path("/fw/2026.8.3.ice.extracted")
    LIB = ROOT / "usr/tesla/UI/lib/libQtCarCANData.so.1.0.0"

    def test_the_root_beats_tm3_fw(self, monkeypatch):
        monkeypatch.setattr(_cfg, "FW_VERSION", "2020.8.1")
        args = SimpleNamespace(rev=None)
        assert candata_to_dbc._resolve_rev(args, self.LIB, self.ROOT) == "2026.8.3"

    def test_an_explicit_rev_beats_everything(self, monkeypatch):
        monkeypatch.setattr(_cfg, "FW_VERSION", "2020.8.1")
        args = SimpleNamespace(rev="2022.45.15")
        assert candata_to_dbc._resolve_rev(args, self.LIB, self.ROOT) == "2022.45.15"

    def test_tm3_fw_still_serves_when_the_root_says_nothing(self, monkeypatch):
        # A lib sitting somewhere with no recognisable revision in the path.
        monkeypatch.setattr(_cfg, "FW_VERSION", "2020.8.1")
        monkeypatch.setattr(_cfg, "rev_from_root", lambda root: None)
        args = SimpleNamespace(rev=None)
        assert candata_to_dbc._resolve_rev(args, self.LIB, self.ROOT) == "2020.8.1"

    def test_nothing_anywhere_is_unknown_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(_cfg, "FW_VERSION", None)
        monkeypatch.setattr(_cfg, "rev_from_root", lambda root: None)
        args = SimpleNamespace(rev=None)
        assert candata_to_dbc._resolve_rev(args, self.LIB, self.ROOT) == "unknown"

    def test_rev_from_lib_reports_failure_rather_than_a_placeholder(self, monkeypatch):
        # It used to return "unknown", which is truthy -- so putting it ahead of
        # TM3_FW would have made TM3_FW unreachable instead of a fallback.
        monkeypatch.setattr(_cfg, "rev_from_root", lambda root: None)
        assert candata_to_dbc._rev_from_lib(self.LIB, self.ROOT) is None


class TestOriginNode:
    """The node a message is filed under decides which node shows it in tm3web.

    The catalog spells nodes uppercase and compact.json spells them lowercase, so
    a message present in only one of the two used to land under its own spelling
    -- giving each ECU a near-empty twin node.
    """

    def test_a_catalogued_message_keeps_the_catalog_spelling(self):
        cat = _cat({"VCFRONT_status": {"message_id": 0x2E1, "length_bytes": 8,
                                       "cycle_time": 100, "originNode": "VCFRONT",
                                       "signals": {}}})
        donor = _donor({"VCFRONT_status": {"message_id": 0x2E1, "originNode": "vcfront",
                                           "signals": {"a": _sig()}}})
        db, _rep = candata_to_dbc.enrich(cat, [donor])
        assert db["messages"]["VCFRONT_status"]["originNode"] == "VCFRONT"

    def test_a_donor_only_message_is_canonicalised_to_the_catalog_spelling(self):
        # VCFRONT_udsResponse ships only in compact.json, which says "vcfront".
        # Left alone it created a second node holding just the UDS pair, which is
        # exactly what a viewer subscribed to "vcfront" would see instead of the
        # 21 real messages under "VCFRONT".
        cat = _cat({"VCFRONT_status": {"message_id": 0x2E1, "length_bytes": 8,
                                       "cycle_time": 100, "originNode": "VCFRONT",
                                       "signals": {}}})
        donor = _donor({
            "VCFRONT_status": {"message_id": 0x2E1, "originNode": "vcfront",
                               "signals": {"a": _sig()}},
            "VCFRONT_udsResponse": {"message_id": 0x601, "originNode": "vcfront",
                                    "signals": {"b": _sig()}},
        })
        db, _rep = candata_to_dbc.enrich(cat, [donor])
        nodes = {m["originNode"] for m in db["messages"].values()}
        assert nodes == {"VCFRONT"}, "the donor's spelling leaked in as a second node"
        assert db["messages"]["VCFRONT_udsResponse"]["senders"] == ["VCFRONT"]

    def test_an_unknown_node_is_left_alone(self):
        # Only spellings the catalog actually knows get rewritten; a node it has
        # never heard of is passed through rather than guessed at.
        cat = _cat({"DI_status": {"message_id": 0x118, "length_bytes": 8,
                                  "cycle_time": 10, "originNode": "DI",
                                  "signals": {}}})
        donor = _donor({"XYZ_thing": {"message_id": 0x700, "originNode": "mystery",
                                      "signals": {"a": _sig()}}})
        db, _rep = candata_to_dbc.enrich(cat, [donor])
        assert db["messages"]["XYZ_thing"]["originNode"] == "mystery"

    def test_the_name_prefix_is_still_the_last_resort(self):
        # No node from either source -> fall back to the message-name prefix.
        cat = _cat({})
        donor = _donor({"PMR_info": {"message_id": 0x3FF, "signals": {"a": _sig()}}})
        db, _rep = candata_to_dbc.enrich(cat, [donor])
        assert db["messages"]["PMR_info"]["originNode"] == "PMR"
