"""Tests for config: ODIN bundle path resolution + CAN bus->channel routing."""
import config
from config import resolve_odin_bundle


def test_override_wins(tmp_path):
    assert resolve_odin_bundle(None, str(tmp_path)) == tmp_path
    # override wins even when a root is given
    assert resolve_odin_bundle(tmp_path, str(tmp_path / "x")) == tmp_path / "x"


def test_none_root_is_none(tmp_path):
    assert resolve_odin_bundle(None) is None


def test_finds_extracted_layout(tmp_path):
    nw = tmp_path / "opt/odin/odin_bundle_extracted/odin_bundle/networks"
    nw.mkdir(parents=True)
    assert resolve_odin_bundle(tmp_path) == nw


def test_finds_single_wrapper_layout(tmp_path):
    nw = tmp_path / "opt/odin/odin_bundle/networks"
    nw.mkdir(parents=True)
    assert resolve_odin_bundle(tmp_path) == nw


def test_unresolved_returns_none(tmp_path):
    (tmp_path / "opt/odin").mkdir(parents=True)   # no bundle dir under it
    assert resolve_odin_bundle(tmp_path) is None


# Assert the routing (against the module's own channel values) so the test holds
# regardless of what .env configures.

def test_can_channel_vehicle_aliases():
    for bus in ("ETH", "VEH", "vehicle", None, "unknown-bus"):
        assert config.can_channel(bus) == config.VEHICLE_CHANNEL


def test_can_channel_party_and_charge():
    assert config.can_channel("PARTY") == config.PARTY_CHANNEL
    assert config.can_channel("pt") == config.PARTY_CHANNEL
    assert config.can_channel("CH") == config.CHARGE_CHANNEL
    assert config.can_channel("charge") == config.CHARGE_CHANNEL
