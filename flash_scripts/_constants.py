"""Constants used by step functions and the dual-CPU runner."""

# Routine IDs (RoutineControl 0x31)
_RC_ERASE      = 0xFF00  # initializeEraseModule
_RC_VERIFY_CRC = 0x0201  # checkModuleProgrammedCorrectly
_RC_CHECK_REV  = 0x0202  # checkCorrectComponentAndRev

# DIDs read for logging during step_board_info (not validated, failure ignored)
_BOARD_INFO_DIDS = (0xF012, 0xF013, 0xF014, 0xF015)

# Flash count limits indexed by operand (0–2) matching hashpicker_sim table
FLASH_COUNT_LIMITS = (200, 100, 50)

# Seed level indexed by security-access level index.
# idx 0 is overridden to 0x01 if protocol_ver < 3.
_SECURITY_SEED_LEVEL = {
    0: 0x05,
    1: 0x01,
    2: 0x05,
    3: 0x11,
    4: 0x11,
    5: 0x03,
    6: 0x01,
    7: 0x05,
}
