"""Tests for uds_local/odj_codec.py.

Self-contained: the FieldSpec/SubSpec layouts here mirror the REAL DIR ODJ
RESOLVER_LEARNING / OFFSET_LEARNING results specs (dumped from firmware), so the
codec is validated against ground truth without needing the ODJ files present.
Expected values are cross-checked against scripts/di/resolver_cal.py's hand-parse.
"""
import struct

from uds_local.odj import FieldSpec, SubSpec
from uds_local.odj_codec import (
    decode_field,
    decode_response,
    encode_fields,
    encode_request,
)

_RUNNING_ENUM = {"FALSE": 0, "TRUE": 1}
_RESOLVER_ENUM = {"LEARN_SUCCESS": 0, "TEST_FAILED": 1, "OFF_LIMITS": 2,
                  "START_SPEED": 3, "SPEED_RANGE": 4, "TORQUE": 5, "BRAKE": 6,
                  "FAULT": 7}
_OFFSET_ENUM = {"SUCCESS": 0, "FAIL_BRAKES": 1, "FAIL_INVALID_GEAR": 2,
                "FAIL_NEG_SPEED": 3, "FAIL_OVER_SPEED": 4, "FAIL_TIMEOUT": 7}


def _fs(bit_length, byte_position, bit_position=0, data_type="uint", enum=None):
    return FieldSpec(bit_length=bit_length, byte_position=byte_position,
                     bit_position=bit_position, data_type=data_type,
                     enum_map=enum or {})


# Mirrors DIR RESOLVER_LEARNING.results (out_size 187).
_RESOLVER_RESULTS = SubSpec(
    security_level=0, input={}, input_size=0, output_size=187,
    output={
        "ERRORTABLE": _fs(1472, 0, data_type="bytes"),
        "RMSERROR": _fs(16, 184, data_type="int"),
        "LEARN_RESULT": _fs(3, 186, 0, "uint", _RESOLVER_ENUM),
        "WRITE_FAILED": _fs(1, 186, 3, "uint", _RUNNING_ENUM),
        "RUNNING": _fs(1, 186, 4, "uint", _RUNNING_ENUM),
    })

# Mirrors DIR OFFSET_LEARNING.results (out_size 17).
_OFFSET_RESULTS = SubSpec(
    security_level=5, input={}, input_size=0, output_size=17,
    output={
        "INITIAL_OFFSET": _fs(32, 0, data_type="int"),
        "FINAL_OFFSET": _fs(32, 4, data_type="int"),
        "FINAL_VD": _fs(32, 8, data_type="int"),
        "FINAL_VQ": _fs(32, 12, data_type="int"),
        "LEARN_RESULT": _fs(4, 16, 0, "uint", _OFFSET_ENUM),
    })


def _resolver_payload(table: bytes, rms: int, flags: int) -> bytes:
    assert len(table) == 184
    return table + struct.pack(">h", rms) + bytes([flags])


class TestResolverDecode:
    def test_running_speed_range(self):
        table = bytes(i % 256 for i in range(184))
        # flags 0x14: LEARN_RESULT=4 (bits0-2), WRITE_FAILED=0 (bit3), RUNNING=1 (bit4)
        out = decode_response(_RESOLVER_RESULTS, _resolver_payload(table, 256, 0x14))
        assert out["ERRORTABLE"] == table          # type=bytes -> raw 184 bytes
        assert out["RMSERROR"] == 256              # int16 BE (raw; scaling is caller-side)
        assert out["LEARN_RESULT"] == "SPEED_RANGE"
        assert out["RUNNING"] is True              # TRUE/FALSE enum -> bool
        assert out["WRITE_FAILED"] is False

    def test_success_idle(self):
        out = decode_response(_RESOLVER_RESULTS, _resolver_payload(b"\x00" * 184, 0, 0x00))
        assert out["LEARN_RESULT"] == "LEARN_SUCCESS"
        assert out["RUNNING"] is False

    def test_raw_mode_skips_enum(self):
        raw = decode_response(_RESOLVER_RESULTS, _resolver_payload(b"\x00" * 184, 0, 0x14),
                              parsed=False)
        assert raw["LEARN_RESULT"] == 4
        assert raw["RUNNING"] == 1

    def test_matches_resolver_cal_bitmath(self):
        # cross-check the exact expressions resolver_cal.parse_resolver_result uses
        flags = 0x1C  # LEARN=4? no: 0x1C = 0b0001_1100 -> LEARN=4, WRITE=1(bit3), RUN=1(bit4)
        out = decode_response(_RESOLVER_RESULTS, _resolver_payload(b"\x00" * 184, 0, flags),
                              parsed=False)
        assert out["LEARN_RESULT"] == (flags & 0x07)
        assert out["WRITE_FAILED"] == ((flags >> 3) & 1)
        assert out["RUNNING"] == ((flags >> 4) & 1)


class TestOffsetDecode:
    def test_signed_int32_fields(self):
        data = struct.pack(">iiii", 1000, -2000, 5, -5) + bytes([0x00])
        out = decode_response(_OFFSET_RESULTS, data)
        assert out["INITIAL_OFFSET"] == 1000
        assert out["FINAL_OFFSET"] == -2000
        assert out["FINAL_VD"] == 5
        assert out["FINAL_VQ"] == -5
        assert out["LEARN_RESULT"] == "SUCCESS"

    def test_fail_enum(self):
        data = struct.pack(">iiii", 0, 0, 0, 0) + bytes([0x04])
        assert decode_response(_OFFSET_RESULTS, data)["LEARN_RESULT"] == "FAIL_OVER_SPEED"


class TestPrimitiveDecode:
    def test_ascii(self):
        fs = _fs(112, 0, data_type="ascii")
        assert decode_field(fs, b"ABCDEFGHIJKLMN") == "ABCDEFGHIJKLMN"

    def test_ascii_strips_nul(self):
        fs = _fs(64, 0, data_type="ascii")
        assert decode_field(fs, b"AB\x00\x00\x00\x00\x00\x00") == "AB"

    def test_uint_be(self):
        assert decode_field(_fs(32, 0, data_type="uint"), b"\x00\x00\x01\x00") == 256

    def test_short_payload_returns_none(self):
        assert decode_field(_fs(16, 10), b"\x00\x00") is None


class TestEncode:
    def test_byte_fields(self):
        sub = SubSpec(security_level=0, input_size=2, output_size=0, output={},
                      input={"CHANNEL": _fs(8, 0), "MODE": _fs(8, 1)})
        assert encode_request(sub, {"CHANNEL": 3, "MODE": 7}) == b"\x03\x07"

    def test_enum_name_input(self):
        sub = SubSpec(security_level=0, input_size=1, output_size=0, output={},
                      input={"STATE": _fs(8, 0, 0, "uint", {"ON": 1, "OFF": 0})})
        assert encode_request(sub, {"STATE": "ON"}) == b"\x01"

    def test_int16_be(self):
        sub = SubSpec(security_level=0, input_size=2, output_size=0, output={},
                      input={"V": _fs(16, 0, data_type="int")})
        assert encode_request(sub, {"V": -1}) == b"\xff\xff"

    def test_sub_byte_bitfield(self):
        sub = SubSpec(security_level=0, input_size=1, output_size=0, output={},
                      input={"FLAG": _fs(1, 0, 4, "uint")})
        assert encode_request(sub, {"FLAG": 1}) == b"\x10"

    def test_absent_field_is_zero_fill(self):
        sub = SubSpec(security_level=0, input_size=2, output_size=0, output={},
                      input={"A": _fs(8, 0), "B": _fs(8, 1)})
        assert encode_request(sub, {"A": 0xAB}) == b"\xab\x00"

    def test_no_input_spec(self):
        assert encode_request(None, {}) == b""

    def test_multibyte_field_is_big_endian(self):
        # Regression: byte-aligned multi-byte inputs go out big-endian (the wire
        # convention). tm3diag's old _prompt_routine_inputs hand-packed LSB-first,
        # so 0x1234 wrongly became b"\x34\x12".
        sub = SubSpec(security_level=0, input_size=2, output_size=0, output={},
                      input={"V": _fs(16, 0, data_type="uint")})
        assert encode_request(sub, {"V": 0x1234}) == b"\x12\x34"


class TestEncodeFields:
    """encode_fields: the shared packer under encode_request, also used directly for
    IO controls (which carry a bare fields dict + input_size, not a SubSpec)."""

    def test_big_endian_and_padding(self):
        # 16-bit field at byte 1, input_size 4 -> byte 0 + trailing byte zero-filled.
        fields = {"V": _fs(16, 1, data_type="uint")}
        assert encode_fields(fields, {"V": 0x00FF}, 4) == b"\x00\x00\xff\x00"

    def test_sub_byte_and_enum(self):
        fields = {"FLAG": _fs(1, 0, 4, "uint"),
                  "MODE": _fs(8, 1, 0, "uint", {"ON": 1, "OFF": 0})}
        assert encode_fields(fields, {"FLAG": 1, "MODE": "ON"}, 2) == b"\x10\x01"

    def test_empty_fields(self):
        assert encode_fields({}, {"X": 1}, 4) == b""

    def test_encode_request_delegates(self):
        sub = SubSpec(security_level=0, input_size=2, output_size=0, output={},
                      input={"A": _fs(8, 0), "B": _fs(8, 1)})
        assert encode_request(sub, {"A": 1, "B": 2}) == encode_fields(
            sub.input, {"A": 1, "B": 2}, sub.input_size) == b"\x01\x02"
