#!/usr/bin/env python3
"""Reimplementation of the PMR bl/bu device verifyCRC algorithm (crc32_table_compute @ flash 0x86050).

This is the CRC the device computes in its UDS verifyCRC (RC 0x0201) handler and compares against the
HOST-supplied expected value. It is NOT zlib.crc32 — it is a forward-poly (0x04C11DB7), MSB-first,
table-driven CRC-32 that processes the flash region one 16-bit WORD at a time, folding the word's HIGH
byte then LOW byte (big-endian within each 16-bit word).

Table @ flash 0x80010 (256 x uint32, little-endian dwords) dumped from Ghidra (program
pmrbl2024_swapped_0x00082000.bin.0). The first two entries are 0x00000000, 0x04C11DB7 — the textbook
forward-poly table, confirming poly 0x04C11DB7 (NOT zlib's reflected 0xEDB88320).

Decompiled inner loop (per word, param_1 = running 32-bit crc, *param_2 = 16-bit word):
    idx  = (word>>8) ^ (crc>>24)          # fold high byte of word
    crc  = ((crc<<8) ^ table[idx]) & 0xFFFFFFFF
    idx  = ((crc>>24) ^ word) & 0xff      # fold low byte of word
    crc  = ((crc<<8) ^ table[idx]) & 0xFFFFFFFF

init seed and final xor are left configurable (pin empirically against the device); default init=0,
xorout=0 to match the wrapper passing seed 0 with no post-XOR observed.

BYTE ORDER (IMPORTANT): the TMS320C28x is 16-bit-word addressed and the .bhx flash bytes are
BYTE-SWAPPED relative to the loadable image (the `c28x_loadimg.py --mode swap` step). The device CRCs
the words AS THEY SIT IN FLASH (the swapped order). So feed this CRC the SWAPPED image bytes (the same
bytes the device has in flash), packed as 16-bit words. crc32_bytes_be() packs each consecutive byte
pair as one big-endian 16-bit word; make sure your byte stream is already in swapped-flash order and
its pairs line up with the flash word boundaries. Always validate against an UNPATCHED region (expect
device verifyCRC == 0x00) before trusting on patched bytes — that also catches a swap/pairing mistake.
"""

# --- table bytes dumped from Ghidra (flash 0x80010), two halves ---------------
HALF0 = [0,0,0,0,183,29,193,4,110,59,130,9,217,38,67,13,220,118,4,19,107,107,197,23,178,77,134,26,5,80,71,30,184,237,8,38,15,240,201,34,214,214,138,47,97,203,75,43,100,155,12,53,211,134,205,49,10,160,142,60,189,189,79,56,112,219,17,76,199,198,208,72,30,224,147,69,169,253,82,65,172,173,21,95,27,176,212,91,194,150,151,86,117,139,86,82,200,54,25,106,127,43,216,110,166,13,155,99,17,16,90,103,20,64,29,121,163,93,220,125,122,123,159,112,205,102,94,116,224,182,35,152,87,171,226,156,142,141,161,145,57,144,96,149,60,192,39,139,139,221,230,143,82,251,165,130,229,230,100,134,88,91,43,190,239,70,234,186,54,96,169,183,129,125,104,179,132,45,47,173,51,48,238,169,234,22,173,164,93,11,108,160,144,109,50,212,39,112,243,208,254,86,176,221,73,75,113,217,76,27,54,199,251,6,247,195,34,32,180,206,149,61,117,202,40,128,58,242,159,157,251,246,70,187,184,251,241,166,121,255,244,246,62,225,67,235,255,229,154,205,188,232,45,208,125,236,119,112,134,52,192,109,71,48,25,75,4,61,174,86,197,57,171,6,130,39,28,27,67,35,197,61,0,46,114,32,193,42,207,157,142,18,120,128,79,22,161,166,12,27,22,187,205,31,19,235,138,1,164,246,75,5,125,208,8,8,202,205,201,12,7,171,151,120,176,182,86,124,105,144,21,113,222,141,212,117,219,221,147,107,108,192,82,111,181,230,17,98,2,251,208,102,191,70,159,94,8,91,94,90,209,125,29,87,102,96,220,83,99,48,155,77,212,45,90,73,13,11,25,68,186,22,216,64,151,198,165,172,32,219,100,168,249,253,39,165,78,224,230,161,75,176,161,191,252,173,96,187,37,139,35,182,146,150,226,178,47,43,173,138,152,54,108,142,65,16,47,131,246,13,238,135,243,93,169,153,68,64,104,157,157,102,43,144,42,123,234,148,231,29,180,224,80,0,117,228,137,38,54,233,62,59,247,237,59,107,176,243,140,118,113,247,85,80,50,250,226,77,243,254,95,240,188,198,232,237,125,194,49,203,62,207,134,214,255,203,131,134,184,213,52,155,121,209,237,189,58,220,90,160,251,216]
HALF1 = [238,224,12,105,89,253,205,109,128,219,142,96,55,198,79,100,50,150,8,122,133,139,201,126,92,173,138,115,235,176,75,119,86,13,4,79,225,16,197,75,56,54,134,70,143,43,71,66,138,123,0,92,61,102,193,88,228,64,130,85,83,93,67,81,158,59,29,37,41,38,220,33,240,0,159,44,71,29,94,40,66,77,25,54,245,80,216,50,44,118,155,63,155,107,90,59,38,214,21,3,145,203,212,7,72,237,151,10,255,240,86,14,250,160,17,16,77,189,208,20,148,155,147,25,35,134,82,29,14,86,47,241,185,75,238,245,96,109,173,248,215,112,108,252,210,32,43,226,101,61,234,230,188,27,169,235,11,6,104,239,182,187,39,215,1,166,230,211,216,128,165,222,111,157,100,218,106,205,35,196,221,208,226,192,4,246,161,205,179,235,96,201,126,141,62,189,201,144,255,185,16,182,188,180,167,171,125,176,162,251,58,174,21,230,251,170,204,192,184,167,123,221,121,163,198,96,54,155,113,125,247,159,168,91,180,146,31,70,117,150,26,22,50,136,173,11,243,140,116,45,176,129,195,48,113,133,153,144,138,93,46,141,75,89,247,171,8,84,64,182,201,80,69,230,142,78,242,251,79,74,43,221,12,71,156,192,205,67,33,125,130,123,150,96,67,127,79,70,0,114,248,91,193,118,253,11,134,104,74,22,71,108,147,48,4,97,36,45,197,101,233,75,155,17,94,86,90,21,135,112,25,24,48,109,216,28,53,61,159,2,130,32,94,6,91,6,29,11,236,27,220,15,81,166,147,55,230,187,82,51,63,157,17,62,136,128,208,58,141,208,151,36,58,205,86,32,227,235,21,45,84,246,212,41,121,38,169,197,206,59,104,193,23,29,43,204,160,0,234,200,165,80,173,214,18,77,108,210,203,107,47,223,124,118,238,219,193,203,161,227,118,214,96,231,175,240,35,234,24,237,226,238,29,189,165,240,170,160,100,244,115,134,39,249,196,155,230,253,9,253,184,137,190,224,121,141,103,198,58,128,208,219,251,132,213,139,188,154,98,150,125,158,187,176,62,147,12,173,255,151,177,16,176,175,6,13,113,171,223,43,50,166,104,54,243,162,109,102,180,188,218,123,117,184,3,93,54,181,180,64,247,177]


def _bytes_to_table(byts):
    assert len(byts) % 4 == 0
    return [byts[i] | (byts[i+1] << 8) | (byts[i+2] << 16) | (byts[i+3] << 24)
            for i in range(0, len(byts), 4)]


TABLE = _bytes_to_table(HALF0 + HALF1)
assert len(TABLE) == 256, f"table has {len(TABLE)} entries, expected 256"


def _gen_forward_table(poly=0x04C11DB7):
    """Standard MSB-first (non-reflected) CRC-32 byte table for the given poly."""
    tbl = []
    for n in range(256):
        c = n << 24
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFFFFFF if (c & 0x80000000) else (c << 1) & 0xFFFFFFFF
        tbl.append(c)
    return tbl


def crc32_words(words, init=0, xorout=0):
    """Device verifyCRC: process 16-bit words, high byte then low byte, MSB-first poly table."""
    crc = init & 0xFFFFFFFF
    for w in words:
        w &= 0xFFFF
        idx = ((w >> 8) ^ (crc >> 24)) & 0xFF        # fold high byte of the word
        crc = ((crc << 8) & 0xFFFFFFFF) ^ TABLE[idx]
        idx = ((crc >> 24) ^ w) & 0xFF               # fold low byte of the word
        crc = ((crc << 8) & 0xFFFFFFFF) ^ TABLE[idx]
    return (crc ^ xorout) & 0xFFFFFFFF


def crc32_bytes_be(data, init=0, xorout=0):
    """Convenience: treat a flat byte buffer as big-endian 16-bit words (pad odd tail with 0)."""
    if len(data) % 2:
        data = bytes(data) + b"\x00"
    words = [(data[i] << 8) | data[i+1] for i in range(0, len(data), 2)]
    return crc32_words(words, init, xorout)


if __name__ == "__main__":
    # --- self-test 1: dumped table == freshly generated forward-poly table ---
    gen = _gen_forward_table(0x04C11DB7)
    if gen == TABLE:
        print("[OK] dumped table matches generated forward-poly 0x04C11DB7 table (all 256 entries)")
    else:
        mismatches = [(i, hex(TABLE[i]), hex(gen[i])) for i in range(256) if TABLE[i] != gen[i]]
        print(f"[FAIL] table mismatch in {len(mismatches)} entries; first few: {mismatches[:5]}")

    # --- show the first few entries as a sanity check ---
    print("table[0..3] =", [hex(TABLE[i]) for i in range(4)],
          "(expect 0x0, 0x4c11db7, 0x9823b6e, 0xd4326d9)")

    # --- self-test 2: a tiny sample so we have a reference value to compare on-device ---
    sample = bytes([0xAB, 0xCD, 0x12, 0x34, 0x00, 0x00, 0xFF, 0xFF])
    print("sample crc (init=0,xorout=0)        =", hex(crc32_bytes_be(sample)))
    print("sample crc (init=0xFFFFFFFF)         =", hex(crc32_bytes_be(sample, init=0xFFFFFFFF)))
    print("sample crc (init=0xFFFFFFFF,xorout=~)=",
          hex(crc32_bytes_be(sample, init=0xFFFFFFFF, xorout=0xFFFFFFFF)))
    print()
    print("NOTE: init/xorout still need pinning against the device — compute the CRC of an UNPATCHED")
    print("region and confirm the device's verifyCRC returns 0x00 for it before trusting on patched bytes.")
