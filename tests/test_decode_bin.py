"""Tests for decode_bin's .bin decrypt provider seam (no recipe shipped)."""
import json

import pytest

import decode_bin


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(decode_bin, "_provider_cache", None)
    monkeypatch.setattr(decode_bin, "_LOCAL", "_no_such_bin_decrypt_impl")
    monkeypatch.setenv("TM3_BIN_KEY", "k")


def test_no_provider_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("TM3_BIN_PROVIDER", raising=False)
    f = tmp_path / "x.odj.bin"
    f.write_bytes(b"\0" * 32)
    with pytest.raises(RuntimeError, match="SECURITY_PROVIDER.md"):
        decode_bin.load_json(f)


def test_missing_provider_callable(tmp_path, monkeypatch):
    p = tmp_path / "prov.py"
    p.write_text("x = 1\n")
    monkeypatch.setenv("TM3_BIN_PROVIDER", str(p))
    f = tmp_path / "x.odj.bin"
    f.write_bytes(b"")
    with pytest.raises(RuntimeError, match="decrypt_bin"):
        decode_bin.load_json(f)


def test_provider_from_file(tmp_path, monkeypatch):
    p = tmp_path / "prov.py"
    p.write_text("import json\ndef decrypt_bin(data, key):\n    return {'key': key, 'n': len(data)}\n")
    monkeypatch.setenv("TM3_BIN_PROVIDER", str(p))
    f = tmp_path / "x.odj.bin"
    f.write_bytes(b"abc")
    assert decode_bin.load_json(f) == {"key": "k", "n": 3}


def test_plain_json_needs_no_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("TM3_BIN_PROVIDER", raising=False)
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"a": 1}))
    assert decode_bin.load_json(f) == {"a": 1}
