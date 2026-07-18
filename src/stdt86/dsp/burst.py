from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from stdt86.dsp.demod_16qam import (
    _agc,
    _interp_symbols,
    matched_filter,
    oerder_meyr_timing,
)
from stdt86.dsp.qam import evm_percent

SYMBOL_RATE = 11_250.0
SYMBOLS_PER_SLOT = 150
BITS_PER_SLOT = 600


@dataclass
class BurstResult:

    symbols: np.ndarray
    evm: float
    cfo: float
    start: int
    length_ms: float
    meta: dict = field(default_factory=dict)


def detect_bursts(
    baseband: np.ndarray,
    fs: float,
    thresh_db: float = 8.0,
    min_ms: float = 10.0,
    max_ms: float = 75.0,
    smooth_ms: float = 0.25,
) -> list[tuple[int, int]]:
    env = np.abs(baseband) ** 2
    k = max(1, int(smooth_ms * 1e-3 * fs))
    env = signal.lfilter(np.ones(k) / k, 1.0, env)
    floor = np.percentile(env, 5)
    on = env > floor * 10.0 ** (thresh_db / 10.0)
    edges = np.flatnonzero(np.diff(on.astype(int)))
    regions = []
    for a, b in zip(edges[::2], edges[1::2], strict=False):
        ms = (b - a) / fs * 1e3
        if min_ms <= ms <= max_ms:
            regions.append((int(a), int(b)))
    return regions


def demod_burst(
    segment: np.ndarray,
    sps: int = 4,
    beta: float = 0.5,
    max_cfo: float = 800.0,
    track_win: int = 24,
    symbol_rate: float = SYMBOL_RATE,
) -> BurstResult:
    mf = matched_filter(segment, beta, sps)
    off = oerder_meyr_timing(mf, sps)
    sym = _agc(_interp_symbols(mf, sps, off)[2:-2])

    nfft = 1 << 14
    s4_spec = np.fft.fftshift(np.fft.fft(sym**4, nfft))
    f4 = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / symbol_rate))
    mask = np.abs(f4) < 4.0 * max_cfo
    cfo = float(f4[mask][np.argmax(np.abs(s4_spec[mask]))] / 4.0)
    k = np.arange(len(sym))
    sym = sym * np.exp(-2j * np.pi * cfo / symbol_rate * k)

    win = min(track_win, max(4, len(sym) // 4))
    m4 = signal.lfilter(np.ones(win) / win, 1.0, sym**4)
    m4 = np.roll(m4, -win // 2)
    phase = (np.unwrap(np.angle(m4)) - np.pi) / 4.0
    sym = (sym * np.exp(-1j * phase)).astype(np.complex64)

    core = sym[10:-4] if len(sym) > 20 else sym
    best = (evm_percent(core), 0.0)
    for theta in np.deg2rad(np.arange(-45.0, 45.5, 1.0)):
        e = evm_percent(core * np.exp(1j * theta))
        if e < best[0]:
            best = (e, theta)
    sym = (sym * np.exp(1j * best[1])).astype(np.complex64)

    evm = best[0]
    return BurstResult(
        symbols=sym,
        evm=evm,
        cfo=cfo,
        start=0,
        length_ms=len(segment) / (symbol_rate * sps) * 1e3,
        meta={"beta": beta, "timing_offset": off},
    )


def demod_bursts(
    baseband: np.ndarray,
    fs: float,
    sps: int = 4,
    beta: float = 0.5,
    max_bursts: int | None = None,
    guard: int = 8,
) -> list[BurstResult]:
    regions = detect_bursts(baseband, fs)
    if max_bursts is not None:
        regions = regions[:max_bursts]
    results = []
    for a, b in regions:
        seg = baseband[a + guard : b - guard]
        if len(seg) < sps * 40:
            continue
        r = demod_burst(seg, sps=sps, beta=beta)
        r.start = a
        results.append(r)
    return results
