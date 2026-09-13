"""Shared pytest setup: make the top-level scripts/ dir importable."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _no_vapi_shim(monkeypatch):
    """Keep the VAPI shim out of every test that does not ask for it.

    ``default_db()`` prefers running the firmware's own decoder, which on a
    developer box with TM3_ROOT set would read a 14 MB library and spawn worker
    processes -- and on a machine without the firmware would do neither, so the
    suite would test different code in the two places. Tests that mean to
    exercise the shim set ``VAPI_ENABLED`` back on themselves.
    """
    import config as _cfg
    monkeypatch.setattr(_cfg, "VAPI_ENABLED", False)
