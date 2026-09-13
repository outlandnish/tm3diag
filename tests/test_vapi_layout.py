"""Tests for vapi_layout: recovering CAN bit-layout from GUICanCracker.

Self-contained -- no firmware and no capstone needed. The field model, the mask
discrimination, the float32 snapping, the byte-order bit walk and the overlap /
mux guards are all exercised directly, using the real instruction shapes seen in
libQtCarVAPI (documented in each test).
"""
import pytest

import vapi_layout as V
from vapi_layout import (
    CRACK_SYM,
    Field,
    Report,
    Store,
    _is_field_mask,
    _lea_shift,
    _occupied_bits,
    _snap_float,
    apply_guards,
    check_alert_pages,
    dispatch_targets,
    extract_stores,
    find_compare_chain,
    find_dispatch,
    name_multiplexors,
)


class TestFieldWidth:
    def test_plain_byte_load(self):
        # movzx eax, byte ptr [rbx+3]  -> whole byte
        assert Field(byte=3).width == 8
        assert Field(byte=3).start == 24

    def test_shift_and_mask(self):
        # movzx eax,byte ptr[rbx+2]; shr al,3; and eax,3  -> DI_brakePedalState 19|2
        f = Field(byte=2, shift=3, mask=0b11, narrowed=True)
        assert (f.start, f.width) == (19, 2)

    def test_shift_without_mask_is_capped_by_the_subregister(self):
        # movzx eax,byte ptr[rbx+2]; shr al,5; movzx eax,al -> DI_gear 21|3
        f = Field(byte=2, shift=5, narrowed=True)
        assert (f.start, f.width) == (21, 3)

    def test_whole_word_load(self):
        # movsx eax, word ptr [rbx+5] -> DIR_axleSpeed 40|16
        f = Field(byte=5, avail=16, signed=True)
        assert (f.start, f.width) == (40, 16)

    def test_mask_outranks_an_inferred_width(self):
        f = Field(byte=1, avail=16, shift=2, fixed=14, mask=0b111)
        assert f.width == 3

    def test_joined_split_field(self):
        # DIR_torqueActual: low = byte3 >>3 (5 bits), high = byte4 <<5 (8 bits)
        f = Field(byte=3, shift=3, fixed=13, signed=True)
        assert (f.start, f.width) == (27, 13)

    def test_sign_extension_width(self):
        # shl eax,5 / sar eax,5 on a 16-bit load -> signed 11 bits at the load's base
        f = Field(byte=0, avail=16, signed=True, fixed=11)
        assert (f.start, f.width) == (0, 11)


class TestNarrowingCap:
    """A register copy AFTER the join truncates the assembled field.

    VCRIGHT_hvacSetTempActualLeft is the worked example:
        movzx edx, byte ptr [rbx+2] ; shl edx,4     ; high half
        movzx eax, byte ptr [rbx+1] ; shr al,4      ; low half
        or    eax, edx                              ; looks 12 bits wide
        movzx eax, al                               ; ...but is 8
    Without the clamp it ran into hvacSetTempActualRight at bit 12 and BOTH were
    dropped as overlapping, which cost the whole message.
    """

    def test_clamp_narrows_a_joined_field(self):
        f = Field(byte=1, shift=4, fixed=12, cap=8)
        assert (f.start, f.width) == (12, 8)

    def test_clamp_never_widens(self):
        assert Field(byte=1, avail=8, cap=16).width == 8

    def test_clamp_outranks_even_a_mask(self):
        # and eax,0x3ff then movzx eax,al keeps only the low 8 bits
        assert Field(byte=0, mask=0x3FF, cap=8).width == 8

    def test_absent_clamp_leaves_the_width_alone(self):
        # VCSEC_Device0BatteryLevel: 10 bits of a word load | 3 bits of byte 5
        assert Field(byte=5, shift=5, fixed=13).width == 13

    def test_word_clamp(self):
        # movzx eax, ax after a 20-bit join -> 16
        assert Field(byte=2, shift=4, fixed=20, cap=16).width == 16


class TestLeaAsShift:
    """GCC writes small left shifts as address arithmetic.

    Read as opaque, the high half of a split field never gets `shl` set, so the
    `or` join does not fire and only the low half survives -- which is why
    VCFRONT_EPAS3PCurrent came out 47|1 instead of 47|16.
    """

    @pytest.mark.parametrize("mem,want", [
        ("[rax + rax]", ("rax", 1)),        # *2
        ("[rax*4]", ("rax", 2)),
        ("[rdx*8]", ("rdx", 3)),
        ("[rax + rax*3]", ("rax", 2)),      # *4
        ("[rax + rax*7]", ("rax", 3)),      # *8
    ])
    def test_power_of_two_multipliers_are_shifts(self, mem, want):
        assert _lea_shift(mem) == want

    @pytest.mark.parametrize("mem", [
        "[rax + rax*2]",        # *3 is a real multiply, not a field move
        "[rax + rax*8]",        # *9
        "[rax + rdx]",          # two different registers: an address
        "[rax + 1]",            # an increment
        "[rax]",                # a plain copy
        "[rip + 0x2b0442]",     # a constant pool reference
    ])
    def test_everything_else_is_not_a_shift(self, mem):
        assert _lea_shift(mem) is None

    def test_cwde_caps_a_joined_field_at_16_and_signs_it(self):
        # VCFRONT_PCSCurrent: the `or` join looks 17 bits wide, then `cwde`
        # sign-extends AX -> EAX, which says only 16 of them are the field.
        f = Field(byte=5, avail=8, shift=7, fixed=17, cap=16, signed=True)
        assert (f.start, f.width, f.signed) == (47, 16, True)

    def test_the_join_it_unblocks(self):
        # movzx eax,word ptr[rbx+6] / lea edx,[rax+rax] / movzx eax,byte ptr[rbx+5]
        # / shr al,7 / or eax,edx / movzx eax,ax  -> 1 + 16 bits, clamped to 16
        f = Field(byte=5, avail=8, shift=7, fixed=17, cap=16)
        assert (f.start, f.width) == (47, 16)


class TestModelled:
    def test_masked_wide_load_is_trusted(self):
        assert Field(byte=0, avail=64, shift=13, mask=1).modelled

    def test_whole_wide_load_is_trusted(self):
        # mov eax, dword ptr [rbx+4] -> GTW_appCrc 32|32
        f = Field(byte=4, avail=32)
        assert f.modelled and (f.start, f.width) == (32, 32)

    def test_shifted_wide_load_without_a_mask_is_not(self):
        # `avail - shift` here would invent a huge field that collides with
        # every real signal in the message, so it must be refused.
        assert not Field(byte=0, avail=64, shift=13).modelled

    def test_unmodelled_flag_wins(self):
        assert not Field(byte=0, mask=1, ok=False).modelled


class TestMaskDiscrimination:
    @pytest.mark.parametrize("m", [0b1, 0b11, 0b111, 0xF, 0xFF, 0xFFF])
    def test_low_contiguous_masks_are_field_masks(self, m):
        assert _is_field_mask(m)

    @pytest.mark.parametrize("m", [0xFFFFFFE0, 0xE0, 0b1010, 0])
    def test_sna_and_sparse_masks_are_not(self, m):
        # `and eax,0xffffffe0` + cmp/setne is the SNA test, not a field mask;
        # taking its popcount produced DI_gear as 21|27.
        assert not _is_field_mask(m)


class TestSnapFloat:
    def test_float32_constant_snaps_to_its_short_decimal(self):
        # a C float 0.4 reaches the binary as 0.4000000059604645
        import struct
        widened = struct.unpack("<f", struct.pack("<f", 0.4))[0]
        assert widened != 0.4
        assert _snap_float(widened) == 0.4

    @pytest.mark.parametrize("v", [0.1, 0.2, 0.01, 11.0, -819.2])
    def test_round_trips(self, v):
        import struct
        assert _snap_float(struct.unpack("<f", struct.pack("<f", v))[0]) == v

    @pytest.mark.parametrize("v", [0.0732421875, 0.146484375, 0.0439453125])
    def test_an_exact_short_decimal_is_the_authored_constant(self, v):
        # 0.0732421875 is 75/1024 written out in full. float32 also accepts
        # 0.07324219, but shortening to it throws away digits Tesla's own
        # compact.json still carries.
        assert _snap_float(v) == v

    def test_a_genuine_double_is_left_alone(self):
        x = 0.1234567890123456789
        assert _snap_float(x) == x

    def test_identity_on_simple_values(self):
        assert _snap_float(1.0) == 1.0
        assert _snap_float(0.0) == 0.0


class TestOccupiedBits:
    def test_intel_runs_upward(self):
        assert _occupied_bits({"start_position": 21, "width": 3}) == [21, 22, 23]

    def test_motorola_walks_down_then_to_the_next_byte(self):
        # start 31 = byte 3 bit 7; 14 bits covers byte3 7..0 then byte4 7..2
        bits = _occupied_bits({"start_position": 31, "width": 14,
                               "endianness": "BIG"})
        assert bits[:3] == [31, 30, 29]
        assert bits[8] == 39          # steps to bit 7 of the next byte
        assert len(bits) == len(set(bits)) == 14

    def test_the_two_orders_differ(self):
        le = _occupied_bits({"start_position": 31, "width": 14})
        be = _occupied_bits({"start_position": 31, "width": 14, "endianness": "BIG"})
        assert le != be


class TestDispatchTargets:
    def test_self_relative_int32_entries(self):
        class FakeElf:
            # two entries at file offset 0: +0x10 and -0x8, relative to the table
            d = (0x10).to_bytes(4, "little", signed=True) + \
                (-8).to_bytes(4, "little", signed=True)

            def v2o(self, addr):
                return 0

        assert dispatch_targets(FakeElf(), 5, 2, 0x1000) == {5: 0x1010, 6: 0xFF8}


BASE = 0x1000       # virtual address the synthetic code is assembled at


class _Code:
    """Hand-assembled x86-64 standing in for a crackMessage case.

    Only the handful of encodings the mux forms actually use, so the dispatch
    walkers can be driven without a 12 MB firmware library.
    """

    def __init__(self):
        self.b = bytearray()

    # -- operands are fixed to what GCC emits for these dispatches --
    def load_sel(self, byte=0):                     # movzx eax, byte ptr [rbx+N]
        self.b += b"\x0f\xb6\x03" if byte == 0 else b"\x0f\xb6\x43" + bytes([byte])
        return self

    def mov_edx_eax(self):
        self.b += b"\x89\xc2"
        return self

    def mov_rdx_rax(self):
        self.b += b"\x48\x89\xc2"
        return self

    def and_edx(self, imm):                         # and edx, imm8
        self.b += b"\x83\xe2" + bytes([imm])
        return self

    def and_dl(self, imm):                          # and dl, imm8
        self.b += b"\x80\xe2" + bytes([imm])
        return self

    def cmp_dl(self, imm):
        self.b += b"\x80\xfa" + bytes([imm])
        return self

    def sub_dl(self, imm):
        self.b += b"\x80\xea" + bytes([imm])
        return self

    def shr_al(self, imm):
        self.b += b"\xc0\xe8" + bytes([imm])
        return self

    def test_dl(self):
        self.b += b"\x84\xd2"
        return self

    def test_ax(self):                              # test ax, ax
        self.b += b"\x66\x85\xc0"
        return self

    def load_edx(self, byte):                       # movzx edx, byte ptr [rbx+N]
        self.b += b"\x0f\xb6\x53" + bytes([byte])
        return self

    def shr_dl(self, imm):                          # shr dl, imm8
        self.b += b"\xc0\xea" + bytes([imm])
        return self

    def or_edx_ecx(self):                           # or edx, ecx
        self.b += b"\x09\xca"
        return self

    def lea_ecx_rdx2(self):                         # lea ecx, [rdx + rdx]
        self.b += b"\x8d\x0c\x12"
        return self

    def test_al_imm(self, imm):                     # test al, imm8
        self.b += b"\xa8" + bytes([imm])
        return self

    def test_mem_imm(self, byte, imm):              # test byte ptr [rbx+N], imm8
        self.b += b"\xf6\x43" + bytes([byte, imm])
        return self

    def add_ax(self, imm):                          # add ax, imm16
        self.b += b"\x66\x05" + imm.to_bytes(2, "little")
        return self

    def and_ax(self, imm):                          # and ax, imm16
        self.b += b"\x66\x25" + imm.to_bytes(2, "little")
        return self

    def cmp_ax(self, imm):                          # cmp ax, imm16
        self.b += b"\x66\x3d" + imm.to_bytes(2, "little")
        return self

    def add_eax(self, imm):                         # add eax, imm32
        self.b += b"\x05" + imm.to_bytes(4, "little")
        return self

    def sub_eax(self, imm):                         # sub eax, imm32
        self.b += b"\x2d" + imm.to_bytes(4, "little")
        return self

    def sub_rsp(self, imm):                         # sub rsp, imm8
        self.b += b"\x48\x83\xec" + bytes([imm])
        return self

    def cmp_al(self, imm):                          # cmp al, imm8
        self.b += b"\x3c" + bytes([imm])
        return self

    def cmp_eax(self, imm):                         # cmp eax, imm32
        self.b += b"\x3d" + imm.to_bytes(4, "little")
        return self

    def _jcc(self, op, target):
        self.b += op + (target - (BASE + len(self.b) + 6)).to_bytes(4, "little",
                                                                   signed=True)
        return self

    def je(self, t):
        return self._jcc(b"\x0f\x84", t)

    def jne(self, t):
        return self._jcc(b"\x0f\x85", t)

    def ja(self, t):
        return self._jcc(b"\x0f\x87", t)

    def jbe(self, t):
        return self._jcc(b"\x0f\x86", t)

    def jb(self, t):
        return self._jcc(b"\x0f\x82", t)

    def at(self, va):
        """Pad forward so the next bytes are assembled at ``va``."""
        off = va - BASE
        assert len(self.b) <= off, "already past that address"
        self.b += b"\x90" * (off - len(self.b))
        return self

    def cmp_esi(self, imm):                         # cmp esi, imm8
        self.b += b"\x83\xfe" + bytes([imm])
        return self

    def mov_edx_imm(self, imm):                     # mov edx, imm32
        self.b += b"\xba" + imm.to_bytes(4, "little")
        return self

    def mov_rdi_rbp(self):                          # mov rdi, rbp
        self.b += b"\x48\x89\xef"
        return self

    def sign_off(self, msg_id, plt):
        """The block every message case ends on: ``messageArrived(3, id)``."""
        return self.mov_edx_imm(msg_id).key(3).mov_rdi_rbp().call(plt)

    def put_table(self, va, targets):
        """Lay a jump table of self-relative int32 entries at ``va``."""
        off = va - BASE
        if len(self.b) < off:
            self.b += b"\x90" * (off - len(self.b))
        for t in targets:
            self.b += (t - va).to_bytes(4, "little", signed=True)
        return self

    def lea_rax_rip(self, target):                  # lea rax, [rip + disp32]
        self.b += b"\x48\x8d\x05" + (target - (BASE + len(self.b) + 7)).to_bytes(
            4, "little", signed=True)
        return self

    def table_jmp(self):                            # movsxd/add/jmp rax
        self.b += b"\x48\x63\x14\x90\x48\x01\xd0\xff\xe0"
        return self

    def load_dword(self, byte):                     # mov eax, dword ptr [rbx+N]
        self.b += b"\x8b\x43" + bytes([byte])
        return self

    def load_word(self, byte):                      # movzx eax, word ptr [rbx+N]
        self.b += b"\x0f\xb7\x43" + bytes([byte])
        return self

    def load_word_ecx(self, byte):                  # movzx ecx, word ptr [rbx+N]
        self.b += b"\x0f\xb7\x4b" + bytes([byte])
        return self

    def shl_eax(self, imm):
        self.b += b"\xc1\xe0" + bytes([imm])
        return self

    def sar_eax(self, imm):
        self.b += b"\xc1\xf8" + bytes([imm])
        return self

    def sar_ax(self, imm):                          # 66 prefix: 16-bit operand
        self.b += b"\x66\xc1\xf8" + bytes([imm])
        return self

    def or_edx_eax(self):
        self.b += b"\x09\xc2"
        return self

    def or_eax_edx(self):
        self.b += b"\x09\xd0"
        return self

    def sar_al(self, imm):                          # sar al, imm8
        self.b += b"\xc0\xf8" + bytes([imm])
        return self

    def movsx_eax_al(self):                         # movsx eax, al
        self.b += b"\x0f\xbe\xc0"
        return self

    def shr_ax(self, imm):                          # shr ax, imm8 (16-bit)
        self.b += b"\x66\xc1\xe8" + bytes([imm])
        return self

    def movzx_eax_ax(self):                         # movzx eax, ax
        self.b += b"\x0f\xb7\xc0"
        return self

    def shl_rax(self, imm):                         # shl rax, imm8 (64-bit)
        self.b += b"\x48\xc1\xe0" + bytes([imm])
        return self

    def or_rdx_rax(self):                           # or rdx, rax
        self.b += b"\x48\x09\xc2"
        return self

    def or_rax_rdx(self):                           # or rax, rdx
        self.b += b"\x48\x09\xd0"
        return self

    def bswap_eax(self):
        self.b += b"\x0f\xc8"
        return self

    def shr_eax(self, imm):                         # shr eax, imm8
        self.b += b"\xc1\xe8" + bytes([imm])
        return self

    def key(self, k):                               # mov esi, imm32
        self.b += b"\xbe" + k.to_bytes(4, "little")
        return self

    def cvt(self):                                  # cvtsi2sd xmm0, eax
        self.b += b"\xf2\x0f\x2a\xc0"
        return self

    def cvt_mem(self, byte):        # cvtsi2sd xmm1, dword ptr [rbx + N]
        self.b += b"\xf2\x0f\x2a\x4b" + bytes([byte])
        return self

    def xorpd_xmm0(self):                           # xorpd xmm0, xmm0
        self.b += b"\x66\x0f\x57\xc0"
        return self

    def xorps_xmm0(self):                           # xorps xmm0, xmm0
        self.b += b"\x0f\x57\xc0"
        return self

    def addsd_xmm0_xmm1(self):                      # addsd xmm0, xmm1
        self.b += b"\xf2\x0f\x58\xc1"
        return self

    def mulsd_rip(self, target):                    # mulsd xmm0, qword [rip+d]
        self.b += b"\xf2\x0f\x59\x05" + (
            target - (BASE + len(self.b) + 8)).to_bytes(4, "little", signed=True)
        return self

    def movsd_xmm0_rip(self, target):               # movsd xmm0, qword [rip+d]
        self.b += b"\xf2\x0f\x10\x05" + (
            target - (BASE + len(self.b) + 8)).to_bytes(4, "little", signed=True)
        return self

    def movsd_xmm1_rip(self, target):               # movsd xmm1, qword [rip+d]
        self.b += b"\xf2\x0f\x10\x0d" + (
            target - (BASE + len(self.b) + 8)).to_bytes(4, "little", signed=True)
        return self

    def mulsd_xmm1_xmm0(self):                      # mulsd xmm1, xmm0
        self.b += b"\xf2\x0f\x59\xc8"
        return self

    def addsd_xmm0_xmm0(self):                      # addsd xmm0, xmm0
        self.b += b"\xf2\x0f\x58\xc0"
        return self

    def addsd_xmm0_xmm6(self):                      # addsd xmm0, xmm6
        self.b += b"\xf2\x0f\x58\xc6"
        return self

    def xor_r12d(self):                             # xor r12d, r12d
        self.b += b"\x45\x31\xe4"
        return self

    def xor_eax(self):                              # xor eax, eax
        self.b += b"\x31\xc0"
        return self

    def movq_xmm6_r12(self):                        # movq xmm6, r12
        self.b += b"\x66\x49\x0f\x6e\xf4"
        return self

    def movq_xmm6_rax(self):                        # movq xmm6, rax
        self.b += b"\x66\x48\x0f\x6e\xf0"
        return self

    def jmp_next(self):                             # jmp to the next instruction
        self.b += b"\xe9\x00\x00\x00\x00"
        return self

    def put_double(self, va, value):
        import struct
        off = va - BASE
        if len(self.b) < off:
            self.b += b"\x90" * (off - len(self.b))
        self.b[off:off + 8] = struct.pack("<d", value)
        return self

    def call(self, target):
        self.b += b"\xe8" + (target - (BASE + len(self.b) + 5)).to_bytes(
            4, "little", signed=True)
        return self

    def pad(self, n=1):
        self.b += b"\x90" * n
        return self

    def here(self):
        return BASE + len(self.b)

    def elf(self, size=0x4000):
        blob = bytes(self.b).ljust(size, b"\x90")
        n = len(self.b)

        class FakeElf:
            d = blob
            syms = {CRACK_SYM: {"value": BASE, "size": n}}

            def v2o(self, addr):
                return addr - BASE

        return FakeElf()


@pytest.fixture
def md():
    capstone = pytest.importorskip("capstone")
    return capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)


class TestCompareChainDispatch:
    """The alertMatrix family selects its page with an if-else chain, not a table.

    GCC only builds a jump table when the case values are dense enough; a handful
    of pages keyed off a payload NIBBLE gets compares instead, which the
    jump-table walker cannot see at all. 80 messages / 2810 signals sat behind it.
    """

    def test_cmp_je_chain_with_a_nibble_selector(self, md):
        # GTW_alertMatrix (0x3e): and edx,0xf / cmp dl,1 / je / cmp dl,2 / je
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[:3] == (0, 0x0F, {1: 0x3000, 2: 0x3100})

    def test_selector_byte_is_reported(self, md):
        c = _Code().load_sel(2).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[0] == 2


class TestOneBitSelectorDispatch:
    """A message whose pages hang on ONE payload bit gets neither shape.

    VC_pcsInterface, VCBATT_pcsInterface, TAS_axleData, UI_systemMonitor and
    VCFRONT_vehicleStatus dispatch on a single bit, so GCC emits a plain
    two-way branch. With no table and no compare chain to find, every signal
    landed on one flat page and the whole message was discarded as overlapping
    -- 2026.8.3 lost 103 signals that way.
    """

    def test_memory_operand_bit_test(self, md):
        # VC_pcsInterface (0x441): test byte ptr [rbx+6],1 / je page0,
        # and page 1 is the fall-through.
        c = _Code().test_mem_imm(6, 1).je(0x3000)
        page1 = c.here()
        c.pad(8)
        sel, mask, targets, _ = find_compare_chain(c.elf(), md, BASE, BASE,
                                                   BASE + 0x4000)
        assert (sel, mask) == (6, 1)
        assert targets == {0: 0x3000, 1: page1}

    def test_register_operand_bit_test(self, md):
        # VCFRONT_vehicleStatus (0x3a1) loads the byte first: test al,1 / je
        c = _Code().load_sel(0).test_al_imm(1).je(0x3000)
        page1 = c.here()
        c.pad(8)
        sel, mask, targets, _ = find_compare_chain(c.elf(), md, BASE, BASE,
                                                   BASE + 0x4000)
        assert (sel, mask) == (0, 1)
        assert targets == {0: 0x3000, 1: page1}

    def test_a_later_and_does_not_redefine_the_selector(self, md):
        # the walk continues into the page body, where `and eax,0xc` is
        # ordinary field extraction -- it must not be folded into the mask
        c = _Code().test_mem_imm(6, 1).je(0x3000)
        c.load_sel(0).and_edx(0x0C).pad(8)
        sel, mask, _, _ = find_compare_chain(c.elf(), md, BASE, BASE,
                                             BASE + 0x4000)
        assert (sel, mask) == (6, 1)

    def test_a_preceding_mask_does_not_bound_the_pages(self, md):
        # 0x441 masks byte 6 with 0xf to read an unrelated signal just before
        # testing bit 0 of it; that 0..15 bound must not survive, or page 1
        # (the fall-through) is never attributed
        c = _Code().load_sel(6).and_edx(0x0F).test_mem_imm(6, 1).je(0x3000)
        page1 = c.here()
        c.pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2] == {0: 0x3000, 1: page1}

    def test_a_multi_bit_mask_is_not_a_two_way_split(self, md):
        # `test al,3` only separates zero from nonzero, and nonzero is two
        # different pages -- refuse rather than invent one
        c = _Code().load_sel(0).test_al_imm(3).je(0x3000).pad(8)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000) is None

    def test_an_established_chain_is_not_hijacked(self, md):
        # a plausibility `test al,1` inside a page body must not steal the
        # selector from a compare chain that already resolved pages
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100)
        c.load_sel(4).test_al_imm(1).je(0x3200).pad(8)
        sel, mask, targets, _ = find_compare_chain(c.elf(), md, BASE, BASE,
                                                   BASE + 0x4000)
        assert (sel, mask) == (0, 0x0F)
        assert targets == {1: 0x3000, 2: 0x3100}

    def test_and_sets_zf_so_a_bare_je_means_page_zero(self, md):
        # BMS_log2 (0x3b2): `and dl,0x3f` / `je page0` -- there is no cmp at all
        c = _Code().load_sel(0).mov_edx_eax().and_dl(0x3F).je(0x3000)
        c.cmp_dl(0x15).je(0x3100).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2] == {0: 0x3000, 0x15: 0x3100}

    def test_sub_walks_the_page_number_down(self, md):
        # RCM_alertMatrix (0x371): and dl,0xf / je p0 / sub dl,1 / jne next,
        # so the page-1 body is the FALL-THROUGH of the jne.
        c = _Code().load_sel(0).mov_edx_eax().and_dl(0x0F).je(0x3000).sub_dl(1)
        c.jne(0x3400)
        after_jne = c.here()
        c.pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2] == {0: 0x3000, 1: after_jne}

    def test_the_page_the_chain_falls_into_is_recovered(self, md):
        # IBST_alertMatrix (0x35d): cmp dl,2 / je p2 / ja hi / test dl,dl / je p0.
        # `ja` bounds the run to 0..2, so the fall-through must be page 1.
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(2).je(0x3200).ja(0x3800).test_dl().je(0x3000)
        page1 = c.here()
        c.pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2] == {0: 0x3000, 1: page1, 2: 0x3200}

    def test_ja_target_is_walked_as_more_pages_not_as_the_end(self, md):
        # The pages above the bound are tested at the `ja` target, so stopping
        # there loses them -- and their signals then land in a neighbour's slot.
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(2).je(0x3200).ja(0x2000).test_dl().je(0x3000)
        c.pad(0x2000 - c.here())
        c.cmp_dl(3).jne(0x2100)             # page 3 falls through the jne
        page3 = c.here()
        c.pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2][3] == page3
        assert set(got[2]) == {0, 1, 2, 3}

    def test_a_lone_comparison_is_not_a_dispatch(self, md):
        # One cmp/je on a payload byte is a plausibility check. Inventing a mux
        # from it would shred a message that has none.
        c = _Code().load_sel(0).mov_edx_eax().cmp_dl(1).je(0x3000).pad(8)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000) is None

    def test_no_selector_load_means_no_dispatch(self, md):
        c = _Code().cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000) is None


class TestModularRebaseInTheChain:
    """`add ax,K` then `and ax,MASK` is ONE rebase, and the chain walks through it.

    VCFRONT1_alertLog bounds its table with the REBASED value::

        cmp ax, 0x10a / ja subtree      ; alerts above 266
        test ax, ax   / je default      ; alert 0
        add ax, 0x3ff / and ax, 0x3ff   ; -1 mod 1024
        cmp ax, 0x109 / ja default      ; ...so this is alert 266, not 265

    Reading the `and` as a fresh field zeroed the bias, which put that bound one
    short. The single-value split then had exactly one alert left over and gave
    alert 266 to the sign-off block -- so when the table was expanded a moment
    later, 266 was already taken and its two signals had no page at all.
    """

    DEFAULT = BASE + 0x300
    SUBTREE = BASE + 0x380
    TABLE = BASE + 0x400
    PAGES = [BASE + 0x500 + i * 0x10 for i in range(10)]     # alerts 1..10

    def _alertlog(self):
        c = _Code().load_word(0).and_ax(0x3FF)
        c.cmp_ax(0x0A).ja(self.SUBTREE)
        c.test_ax().je(self.DEFAULT)
        c.add_ax(0x3FF).and_ax(0x3FF)
        c.cmp_ax(0x09).ja(self.DEFAULT)
        c.mov_rdx_rax().lea_rax_rip(self.TABLE).table_jmp()
        c.at(self.DEFAULT).pad(8)
        c.at(self.SUBTREE).pad(8)
        c.put_table(self.TABLE, self.PAGES)
        return c

    def test_the_last_page_in_the_run_is_not_lost_to_the_bound(self, md):
        got = find_compare_chain(self._alertlog().elf(), md, BASE, BASE,
                                 BASE + 0x4000)
        assert got[2][10] == self.PAGES[9]

    def test_the_rest_of_the_table_still_lands(self, md):
        got = find_compare_chain(self._alertlog().elf(), md, BASE, BASE,
                                 BASE + 0x4000)
        assert [got[2][a] for a in range(1, 11)] == self.PAGES

    def test_no_page_is_handed_to_the_sign_off(self, md):
        got = find_compare_chain(self._alertlog().elf(), md, BASE, BASE,
                                 BASE + 0x4000)
        assert [a for a, t in got[2].items() if t == self.DEFAULT] == [0]

    def test_a_mask_with_no_rebase_before_it_still_starts_at_zero(self, md):
        # the complement: `and edx,0xf` on a fresh selector defines the field,
        # so `cmp dl,1` is page 1 and not page 1 shifted by a stale bias
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)
        assert got[2] == {1: 0x3000, 2: 0x3100}


class TestJoinedSelector:
    """A selector ASSEMBLED from two payload pieces, straddling a byte boundary.

    VCFRONT_lightStatus picks its page with a 3-bit index built from byte 4 bit 7
    and byte 5 bits 0-1::

        movzx eax, byte ptr [rbx + 5]
        mov   edx, eax
        and   edx, 3
        lea   ecx, [rdx + rdx]        ; the high two bits, shifted up one
        movzx edx, byte ptr [rbx + 4]
        shr   dl, 7                   ; the LSB
        or    edx, ecx
        sub   dl, 1                   ; ZF iff the index is 1
        je    page1

    In Intel numbering that is bits 39..41 -- perfectly contiguous, and already
    recovered as the signal VCFRONT_lightStatusMuxIndex. The compare chain
    tracks a selector as a byte plus a mask and so cannot express it; six
    signals were emitted as unconditional when they are only valid on page 1.
    """

    STORE = 0x2000
    ARRIVED = 0x2100
    PAGE = BASE + 0x300

    def _split(self, hi_byte=5, lo_byte=4, page=1):
        c = _Code().load_sel(hi_byte).mov_edx_eax().and_edx(3).lea_ecx_rdx2()
        c.load_edx(lo_byte).shr_dl(7).or_edx_ecx().sub_dl(page).je(self.PAGE)
        c.sign_off(0x3F6, self.ARRIVED)
        c.at(self.PAGE).pad(8)
        return c

    def _find(self, c, md):
        return V.find_joined_selector(c.elf(), md, BASE, BASE + 0x280, BASE,
                                      BASE + 0x1000, self.STORE)

    def test_the_selector_field_spans_the_two_bytes(self, md):
        sel, mask, targets, _ = self._find(self._split(), md)
        assert V._mask_field(sel, mask) == (39, 3)
        assert targets == {1: self.PAGE}

    def test_the_page_is_seeded_with_the_untouched_load(self, md):
        # the page body opens `shr al,2` on byte 5, still live across the branch
        state = self._find(self._split(), md)[3]
        assert {r: f.byte for r, f in state[1].items()} == {"a": 5}

    def test_a_selector_inside_one_byte_is_left_to_the_compare_chain(self, md):
        # `and edx,0xf` / `sub dl,1` is the ordinary shape, and two models
        # claiming the same dispatch is how pages get misnumbered
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F).sub_dl(1)
        c.je(self.PAGE).sign_off(0x3F6, self.ARRIVED)
        c.at(self.PAGE).pad(8)
        assert self._find(c, md) is None

    def test_without_the_sign_off_it_is_only_a_comparison(self, md):
        c = _Code().load_sel(5).mov_edx_eax().and_edx(3).lea_ecx_rdx2()
        c.load_edx(4).shr_dl(7).or_edx_ecx().sub_dl(1).je(self.PAGE)
        c.key(0x1234).cvt().call(self.STORE)         # a store, not the sign-off
        c.at(self.PAGE).pad(8)
        assert self._find(c, md) is None

    def test_build_regions_marks_the_page_and_names_the_selector(self, md):
        # the wiring, not just the recogniser: without this the unit tests above
        # pass with find_joined_selector never called at all
        top, case, default = BASE + 0x200, BASE + 0x100, BASE + 0x400
        c = _Code().cmp_esi(3).ja(default)
        c.mov_rdx_rax().lea_rax_rip(top).table_jmp()
        c.at(case)
        c.load_sel(5).mov_edx_eax().and_edx(3).lea_ecx_rdx2()
        c.load_edx(4).shr_dl(7).or_edx_ecx().sub_dl(1).je(self.PAGE)
        c.sign_off(0x3F6, self.ARRIVED)
        c.put_table(top, [case, default, default, default])
        c.at(self.PAGE).pad(8).at(default).pad(8)
        marks, muxsel, seeds = V.build_regions(
            c.elf(), md, BASE, BASE + 0x800, None, {0: 27}, store_plt=self.STORE)
        assert {mux for _, _, mux in marks if mux is not None} == {1}
        assert V._mask_field(*muxsel[0]) == (39, 3)
        assert seeds[self.PAGE]["a"].byte == 5

    def test_pieces_that_are_not_adjacent_are_not_a_selector(self, md):
        # shifted three places instead of one, so the runs leave a hole
        c = _Code().load_sel(5).mov_edx_eax().and_edx(3)
        c.b += b"\xc1\xe2\x03"                       # shl edx, 3
        c.b += b"\x89\xd1"                           # mov ecx, edx
        c.load_edx(4).shr_dl(7).or_edx_ecx().sub_dl(1).je(self.PAGE)
        c.sign_off(0x3F6, self.ARRIVED)
        c.at(self.PAGE).pad(8)
        assert self._find(c, md) is None


class TestOrJoin:
    """The two-piece join, in isolation."""

    def test_adjacent_runs_join(self):
        # byte 4 bit 7 (1 bit at result bit 0) under byte 5 bits 0-1 (at bit 1)
        lo = Field(byte=4, shift=7, narrowed=True)
        hi = Field(byte=5, mask=3, shl=1)
        f = V._or_join(lo, hi)
        assert (f.start, f.width, f.big) == (39, 3, False)

    def test_a_gap_between_the_runs_is_refused(self):
        lo = Field(byte=4, shift=7, narrowed=True)
        hi = Field(byte=5, mask=3, shl=3)
        assert not V._or_join(lo, hi).ok

    def test_a_lower_byte_supplying_the_high_bits_is_motorola(self):
        lo = Field(byte=5, avail=8)
        hi = Field(byte=4, avail=8, shl=8)
        assert V._or_join(lo, hi).big


class TestBareExit:
    """The sign-off block that ends every message case, and nothing else."""

    STORE = 0x2000
    ARRIVED = 0x2100

    def test_a_call_that_is_not_a_store_is_the_sign_off(self, md):
        c = _Code().sign_off(0x3E6, self.ARRIVED).pad(4)
        assert V._bare_exit(c.elf(), md, BASE, self.STORE)

    def test_a_store_first_is_not(self, md):
        c = _Code().key(0x1234).cvt().call(self.STORE)
        c.sign_off(0x3E6, self.ARRIVED).pad(4)
        assert not V._bare_exit(c.elf(), md, BASE, self.STORE)

    def test_a_branch_first_is_not(self, md):
        # more decoding to come, so this is not where the case gives up
        c = _Code().load_sel(0).test_al_imm(1).je(0x3000)
        c.sign_off(0x3E6, self.ARRIVED).pad(4)
        assert not V._bare_exit(c.elf(), md, BASE, self.STORE)

    def test_a_long_run_of_setup_is_not(self, md):
        c = _Code().pad(40).sign_off(0x3E6, self.ARRIVED).pad(4)
        assert not V._bare_exit(c.elf(), md, BASE, self.STORE)


class TestMaskTest:
    """``test al, 0xf`` is the ``and`` form without the write-back."""

    @pytest.mark.parametrize("imm,want", [(0x03, 0x03), (0x0F, 0x0F),
                                          (0x3F, 0x3F)])
    def test_multi_bit_field_masks_are_selectors(self, imm, want):
        assert V._mask_test(["al", hex(imm)], {"a"}) == (None, want)

    def test_a_single_bit_is_left_to_the_two_way_split(self):
        # _bit_test models that one, with page bounds of its own
        assert V._mask_test(["al", "0x1"], {"a"}) is None

    @pytest.mark.parametrize("imm", ["0xe0", "0xa", "0x0"])
    def test_sparse_and_high_masks_are_refused(self, imm):
        assert V._mask_test(["al", imm], {"a"}) is None

    def test_a_memory_operand_names_its_payload_byte(self):
        assert V._mask_test(["byte ptr [rbx + 6]", "0xf"], set()) == (6, 0x0F)

    def test_a_register_that_is_not_the_selector_is_refused(self):
        assert V._mask_test(["cl", "0xf"], {"a"}) is None


class TestSinglePageDispatch:
    """A mux with ONE page is a branch over the case's sign-off block.

    29 messages in 2026.8.3 -- RCU_alertMatrix, GTW_hrl, VCBATT1_LVSelfTests,
    UI_seatControl and the rest -- have a selector with a single live value, so
    GCC emits one branch rather than a table or a chain::

        movzx eax, byte ptr [rbx]
        test  al, 0xf
        je    page0
        mov   edx, 0x3e6            ; the frame arrived but nothing was decoded
        mov   esi, 3
        call  CANDataManager::messageArrived

    A lone comparison is normally refused, and rightly: a plausibility check on
    a payload byte looks identical. What separates them is where the OTHER side
    goes -- over the sign-off block, the case has given up, which no ordinary
    check does. 2020's compact.json settles it independently: it gives GTW_hrl
    mux ids 1 and 2, and the branch says 2.
    """

    STORE = 0x2000
    ARRIVED = 0x2100

    def test_mask_test_je_names_page_zero(self, md):
        # RCU_alertMatrix (0x3e6): the low nibble of byte 0 selects, and only
        # index 0 has a page.
        c = _Code().load_sel(0).test_al_imm(0x0F).je(0x3000)
        c.sign_off(0x3E6, self.ARRIVED).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                 store_plt=self.STORE)
        assert got[:3] == (0, 0x0F, {0: 0x3000})

    def test_without_the_sign_off_evidence_it_stays_refused(self, md):
        # the same code read without knowing which call stores: one target is
        # not a dispatch on its own
        c = _Code().load_sel(0).test_al_imm(0x0F).je(0x3000)
        c.sign_off(0x3E6, self.ARRIVED).pad(4)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000) is None

    def test_a_branch_over_a_STORE_is_still_not_a_dispatch(self, md):
        # `test al,0xf / je` guarding a signal that is only stored when the
        # nibble is nonzero is a plausibility check, not a page
        c = _Code().load_sel(0).test_al_imm(0x0F).je(0x3000)
        c.key(0x1234).cvt().call(self.STORE).pad(4)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                  store_plt=self.STORE) is None

    def test_cmp_je_names_a_page_other_than_zero(self, md):
        # GTW_hrl (0x7f1): and edx,3 / cmp dl,2 / je -- compact.json puts
        # GTW_hrlState on mux 2, and so does this.
        c = _Code().load_sel(0).mov_edx_eax().and_edx(3).cmp_dl(2).je(0x3000)
        c.sign_off(0x7F1, self.ARRIVED).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                 store_plt=self.STORE)
        assert got[:3] == (0, 3, {2: 0x3000})

    def test_jne_over_the_sign_off_puts_the_page_at_the_target(self, md):
        # VCBATT1_LVSelfTests (0x45f): test al,0x10 / jne page -- the page is
        # where the bit is SET, and the fall-through gives up.
        c = _Code().load_sel(1).test_al_imm(0x10).jne(0x3000)
        c.sign_off(0x45F, self.ARRIVED).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                 store_plt=self.STORE)
        assert got[:3] == (1, 0x10, {1: 0x3000})

    def test_jne_on_a_wide_mask_names_nothing(self, md):
        # `test al,3 / jne page` says the field is nonzero: pages 1, 2 and 3 at
        # once. Naming one of them would put two thirds of the signals on the
        # wrong page, which is worse than leaving the message flat.
        c = _Code().load_sel(0).test_al_imm(3).jne(0x3000)
        c.sign_off(0x29A, self.ARRIVED).pad(4)
        assert find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                  store_plt=self.STORE) is None

    def test_the_single_page_is_seeded_with_the_live_selector_byte(self, md):
        # RCU_a001_valveDriverFault opens `shr al,4; and eax,1` on the selector
        # byte itself -- with no seed it modelled as nothing at all.
        c = _Code().load_sel(0).test_al_imm(0x0F).je(0x3000)
        c.sign_off(0x3E6, self.ARRIVED).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                   store_plt=self.STORE)[3]
        assert state[0]["a"].byte == 0

    def test_a_real_two_page_bit_dispatch_still_uses_its_fall_through(self, md):
        # VC_pcsInterface has a page on each side of the bit. Knowing the store
        # PLT must not turn its page 1 into a default.
        c = _Code().test_mem_imm(6, 1).je(0x3000)
        page1 = c.here()
        c.key(0x1234).cvt().call(self.STORE).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                 store_plt=self.STORE)
        assert got[2] == {0: 0x3000, 1: page1}

    def test_a_one_bit_selector_does_not_invent_a_page_at_the_sign_off(self, md):
        # `test al,1 / je page0` with the sign-off falling through: page 1 does
        # not exist, and recording it would hand the sign-off block a region.
        c = _Code().load_sel(0).test_al_imm(1).je(0x3000)
        c.sign_off(0x3A1, self.ARRIVED).pad(4)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000,
                                 store_plt=self.STORE)
        assert got[2] == {0: 0x3000}


class TestPageEntryRegisterState:
    """A page body can open on a byte loaded BEFORE the branch.

    IBST_a181_DIchassisControlDLC decodes as `shr al,4; and eax,1` where `al` is
    the selector byte, still live across the dispatch. extract_stores sweeps
    linearly, so it reaches the far-away page body with no register state and
    drops the signal. Snapshotting must be PER PAGE: taking one snapshot for the
    whole message and reusing it regressed 2020 coverage 86.0% -> 82.4%, because
    the chain mutates registers between one page's branch and the next.
    """

    def test_pristine_load_is_carried_to_each_page(self, md):
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[3]
        assert state[1]["a"].byte == 0 and state[1]["a"].avail == 8
        assert state[2]["a"].byte == 0

    def test_masked_copy_is_excluded(self, md):
        # `and edx,0xf` makes edx the selector, not a payload byte; handing it to
        # a page body as if it were byte 0 would invent a wrong field.
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[3]
        assert "d" not in state[1]

    def test_the_chain_narrowing_a_register_is_followed_not_dropped(self, md):
        """`shr al,4` between the branches refines the load, it does not destroy it.

        Page 2 must not be told `a` is still the whole byte -- that was the
        86.0% -> 82.4% regression -- but it does hold byte 0 shifted right 4,
        and saying so is what lets its first signal decode. DAS_telemetryEvent
        computes its jump table in rdx/rcx expressly to keep this value live,
        and all ten of its pages open by using it.
        """
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).shr_al(4).cmp_dl(2).je(0x3100).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[3]
        assert (state[1]["a"].byte, state[1]["a"].shift) == (0, 0)
        assert (state[2]["a"].byte, state[2]["a"].shift) == (0, 4)

    def test_an_unfollowable_change_still_drops_the_register(self, md):
        # a shift by a register amount is not something the model can carry, so
        # the page must be told nothing rather than something stale
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000)
        c.b += b"\xd2\xe8"                       # shr al, cl
        c.cmp_dl(2).je(0x3100).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[3]
        assert "a" in state[1]
        assert "a" not in state[2]

    def test_a_call_clears_what_preceded_it(self, md):
        # The real preamble stores the index signal first, and storeSignalValue
        # is a call: every payload register is caller-saved, so only the RE-load
        # after it is live. Carrying byte 3 across would be a wrong field.
        c = _Code().load_sel(3)
        c.b += b"\xe8" + (0x3900 - (BASE + len(c.b) + 5)).to_bytes(4, "little",
                                                                  signed=True)
        c.load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(0x3000).cmp_dl(2).je(0x3100).pad(4)
        state = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x4000)[3]
        assert state[1]["a"].byte == 0


class TestJumpTableDispatch:
    def test_mask_bounds_a_table_that_has_no_cmp(self, md):
        # DAS_telemetryRadar (0x60c, 290 signals): the nibble mask makes the range
        # check redundant, so GCC drops the cmp AND hoists the lea above the and.
        # Held back to the indirect jmp, the table still resolves.
        c = _Code().load_sel(0).mov_rdx_rax().lea_rax_rip(0x2500).and_edx(0x1F)
        c.table_jmp().pad(4)
        got = find_dispatch(c.elf(), md, BASE, BASE + 0x1000)
        assert got == (0, 0, 0x20, 0x2500)

    def test_a_rip_lea_alone_is_not_a_table(self, md):
        # A string constant must not be mistaken for a dispatch: with no bound
        # and no indirect jump there is nothing to index.
        c = _Code().load_sel(0).mov_rdx_rax().lea_rax_rip(0x2500).pad(8)
        assert find_dispatch(c.elf(), md, BASE, BASE + 0x1000) is None


TABLE = BASE + 0x800        # somewhere past the code in the synthetic blob


class TestSearchTreeDispatch:
    """A big switch is a TREE of range splits with tables at its leaves.

    VCBATT2_alertLog tests alert 227 on its own, hands everything above it to
    one subtree and everything below 0x3b to another, and tables only the run
    in between. Reading the root table alone left 128 of its 209 signals with
    no page, and those pages then fell to whichever message's mark preceded
    them in the address space.
    """

    def _tree(self):
        # movzx eax, word ptr [rbx] / and ax,0x3ff -- a 10-bit alert id
        c = _Code().load_word(0).and_ax(0x3FF)
        c.cmp_ax(0xE3).je(BASE + 0x240)      # alert 227, its own page
        c.ja(BASE + 0x300)                   # 228..    -> subtree
        c.cmp_ax(0x3A).jbe(BASE + 0x400)     # ..58     -> subtree
        c.add_ax(0x3C5).and_ax(0x3FF)        # rebase by -59
        c.cmp_ax(2).ja(BASE + 0x500)
        c.mov_rdx_rax().lea_rax_rip(TABLE).table_jmp()
        c.put_table(TABLE, [BASE + 0x600, BASE + 0x610, BASE + 0x620])
        return c

    def test_table_at_a_leaf_is_expanded_in_place(self, md):
        got = find_compare_chain(self._tree().elf(), md, BASE, BASE,
                                 BASE + 0x1000)
        assert got[2][59] == BASE + 0x600     # rebased, not 0
        assert got[2][60] == BASE + 0x610
        assert got[2][61] == BASE + 0x620

    def test_a_page_tested_above_the_table_is_kept(self, md):
        got = find_compare_chain(self._tree().elf(), md, BASE, BASE,
                                 BASE + 0x1000)
        assert got[2][227] == BASE + 0x240

    def test_the_selector_is_the_one_that_branched(self, md):
        # the walk carries on into page bodies that mask other fields; the
        # reported selector must be the one the dispatch actually tested
        got = find_compare_chain(self._tree().elf(), md, BASE, BASE,
                                 BASE + 0x1000)
        assert got[0] == 0 and got[1] == 0x3FF

    def test_a_page_above_the_selector_mask_is_refused(self, md):
        # a 10-bit selector cannot choose page 1280; a bogus page is worse than
        # a missing one, because it claims a region belonging to something else
        c = _Code().load_word(0).and_ax(0x3FF)
        c.cmp_ax(1).je(BASE + 0x240).cmp_ax(2).je(BASE + 0x250)
        c.cmp_ax(0x500).je(BASE + 0x260).pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x1000)
        assert set(got[2]) == {1, 2}

    def test_a_split_that_leaves_one_value_names_that_page(self, md):
        """Every <NODE>_alertMatrix reaches page 0 this way.

        `cmp dl,1` / `jb page0` can only be taken when dl is 0, so the target
        IS page 0. Treated as a range to search it yielded nothing, and page 0
        is the biggest page in the message: 287 of 2020's signals had no page
        at all for want of this one branch.
        """
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(1).je(BASE + 0x240)
        c.jb(BASE + 0x250)                    # dl < 1 -> dl == 0
        c.cmp_dl(2).je(BASE + 0x260).pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x1000)
        assert got[2] == {0: BASE + 0x250, 1: BASE + 0x240, 2: BASE + 0x260}

    def test_a_split_leaving_a_range_is_still_searched(self, md):
        # `jb` over more than one value is a subtree, not a page: walk it
        c = _Code().load_sel(0).mov_edx_eax().and_edx(0x0F)
        c.cmp_dl(4).je(BASE + 0x240)
        c.jb(BASE + 0x100)                    # dl < 4: pages 0..3, a subtree
        c.pad(4)
        while c.here() < BASE + 0x100:        # the subtree body
            c.pad(1)
        c.cmp_dl(3).je(BASE + 0x260).pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x1000)
        assert got[2] == {3: BASE + 0x260, 4: BASE + 0x240}

    def test_a_second_register_holding_the_selector(self, md):
        # PARK_pscEnvSlot reads byte 0 into eax and then word 0 into ecx, and
        # goes on to compare `al`: the later load must not steal the selector
        c = _Code().load_sel(0).load_word_ecx(0)
        c.cmp_al(2).je(BASE + 0x240).cmp_al(3).je(BASE + 0x250).pad(8)
        got = find_compare_chain(c.elf(), md, BASE, BASE, BASE + 0x1000)
        assert got[2] == {2: BASE + 0x240, 3: BASE + 0x250}


class TestPageClaims:
    """A message must not claim page bodies belonging to another message.

    BMS_kwhCounter is the last case in address order, so nothing bounds its
    body and find_dispatch scanned 1.2 MB to the end of the function, latching
    onto VCBATT2_alertLog's table. It has TWO catalog signals and claimed nine
    pages; VCBATT2's own stores then landed in a region owned by the wrong
    message and 22 of them were left with no page at all.
    """

    TOP_TABLE = BASE + 0x200
    CASE = BASE + 0x300
    DEFAULT = BASE + 0x400
    MUX_TABLE = BASE + 0x900

    def _two_level(self):
        """A message switch whose one real case carries a 4-page mux switch."""
        c = _Code()
        c.cmp_esi(3).ja(self.DEFAULT)               # 4 message ids
        c.mov_rdx_rax().lea_rax_rip(self.TOP_TABLE).table_jmp()
        c.put_table(self.TOP_TABLE, [self.CASE, self.DEFAULT,
                                     self.DEFAULT, self.DEFAULT])
        c.at(self.CASE)
        c.load_sel(0).mov_rdx_rax().lea_rax_rip(self.MUX_TABLE).and_edx(3)
        c.table_jmp()
        c.at(self.DEFAULT).pad(8)
        c.put_table(self.MUX_TABLE, [BASE + 0x600, BASE + 0x610,
                                     BASE + 0x620, BASE + 0x630])
        return c

    def _pages(self, md, c, sig_counts):
        marks, _, _ = V.build_regions(c.elf(), md, BASE, BASE + len(c.b),
                                      None, sig_counts)
        return {mux for _, _, mux in marks if mux is not None}

    def test_a_plausible_page_count_is_kept(self, md):
        c = self._two_level()
        assert self._pages(md, c, {0: 40}) == {0, 1, 2, 3}

    def test_more_pages_than_signals_is_refused(self, md):
        # message 0 has two signals; four pages cannot be its own
        c = self._two_level()
        assert self._pages(md, c, {0: 2}) == set()

    def test_without_a_catalog_the_guard_stays_out_of_the_way(self, md):
        c = self._two_level()
        assert self._pages(md, c, None) == {0, 1, 2, 3}

    def _one_page(self):
        """A message switch whose one real case has a single-page mux."""
        c = _Code()
        c.cmp_esi(3).ja(self.DEFAULT)
        c.mov_rdx_rax().lea_rax_rip(self.TOP_TABLE).table_jmp()
        c.put_table(self.TOP_TABLE, [self.CASE, self.DEFAULT,
                                     self.DEFAULT, self.DEFAULT])
        c.at(self.CASE)
        c.load_sel(0).test_al_imm(0x0F).je(BASE + 0x600)
        c.sign_off(0x3E6, 0x2100)
        c.at(self.DEFAULT).pad(8)
        c.at(BASE + 0x600).pad(8)
        return c

    def test_a_single_page_mux_is_marked(self, md):
        # A table needs two entries to be a dispatch; a chain that resolved one
        # page only got there over the sign-off block, which is evidence enough.
        marks, _, seeds = V.build_regions(
            self._one_page().elf(), md, BASE, BASE + 0x800, None, {0: 59},
            store_plt=0x2000)
        assert {mux for _, _, mux in marks if mux is not None} == {0}
        assert seeds[BASE + 0x600]["a"].byte == 0

    def test_it_stays_refused_without_the_store_plt(self, md):
        marks, _, _ = V.build_regions(self._one_page().elf(), md, BASE,
                                      BASE + 0x800, None, {0: 59})
        assert {mux for _, _, mux in marks if mux is not None} == set()


class TestPageOwnership:
    """A page a message claims must decode that message's own signals.

    Signal keys are unique, so two messages can never share a store. When
    RCM_collision's compare chain walked out of its own case body and into
    GTW_info's dispatch it resolved four perfectly good pages -- all four of
    them GTW_info's. GTW_info was left flat, and every one of its seven signals
    then overlapped every other.
    """

    TOP_TABLE = BASE + 0x200
    CASE = BASE + 0x300
    DEFAULT = BASE + 0x400
    MUX_TABLE = BASE + 0x900
    PAGES = [BASE + 0x600, BASE + 0x610, BASE + 0x620, BASE + 0x630]
    KEYS = [0xA1, 0xA2, 0xA3, 0xA4]
    STORE = 0x2000

    def test_the_first_store_names_the_owner(self, md):
        c = _Code().key(0xA1).cvt().call(self.STORE).pad(4)
        assert V._page_owner(c.elf(), md, BASE, self.STORE, {0xA1: 5}) == 5

    def test_a_sign_off_before_any_store_names_nobody(self, md):
        # an empty page: neutral, not evidence against the dispatch
        c = _Code().sign_off(0x3E6, 0x2100).pad(4)
        assert V._page_owner(c.elf(), md, BASE, self.STORE, {0xA1: 5}) is None

    def test_a_key_the_catalog_does_not_know_names_nobody(self, md):
        c = _Code().key(0xBB).cvt().call(self.STORE).pad(4)
        assert V._page_owner(c.elf(), md, BASE, self.STORE, {0xA1: 5}) is None

    def _owned(self):
        """A message switch whose one case has a 4-page mux, pages stubbed."""
        c = _Code()
        c.cmp_esi(3).ja(self.DEFAULT)
        c.mov_rdx_rax().lea_rax_rip(self.TOP_TABLE).table_jmp()
        c.put_table(self.TOP_TABLE, [self.CASE, self.DEFAULT,
                                     self.DEFAULT, self.DEFAULT])
        c.at(self.CASE)
        c.load_sel(0).mov_rdx_rax().lea_rax_rip(self.MUX_TABLE).and_edx(3)
        c.table_jmp()
        c.at(self.DEFAULT).pad(8)
        for page, k in zip(self.PAGES, self.KEYS):
            c.at(page).key(k).cvt().call(self.STORE)
        c.put_table(self.MUX_TABLE, self.PAGES)
        return c

    def _pages(self, md, owner):
        marks, _, _ = V.build_regions(
            self._owned().elf(), md, BASE, BASE + 0x1000, None, {0: 40},
            store_plt=self.STORE, key_mid=dict.fromkeys(self.KEYS, owner))
        return {mux for _, _, mux in marks if mux is not None}

    def test_pages_that_store_this_message_are_kept(self, md):
        assert self._pages(md, 0) == {0, 1, 2, 3}

    def test_pages_that_store_another_message_are_refused(self, md):
        # message 0 dispatched, but every page decodes message 7's signals
        assert self._pages(md, 7) == set()

    def test_without_the_catalog_the_guard_stays_out_of_the_way(self, md):
        marks, _, _ = V.build_regions(self._owned().elf(), md, BASE,
                                      BASE + 0x1000, None, {0: 40},
                                      store_plt=self.STORE)
        assert {mux for _, _, mux in marks if mux is not None} == {0, 1, 2, 3}


class TestTableLowBound:
    """The table's low bound is what its first entry stands for.

    Missing it does not lose a page, it MISNUMBERS every page in the message --
    and for an alertLog a page number IS the alert number, so each alert's
    payload silently decodes as a different alert's. 13247 signals were wrong.
    """

    def test_modular_rebase_via_and(self, md):
        # TRCM_alertLog: add ax,0x3f1 / and ax,0x3ff -- subtract 15 mod 1024
        c = _Code().load_word(0).add_ax(0x3F1).and_ax(0x3FF).cmp_ax(0x198)
        c.ja(0x2000).mov_rdx_rax().lea_rax_rip(0x2500).table_jmp().pad(4)
        sel, lo, count, table = find_dispatch(c.elf(), md, BASE, BASE + 0x1000)
        assert (lo, count) == (15, 0x199)

    def test_modular_rebase_via_a_subregister_bound(self, md):
        # EPAS3S_alertLog: add eax,0x6d / cmp al,9 -- the truncation to 8 bits
        # is the modulus; there is no `and` at all
        c = _Code().load_sel(0).add_eax(0x6D).cmp_al(9)
        c.ja(0x2000).mov_rdx_rax().lea_rax_rip(0x2500).table_jmp().pad(4)
        sel, lo, count, table = find_dispatch(c.elf(), md, BASE, BASE + 0x1000)
        assert (lo, count) == (147, 10)

    def test_plain_sub_with_a_hex_immediate(self, md):
        # OCS1P_alertLog: sub eax,0x79. Capstone prints >=10 in hex, so a
        # decimal-only guard silently zeroed the bound.
        c = _Code().load_sel(0).sub_eax(0x79).cmp_al(23)
        c.ja(0x2000).mov_rdx_rax().lea_rax_rip(0x2500).table_jmp().pad(4)
        assert find_dispatch(c.elf(), md, BASE, BASE + 0x1000)[1] == 0x79

    def test_a_full_width_bound_is_not_a_modular_rebase(self, md):
        # `cmp eax,9` does not truncate, so `add eax,0x6d` is ordinary
        # arithmetic -- reading it as a rebase would invent a bound of 2^32-0x6d
        c = _Code().load_sel(0).add_eax(0x6D).cmp_eax(9)
        c.ja(0x2000).mov_rdx_rax().lea_rax_rip(0x2500).table_jmp().pad(4)
        assert find_dispatch(c.elf(), md, BASE, BASE + 0x1000)[1] == 0

    def test_frame_arithmetic_is_not_a_rebase(self, md):
        # `sub rsp,0x28` in a case body must not be read as the table's bound
        c = _Code().load_sel(0).sub_rsp(0x28).cmp_al(9)
        c.ja(0x2000).mov_rdx_rax().lea_rax_rip(0x2500).table_jmp().pad(4)
        assert find_dispatch(c.elf(), md, BASE, BASE + 0x1000)[1] == 0


class TestNWayJoin:
    """A field can be assembled from more than two pieces.

    PMR_bootGitHash takes bytes 1, 2-3 and 4-7. Requiring the low side of an `or`
    to sit at bit 0 only expresses a TWO-way join: at every intermediate step of
    a longer chain BOTH sides carry a shift, so the whole thing was refused.
    That was 74 of the 140 unmodelled 2022 decode sites.
    """

    STORE = 0x3900

    def test_three_pieces_join_into_one_field(self, md):
        # bytes 2-3 at result bit 8, bytes 4-7 at bit 24, byte 1 at bit 0.
        # A 56-bit field does not fit in eax, and GCC knows it: the real
        # PMR_bootGitHash/GTW_bootGitHash shift through RAX.
        c = _Code().key(0x33333333)
        c.load_word(2).shl_rax(8).mov_rdx_rax()          # rdx = bytes2-3 << 8
        c.load_dword(4).shl_rax(0x18)                    # rax = bytes4-7 << 24
        c.or_rdx_rax()                                   # rdx = the top 48 bits
        c.load_sel(1)                                    # rax = byte 1
        c.or_rax_rdx().cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (8, 56)

    def test_a_shift_past_the_register_top_bounds_the_piece(self, md):
        # GTW_nmDebugWakeUp: `shl eax,0x1f` on a BYTE load keeps one bit, not
        # eight, so the field is 32 bits and not 39 -- at 39 it swallowed the
        # three signals sitting above it and cost the whole message.
        c = _Code().key(0x36363636)
        c.load_word(4).shl_eax(0xF).mov_edx_eax()        # bytes 4-5 at bit 15
        c.load_sel(6).shl_eax(0x1F)                      # byte 6: ONE bit left
        c.or_edx_eax()
        c.load_word(2).shr_ax(1).movzx_eax_ax()          # bytes 2-3 >> 1
        c.or_eax_edx().cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (17, 32)

    def test_pieces_that_are_not_adjacent_are_refused(self, md):
        # A gap between the runs means this is not one contiguous field; emit
        # nothing rather than invent a width that spans the hole.
        c = _Code().key(0x44444444)
        c.load_word(2).shl_eax(8).mov_edx_eax()
        c.load_sel(1).shl_eax(0x1F)                      # nowhere near bit 24
        c.or_eax_edx().cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (-1, 0)

    def test_two_way_join_still_works(self, md):
        # DIR_torqueActual shape: low = byte3 >> 3 (5 bits), high = byte4 << 5
        c = _Code().key(0x55555555)
        c.load_sel(4).shl_eax(5).mov_edx_eax()
        c.load_sel(3).shr_al(3)
        c.or_eax_edx().cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (27, 13)


class TestFloatConversionFromMemory:
    """A payload word can reach the double without an integer register at all.

    TCU_log and TCU_alertLog convert straight out of memory::

        cvtsi2sd xmm1, dword ptr [rbx + 4]
        xorpd    xmm0, xmm0
        addsd    xmm0, xmm1              ; a MOVE, written as an add

    Only the register form was followed, so all six of those signals modelled
    as nothing. The zeroed accumulator matters too: read as an unknown constant
    it threw the scale away.
    """

    STORE = 0x3900

    def test_dword_straight_from_the_payload(self, md):
        # TCU_w014_Reason: cvtsi2sd xmm0, dword ptr [rbx+2]
        c = _Code().key(0x152C74E3).cvt_mem(2).call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width, st.signed) == (16, 32, True)

    def test_adding_into_a_zeroed_register_is_a_move(self, md):
        # TCU_mdmSINR: the add contributes no offset and leaves the scale known
        c = _Code().key(0xE58A65BF).cvt_mem(4).xorpd_xmm0().addsd_xmm0_xmm1()
        c.call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (32, 32)
        assert (st.scale_known, st.offset) == (True, 0.0)

    def test_xorps_zeroes_the_same_way(self, md):
        # GCC picks xorps or xorpd by which unit is free; both mean 0.0
        c = _Code().key(0xE58A65BF).cvt_mem(4).xorps_xmm0().addsd_xmm0_xmm1()
        c.call(self.STORE).pad(4)
        assert extract_stores(c.elf(), md, self.STORE)[0].scale_known

    def test_a_real_constant_is_still_an_offset(self, md):
        # adding a rip-relative double is a genuine offset, not a move
        c = _Code().key(0x1234).cvt_mem(0)
        c.b += b"\xf2\x0f\x58\x05" + (0x1800 - (BASE + len(c.b) + 8)).to_bytes(
            4, "little", signed=True)                    # addsd xmm0, [rip+d]
        c.call(self.STORE).pad(4)
        c.put_double(0x1800, -15.5)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert st.offset == -15.5

    def test_a_scale_after_the_move_is_still_read(self, md):
        # the add must not leave the register marked zero, or a later mulsd
        # would be attributed to a value that is no longer there
        c = _Code().key(0x1234).cvt_mem(4).xorpd_xmm0().addsd_xmm0_xmm1()
        c.mulsd_rip(0x1900).call(self.STORE).pad(4)
        c.put_double(0x1900, 0.25)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.scale, st.scale_known) == (0.25, True)


class TestScaleAndOffset:
    """Where the scale actually lives: 4922 of 2026.8.3's signals lost theirs.

    Three shapes, all silently answered with scale 1:

    * the 0.0 GCC adds comes from an integer register -- `xor r12d,r12d` once at
      the top of the function, then `movq xmm6,r12` in every block that needs it;
    * the scale is loaded into its own register and the VALUE multiplied into it
      -- `movsd xmm1,[rip+X]` / `mulsd xmm1,xmm0`;
    * and nothing reset the running scale at a block boundary, so GCC's
      unsigned-64-to-double idiom (`addsd xmm0,xmm0` before a `jmp`) leaked a
      doubling into every following block. GTW_hrlPagesCount came out scaled by
      2^24 and DIR_Vsx by 2^26.

    Tesla's own compact.json settles all three: 2020.8.1 now agrees on 10166 of
    10192 scales exactly, 25 more to float32 precision, and one signal where
    Tesla's two artifacts disagree with each other.
    """

    STORE = 0x3900

    def test_a_zero_moved_in_from_an_integer_register(self, md):
        # UI_latitude: xor r12d,r12d (far above) / movq xmm6,r12 / addsd xmm0,xmm6
        c = _Code().xor_r12d().key(0x5BFED8CD).load_sel(2).cvt()
        c.movq_xmm6_r12().mulsd_rip(0x1900).addsd_xmm0_xmm6()
        c.call(self.STORE).pad(4).put_double(0x1900, 1e-06)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.scale, st.offset, st.scale_known) == (1e-06, 0.0, True)

    def test_that_zero_survives_a_call_because_r12_is_callee_saved(self, md):
        # which is the whole point of parking it there: it is zeroed ONCE for
        # the entire 1.2 MB of crackMessage
        c = _Code().xor_r12d().key(0x1111).load_sel(0).cvt().call(self.STORE)
        c.key(0x2222).load_sel(2).cvt().movq_xmm6_r12().mulsd_rip(0x1900)
        c.addsd_xmm0_xmm6().call(self.STORE).pad(4).put_double(0x1900, 0.25)
        st = extract_stores(c.elf(), md, self.STORE)[1]
        assert (st.scale, st.scale_known) == (0.25, True)

    def test_a_caller_saved_zero_does_not(self, md):
        # rax is clobbered by the call, so whatever it holds afterwards is not
        # known to be zero and the add is a constant we cannot read
        c = _Code().xor_eax().key(0x1111).load_sel(0).cvt().call(self.STORE)
        c.key(0x2222).load_sel(2).cvt().movq_xmm6_rax().mulsd_rip(0x1900)
        c.addsd_xmm0_xmm6().call(self.STORE).pad(4).put_double(0x1900, 0.25)
        assert not extract_stores(c.elf(), md, self.STORE)[1].scale_known

    def test_the_value_multiplied_into_the_scales_register(self, md):
        # DAS_navDistance: movsd xmm1,[rip+X] / mulsd xmm1,xmm0 -- the constant
        # is the DESTINATION, so looking only at the source found nothing
        c = _Code().key(0x928F0F7D).load_sel(5).movsd_xmm1_rip(0x1900).cvt()
        c.mulsd_xmm1_xmm0().xorpd_xmm0().addsd_xmm0_xmm1()
        c.call(self.STORE).pad(4).put_double(0x1900, 0.5)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.scale, st.offset, st.scale_known) == (0.5, 0.0, True)

    def test_a_constant_in_a_register_is_still_an_offset(self, md):
        c = _Code().key(0x1234).load_sel(5).movsd_xmm1_rip(0x1900).cvt()
        c.addsd_xmm0_xmm1().call(self.STORE).pad(4).put_double(0x1900, -15.5)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.offset, st.scale_known) == (-15.5, True)

    def test_a_doubling_before_a_jmp_does_not_scale_the_next_block(self, md):
        # `addsd xmm0,xmm0` / `movq rax,xmm0` / `jmp` is GCC's unsigned 64-bit
        # conversion, and it belongs to the block that jumped away
        c = _Code().load_sel(0).cvt().addsd_xmm0_xmm0().jmp_next()
        c.key(0x2222).load_sel(2).cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert st.scale == 1.0

    def test_a_doubling_inside_the_block_still_counts(self, md):
        # the complement: within one block `addsd xmm0,xmm0` really is x2
        c = _Code().key(0x2222).load_sel(2).cvt().addsd_xmm0_xmm0()
        c.call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert st.scale == 2.0

    def test_a_payload_load_before_a_jmp_is_dropped_too(self, md):
        # nothing falls through a jmp, so the load belongs to the other block
        c = _Code().load_sel(0).jmp_next().key(0x3333).cvt()
        c.call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (-1, 0)


class TestConstantStore:
    """A store the decoder feeds from a constant is a finished answer.

    The generator emits one store per CATALOGUED signal, so a field this
    revision's frame does not carry is still stored -- from 0.0, with no payload
    read at all. DI_alertLog's a080/a081/a082 track voltages are the six in
    2026.8.3. Reporting them as an unmodelled decode said we had failed to
    follow something, when the code plainly has nothing to follow.
    """

    STORE = 0x3900

    def test_a_zeroed_register_is_a_constant(self, md):
        # DI_a082_track1Voltage: xorpd xmm0,xmm0 / mov esi,<key> / call
        c = _Code().xorpd_xmm0().key(0x1234).call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.width, st.constant) == (0, True)

    def test_a_constant_from_the_pool_counts_too(self, md):
        c = _Code().key(0x1234).movsd_xmm0_rip(0x1900).call(self.STORE)
        c.pad(4).put_double(0x1900, 3.0)
        assert extract_stores(c.elf(), md, self.STORE)[0].constant

    def test_a_decode_we_could_not_follow_is_not_a_constant(self, md):
        # a payload value DID reach xmm0; we just could not model its width, and
        # calling that a constant would hide a real gap
        c = _Code().load_dword(0).shr_eax(3).cvt().key(0x1234)
        c.call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.width, st.constant) == (0, False)

    def test_a_signal_that_does_decode_is_neither(self, md):
        c = _Code().load_sel(2).cvt().key(0x1234).call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.width, st.constant) == (8, False)


class TestMergedStoreStub:
    """GCC merges page bodies that decode the same field under different keys.

    UI_ventPanelControlRequest computes its value ONCE, above the branch that
    picks between the two stubs::

        movzx eax, byte ptr [rbx+1]
        cvtsi2sd xmm0, eax
        test  byte ptr [rbx], 1     ; the mux bit
        mulsd xmm0, [rip+X]
        je    left_stub             ; mov esi,<key> / call storeSignalValue
        ...                         ; right_stub, the fall-through

    The linear sweep reaches the stub long after the value has been reset, so
    the signal modelled as nothing. The branch that reached it is the only place
    the value can have come from.
    """

    STORE = 0x3900

    def test_the_stub_inherits_the_value_from_its_branch(self, md):
        c = _Code().load_sel(1).cvt().mulsd_rip(0x1900).test_mem_imm(0, 1)
        c.je(BASE + 0x300)
        c.key(0x1111).call(self.STORE)              # the fall-through stub
        c.at(BASE + 0x300).mov_rdi_rbp().key(0xB01E03F6).call(self.STORE)
        c.pad(4).put_double(0x1900, 0.5)
        by_key = {s.key: s for s in extract_stores(c.elf(), md, self.STORE)}
        st = by_key[0xB01E03F6]
        assert (st.start, st.width, st.scale) == (8, 8, 0.5)

    def test_a_block_that_computes_its_own_value_inherits_nothing(self, md):
        # the branch target loads its own byte, so carrying a stale value there
        # would overwrite a perfectly good one with the wrong field
        c = _Code().load_sel(1).cvt().test_mem_imm(0, 1).je(BASE + 0x300)
        c.key(0x1111).call(self.STORE)
        c.at(BASE + 0x300).load_sel(5).cvt().key(0x2222).call(self.STORE).pad(4)
        by_key = {s.key: s for s in extract_stores(c.elf(), md, self.STORE)}
        assert (by_key[0x2222].start, by_key[0x2222].width) == (40, 8)

    def test_a_branch_to_the_sign_off_carries_nothing(self, md):
        # the case giving up is not a store stub
        c = _Code().load_sel(1).cvt().test_mem_imm(0, 1).je(BASE + 0x300)
        c.key(0x1111).call(self.STORE)
        c.at(BASE + 0x300).sign_off(0x29A, 0x2100).pad(4)
        assert not V._bare_store(c.elf(), md, BASE + 0x300, self.STORE)

    def test_a_stub_with_no_key_of_its_own_is_not_one(self, md):
        c = _Code().mov_rdi_rbp().call(self.STORE).pad(4)
        assert not V._bare_store(c.elf(), md, BASE, self.STORE)

    def test_a_bare_stub_is_recognised(self, md):
        c = _Code().mov_rdi_rbp().key(0xB01E03F6).call(self.STORE).pad(4)
        assert V._bare_store(c.elf(), md, BASE, self.STORE)


class TestShiftPairs:
    """`shl` then `sar` is sign extension, and the register width decides the rule.

    On the FULL register the two shifts cancel and the field is (avail - K) bits
    at the load's base. On a SUB-register the shl first pushes the load's top bits
    out, so it is a net right shift of (M - K) -- reading it as a shift of M put
    the start too high (BMS_cacMinUpdateAhError 39|9 rather than 37|9).
    """

    STORE = 0x3900

    def test_subregister_pair_is_a_net_shift(self, md):
        # movzx eax, word ptr [rbx+4] / shl eax,2 / sar ax,7  -> 37|9 signed
        c = _Code().load_word(4).key(0x508A4DC4).shl_eax(2).sar_ax(7)
        c.cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width, st.signed) == (37, 9, True)

    def test_a_sar_smaller_than_the_shl_is_a_net_left_shift(self, md):
        """DAS_TE_accMinJ: `shl eax,6` / `sar al,3` -> 37|5 signed.

        With M < K the pair does not move the field down at all, it places the
        high half at sub-register bit (K-M) and sign-extends from the top of
        the byte. Read as a right shift of M it kept the stale `shl` and the
        field modelled as nothing.

        The width comes from the ORIGINAL shift: `shl eax,6` leaves only 2 bits
        of the byte inside al, and `sar` moves those 2 back down. Sizing it by
        the net shift gives 8 bits, and compact.json says 5.
        """
        c = _Code().key(0x8962F28C)
        c.load_sel(5).shl_eax(6).sar_al(3).mov_edx_eax()   # 2 bits of byte 5
        c.load_sel(4).shr_al(5)                            # byte 4 bits 5..7
        c.or_eax_edx().movsx_eax_al()
        c.cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width, st.signed) == (37, 5, True)

    def test_full_register_pair_cancels(self, md):
        # movzx eax, word ptr [rbx+0] / shl eax,5 / sar eax,5 -> 0|11 signed,
        # the field at the load's base rather than shifted up by 5.
        c = _Code().load_word(0).key(0x11111111).shl_eax(5).sar_eax(5)
        c.cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width, st.signed) == (0, 11, True)

    def test_subregister_pair_that_keeps_one_bit(self, md):
        # BMS_loadReg_output's high half: byte load, shl 15, sar ax,15 -> 1 bit.
        # Ordering matters: the full-register rule would give max(8-15,0) = 0 and
        # silently shrink the joined field by one bit.
        c = _Code().load_sel(2).key(0x22222222).shl_eax(15).sar_ax(15)
        c.cvt().call(self.STORE).pad(4)
        st = extract_stores(c.elf(), md, self.STORE)[0]
        assert (st.start, st.width) == (16, 1)


class TestBswapIsMotorola:
    """A whole-register byte reversal states the byte order outright.

    ESP_infoApplicationCRC: `mov eax, dword ptr [rbx+4]` / `bswap eax`. All 117
    Motorola signals the extraction got wrong in 2020 were this one instruction
    -- every one had the right WIDTH and only a Motorola start bit missing.
    """

    STORE = 0x3900

    def _stores(self, c, md):
        return extract_stores(c.elf(), md, self.STORE)

    def test_bswapped_dword_is_big_endian_with_an_msb_start(self, md):
        c = _Code().load_dword(4).key(0x4A51C840).bswap_eax().cvt()
        c.call(self.STORE).pad(4)
        st = self._stores(c, md)[0]
        # Motorola start is the MSB: byte 4, bit 7
        assert (st.big, st.start, st.width) == (True, 39, 32)

    def test_msb_start_tracks_the_first_payload_byte(self, md):
        # GTW_uptimeSeconds: dword at byte 1 -> 15|32 @0
        c = _Code().load_dword(1).key(0x28EEF817).bswap_eax().cvt()
        c.call(self.STORE).pad(4)
        st = self._stores(c, md)[0]
        assert (st.big, st.start, st.width) == (True, 15, 32)

    def test_a_swap_of_something_we_did_not_model_whole_is_refused(self, md):
        # Swapping a value we already shifted means the field is not the whole
        # load; emit nothing rather than a start bit we would be guessing at.
        c = _Code().load_dword(4).key(0x4A51C840).shr_eax(4).bswap_eax().cvt()
        c.call(self.STORE).pad(4)
        st = self._stores(c, md)[0]
        assert (st.start, st.width) == (-1, 0)

    def test_without_bswap_the_same_load_stays_intel(self, md):
        c = _Code().load_dword(4).key(0x4A51C840).cvt().call(self.STORE).pad(4)
        st = self._stores(c, md)[0]
        assert (st.big, st.start, st.width) == (False, 32, 32)


class TestNameMultiplexors:
    def test_exact_bit_match_beats_widest_in_the_byte(self):
        # A nibble selector shares byte 0 with another nibble; picking the wider
        # neighbour names the wrong multiplexor and the guard drops the message.
        msgs = {"M": _msg({"index": _sig(0, 4), "other": _sig(4, 4),
                           "page": _sig(8, 8, mux_id=1)}, mid=0x3E)}
        name_multiplexors(msgs, {0x3E: (0, 0x0F)})
        sigs = msgs["M"]["signals"]
        assert sigs["index"].get("is_muxer") and not sigs["other"].get("is_muxer")

    def test_falls_back_to_the_widest_signal_in_the_byte(self):
        msgs = {"M": _msg({"index": _sig(0, 6), "flag": _sig(6, 1),
                           "page": _sig(8, 8, mux_id=1)}, mid=0x3E)}
        name_multiplexors(msgs, {0x3E: (0, None)})
        assert msgs["M"]["signals"]["index"].get("is_muxer")

    def test_a_message_with_no_slots_is_left_alone(self):
        msgs = {"M": _msg({"a": _sig(0, 4)}, mid=0x3E)}
        name_multiplexors(msgs, {0x3E: (0, 0x0F)})
        assert "is_muxer" not in msgs["M"]["signals"]["a"]

    def test_a_one_bit_selector_in_a_high_byte(self):
        # VC_pcsInterfaceMuxIndex: byte 6, mask 1 -> the 1-bit field at bit 48
        msgs = {"M": _msg({"mux": _sig(48, 1), "counter": _sig(50, 4),
                           "page": _sig(0, 13, mux_id=1)}, mid=0x441)}
        name_multiplexors(msgs, {0x441: (6, 1)})
        assert msgs["M"]["signals"]["mux"].get("is_muxer")
        assert not msgs["M"]["signals"]["counter"].get("is_muxer")

    def test_a_mask_that_is_not_low_aligned(self):
        # a selector isolated with `test byte ptr [rbx+2],4` is the one-bit
        # field at bit 2*8+2, not at the bottom of the byte
        msgs = {"M": _msg({"low": _sig(16, 1), "sel": _sig(18, 1),
                           "page": _sig(0, 8, mux_id=1)}, mid=0x3E)}
        name_multiplexors(msgs, {0x3E: (2, 4)})
        assert msgs["M"]["signals"]["sel"].get("is_muxer")
        assert not msgs["M"]["signals"]["low"].get("is_muxer")


def _msg(signals, mid=0x118):
    return {"message_id": mid, "length_bytes": 8, "cycle_time": 10,
            "originNode": "DI", "senders": ["DI"], "signals": signals}


def _sig(start, width, **kw):
    return {"start_position": start, "width": width, "signedness": "UNSIGNED",
            "endianness": "LITTLE", "scale": 1, "offset": 0, **kw}


def _guard(messages):
    """Run the real guard from vapi_layout over `messages`."""
    rep = Report()
    rep.recovered = sum(len(m["signals"]) for m in messages.values())
    return apply_guards(messages, rep), rep


class TestGuards:
    def test_clean_message_survives_untouched(self):
        msgs = {"DI_systemStatus": _msg({"a": _sig(0, 8), "b": _sig(8, 4)})}
        out, rep = _guard(msgs)
        assert len(out["DI_systemStatus"]["signals"]) == 2
        assert rep.gaps == [] and rep.muxed == []

    def test_a_single_bad_width_drops_only_the_over_wide_signal(self):
        # `a` is one bit too wide and runs into `b`. Only `a` is suspect --
        # dropping its victim too is a second loss with no evidence behind it.
        msgs = {"M": _msg({"ok": _sig(0, 8), "a": _sig(8, 9), "b": _sig(16, 8),
                           "c": _sig(32, 8), "d": _sig(40, 8), "e": _sig(48, 8)})}
        out, rep = _guard(msgs)
        assert "M" in out                       # message kept
        assert {g["signal"] for g in rep.gaps} == {"a"}
        assert set(out["M"]["signals"]) == {"ok", "b", "c", "d", "e"}

    def test_the_widest_offender_goes_first(self):
        # GTW_vehNm: one 39-bit field collided with three 1-bit signals, and
        # dropping the conflicted set as a whole cost all four
        msgs = {"M": _msg({"wide": _sig(17, 39), "x": _sig(49, 1),
                           "y": _sig(50, 1), "z": _sig(51, 1),
                           "ok": _sig(0, 8)})}
        out, rep = _guard(msgs)
        assert {g["signal"] for g in rep.gaps} == {"wide"}
        assert set(out["M"]["signals"]) == {"x", "y", "z", "ok"}

    def test_pervasive_overlap_defers_the_whole_message(self):
        msgs = {"M": _msg({n: _sig(0, 8) for n in "abcd"})}
        out, rep = _guard(msgs)
        assert out == {} and len(rep.muxed) == 1

    def test_mux_slots_may_share_bits(self):
        sigs = {"sel": _sig(0, 8, is_muxer=True),
                "x": _sig(16, 8, mux_id=1), "y": _sig(16, 8, mux_id=2)}
        out, rep = _guard({"M": _msg(sigs)})
        assert set(out["M"]["signals"]) == {"sel", "x", "y"}
        assert rep.gaps == []

    def test_mux_without_a_multiplexor_is_deferred(self):
        # cantools cannot express m<id> signals with no multiplexor
        sigs = {"x": _sig(16, 8, mux_id=1), "y": _sig(16, 8, mux_id=2)}
        out, rep = _guard({"M": _msg(sigs)})
        assert out == {}
        assert rep.muxed[0]["reason"] == "no multiplexor found"

    def test_mux_independent_signal_conflicts_with_every_slot(self):
        # a frame carries the independent signal AND one slot, so they must not
        # share bits even though the slots may share with each other. The
        # unpaged signal is the one we know least about, so it is the one to go.
        sigs = {"sel": _sig(0, 8, is_muxer=True),
                "indep": _sig(16, 8),
                "x": _sig(16, 8, mux_id=1)}
        out, rep = _guard({"M": _msg(sigs)})
        assert {g["signal"] for g in rep.gaps} == {"indep"}
        assert set(out["M"]["signals"]) == {"sel", "x"}

    def test_the_multiplexor_outranks_what_it_collides_with(self):
        """SDCR_info, 2026.8.3: SDCR_infoAppCrc landed on the selector's byte.

        The multiplexor is the most corroborated signal in the message -- the
        dispatch we read the pages from branches on exactly those bits -- so it
        wins, and the message survives. Dropping it instead would strand every
        page and cost all 11 signals.
        """
        sigs = {"idx": _sig(0, 8, is_muxer=True),
                "clash": _sig(0, 8, mux_id=13),   # collides with the muxer
                "keep": _sig(16, 8),
                "p1": _sig(32, 8, mux_id=10), "p2": _sig(32, 8, mux_id=11)}
        out, rep = _guard({"SDCR_info": _msg(sigs)})
        assert set(out["SDCR_info"]["signals"]) == {"idx", "keep", "p1", "p2"}
        assert [g["reason"] for g in rep.gaps] == ["overlaps the multiplexor"]

    def test_pruning_a_non_multiplexor_keeps_the_message(self):
        sigs = {"idx": _sig(0, 8, is_muxer=True),
                "a": _sig(16, 9), "b": _sig(24, 8),   # these two collide
                "p1": _sig(32, 8, mux_id=10)}
        out, rep = _guard({"SDCR_info": _msg(sigs)})
        assert set(out["SDCR_info"]["signals"]) == {"idx", "p1"}
        assert rep.muxed == []

    def test_one_unpaged_signal_does_not_condemn_the_message(self):
        """VCFRONT2_alertLog, 2026.8.3: 14 unpaged signals cost all 458.

        A signal with no page is read on EVERY page, so one we failed to
        attribute collides with all of them and trips the pervasive-overlap
        rule. Drop it and keep the pages, which are self-consistent.
        """
        sigs = {"sel": _sig(0, 8, is_muxer=True), "stray": _sig(16, 8)}
        sigs |= {f"p{i}": _sig(16, 8, mux_id=i) for i in range(20)}
        out, rep = _guard({"VCFRONT2_alertLog": _msg(sigs)})
        assert set(out["VCFRONT2_alertLog"]["signals"]) == set(sigs) - {"stray"}
        assert [g["signal"] for g in rep.gaps] == ["stray"]
        assert rep.gaps[0]["reason"] == "page not attributed"

    def test_a_mux_independent_signal_is_kept(self):
        # the counter/checksum case: no page, but it collides with nothing, so
        # absence of a page must NOT be read as a failure to attribute one
        sigs = {"sel": _sig(0, 8, is_muxer=True), "counter": _sig(56, 8)}
        sigs |= {f"p{i}": _sig(16, 8, mux_id=i) for i in range(20)}
        out, rep = _guard({"M": _msg(sigs)})
        assert set(out["M"]["signals"]) == set(sigs)
        assert rep.gaps == [] and rep.muxed == []

    def test_a_genuinely_broken_message_is_still_deferred(self):
        # pages that overlap EACH OTHER are not rescued by any of the above
        sigs = {"sel": _sig(0, 8, is_muxer=True)}
        sigs |= {f"p{i}": _sig(16, 33, mux_id=1) for i in range(12)}
        out, rep = _guard({"M": _msg(sigs)})
        assert out == {}
        assert rep.muxed[0]["reason"] == "unresolved mux branches"


class TestAlertPageOracle:
    """alertLog page numbers are checked against the signal names themselves.

    A misread dispatch bound does not lose pages, it shifts every one of them by
    a constant -- coverage stays perfect while each alert decodes as a different
    alert. 13247 signals were wrong that way and nothing flagged it, so the
    check runs on every extraction.
    """

    def test_matching_pages_are_counted(self):
        msgs = {"VCBATT2_alertLog": _msg(
            {"VCBATT2_a192_hvState": _sig(16, 8, mux_id=192),
             "VCBATT2_a227_ecuReset": _sig(16, 1, mux_id=227)})}
        rep = check_alert_pages(msgs, Report())
        assert (rep.alert_pages_ok, rep.alert_pages_wrong) == (2, [])

    def test_a_shifted_page_is_reported(self):
        msgs = {"VCBATT2_alertLog": _msg(
            {"VCBATT2_a192_hvState": _sig(16, 8, mux_id=133)})}
        rep = check_alert_pages(msgs, Report())
        assert rep.alert_pages_ok == 0
        assert rep.alert_pages_wrong == [
            {"message": "VCBATT2_alertLog", "signal": "VCBATT2_a192_hvState",
             "page": 133, "alert": 192}]

    def test_unpaged_is_counted_apart_from_wrong(self):
        # missing a page is a coverage gap; being on the wrong one is a defect
        msgs = {"VCBATT2_alertLog": _msg({"VCBATT2_a192_hvState": _sig(16, 8)})}
        rep = check_alert_pages(msgs, Report())
        assert (rep.alert_pages_unpaged, rep.alert_pages_wrong) == (1, [])

    def test_only_alertlogs_and_only_named_alerts(self):
        msgs = {"DI_systemStatus": _msg({"DI_a001_x": _sig(0, 8, mux_id=9)}),
                "X_alertLog": _msg({"X_alertID": _sig(0, 8),
                                    "X_alertState": _sig(8, 1, mux_id=3)})}
        rep = check_alert_pages(msgs, Report())
        assert (rep.alert_pages_ok, rep.alert_pages_unpaged,
                rep.alert_pages_wrong) == (0, 0, [])


class TestStore:
    def test_defaults(self):
        s = Store(key=0x9D7BBC55, start=21, width=3, signed=False)
        assert (s.scale, s.offset, s.scale_known, s.addr) == (1.0, 0.0, True, 0)
        assert s.big is False


class TestByteOrderInference:
    """Byte order comes from which payload byte supplies the HIGH half.

    GTW_12V0_DISP (2020, 0x113) is the worked example:
        movzx eax, byte ptr [rbx+3]   ; high half, the LOWER byte -> Motorola
        shl   eax, 6
        mov   edx, eax
        movzx eax, byte ptr [rbx+4]   ; low half
        shr   al, 2
        movzx eax, al
        or    eax, edx
    -> Motorola 31|14, which is what the same-rev DBC says.
    """

    @staticmethod
    def _join(hi, lo):
        """The `or` rule from extract_stores, in isolation."""
        assert hi.shl and not lo.shl and lo.width == hi.shl
        nf = Field(byte=lo.byte, avail=lo.avail, shift=lo.shift,
                   signed=hi.signed, fixed=hi.shl + hi.width,
                   big=hi.byte < lo.byte)
        if nf.big:
            nf.be_start = hi.byte * 8 + hi.shift + hi.width - 1
        return nf

    def test_low_byte_high_half_is_motorola(self):
        hi = Field(byte=3, shl=6)                       # byte 3 << 6
        lo = Field(byte=4, shift=2, narrowed=True)      # byte 4 >> 2, 6 bits
        f = self._join(hi, lo)
        assert f.big and f.width == 14 and f.be_start == 31

    def test_high_byte_high_half_is_intel(self):
        # DIR_torqueActual: high half is byte 4, ABOVE the low half at byte 3
        hi = Field(byte=4, shl=5, signed=True)
        lo = Field(byte=3, shift=3, narrowed=True)      # 5 bits
        f = self._join(hi, lo)
        assert not f.big and (f.start, f.width) == (27, 13) and f.signed

    def test_motorola_start_is_the_msb_of_the_high_byte(self):
        # 12-bit: byte 3 whole << 4, byte 4 top nibble -> MSB at byte3 bit7 = 31
        f = self._join(Field(byte=3, shl=4), Field(byte=4, shift=4, narrowed=True))
        assert (f.be_start, f.width) == (31, 12)

    def test_copy_preserves_byte_order(self):
        # Regression: a `movzx eax, ax` sits between the `or` and the cvtsi2sd,
        # and the register-copy path used to drop big/be_start -- silently
        # turning every recovered Motorola field back into an Intel one.
        f = self._join(Field(byte=3, shl=6), Field(byte=4, shift=2, narrowed=True))
        copy = Field(byte=f.byte, avail=f.avail, shift=f.shift, mask=f.mask,
                     signed=f.signed, narrowed=f.narrowed, shl=f.shl,
                     fixed=f.fixed, big=f.big, be_start=f.be_start, ok=f.ok)
        assert copy.big and copy.be_start == 31
