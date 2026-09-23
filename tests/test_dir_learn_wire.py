"""Bytes the bundled DIR learn procedures put on the wire, pinned.

Runs the real procedures through Engine/BenchBackend/adapters/ODJ codec with only
the UdsSession recorded, so an encoding regression (ROTOR_TEMPERATURE -40 °C once
went out as 0xD8 = +176 °C) fails here. Skips without the firmware data.
"""
import odin_service
import pytest
from odin_runner import BenchBackend, CidStore

import config
from uds_local.node_config import load_node_config

_TASKS = "Gen3/tasks/PROC_DIR_X_"

pytestmark = pytest.mark.skipif(
    config.ODIN_BUNDLE is None or config.ODJ_DIR is None
    or not (config.ODIN_BUNDLE / f"{_TASKS}ROTOR-OFFSET-LEARN.py").exists()
    or not (config.ODJ_DIR / "DI.odj.bin").exists(),
    reason="needs the ODIN bundle and DIR ODJ")


class _Dir:
    """Records requests; START succeeds, RESULTS reports finished with SUCCESS."""

    def __init__(self):
        self.wire: list[str] = []

    def diagnostic_session(self, mode):
        self.wire.append(bytes([0x10, mode]).hex(" "))

    def security_access(self, level_idx=0, seed_level=None):
        self.wire.append(bytes([0x27, seed_level]).hex(" "))

    def routine_control(self, rid, arg=b"", subtype=0x01):
        self.wire.append((bytes([0x31, subtype, rid >> 8, rid & 0xFF]) + bytes(arg)).hex(" "))
        return b"\x00\x00" if subtype == 0x01 else bytes(200)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _run(task):
    cfg = load_node_config("DIR", config.NODES_JSON, config.ETH_COMPACT, config.ODJ_DIR)
    dir_ = _Dir()
    bb = BenchBackend("chan", firmware_root=config.ROOT)
    bb._cid = CidStore(seed={"VAPI_driveRailOn": "true", "VAPI_shiftState": "D"}, derive=None)
    bb._node = lambda _name: (cfg, dir_)
    res = odin_service.run_procedure(_TASKS + task, backend=bb, time_scale=0.0)
    return res["exit_code"], dir_.wire


def test_rotor_offset_learn():
    # LEARN_SELECT=ROTOR_OFFSET(1), ROTOR_TEMPERATURE=-40 °C -> raw 0x00 (linear offset -40)
    assert _run("ROTOR-OFFSET-LEARN") == (0, ["10 03", "27 05", "31 01 04 06 00 01",
                                               "31 03 04 06"])


def test_resolver_error_learn():
    assert _run("RESOLVER-ERROR-LEARN") == (0, ["10 03", "27 05", "31 01 04 09",
                                                 "31 03 04 09"])
