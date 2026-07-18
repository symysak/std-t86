from __future__ import annotations

import numpy as np
from scipy import signal

from stdt86.dsp.burst import SYMBOL_RATE
from stdt86.dsp.pulse import rrc_taps

SPS = 8
FS_BB = SYMBOL_RATE * SPS
CHANNEL_BW = 14_000.0

_MIN_INTERMEDIATE_FS = 100_000.0

_TRACK_BLOCK_S = 0.16
_TRACK_MIN_PROMINENCE = 8.0


class _NCO:

    def __init__(self, freq_hz: float, fs: float) -> None:
        self.fs = fs
        self.freq_hz = freq_hz
        self._phase = 0.0

    def mix(self, x: np.ndarray) -> np.ndarray:
        n = np.arange(len(x), dtype=np.float64)
        ph = self._phase + self.freq_hz / self.fs * n
        out = (x * np.exp(-2j * np.pi * ph)).astype(np.complex64)
        self._phase = float((self._phase + self.freq_hz / self.fs * len(x)) % 1.0)
        return out

    def retune(self, freq_hz: float) -> None:
        self.freq_hz = freq_hz


class _FirState:

    def __init__(self, taps: np.ndarray) -> None:
        self.taps = np.asarray(taps, dtype=np.float64)
        self._zi = np.zeros(len(self.taps) - 1, dtype=np.complex128)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = signal.lfilter(self.taps, 1.0, x, zi=self._zi)
        return y.astype(np.complex64)


class _Decimator:

    def __init__(self, fs: float, factor: int) -> None:
        self.factor = factor
        if factor > 1:
            nyq_out = fs / factor / 2.0
            cutoff = min(0.8 * nyq_out, nyq_out - 1000.0)
            numtaps = int(max(31, 6.0 * fs / max(nyq_out, 1.0))) | 1
            self._fir = _FirState(signal.firwin(numtaps, cutoff / (fs / 2.0)))
        else:
            self._fir = None
        self._phase = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.factor == 1:
            return x
        y = self._fir.process(x)
        out = y[self._phase :: self.factor]
        consumed = len(y) - self._phase
        self._phase = (-consumed) % self.factor
        return out


class _CubicResampler:

    def __init__(self, fs_in: float, fs_out: float) -> None:
        self.step = fs_in / fs_out
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_abs = 0
        self._out_count = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        self._buf = np.concatenate([self._buf, x])
        end_abs = self._buf_abs + len(self._buf)
        k0 = self._out_count
        t0 = k0 * self.step
        if t0 < 1.0:
            k0 = int(np.ceil(1.0 / self.step))
        k_max = int(np.floor((end_abs - 3) / self.step))
        if k_max < k0:
            return np.zeros(0, dtype=np.complex64)
        k = np.arange(k0, k_max + 1, dtype=np.float64)
        t = k * self.step - self._buf_abs
        i = np.floor(t).astype(np.int64)
        frac = t - i
        p0 = self._buf[i - 1]
        p1 = self._buf[i]
        p2 = self._buf[i + 1]
        p3 = self._buf[i + 2]
        f2 = frac * frac
        f3 = f2 * frac
        out = 0.5 * (
            2.0 * p1
            + (-p0 + p2) * frac
            + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * f2
            + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * f3
        )
        self._out_count = k_max + 1
        keep_from = int(np.floor((k_max + 1) * self.step)) - 1
        keep_local = max(0, keep_from - self._buf_abs)
        self._buf = self._buf[keep_local:]
        self._buf_abs += keep_local
        return out.astype(np.complex64)


def _cfo_4th(mf: np.ndarray, fs: float, search_hz: float) -> tuple[float, float]:
    nfft = 1 << max(14, int(np.ceil(np.log2(len(mf)))))
    spec = np.abs(np.fft.fftshift(np.fft.fft(mf.astype(np.complex128) ** 4, nfft)))
    fax = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    band = spec[np.abs(fax) < search_hz]
    fband = fax[np.abs(fax) < search_hz]
    peak = int(np.argmax(band))
    prominence = float(band[peak] / (np.median(band) + 1e-12))
    return float(fband[peak] / 4.0), prominence


def estimate_cfo_4th(mf: np.ndarray, fs: float, search_hz: float = 2000.0) -> float:
    return _cfo_4th(mf, fs, search_hz)[0]


class _CfoCorrector:

    def __init__(self, fs: float, acq_len: int, block: int,
                 kp: float = 0.5, ki: float = 0.3, max_step_hz: float = 200.0,
                 track_search_hz: float = 1200.0) -> None:
        self.fs = fs
        self.acq_len = acq_len
        self.block = block
        self.kp = kp
        self.ki = ki
        self.max_step_hz = max_step_hz
        self.track_search_hz = track_search_hz
        self.cfo_hz: float | None = None
        self._vel = 0.0
        self._nco = _NCO(0.0, fs)
        self._acq: list[np.ndarray] = []
        self._acq_n = 0
        self._blk: list[np.ndarray] = []
        self._blk_n = 0

    def reset(self) -> None:
        self.cfo_hz = None
        self._vel = 0.0
        self._nco = _NCO(0.0, self.fs)
        self._acq = []
        self._acq_n = 0
        self._blk = []
        self._blk_n = 0

    def _emit_tracked(self, x: np.ndarray) -> np.ndarray:
        out: list[np.ndarray] = []
        while len(x):
            take = min(len(x), self.block - self._blk_n)
            piece = self._nco.mix(x[:take])
            out.append(piece)
            self._blk.append(piece)
            self._blk_n += take
            x = x[take:]
            if self._blk_n >= self.block:
                blk = np.concatenate(self._blk)
                resid, prom = _cfo_4th(blk, self.fs, self.track_search_hz)
                if prom >= _TRACK_MIN_PROMINENCE:
                    self._vel = float(np.clip(self._vel + self.ki * resid,
                                              -self.max_step_hz, self.max_step_hz))
                    step = float(np.clip(self.kp * resid + self._vel,
                                         -self.max_step_hz, self.max_step_hz))
                    self.cfo_hz += step
                    self._nco.retune(self.cfo_hz)
                self._blk = []
                self._blk_n = 0
        return np.concatenate(out) if out else np.zeros(0, dtype=np.complex64)

    def process(self, mf: np.ndarray) -> np.ndarray:
        if self.cfo_hz is not None:
            return self._emit_tracked(mf)
        self._acq.append(mf)
        self._acq_n += len(mf)
        if self._acq_n < self.acq_len:
            return np.zeros(0, dtype=np.complex64)
        pending = np.concatenate(self._acq)
        self._acq = []
        self.cfo_hz = estimate_cfo_4th(pending[: self.acq_len], self.fs,
                                       search_hz=8000.0)
        self._nco.retune(self.cfo_hz)
        return self._emit_tracked(pending)


class StreamFrontEnd:

    def __init__(self, fs: float, f0: float, acquire_seconds: float = 1.0) -> None:
        self.fs = float(fs)
        self.f0 = float(f0)
        self._nco = _NCO(f0, fs)
        decim = max(1, int(self.fs // _MIN_INTERMEDIATE_FS))
        self._decim = _Decimator(self.fs, decim)
        fs1 = self.fs / decim
        cutoff = CHANNEL_BW / 2.0
        numtaps = int(max(129, 8.0 * fs1 / cutoff)) | 1
        self._chan = _FirState(signal.firwin(numtaps, cutoff / (fs1 / 2.0)))
        self._resamp = _CubicResampler(fs1, FS_BB)
        self._rrc = _FirState(rrc_taps(0.5, SPS, span=10))
        self._cfo = _CfoCorrector(FS_BB, acq_len=int(acquire_seconds * FS_BB),
                                  block=int(_TRACK_BLOCK_S * FS_BB))


    def _to_mf(self, chunk: np.ndarray) -> np.ndarray:
        x = self._nco.mix(np.asarray(chunk, dtype=np.complex64))
        x = self._decim.process(x)
        x = self._chan.process(x)
        x = self._resamp.process(x)
        return self._rrc.process(x)


    @property
    def cfo_hz(self) -> float | None:
        return self._cfo.cfo_hz

    def reacquire_cfo(self) -> None:
        self._cfo.reset()

    def process(self, chunk: np.ndarray) -> np.ndarray:
        return self._cfo.process(self._to_mf(chunk))


__all__ = ["FS_BB", "SPS", "StreamFrontEnd", "estimate_cfo_4th"]
