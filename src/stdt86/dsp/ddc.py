from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy import signal


def downconvert(samples: np.ndarray, fs: float, f0: float) -> np.ndarray:
    n = np.arange(len(samples), dtype=np.float64)
    nco = np.exp(-2j * np.pi * f0 / fs * n).astype(np.complex64)
    return (samples * nco).astype(np.complex64)


def channel_filter(samples: np.ndarray, fs: float, channel_bw: float) -> np.ndarray:
    nyq = fs / 2.0
    cutoff = channel_bw / 2.0
    if cutoff >= nyq:
        return samples
    numtaps = int(max(129, 8.0 * fs / cutoff)) | 1
    numtaps = min(numtaps, max(129, len(samples) // 4 | 1))
    taps = signal.firwin(numtaps, cutoff / nyq)
    return signal.filtfilt(taps, 1.0, samples).astype(np.complex64)


def resample_to_sps(
    samples: np.ndarray,
    fs: float,
    symbol_rate: float,
    sps: int = 4,
    channel_bw: float | None = None,
    max_denominator: int = 1000,
) -> tuple[np.ndarray, float]:
    target_fs = symbol_rate * sps
    ratio = Fraction(target_fs / fs).limit_denominator(max_denominator)
    up, down = ratio.numerator, ratio.denominator
    if up == 0:
        raise ValueError("リサンプル比が 0 になりました。symbol_rate/sps/fs を確認してください。")

    filtered = samples
    if channel_bw is not None:
        filtered = channel_filter(samples, fs, channel_bw)

    out = signal.resample_poly(filtered, up, down)
    fs_out = fs * up / down
    return out.astype(np.complex64), float(fs_out)
