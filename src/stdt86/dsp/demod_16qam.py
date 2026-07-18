from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from stdt86.dsp.pulse import rrc_taps
from stdt86.dsp.qam import evm_percent, slice_symbols


@dataclass
class DemodResult:

    symbols: np.ndarray
    decisions: np.ndarray
    evm: float
    timing_offset: float
    sps: int
    meta: dict = field(default_factory=dict)


def matched_filter(samples: np.ndarray, beta: float, sps: int, span: int = 10) -> np.ndarray:
    taps = rrc_taps(beta, sps, span).astype(np.float64)
    delay = (len(taps) - 1) // 2
    filt = np.convolve(samples, taps, mode="full")
    return filt[delay : delay + len(samples)].astype(np.complex64)


def oerder_meyr_timing(samples: np.ndarray, sps: int) -> float:
    n = len(samples)
    k = np.arange(n)
    x2 = np.abs(samples) ** 2
    spectral = np.sum(x2 * np.exp(-2j * np.pi * k / sps))
    tau = -np.angle(spectral) / (2.0 * np.pi)
    return float((tau % 1.0) * sps)


def _interp_symbols(samples: np.ndarray, sps: int, offset: float) -> np.ndarray:
    from scipy.interpolate import CubicSpline

    n = len(samples)
    positions = np.arange(offset, n - 1, sps)
    grid = np.arange(n)
    re = CubicSpline(grid, samples.real)(positions)
    im = CubicSpline(grid, samples.imag)(positions)
    return (re + 1j * im).astype(np.complex64)


def _agc(syms: np.ndarray) -> np.ndarray:
    power = np.mean(np.abs(syms) ** 2)
    if power <= 0:
        return syms
    return (syms / np.sqrt(power)).astype(np.complex64)


def dd_carrier_recovery(
    syms: np.ndarray,
    loop_bw: float = 0.01,
    damping: float = 1.0,
) -> np.ndarray:
    theta = 2.0 * np.pi * loop_bw
    denom = 1.0 + 2.0 * damping * theta + theta**2
    g1 = (4.0 * damping * theta) / denom
    g2 = (4.0 * theta**2) / denom

    out = np.empty_like(syms)
    phase = 0.0
    freq = 0.0
    for i, s in enumerate(syms):
        y = s * np.exp(-1j * phase)
        out[i] = y
        d = slice_symbols(np.array([y]))[0]
        err = float(np.angle(y * np.conj(d)))
        freq += g2 * err
        phase += freq + g1 * err
    return out


def demodulate_16qam(
    baseband: np.ndarray,
    sps: int = 4,
    beta: float = 0.35,
    span: int = 10,
    loop_bw: float = 0.01,
    trim: int = 20,
) -> DemodResult:
    if sps < 2:
        raise ValueError("タイミング推定には sps>=2 が必要です。")

    mf = matched_filter(baseband, beta, sps, span)
    offset = oerder_meyr_timing(mf, sps)
    sym_raw = _interp_symbols(mf, sps, offset)
    if trim > 0 and len(sym_raw) > 2 * trim:
        sym_raw = sym_raw[trim:-trim]
    sym_agc = _agc(sym_raw)
    sym_carrier = dd_carrier_recovery(sym_agc, loop_bw=loop_bw)
    if len(sym_carrier) > 2 * trim:
        sym_carrier = sym_carrier[trim:]
    decisions = slice_symbols(sym_carrier)
    evm = evm_percent(sym_carrier, decisions)
    return DemodResult(
        symbols=sym_carrier,
        decisions=decisions,
        evm=evm,
        timing_offset=offset,
        sps=sps,
        meta={"beta": beta, "span": span, "loop_bw": loop_bw},
    )
