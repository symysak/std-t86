from __future__ import annotations

import numpy as np

PN_TAPS = (0, 4)


def municipal_code_to_seed(code: int) -> int:
    seed = code & 0x1FF
    if seed == 0:
        raise ValueError("シードが 0 になりました（有効範囲 1..511）。")
    return seed


def city_name(code: int) -> str | None:
    from stdt86.data.city_codes import CITY_CODES

    return CITY_CODES.get(code)


def seed_for_city(name: str) -> dict[str, int]:
    from stdt86.data.city_codes import CITY_CODES

    return {v: municipal_code_to_seed(c) for c, v in CITY_CODES.items() if name in v}


def lfsr_pn(seed: int, length: int) -> np.ndarray:
    st = [(seed >> (8 - i)) & 1 for i in range(9)]
    out = np.empty(length, dtype=np.uint8)
    t0, t1 = 8 - PN_TAPS[0], 8 - PN_TAPS[1]
    for k in range(length):
        out[k] = st[8]
        fb = st[t0] ^ st[t1]
        st = [fb, *st[:8]]
    return out


def descramble(bits: np.ndarray, seed: int) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.uint8)
    return bits ^ lfsr_pn(seed, len(bits))


scramble = descramble
