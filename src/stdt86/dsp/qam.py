from __future__ import annotations

import numpy as np

NORM = 1.0 / np.sqrt(10.0)
LEVELS = np.array([-3.0, -1.0, 1.0, 3.0]) * NORM

_PAIR_TO_LEVEL = np.array([3.0, 1.0, -3.0, -1.0]) * NORM
_LEVELIDX_TO_PAIR = {0: (1, 0), 1: (1, 1), 2: (0, 1), 3: (0, 0)}


def bits_to_symbols(bits: np.ndarray) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.int8).ravel()
    if bits.size % 4 != 0:
        raise ValueError("16QAM は 4 bit/シンボルなので長さは 4 の倍数が必要です。")
    quads = bits.reshape(-1, 4)
    i = _PAIR_TO_LEVEL[quads[:, 0] * 2 + quads[:, 1]]
    q = _PAIR_TO_LEVEL[quads[:, 2] * 2 + quads[:, 3]]
    return (i + 1j * q).astype(np.complex64)


def symbols_to_bits(syms: np.ndarray) -> np.ndarray:
    ii = np.argmin(np.abs(syms.real[:, None] - LEVELS[None, :]), axis=1)
    qq = np.argmin(np.abs(syms.imag[:, None] - LEVELS[None, :]), axis=1)
    out = np.empty((len(syms), 4), dtype=np.uint8)
    for k in range(4):
        pi, pj = _LEVELIDX_TO_PAIR[k]
        out[ii == k, 0] = pi
        out[ii == k, 1] = pj
        out[qq == k, 2] = pi
        out[qq == k, 3] = pj
    return out.ravel()


def symbols_to_bits_threshold(
    syms: np.ndarray, thresholds: tuple[float, float] | None = None
) -> np.ndarray:
    r, q = syms.real, syms.imag
    if thresholds is None:
        tr = float(np.mean(np.abs(r)))
        tq = float(np.mean(np.abs(q)))
    else:
        tr, tq = thresholds
    out = np.empty(len(syms) * 4, dtype=np.uint8)
    out[0::4] = r < 0
    out[1::4] = np.abs(r) < tr
    out[2::4] = q < 0
    out[3::4] = np.abs(q) < tq
    return out


def slice_symbols(syms: np.ndarray) -> np.ndarray:
    i = LEVELS[np.argmin(np.abs(syms.real[:, None] - LEVELS[None, :]), axis=1)]
    q = LEVELS[np.argmin(np.abs(syms.imag[:, None] - LEVELS[None, :]), axis=1)]
    return (i + 1j * q).astype(np.complex64)


def evm_percent(syms: np.ndarray, decisions: np.ndarray | None = None) -> float:
    if decisions is None:
        decisions = slice_symbols(syms)
    err = np.mean(np.abs(syms - decisions) ** 2)
    ref = np.mean(np.abs(decisions) ** 2)
    return float(np.sqrt(err / (ref + 1e-20)) * 100.0)
