"""Tests for tm3uds.py CLI argument parsing (no CAN hardware required)."""

import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = str(Path(__file__).parent.parent / "tm3uds.py")
_PY = sys.executable


def _run(*args: str, expect_exit: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run([_PY, _TOOL, *args], capture_output=True, text=True)
    if result.returncode != expect_exit:
        pytest.fail(
            f"Exit {result.returncode} (expected {expect_exit})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


class TestHelp:
    def test_top_level_help(self):
        r = _run("--help")
        assert "scan" in r.stdout
        assert "read-did" in r.stdout

    def test_scan_help(self):
        r = _run("scan", "--help")
        assert "--timeout" in r.stdout

    def test_read_did_help(self):
        r = _run("read-did", "--help")
        assert "did" in r.stdout

    def test_write_did_help(self):
        r = _run("write-did", "--help")
        assert "data" in r.stdout

    def test_routine_help(self):
        r = _run("routine", "--help")
        assert "routine_id" in r.stdout

    def test_session_help(self):
        r = _run("session", "--help")
        assert "mode" in r.stdout

    def test_reset_help(self):
        _run("reset", "--help")

    def test_security_access_help(self):
        _run("security-access", "--help")


class TestArgValidation:
    def test_no_subcommand_exits_nonzero(self):
        _run(expect_exit=2)

    def test_read_did_without_node_exits_nonzero(self):
        _run("read-did", "0xF180", expect_exit=2)

    def test_write_did_missing_data_arg_exits_nonzero(self):
        _run("--node", "PCS", "write-did", "0xF180", expect_exit=2)

    def test_routine_missing_routine_id_exits_nonzero(self):
        _run("--node", "PCS", "routine", expect_exit=2)

    def test_session_missing_mode_exits_nonzero(self):
        _run("--node", "PCS", "session", expect_exit=2)

    def test_scan_does_not_require_node(self):
        # --channel is a global flag (before the subcommand).
        r = subprocess.run(
            [_PY, _TOOL, "--channel", "vcan0", "scan"],
            capture_output=True, text=True,
        )
        assert r.returncode != 2, (
            f"scan failed with argparse exit 2 — node should not be required for scan.\n"
            f"stderr: {r.stderr}"
        )

    def test_unknown_subcommand_exits_nonzero(self):
        _run("frobnicate", expect_exit=2)
