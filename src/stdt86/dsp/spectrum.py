from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy import signal


def welch_psd(
    samples: np.ndarray,
    fs: float,
    nperseg: int = 4096,
) -> tuple[np.ndarray, np.ndarray]:
    nperseg = min(nperseg, len(samples))
    freqs, psd = signal.welch(
        samples,
        fs=fs,
        nperseg=nperseg,
        return_onesided=False,
        detrend=False,
    )
    freqs = np.fft.fftshift(freqs)
    psd = np.fft.fftshift(psd)
    psd_db = 10.0 * np.log10(psd + 1e-20)
    return freqs, psd_db


def estimate_channel_offset(
    samples: np.ndarray,
    fs: float,
    bandwidth: float = 15_000.0,
    nperseg: int = 4096,
    max_offset: float | None = None,
) -> float:
    freqs, psd_db = welch_psd(samples, fs, nperseg=nperseg)
    psd_lin = 10.0 ** (psd_db / 10.0)
    if max_offset is not None:
        keep = np.abs(freqs) <= max_offset
        freqs, psd_lin = freqs[keep], psd_lin[keep]
    df = float(freqs[1] - freqs[0])
    half = max(1, int(round((bandwidth / 2.0) / df)))
    kernel = np.ones(2 * half + 1)
    energy = np.convolve(psd_lin, kernel, mode="same")
    center = int(np.argmax(energy))
    lo = max(0, center - half)
    hi = min(len(freqs), center + half + 1)
    w = psd_lin[lo:hi]
    return float(np.sum(w * freqs[lo:hi]) / (np.sum(w) + 1e-20))


def plot_spectrum(
    samples: np.ndarray,
    fs: float,
    out_path: str | Path,
    title: str = "Spectrum",
    bandwidth: float = 15_000.0,
) -> float:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    offset = estimate_channel_offset(samples, fs, bandwidth=bandwidth)
    freqs, psd_db = welch_psd(samples, fs)

    fig, (ax_psd, ax_spec) = plt.subplots(2, 1, figsize=(9, 7))

    ax_psd.plot(freqs / 1e3, psd_db, lw=0.8)
    ax_psd.axvspan(
        (offset - bandwidth / 2) / 1e3,
        (offset + bandwidth / 2) / 1e3,
        color="tab:orange",
        alpha=0.25,
        label=f"channel @ {offset / 1e3:.1f} kHz",
    )
    ax_psd.set_xlabel("Frequency [kHz]")
    ax_psd.set_ylabel("PSD [dB/Hz]")
    ax_psd.set_title(f"{title} — Welch PSD")
    ax_psd.legend(loc="upper right")
    ax_psd.grid(True, alpha=0.3)

    nperseg = min(1024, len(samples))
    f_spec, t_spec, sxx = signal.spectrogram(
        samples, fs=fs, nperseg=nperseg, return_onesided=False, detrend=False
    )
    f_spec = np.fft.fftshift(f_spec)
    sxx = np.fft.fftshift(sxx, axes=0)
    ax_spec.pcolormesh(
        t_spec, f_spec / 1e3, 10 * np.log10(sxx + 1e-20), shading="auto"
    )
    ax_spec.set_xlabel("Time [s]")
    ax_spec.set_ylabel("Frequency [kHz]")
    ax_spec.set_title("Spectrogram")

    fig.tight_layout()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return offset
