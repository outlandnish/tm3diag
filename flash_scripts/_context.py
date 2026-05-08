"""FlashContext + FlashScript dataclasses and the StepFn type alias."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from uds_local.client import UdsSession


@dataclass
class FlashContext:
    """Carries per-flash-run state shared between step functions."""
    bhx_file: object           # bhx.BhxFile
    entry: object              # metadata.FirmwareEntry
    module_byte: int = 0x00              # written by module_to_program step
    fallback_module_byte: int | None = None  # tried if module_byte gets NRC 0x10/0x31
    erase_timeout: float = 3.0           # P2 seconds applied around erase (restored after)
    security_level: int = 0    # security access level index
    protocol_ver: int | None = None  # set by step_verify_comp_fw, consumed by step_security_access
    expected_fw_type: int = 0x01  # 1 = regular firmware; 2 = bootloader image
    # CAN access plumbing for steps that need to open a transient session to
    # another ECU (e.g. SCRIPT_BL_UPDATER_VCFRONT's VCRIGHT prep). Populated by
    # phase 4 / `FlashScript.run` from the caller's CLI args.
    channel: str | None = None
    interface: str | None = None


StepFn = Callable[["UdsSession", FlashContext], None]


@dataclass
class FlashScript:
    """An ordered sequence of step functions that implement one ECU flash flow.

    module_byte:      WDBI 0x0102 value sent before erase (0x00 for single-CPU)
    erase_timeout:    P2 seconds applied around RC 0xFF00 (restored to 3.0 after)
    security_level:   level_idx passed to security_access (0 = tesla_hash/0x01)
    expected_fw_type: byte[1] of DID 0x0101 must equal this (1 = regular fw,
                      2 = bootloader image; only SCRIPT_BL uses 2)
    """
    steps: list[StepFn]
    module_byte: int = 0x00
    erase_timeout: float = 3.0
    security_level: int = 0
    expected_fw_type: int = 0x01

    def run(
        self,
        sess: "UdsSession",
        bhx_file: object,
        entry: object,
        channel: str | None = None,
        interface: str | None = None,
    ) -> None:
        ctx = FlashContext(
            bhx_file=bhx_file,
            entry=entry,
            module_byte=self.module_byte,
            erase_timeout=self.erase_timeout,
            security_level=self.security_level,
            expected_fw_type=self.expected_fw_type,
            channel=channel,
            interface=interface,
        )
        for step in self.steps:
            step(sess, ctx)
