"""The signal database every tool resolves by default.

compact.json is the subset Tesla ships to the diagnostic tool and it shrinks
every release (2022.45.15: 140 messages / 347 signals against the catalog's
446 / 26227). The generated DBC covers the whole catalog with same-revision
layout, so it must win -- and when it is absent the fallback has to be LOUD,
because a tool that silently cannot see most of the bus looks like a bus fault.
"""
import warnings

import pytest

import can_decoder
import config as _cfg
from uds_local.node_config import _message_ids

DBC = """VERSION ""

NS_ :

BS_:

BU_ : DIR


BO_ 1800 DIR_diagRequest: 8 DIR
 SG_ DIR_diagPayload : 0|64@1+ (1,0) [0|0] "" DIR

BO_ 1801 DIR_diagResponse: 8 DIR
 SG_ DIR_diagRespPayload : 0|64@1+ (1,0) [0|0] "" DIR
"""


@pytest.fixture
def dbc(tmp_path):
    p = tmp_path / "Model3_ETH.test.dbc"
    p.write_text(DBC)
    return p


class TestMessageIds:
    def test_reads_a_dbc(self, dbc):
        got = _message_ids(dbc)
        assert got["DIR_diagRequest"]["message_id"] == 1800
        assert got["DIR_diagResponse"]["message_id"] == 1801

    def test_reads_a_compact_json(self, tmp_path):
        import json
        p = tmp_path / "Model3_ETH.compact.json"
        p.write_text(json.dumps(
            {"messages": {"DIR_diagRequest": {"message_id": 1800}}}))
        assert _message_ids(p)["DIR_diagRequest"]["message_id"] == 1800


class TestDefaultDb:
    def test_prefers_the_generated_dbc(self, dbc, monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)
        with warnings.catch_warnings():
            warnings.simplefilter("error")       # must not warn
            db = can_decoder.default_db()
        assert 1800 in db.messages
        assert db.messages[1800]["name"] == "DIR_diagRequest"

    def test_warns_once_when_falling_back(self, monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", None)
        monkeypatch.setattr(can_decoder, "_FALLBACK_WARNED", False)
        called = []
        monkeypatch.setattr(can_decoder, "CanDatabase",
                            type("Stub", (), {"__init__":
                                              lambda self: called.append(1)}))
        with pytest.warns(UserWarning, match="candata_to_dbc"):
            can_decoder.default_db()
        with warnings.catch_warnings():
            warnings.simplefilter("error")       # second call is quiet
            can_decoder.default_db()
        assert len(called) == 2


class TestVapiSelection:
    """Running the firmware's own decoder beats decoding from its layouts.

    But only when this machine can actually do it: the shim needs the firmware
    .so pair AND unicorn AND the DBC (which is still where names, units and
    encode_frame come from, since crackMessage only goes one way). Anything
    missing has to fall through quietly to the layout path -- a diagnostic tool
    that refuses to start because a 14 MB library is absent is useless.
    """

    @pytest.fixture
    def libs(self, tmp_path, monkeypatch):
        vapi, candata = tmp_path / "libQtCarVAPI.so", tmp_path / "libQtCarCANData.so"
        monkeypatch.setattr(_cfg, "VAPI_ENABLED", True)
        monkeypatch.setattr(_cfg, "vapi_libs", lambda root=None: (vapi, candata))
        return vapi, candata

    def test_the_shim_wins_when_everything_is_available(self, dbc, libs, monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)
        built = []
        monkeypatch.setattr(can_decoder, "_build_vapi_db",
                            lambda *a: built.append(a) or "SHIM")
        assert can_decoder.default_db() == "SHIM"
        assert built == [(libs[0], libs[1], dbc)]

    def test_tm3_vapi_off_forces_the_layout_path(self, dbc, libs, monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)
        monkeypatch.setattr(_cfg, "VAPI_ENABLED", False)
        assert 1800 in can_decoder.default_db().messages   # the plain DBC

    def test_no_firmware_libs_means_no_shim(self, dbc, monkeypatch):
        monkeypatch.setattr(_cfg, "VAPI_ENABLED", True)
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)
        monkeypatch.setattr(_cfg, "vapi_libs", lambda root=None: None)
        assert 1800 in can_decoder.default_db().messages

    def test_no_dbc_still_gets_the_shim(self, libs, monkeypatch):
        # The catalog in libQtCarCANData carries every name, unit, enum and node
        # the decode path renders, so a host with firmware but no built DBC
        # decodes in full. compact.json is handed over only as a layout source,
        # for encode_frame and the faulted-decode fallback.
        monkeypatch.setattr(_cfg, "ETH_DBC", None)
        got = []
        monkeypatch.setattr(can_decoder, "_build_vapi_db",
                            lambda *a: got.append(a) or "SHIM")
        assert can_decoder.default_db() == "SHIM"
        assert got[0][:2] == libs
        assert got[0][2] == can_decoder._ETH_COMPACT

    def test_a_built_dbc_is_preferred_over_compact_json_for_layouts(self, dbc,
                                                                    libs,
                                                                    monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)
        got = []
        monkeypatch.setattr(can_decoder, "_build_vapi_db",
                            lambda *a: got.append(a) or "SHIM")
        can_decoder.default_db()
        assert got[0][2] == dbc

    def test_a_shim_that_will_not_load_warns_and_falls_back(self, dbc, libs,
                                                            monkeypatch):
        monkeypatch.setattr(_cfg, "ETH_DBC", dbc)

        def boom(*_a):
            raise OSError("unicorn exploded")

        monkeypatch.setattr(can_decoder, "_build_vapi_db", boom)
        with pytest.warns(UserWarning, match="recovered layouts"):
            db = can_decoder.default_db()
        assert 1800 in db.messages


class TestEthDbcResolution:
    def test_env_override_wins(self, dbc, monkeypatch):
        monkeypatch.setenv("TM3_ETH_DBC", str(dbc))
        assert _cfg._resolve_eth_dbc() == dbc

    def test_missing_env_path_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TM3_ETH_DBC", str(tmp_path / "nope.dbc"))
        assert _cfg._resolve_eth_dbc() is None

    def test_no_revision_means_no_conventional_path(self, monkeypatch):
        monkeypatch.delenv("TM3_ETH_DBC", raising=False)
        monkeypatch.setattr(_cfg, "FW_VERSION", None)
        monkeypatch.setattr(_cfg, "_ROOT", None)
        assert _cfg._resolve_eth_dbc() is None

    def test_conventional_path_is_product_and_revision(self, monkeypatch,
                                                       tmp_path):
        monkeypatch.delenv("TM3_ETH_DBC", raising=False)
        monkeypatch.setattr(_cfg, "_ROOT", None)
        monkeypatch.setattr(_cfg, "FW_VERSION", "2022.45.15")
        monkeypatch.setattr(_cfg, "PRODUCT", "Model3")
        monkeypatch.setattr(_cfg, "_PROJECT_DIR", tmp_path)
        assert _cfg._resolve_eth_dbc() is None          # not built yet
        (tmp_path / "Model3_ETH.2022.45.15.dbc").write_text(DBC)
        assert _cfg._resolve_eth_dbc().name == "Model3_ETH.2022.45.15.dbc"


class TestRevFromRoot:
    """The firmware root names the DBC, so pointing .env at a root is enough."""

    @pytest.mark.parametrize("name,want", [
        ("2026.8.3.ice.extracted", "2026.8.3"),
        ("2022.45.15.ice.extracted", "2022.45.15"),
        # An extraction dir is just as often named for the .ice it came from,
        # with nothing appended -- the bench host names all three that way.
        ("2022.45.15.ice", "2022.45.15"),
        ("2020.8.1.ice", "2020.8.1"),
        ("2020.8.1-9-ae1963092f.model3", "2020.8.1-9-ae1963092f"),
        ("squashfs-root", "squashfs-root"),
    ])
    def test_strips_the_extraction_suffix(self, name, want):
        from pathlib import Path
        assert _cfg.rev_from_root(Path("/fw") / name) == want

    def test_no_root_no_revision(self):
        assert _cfg.rev_from_root(None) is None

    def test_root_resolves_the_dbc_without_tm3_fw(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TM3_ETH_DBC", raising=False)
        monkeypatch.setattr(_cfg, "FW_VERSION", None)      # TM3_FW unset
        monkeypatch.setattr(_cfg, "PRODUCT", "Model3")
        monkeypatch.setattr(_cfg, "_PROJECT_DIR", tmp_path)
        monkeypatch.setattr(_cfg, "_ROOT", tmp_path / "2026.8.3.ice.extracted")
        assert _cfg._resolve_eth_dbc() is None             # not built yet
        (tmp_path / "Model3_ETH.2026.8.3.dbc").write_text(DBC)
        assert _cfg._resolve_eth_dbc().name == "Model3_ETH.2026.8.3.dbc"

    def test_root_wins_over_tm3_fw(self, monkeypatch, tmp_path):
        # TM3_FW is the revision vehicle_sim TRANSMITS; it must not repoint the
        # database away from the firmware root everything else is read from.
        monkeypatch.delenv("TM3_ETH_DBC", raising=False)
        monkeypatch.setattr(_cfg, "FW_VERSION", "2020.8.1")
        monkeypatch.setattr(_cfg, "PRODUCT", "Model3")
        monkeypatch.setattr(_cfg, "_PROJECT_DIR", tmp_path)
        monkeypatch.setattr(_cfg, "_ROOT", tmp_path / "2026.8.3.ice.extracted")
        (tmp_path / "Model3_ETH.2026.8.3.dbc").write_text(DBC)
        (tmp_path / "Model3_ETH.2020.8.1.dbc").write_text(DBC)
        assert _cfg._resolve_eth_dbc().name == "Model3_ETH.2026.8.3.dbc"

    def test_tm3_fw_is_used_when_the_root_has_no_dbc(self, monkeypatch,
                                                     tmp_path):
        monkeypatch.delenv("TM3_ETH_DBC", raising=False)
        monkeypatch.setattr(_cfg, "FW_VERSION", "2020.8.1")
        monkeypatch.setattr(_cfg, "PRODUCT", "Model3")
        monkeypatch.setattr(_cfg, "_PROJECT_DIR", tmp_path)
        monkeypatch.setattr(_cfg, "_ROOT", tmp_path / "2026.8.3.ice.extracted")
        (tmp_path / "Model3_ETH.2020.8.1.dbc").write_text(DBC)
        assert _cfg._resolve_eth_dbc().name == "Model3_ETH.2020.8.1.dbc"
