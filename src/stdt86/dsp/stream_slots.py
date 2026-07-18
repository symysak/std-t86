from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter1d

from stdt86.dsp.burst import SYMBOLS_PER_SLOT
from stdt86.dsp.iq_control import (
    SPS,
    SW_SYM,
    SW_TEMPLATES,
    _extract_slot,
    _sync_metric,
)
from stdt86.dsp.qam import evm_percent

_BACK = SW_SYM * SPS
_FWD = (SYMBOLS_PER_SLOT - SW_SYM) * SPS
_TEMPLATE_SPAN = (len(next(iter(SW_TEMPLATES.values()))) - 1) * SPS + 1
_MIN_SEP = SYMBOLS_PER_SLOT * SPS - 40


@dataclass
class DetectedBurst:

    pos: int
    sw: str
    slot: np.ndarray
    corr: float
    evm: float
    power_db: float = 0.0


_SQUELCH_DB = 25.0
_SQUELCH_DECAY_DB_S = 0.25


class SlotTracker:

    def __init__(self, sync_thresh: float = 0.6) -> None:
        self.sync_thresh = sync_thresh
        self.squelch_enabled = True
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_abs = 0
        self._next_scan = _BACK
        self._last_pos = -(10 * _MIN_SEP)
        self._p_ref = 0.0
        self._p_ref_pos = 0

    @property
    def finalized_pos(self) -> int:
        return self._next_scan

    def _squelch_ok(self, pos: int, power: float, fs_bb: float = 90_000.0) -> bool:
        dt = max(0, pos - self._p_ref_pos) / fs_bb
        ref = self._p_ref * 10.0 ** (-_SQUELCH_DECAY_DB_S * dt / 10.0)
        ok = power > ref * 10.0 ** (-_SQUELCH_DB / 10.0)
        self._p_ref = max(ref, power)
        self._p_ref_pos = pos
        return ok or not self.squelch_enabled

    def process(self, mf_chunk: np.ndarray) -> list[DetectedBurst]:
        if len(mf_chunk):
            self._buf = np.concatenate([self._buf, mf_chunk])
        buf_end = self._buf_abs + len(self._buf)
        emit_to = min(buf_end - _TEMPLATE_SPAN - _MIN_SEP, buf_end - _FWD)
        if emit_to <= self._next_scan:
            return []

        wa = max(self._buf_abs, self._next_scan - _MIN_SEP)
        wb = emit_to + _MIN_SEP
        seg = self._buf[wa - self._buf_abs : wb - self._buf_abs + _TEMPLATE_SPAN]
        names = list(SW_TEMPLATES)
        metrics = [_sync_metric(seg, SW_TEMPLATES[k]) for k in names]
        n = min(min(len(m) for m in metrics), wb - wa)
        mstack = np.stack([m[:n] for m in metrics])
        mm = mstack.max(axis=0)
        which = mstack.argmax(axis=0)

        maxf = maximum_filter1d(mm, size=2 * _MIN_SEP + 1, mode="nearest")
        cand = np.flatnonzero((mm >= self.sync_thresh) & (mm >= maxf))
        out: list[DetectedBurst] = []
        for i in cand:
            pos = wa + int(i)
            if not (self._next_scan <= pos < emit_to):
                continue
            if pos - self._last_pos <= _MIN_SEP:
                continue
            if out and pos - out[-1].pos <= _MIN_SEP:
                continue
            sw = names[which[i]]
            local = pos - self._buf_abs
            if local < _BACK:
                continue
            span = self._buf[local - _BACK: local + _FWD]
            power = float(np.mean(np.abs(span) ** 2)) if len(span) else 0.0
            if not self._squelch_ok(pos, power):
                continue
            slot = _extract_slot(self._buf, local, SW_TEMPLATES[sw])
            if slot is None:
                continue
            out.append(DetectedBurst(pos=pos, sw=sw, slot=slot,
                                     corr=float(mm[i]), evm=evm_percent(slot),
                                     power_db=10.0 * np.log10(power + 1e-20)))
        out.sort(key=lambda b: b.pos)
        if out:
            self._last_pos = out[-1].pos
        self._next_scan = emit_to
        keep_from = max(self._buf_abs, self._next_scan - _MIN_SEP - _BACK)
        cut = keep_from - self._buf_abs
        if cut > 0:
            self._buf = self._buf[cut:]
            self._buf_abs = keep_from
        return out


__all__ = ["DetectedBurst", "SlotTracker"]
