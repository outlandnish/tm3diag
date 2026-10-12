"""Tests for dump_alerts' alert-hash provider seam (no recipe shipped)."""
import pytest

import dump_alerts


def test_no_provider_fails_closed(monkeypatch):
    monkeypatch.delenv("TM3_ALERT_HASH_PROVIDER", raising=False)
    with pytest.raises(SystemExit) as exc:
        dump_alerts.load_provider(None)
    assert "SECURITY_PROVIDER.md" in str(exc.value)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(SystemExit):
        dump_alerts.load_provider(str(tmp_path / "nope.py"))


def test_provider_from_file_drives_reverse_map(tmp_path, monkeypatch):
    p = tmp_path / "prov.py"
    p.write_text(
        "def alert_hash(s, salt):\n    return f'{s}|{salt}'\n"
        "def bus_bucket(b, salt):\n    return f'bus:{b}|{salt}'\n"
    )
    monkeypatch.setenv("TM3_ALERT_HASH_PROVIDER", str(p))
    prov = dump_alerts.load_provider(None)
    assert prov.BUS_NAMES == dump_alerts.BUS_NAMES
    inner = {"DI_a123_someAlert|x"}
    got = dump_alerts.reverse_map(prov, inner, "x", {"DI_a123_someAlertJUNK"})
    assert got == {"DI_a123_someAlert|x": "DI_a123_someAlert"}


def test_provider_missing_callable(tmp_path):
    p = tmp_path / "prov.py"
    p.write_text("def alert_hash(s, salt):\n    return s\n")
    with pytest.raises(SystemExit):
        dump_alerts.load_provider(str(p))
