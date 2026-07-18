from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from stdt86.control import channel as ch
from stdt86.dsp.burst import SYMBOL_RATE, SYMBOLS_PER_SLOT
from stdt86.dsp.ddc import channel_filter, downconvert, resample_to_sps
from stdt86.dsp.pulse import rrc_taps
from stdt86.dsp.qam import (
    bits_to_symbols,
    evm_percent,
    symbols_to_bits,
    symbols_to_bits_threshold,
)
from stdt86.dsp.spectrum import welch_psd

SPS = 8
SW_SYM = 69
CHANNEL_BW = 14_000.0

_SW_HEX = {"S1": "000a0a00a0", "S3": "00a000aaaa",
           "S5": "00a0aaaaa0", "S6": "0000aa0a0a"}


def _hexbits(h: str) -> np.ndarray:
    n = len(h) * 4
    v = int(h, 16)
    return np.array([(v >> (n - 1 - i)) & 1 for i in range(n)], dtype=np.uint8)


SW_TEMPLATES = {k: bits_to_symbols(_hexbits(v)) for k, v in _SW_HEX.items()}


def find_channel(iq: np.ndarray, fs: float, search_bw: float = 6_000.0) -> float:
    f, pxx = welch_psd(iq, fs, nperseg=min(65536, len(iq)))
    p = 10.0 ** (pxx / 10.0)
    df = f[1] - f[0]
    win = max(1, int(search_bw / df))
    kernel = np.ones(win)
    band = np.convolve(p, kernel, mode="same")
    return float(f[int(np.argmax(band))])


def _front_end(iq: np.ndarray, fs: float, f0: float) -> np.ndarray:
    sh = downconvert(iq, fs, f0)
    fr, psd = welch_psd(sh, fs, nperseg=min(32768, len(sh)))
    keep = np.abs(fr) < 7000
    p = 10.0 ** (psd[keep] / 10.0)
    df = float(np.sum(p * fr[keep]) / (np.sum(p) + 1e-20))
    sh = downconvert(sh, fs, df)
    bb = channel_filter(sh, fs, CHANNEL_BW)
    bb, fsbb = resample_to_sps(bb, fs, SYMBOL_RATE, sps=SPS)
    mf = np.convolve(bb, rrc_taps(0.5, SPS, span=10), mode="same")
    spec = np.abs(np.fft.fftshift(np.fft.fft(mf**4, 1 << 18)))
    fax = np.fft.fftshift(np.fft.fftfreq(1 << 18, 1.0 / fsbb))
    m = np.abs(fax) < 2000
    cfo = fax[m][np.argmax(spec[m])] / 4.0
    return (mf * np.exp(-2j * np.pi * cfo / fsbb * np.arange(len(mf)))).astype(np.complex64)


def _sync_metric(mf: np.ndarray, template: np.ndarray) -> np.ndarray:
    t = template
    span = (len(t) - 1) * SPS + 1
    corr = np.zeros(len(mf) - span, dtype=complex)
    en = np.zeros(len(corr))
    p = np.abs(mf) ** 2
    for k in range(len(t)):
        corr += mf[k * SPS : k * SPS + len(corr)] * np.conj(t[k])
        en += p[k * SPS : k * SPS + len(corr)]
    return np.abs(corr) / (np.sqrt(en * np.sum(np.abs(t) ** 2)) + 1e-9)


def _find_peaks(metric: np.ndarray, thresh: float, min_sep: int) -> list[int]:
    order = np.argsort(metric)[::-1]
    taken: list[int] = []
    for i in order:
        if metric[i] < thresh:
            break
        if all(abs(int(i) - j) > min_sep for j in taken):
            taken.append(int(i))
    return sorted(taken)


def _extract_slot(mf: np.ndarray, sync_sample: int, template: np.ndarray) -> np.ndarray | None:
    idx = sync_sample - SW_SYM * SPS + np.arange(SYMBOLS_PER_SLOT) * SPS
    if idx[0] < 0 or idx[-1] >= len(mf):
        return None
    slot = mf[idx].astype(complex)
    slot = slot / (np.sqrt(np.mean(np.abs(slot) ** 2)) + 1e-12)
    spec = np.fft.fftshift(np.fft.fft(slot**4, 4096))
    fb = np.fft.fftshift(np.fft.fftfreq(4096))
    w = np.abs(fb) < 0.05
    cyc = fb[w][np.argmax(np.abs(spec[w]))] / 4.0
    slot = slot * np.exp(-2j * np.pi * cyc * np.arange(SYMBOLS_PER_SLOT))
    rx = slot[SW_SYM : SW_SYM + len(template)]
    slot = slot * np.exp(-1j * np.angle(np.sum(rx * np.conj(template))))
    return slot.astype(np.complex64)


def _slot_bits(slot: np.ndarray) -> np.ndarray:
    return symbols_to_bits(slot)


def _control_slots_from_mf(
    mf: np.ndarray, sync_thresh: float = 0.6
) -> list[tuple[int, np.ndarray]]:
    template = SW_TEMPLATES["S1"]
    metric = _sync_metric(mf, template)
    out: list[tuple[int, np.ndarray]] = []
    for s in _find_peaks(metric, sync_thresh, SYMBOLS_PER_SLOT * SPS - 40):
        slot = _extract_slot(mf, s, template)
        if slot is not None:
            out.append((int(s), slot))
    return out


def _decode_control_slot(slot: np.ndarray, seed: int) -> ch.ControlMessage | None:
    bits = _slot_bits(slot)[:600]
    if len(bits) < ch.CAC_OFFSET + ch.CAC_SPAN:
        return None
    return ch.decode_slot(bits, seed)


def demod_control(
    iq: np.ndarray,
    fs: float,
    seed: int,
    f0: float | None = None,
    sync_thresh: float = 0.6,
) -> list[ch.ControlMessage]:
    if f0 is None:
        f0 = find_channel(iq, fs)
    mf = _front_end(iq, fs, f0)
    msgs = [_decode_control_slot(slot, seed) for _, slot in _control_slots_from_mf(mf, sync_thresh)]
    return [m for m in msgs if m is not None]


def analyze(
    iq: np.ndarray, fs: float, seed: int,
    f0: float | None = None, sync_thresh: float = 0.6, max_slots_const: int = 400,
) -> dict:
    if f0 is None:
        f0 = find_channel(iq, fs)
    mf = _front_end(iq, fs, f0)
    slots = _control_slots_from_mf(mf, sync_thresh)
    msgs: list[ch.ControlMessage] = []
    evms: list[float] = []
    const: list[np.ndarray] = []
    for _, slot in slots:
        evms.append(evm_percent(slot))
    const_cut = sorted(evms)[: max_slots_const][-1] if evms else 0.0
    for (_, slot), evm in zip(slots, evms, strict=True):
        if evm <= const_cut and len(const) < max_slots_const:
            const.append(slot)
        msg = _decode_control_slot(slot, seed)
        if msg is not None:
            msgs.append(msg)
    constellation = np.concatenate(const) if const else np.empty(0, np.complex64)
    return {
        "f0": f0,
        "messages": msgs,
        "evm_median": float(np.median(evms)) if evms else None,
        "evm_best": float(np.min(evms)) if evms else None,
        "slot_count": len(slots),
        "constellation": constellation,
    }


_TCH_SYM_1 = [s for s in range(4, 69) if s != 10]
_TCH_SYM_2 = [s for s in range(80, 145) if s != 138]

CHANNEL_TYPES = {0b1010: "TCH(I)", 0b0010: "FACCH", 0b1000: "TCH(B)", 0b0000: "空線"}
VOICE_TYPES = frozenset({"TCH(B)", "TCH(I)"})
_C_SYM = 79


@dataclass
class TchBurst:

    pos: int
    bits: np.ndarray
    ctype: str
    c_dist: int


def classify_channel_type(c_bits: np.ndarray) -> tuple[str, int]:
    v = 0
    for b in c_bits[:4]:
        v = (v << 1) | int(b)
    best = min(CHANNEL_TYPES, key=lambda c: bin(c ^ v).count("1"))
    return CHANNEL_TYPES[best], bin(best ^ v).count("1")


def tch_burst_from_slot(slot: np.ndarray, pos: int) -> TchBurst | None:
    if slot is None or len(slot) < 145:
        return None
    bits = np.concatenate([
        symbols_to_bits_threshold(slot[_TCH_SYM_1]),
        symbols_to_bits_threshold(slot[_TCH_SYM_2]),
    ])
    if bits.size != 512:
        return None
    data = slot[_TCH_SYM_1 + _TCH_SYM_2]
    thr = (float(np.mean(np.abs(data.real))), float(np.mean(np.abs(data.imag))))
    c_bits = symbols_to_bits_threshold(slot[_C_SYM : _C_SYM + 1], thr)
    ctype, dist = classify_channel_type(c_bits)
    return TchBurst(pos=int(pos), bits=bits, ctype=ctype, c_dist=dist)


def _tch_bursts_from_mf(mf: np.ndarray, sync_thresh: float = 0.6) -> list[TchBurst]:
    template = SW_TEMPLATES["S3"]
    metric = _sync_metric(mf, template)
    peaks = _find_peaks(metric, sync_thresh, SYMBOLS_PER_SLOT * SPS - 40)
    out: list[TchBurst] = []
    for s in peaks:
        slot = _extract_slot(mf, s, template)
        burst = tch_burst_from_slot(slot, s)
        if burst is not None:
            out.append(burst)
    return out


def smooth_voice_flags(bursts: list[TchBurst], win: int = 2) -> list[bool]:
    if not bursts:
        return []
    slot_samples = SYMBOLS_PER_SLOT * SPS
    phase = int(bursts[0].pos) % slot_samples
    rings: dict[int, list[int]] = {}
    for i, b in enumerate(bursts):
        rings.setdefault(round((b.pos - phase) / slot_samples) % 6, []).append(i)
    raw = [b.ctype in VOICE_TYPES for b in bursts]
    smoothed = list(raw)
    for idxs in rings.values():
        for k, i in enumerate(idxs):
            nb = [raw[idxs[j]] for j in range(max(0, k - win), min(len(idxs), k + win + 1))]
            smoothed[i] = sum(nb) * 2 > len(nb)
    return smoothed


def demod_tch(
    iq: np.ndarray,
    fs: float,
    f0: float | None = None,
    sync_thresh: float = 0.6,
    voice_only: bool = True,
) -> list[np.ndarray]:
    if f0 is None:
        f0 = find_channel(iq, fs)
    mf = _front_end(iq, fs, f0)
    bursts = _tch_bursts_from_mf(mf, sync_thresh)
    if not voice_only:
        return [b.bits for b in bursts]
    flags = smooth_voice_flags(bursts)
    return [b.bits for b, v in zip(bursts, flags, strict=True) if v]


def demod_broadcast(
    iq: np.ndarray,
    fs: float,
    seed: int,
    f0: float | None = None,
    sync_thresh: float = 0.6,
) -> dict:
    if f0 is None:
        f0 = find_channel(iq, fs)
    mf = _front_end(iq, fs, f0)
    control: list[tuple[int, ch.ControlMessage]] = []
    for pos, slot in _control_slots_from_mf(mf, sync_thresh):
        msg = _decode_control_slot(slot, seed)
        if msg is not None:
            control.append((pos, msg))
    return {
        "fs_bb": SYMBOL_RATE * SPS,
        "control": control,
        "tch": _tch_bursts_from_mf(mf, sync_thresh),
        "f0": f0,
    }
