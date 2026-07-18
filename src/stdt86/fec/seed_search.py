from __future__ import annotations

import numpy as np

from stdt86.fec.convolutional import CONTROL_K, CONTROL_POLYS
from stdt86.fec.scrambler import lfsr_pn

N_SEEDS = 512
CAC_LEN = 256


def _taps(poly: int, K: int) -> np.ndarray:
    return np.array([(poly >> k) & 1 for k in range(K)], dtype=np.uint8)


_G1 = _taps(CONTROL_POLYS[0], CONTROL_K)
_G2 = _taps(CONTROL_POLYS[1], CONTROL_K)


def syndrome(cac_bits: np.ndarray) -> np.ndarray:
    cac_bits = np.asarray(cac_bits, dtype=np.uint8)
    v1, v2 = cac_bits[0::2], cac_bits[1::2]
    s = np.convolve(v1.astype(np.int64), _G2.astype(np.int64)) \
        + np.convolve(v2.astype(np.int64), _G1.astype(np.int64))
    return (s % 2).astype(np.uint8)


def _pn_syndromes() -> np.ndarray:
    global _PN_SYN
    try:
        return _PN_SYN
    except NameError:
        _PN_SYN = np.array([syndrome(lfsr_pn(s, CAC_LEN)) for s in range(N_SEEDS)])
        return _PN_SYN


class SeedSearcher:

    def __init__(self, top: int = 8) -> None:
        self.top = top
        self.weights = np.zeros(N_SEEDS, dtype=np.int64)
        self.n_slots = 0

    def push(self, cac_bits: np.ndarray) -> None:
        syn = syndrome(cac_bits)
        self.weights += np.bitwise_xor(
            _pn_syndromes(), syn[None, :]).sum(axis=1).astype(np.int64)
        self.n_slots += 1

    def candidates(self) -> list[int]:
        return [int(s) for s in np.argsort(self.weights)[: self.top]]

    def ranking(self, top: int = 5) -> list[tuple[int, int]]:
        order = np.argsort(self.weights)[:top]
        return [(int(s), int(self.weights[s])) for s in order]


def search_seed(cac_list: list[np.ndarray]) -> tuple[int, np.ndarray]:
    weights = np.zeros(N_SEEDS, dtype=np.int64)
    for cac in cac_list:
        weights += np.bitwise_xor(
            _pn_syndromes(), syndrome(cac)[None, :]).sum(axis=1).astype(np.int64)
    return int(np.argmin(weights)), weights


__all__ = ["SeedSearcher", "search_seed", "syndrome"]
