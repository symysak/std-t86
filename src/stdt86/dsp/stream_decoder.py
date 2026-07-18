from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from stdt86.control import channel as ch
from stdt86.dsp.burst import SYMBOLS_PER_SLOT
from stdt86.dsp.iq_control import (
    SPS,
    VOICE_TYPES,
    TchBurst,
    tch_burst_from_slot,
)
from stdt86.dsp.qam import symbols_to_bits
from stdt86.dsp.stream_frontend import FS_BB, StreamFrontEnd
from stdt86.dsp.stream_slots import DetectedBurst, SlotTracker
from stdt86.fec.seed_search import SeedSearcher

_SLOT_SAMPLES = SYMBOLS_PER_SLOT * SPS


@dataclass
class BroadcastWindow:

    window_id: int
    open_pos: int
    close_pos: int | None = None
    target: dict | None = None
    target_crc_ok: bool = False

    def contains(self, pos: int) -> bool:
        return self.open_pos <= pos and (self.close_pos is None or pos < self.close_pos)


class BroadcastTracker:

    def __init__(self, keep: int = 16, confirm_window: int = 180_000,
                 confirm_count: int = 2, strict: bool = True) -> None:
        self.open: BroadcastWindow | None = None
        self.recent: deque[BroadcastWindow] = deque(maxlen=keep)
        self._next_id = 1
        self.confirm_window = confirm_window
        self.confirm_count = confirm_count
        self.strict = strict
        self._hist: dict[int, deque[int]] = {}
        self.target_updated: list[BroadcastWindow] = []
        self.id_valid_bits: int | None = None

    def _confirmed(self, pos: int, msg: ch.ControlMessage) -> bool:
        if msg.crc_ok:
            return True
        if self.strict:
            return False
        dq = self._hist.setdefault(msg.msg_type, deque())
        dq.append(pos)
        while dq and pos - dq[0] > self.confirm_window:
            dq.popleft()
        return len(dq) >= self.confirm_count

    def on_control(self, pos: int, msg: ch.ControlMessage) -> list[BroadcastWindow]:
        changed: list[BroadcastWindow] = []
        vb = msg.fields.get("子局識別番号有効ビット数")
        if msg.crc_ok and vb is not None and 0 <= int(vb) <= 8:
            self.id_valid_bits = 8 + int(vb)
        if msg.msg_type not in (ch.MSG_BROADCAST_START, ch.MSG_DELAYED_START,
                                ch.MSG_FORCED_RELEASE):
            return changed
        if msg.msg_type in (ch.MSG_BROADCAST_START, ch.MSG_DELAYED_START):
            self._refresh_target(msg)
        if not self._confirmed(pos, msg):
            return changed
        if (msg.msg_type in (ch.MSG_BROADCAST_START, ch.MSG_DELAYED_START)
                and self.open is None):
            self.open = BroadcastWindow(
                self._next_id, pos,
                target=ch.broadcast_target(msg, self.id_valid_bits),
                target_crc_ok=msg.crc_ok)
            self._next_id += 1
            changed.append(self.open)
        elif msg.msg_type == ch.MSG_FORCED_RELEASE and self.open is not None:
            self.open.close_pos = pos
            self.recent.append(self.open)
            changed.append(self.open)
            self.open = None
        return changed

    def _refresh_target(self, msg: ch.ControlMessage) -> None:
        w = self.open
        if w is None or w.target_crc_ok:
            return
        if not msg.crc_ok:
            return
        target = ch.broadcast_target(msg, self.id_valid_bits)
        if target is None:
            return
        if w.target is None or target != w.target:
            w.target = target
            self.target_updated.append(w)
        w.target_crc_ok = True

    def window_for(self, pos: int) -> BroadcastWindow | None:
        if self.open is not None and self.open.contains(pos):
            return self.open
        for w in reversed(self.recent):
            if w.contains(pos):
                return w
        return None


class _VoiceSmoother:

    def __init__(self, win: int = 2) -> None:
        self.win = win
        self.horizon = (6 * win + 2) * _SLOT_SAMPLES
        self._pending: deque[tuple[TchBurst, int, bool]] = deque()
        self._hist: dict[int, deque[bool]] = {}
        self._last: tuple[int, int] | None = None

    @property
    def oldest_pending_pos(self) -> int | None:
        return self._pending[0][0].pos if self._pending else None

    def _ring_of(self, pos: int) -> int:
        if self._last is None:
            ring = 0
        else:
            lp, lr = self._last
            ring = (lr + round((pos - lp) / _SLOT_SAMPLES)) % 6
        self._last = (pos, ring)
        return ring

    def push(self, burst: TchBurst) -> list[tuple[TchBurst, bool]]:
        ring = self._ring_of(burst.pos)
        self._pending.append((burst, ring, burst.ctype in VOICE_TYPES))
        return self._release(burst.pos)

    def release(self, finalized_pos: int) -> list[tuple[TchBurst, bool]]:
        return self._release(finalized_pos)

    def _mates(self, pending: list, start: int, pos: int, ring: int) -> list[bool]:
        limit = pos + self.horizon
        out = [v for b, r, v in itertools.islice(pending, start, None)
               if r == ring and b.pos <= limit]
        return out[: self.win]

    def _release(self, high_water: int) -> list[tuple[TchBurst, bool]]:
        out: list[tuple[TchBurst, bool]] = []
        while self._pending:
            b, ring, v = self._pending[0]
            mates = self._mates(self._pending, 1, b.pos, ring)
            if len(mates) < self.win and high_water <= b.pos + self.horizon:
                break
            votes = list(self._hist.get(ring, ())) + [v] + mates
            self._hist.setdefault(ring, deque(maxlen=self.win)).append(v)
            self._pending.popleft()
            out.append((b, sum(votes) * 2 > len(votes)))
        return out

    def flush(self) -> list[tuple[TchBurst, bool]]:
        pend = list(self._pending)
        self._pending.clear()
        out: list[tuple[TchBurst, bool]] = []
        for i, (b, ring, v) in enumerate(pend):
            mates = self._mates(pend, i + 1, b.pos, ring)
            votes = list(self._hist.get(ring, ())) + [v] + mates
            self._hist.setdefault(ring, deque(maxlen=self.win)).append(v)
            out.append((b, sum(votes) * 2 > len(votes)))
        return out


@dataclass
class FeedResult:

    control: list[tuple[int, ch.ControlMessage]] = field(default_factory=list)
    tch: list[TchBurst] = field(default_factory=list)
    voice: list[tuple[TchBurst, bool, int | None]] = field(default_factory=list)
    broadcast_started: list[BroadcastWindow] = field(default_factory=list)
    broadcast_ended: list[BroadcastWindow] = field(default_factory=list)
    broadcast_updated: list[BroadcastWindow] = field(default_factory=list)
    evms: list[float] = field(default_factory=list)
    sw_counts: dict[str, int] = field(default_factory=dict)
    slots: list[tuple[float, np.ndarray]] = field(default_factory=list)
    seed_detected: dict | None = None


_SEED_MIN_SLOTS = 8
_SEED_TOP = 48
_SEED_SCORE_CACS = 12
_SEED_SCORE_PER_FEED = 2
_SEED_WINDOW = 96


class _SeedSearch:

    def __init__(self) -> None:
        self._pre = SeedSearcher(top=_SEED_TOP)
        self.slots: deque[tuple[int, np.ndarray]] = deque(maxlen=_SEED_WINDOW)
        self._cands: list[int] = []
        self._cacs: list[np.ndarray] = []
        self._scores: dict[int, tuple[float, int, int]] = {}
        self._cursor = 0
        self._budget = 0

    def reset(self) -> None:
        self._pre = SeedSearcher(top=_SEED_TOP)
        self.slots.clear()
        self._cands = []
        self._cacs = []
        self._scores = {}
        self._cursor = 0
        self._budget = 0

    def begin_feed(self) -> None:
        self._budget = _SEED_SCORE_PER_FEED

    def _start_round(self) -> None:
        recent = list(self.slots)[-_SEED_SCORE_CACS:]
        self._cacs = [ch.extract_cac(b) for _, b in recent]
        self._cands = self._pre.candidates()
        self._scores = {}
        self._cursor = 0

    def push(self, pos: int, bits: np.ndarray) -> dict | None:
        self.slots.append((pos, bits))
        self._pre.push(ch.extract_cac(bits))
        if len(self.slots) < _SEED_MIN_SLOTS:
            return None
        if not self._cands:
            self._start_round()
        while self._budget > 0 and self._cursor < len(self._cands):
            s = self._cands[self._cursor]
            self._cursor += 1
            self._budget -= 1
            self._scores[s] = ch.score_seed(self._cacs, s)
            info = self._confident_info()
            if info is not None:
                return info
        if self._cursor >= len(self._cands):
            self._cands = []
        return None

    def _confident_info(self) -> dict | None:
        if len(self._scores) < 2:
            return None
        ranked = sorted(((v, s) for s, v in self._scores.items()), reverse=True)
        (score, crc_hits, known), seed = ranked[0]
        second = ranked[1][0][0]
        n = len(self._cacs)
        if crc_hits < 1 or not (
                score >= max(6.0, n * 0.5) and score >= 1.5 * max(second, 1.0)):
            return None
        return {
            "seed": seed, "score": score, "second_score": second,
            "confident": True, "crc_hits": crc_hits, "known": known,
            "n_slots": n, "ranking": self._pre.ranking(),
            "candidates": ch.candidates_for_seed(seed),
        }


class StreamingDecoder:

    def __init__(self, fs: float, f0: float | None, seed: int | None,
                 sync_thresh: float = 0.6) -> None:
        self.fs = float(fs)
        self.seed = seed
        self.f0 = 0.0 if f0 is None else float(f0)
        self.sync_thresh = sync_thresh
        self.frontend = StreamFrontEnd(fs, self.f0)
        self.tracker = SlotTracker(sync_thresh)
        self.broadcast = BroadcastTracker()
        self._smoother = _VoiceSmoother()
        self._seed_search = _SeedSearch()
        self._ended_backlog: list[BroadcastWindow] = []

    @property
    def fs_bb(self) -> float:
        return FS_BB

    @property
    def cfo_hz(self) -> float | None:
        return self.frontend.cfo_hz

    @property
    def squelch_enabled(self) -> bool:
        return self.tracker.squelch_enabled

    def set_squelch_enabled(self, enabled: bool) -> None:
        self.tracker.squelch_enabled = bool(enabled)

    def reacquire_cfo(self) -> None:
        self.frontend.reacquire_cfo()

    @property
    def broadcast_strict(self) -> bool:
        return self.broadcast.strict

    def set_broadcast_strict(self, strict: bool) -> None:
        self.broadcast.strict = bool(strict)

    def reset_seed(self) -> None:
        self.seed = None
        self._seed_search.reset()

    def _apply_detected_seed(self, info: dict, res: FeedResult) -> None:
        self.seed = info["seed"]
        res.seed_detected = info
        for pos, bits in self._seed_search.slots:
            self._decode_control(pos, bits, res)
        self._seed_search.reset()

    def _decode_control(self, pos: int, bits: np.ndarray, res: FeedResult) -> None:
        msg = ch.decode_slot(bits, self.seed)
        res.control.append((pos, msg))
        for w in self.broadcast.on_control(pos, msg):
            if w.close_pos is not None:
                self._ended_backlog.append(w)
            else:
                res.broadcast_started.append(w)
        if self.broadcast.target_updated:
            res.broadcast_updated.extend(self.broadcast.target_updated)
            self.broadcast.target_updated = []

    def _handle_bursts(self, bursts: list[DetectedBurst], res: FeedResult) -> None:
        for b in bursts:
            res.evms.append(b.evm)
            res.sw_counts[b.sw] = res.sw_counts.get(b.sw, 0) + 1
            res.slots.append((b.evm, b.slot))
            if b.sw in ("S1", "S5", "S6"):
                bits = np.asarray(symbols_to_bits(b.slot))[:600]
                if len(bits) < ch.CAC_OFFSET + ch.CAC_SPAN:
                    continue
                if self.seed is None:
                    info = self._seed_search.push(b.pos, bits)
                    if info is not None:
                        self._apply_detected_seed(info, res)
                else:
                    self._decode_control(b.pos, bits, res)
            else:
                burst = tch_burst_from_slot(b.slot, b.pos)
                if burst is None:
                    continue
                if (burst.ctype == "FACCH" and burst.c_dist == 0
                        and self.seed is not None):
                    msg = ch.decode_facch(burst.bits, self.seed)
                    if msg.crc_ok or msg.msg_type in ch.MESSAGE_TYPES:
                        res.control.append((b.pos, msg))
                    continue
                res.tch.append(burst)
                self._emit_voice(self._smoother.push(burst), res)

    def _emit_voice(self, decided: list[tuple[TchBurst, bool]],
                    res: FeedResult) -> None:
        for burst, is_voice in decided:
            w = self.broadcast.window_for(burst.pos)
            res.voice.append((burst, is_voice, w.window_id if w else None))

    def _release_ended(self, res: FeedResult) -> None:
        wm = self._smoother.oldest_pending_pos
        watermark = self.tracker.finalized_pos if wm is None \
            else min(wm, self.tracker.finalized_pos)
        keep: list[BroadcastWindow] = []
        for w in self._ended_backlog:
            (res.broadcast_ended if (w.close_pos or 0) < watermark
             else keep).append(w)
        self._ended_backlog = keep

    def feed(self, iq_chunk: np.ndarray) -> FeedResult:
        res = FeedResult()
        mf = self.frontend.process(iq_chunk)
        if len(mf) == 0:
            return res
        if self.seed is None:
            self._seed_search.begin_feed()
        self._handle_bursts(self.tracker.process(mf), res)
        self._emit_voice(self._smoother.release(self.tracker.finalized_pos), res)
        self._release_ended(res)
        return res

    def flush(self) -> FeedResult:
        res = FeedResult()
        self._emit_voice(self._smoother.flush(), res)
        res.broadcast_ended.extend(self._ended_backlog)
        self._ended_backlog = []
        return res


__all__ = [
    "BroadcastTracker",
    "BroadcastWindow",
    "FeedResult",
    "StreamingDecoder",
]
