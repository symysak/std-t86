from __future__ import annotations

import numpy as np

from stdt86.codec.s_codec import conv_encode, viterbi_decode

CONTROL_POLYS = (0o53, 0o75)
CONTROL_K = 6
CRC16_POLY = 0x1021
CRC16_INIT = 0xFFFF


def crc16_ccitt(bits: np.ndarray) -> int:
    reg = CRC16_INIT
    for b in np.asarray(bits, dtype=np.uint8):
        reg ^= int(b) << 15
        reg = ((reg << 1) & 0xFFFF) ^ CRC16_POLY if reg & 0x8000 else (reg << 1) & 0xFFFF
    return reg


def crc16_bits(bits: np.ndarray) -> np.ndarray:
    v = crc16_ccitt(bits)
    return np.array([(v >> (15 - i)) & 1 for i in range(16)], dtype=np.uint8)


def control_conv_encode(bits: np.ndarray) -> np.ndarray:
    return conv_encode(bits, CONTROL_POLYS, CONTROL_K)


def control_viterbi_decode(coded: np.ndarray) -> np.ndarray:
    return viterbi_decode(coded, CONTROL_POLYS, CONTROL_K)
