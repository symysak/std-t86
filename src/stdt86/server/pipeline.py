from __future__ import annotations

import queue
import threading
import time
from collections import Counter

import numpy as np

from stdt86.dsp.stream_decoder import StreamingDecoder
from stdt86.io.sources import IQSource
from stdt86.server import events
from stdt86.server.audio import AudioWorker
from stdt86.server.state import LiveState

CHUNK_SECONDS = 0.16
SAMPLE_QUEUE_CHUNKS = 32
CONTROL_RESET_S = 1.0


class Pipeline:

    def __init__(self, source: IQSource, f0: float | None, seed: int | None,
                 municipal_code: int | None = None,
                 sync_thresh: float = 0.6,
                 audio_log_dir: str | None = "logs") -> None:
        self.source = source
        self.sync_thresh = sync_thresh
        self.decoder = StreamingDecoder(source.fs, f0, seed, sync_thresh)
        self.state = LiveState(seed, municipal_code)
        self.state.f0_hz = f0
        self.state.center_hz = getattr(source, "center_hz", None)
        self.event_q: queue.Queue = queue.Queue()
        self.pcm_q: queue.Queue = queue.Queue()
        self.audio = AudioWorker(seed, self.state, self._emit,
                                 lambda wid, pcm: self.pcm_q.put((wid, pcm)),
                                 log_dir=audio_log_dir)
        self.iq_recorder = None
        if audio_log_dir:
            from stdt86.server.iq_recorder import IQRecorder

            self.iq_recorder = IQRecorder(
                source.fs, 0.0 if f0 is None else f0, audio_log_dir,
                emit=self._emit, state=self.state,
                sidecar_refresh=self.audio.refresh_sidecar)
        self._chunk = max(1, int(source.fs * CHUNK_SECONDS))
        self._samples_q: queue.Queue = queue.Queue(maxsize=SAMPLE_QUEUE_CHUNKS)
        self._stop = threading.Event()
        self._cfo_reset_req = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="stdt86-reader")
        self._dsp = threading.Thread(target=self._dsp_loop, daemon=True,
                                     name="stdt86-dsp")
        self.finished = threading.Event()

    def _reset_on_signal_loss(self, t_in: float) -> None:
        self.state.reset_control(clear_broadcast=True)
        self.state.municipality_code = None
        self.decoder.reacquire_cfo()
        if self.state.municipal_code is None:
            self.decoder.reset_seed()
            self.state.seed = None
            self.audio.seed = None
        self._emit(events.log_event(
            t_in, "信号喪失: 制御チャネル状態をリセットしました"
            "（CFO 再捕捉・スクランブル値・自治体を再探索）"))


    def start(self) -> None:
        self.audio.start()
        if self.iq_recorder is not None:
            self.iq_recorder.start()
        self._reader.start()
        self._dsp.start()

    def set_squelch(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        self.decoder.set_squelch_enabled(enabled)
        self.state.squelch_enabled = enabled
        self._emit(events.log_event(
            self.state.t,
            "電力スケルチを有効化しました" if enabled else
            "電力スケルチを無効化しました（悪条件でも全スロットの復号を試行します）"))
        return enabled

    def set_broadcast_strict(self, strict: bool) -> bool:
        strict = bool(strict)
        self.decoder.set_broadcast_strict(strict)
        self.state.broadcast_strict = strict
        self._emit(events.log_event(
            self.state.t,
            "通報検出: 厳格（CRC 一致必須）にしました" if strict else
            "通報検出: 反復許容（CRC 不一致でも反復で確定）にしました"))
        return strict

    def request_cfo_reset(self) -> None:
        self._cfo_reset_req.set()

    def stop(self) -> None:
        self._stop.set()
        self.source.close()
        self._reader.join(timeout=5.0)
        self._dsp.join(timeout=10.0)
        self.audio.stop()
        if self.iq_recorder is not None:
            self.iq_recorder.stop()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _emit(self, ev: dict) -> None:
        self.event_q.put(ev)
        if ev.get("type") in ("control_msg", "broadcast_start", "broadcast_end",
                              "broadcast_update", "audio_status", "iq_status", "log"):
            self.state.add_log(ev)


    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.source.read(self._chunk)
            except Exception:
                chunk = None
            if chunk is None or len(chunk) == 0:
                break
            if not getattr(self.source, "lossy", True):
                while not self._stop.is_set():
                    try:
                        self._samples_q.put(chunk, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                continue
            try:
                self._samples_q.put_nowait(chunk)
            except queue.Full:
                try:
                    self._samples_q.get_nowait()
                except queue.Empty:
                    pass
                self.state.overflows += 1
                try:
                    self._samples_q.put_nowait(chunk)
                except queue.Full:
                    pass
        self._samples_q.put(None)


    def _dsp_loop(self) -> None:
        fs_in = self.source.fs
        in_count = 0
        last_quality_t = -1.0
        last_level_in = -(10**18)
        tch_bucket: Counter[str] = Counter()
        tch_bucket_t = 0.0
        msg_times: list[float] = []

        def now_t() -> float:
            return in_count / fs_in

        const_best: list[tuple[float, np.ndarray]] = []
        const_t = 0.0
        last_control_in = 0
        armed = False
        had_valid = False

        while not self._stop.is_set():
            try:
                chunk = self._samples_q.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None:
                break
            in_count += len(chunk)
            t_in = now_t()
            if self.iq_recorder is not None:
                self.iq_recorder.push(chunk)

            if self._cfo_reset_req.is_set():
                self._cfo_reset_req.clear()
                self.decoder.reacquire_cfo()
                self._emit(events.log_event(t_in, "CFO を手動で再捕捉しました"))

            if in_count - last_level_in >= fs_in * 0.2:
                last_level_in = in_count
                p = float(np.mean(np.abs(chunk) ** 2))
                self.state.level_dbfs = 10.0 * np.log10(p + 1e-20)

            try:
                res = self.decoder.feed(chunk)
            except Exception as exc:
                self._emit(events.log_event(t_in, f"DSP エラー: {exc!r}"))
                continue
            self._handle_result(res, msg_times, tch_bucket)

            valid = any(m.crc_ok for _, m in res.control)
            if valid:
                last_control_in = in_count
                had_valid = True
            wrong_seed = (self.state.municipal_code is None
                          and self.decoder.seed is not None and bool(res.control))
            if (had_valid or wrong_seed) and not armed:
                armed = True
                if not valid:
                    last_control_in = in_count
            if armed and (in_count - last_control_in) / fs_in >= CONTROL_RESET_S:
                self._reset_on_signal_loss(t_in)
                msg_times.clear()
                last_control_in = in_count
                armed = False
                had_valid = False

            const_best += res.slots
            if t_in - const_t >= 0.2:
                const_best.sort(key=lambda x: x[0])
                self.event_q.put(events.constellation_event(
                    t_in, [s for _, s in const_best[:4]]))
                const_best = []
                const_t = t_in

            if t_in - tch_bucket_t >= 1.0:
                if tch_bucket:
                    self.event_q.put(events.tch_second_event(t_in, dict(tch_bucket)))
                    tch_bucket.clear()
                tch_bucket_t = t_in

            if t_in - last_quality_t >= 1.0:
                last_quality_t = t_in
                self._emit_quality(t_in, msg_times)

        res = self.decoder.flush()
        self._handle_result(res, msg_times, tch_bucket)
        w = self.decoder.broadcast.open
        if w is not None:
            self.audio.window_closed(w.window_id)
            if self.iq_recorder is not None:
                self.iq_recorder.window_closed(w.window_id)
        self.state.sync_locked = False
        self.finished.set()
        self.event_q.put(events.log_event(now_t(), "ソース終端に達しました。"))

    def _handle_result(self, res, msg_times: list[float],
                       tch_bucket: Counter) -> None:
        fs_bb = self.decoder.fs_bb
        if res.seed_detected is not None:
            self._on_seed_detected(res.seed_detected)
        for pos, msg in res.control:
            t = pos / fs_bb
            self.state.add_control(msg)
            self.state.t = t
            msg_times.append(t)
            self._emit(events.control_msg_event(t, pos, msg))
            code = msg.fields.get("市区町村コード(完全)")
            if msg.crc_ok and code is not None and code != self.state.municipality_code:
                self.state.municipality_code = code
                name = msg.fields.get("市区町村名") or f"コード{code}"
                self._emit(events.log_event(
                    t, f"★ FACCH 番号通知: 市区町村を確定 — {name}"
                    f"（完全コード {code}）"))
        for b in res.tch:
            tch_bucket[b.ctype] += 1
            self.state.add_tch(b.ctype)
        for w in res.broadcast_started:
            t = w.open_pos / fs_bb
            wall = self._now_ms()
            self.state.broadcast_started(w.window_id, t, wall, target=w.target)
            self.audio.set_window_target(w.window_id, w.target)
            if self.iq_recorder is not None:
                self.iq_recorder.window_opened(w.window_id, w.target)
            self._emit(events.broadcast_event("start", t, w.window_id, wall_ms=wall,
                                              target=w.target))
            if w.target:
                self._emit(events.log_event(
                    t, f"通報 #{w.window_id} 報知対象: {w.target['label']}"))
        for w in res.broadcast_updated:
            self.state.broadcast_target(w.window_id, w.target)
            self.audio.set_window_target(w.window_id, w.target)
            self._emit(events.broadcast_event("update", self.state.t, w.window_id,
                                              target=w.target))
            if w.target:
                self._emit(events.log_event(
                    self.state.t,
                    f"通報 #{w.window_id} 報知対象を確定: {w.target['label']}"))
        for burst, is_voice, window_id in res.voice:
            if is_voice and window_id is not None:
                self.audio.push_burst(window_id, burst.bits, burst.pos)
        for w in res.broadcast_ended:
            t = (w.close_pos or 0) / fs_bb
            wall = self._now_ms()
            self.state.broadcast_ended(w.window_id, t, wall)
            self._emit(events.broadcast_event("end", t, w.window_id, wall_ms=wall))
            self.audio.window_closed(w.window_id)
            if self.iq_recorder is not None:
                self.iq_recorder.window_closed(w.window_id)
        if res.evms:
            self.state.evm_median = float(np.median(res.evms))
            self.state.evm_best = float(np.min(res.evms))
        self.state.cfo_hz = self.decoder.cfo_hz
        self.state.sync_locked = bool(res.sw_counts)

    def _on_seed_detected(self, info: dict) -> None:
        seed = info["seed"]
        self.state.seed = seed
        self.audio.seed = seed
        cands = info.get("candidates") or []
        names = "、".join(n for _, n in cands) or "?"
        self._emit({
            "type": "seed_detected",
            "t": round(self.state.t, 1),
            "seed": seed,
            "score": info["score"],
            "crc_hits": info["crc_hits"],
            "known": info["known"],
            "n_slots": info["n_slots"],
            "candidates": cands,
        })
        self._emit(events.log_event(
            self.state.t,
            f"スクランブル値を自動判定: {seed}（候補: {names}）"
            f" score={info['score']:.0f} 既知種別 {info['known']}/{info['n_slots']}"))

    def _emit_quality(self, t: float, msg_times: list[float]) -> None:
        cutoff = t - 10.0
        while msg_times and msg_times[0] < cutoff:
            msg_times.pop(0)
        self.state.msgs_per_s = len(msg_times) / 10.0
        summary = None
        try:
            summary = self.state.control_summary()
            total = summary.get("total") or 0
            self.state.crc_ok_rate = (summary["crc_ok"] / total) if total else None
        except Exception:
            pass
        self._emit_quality_event(t)
        if summary is not None:
            self.event_q.put({"type": "control_summary", "t": round(t, 1),
                              "control": summary})

    def _emit_quality_event(self, t: float) -> None:
        st = self.state
        self.event_q.put(events.quality_event(
            t, cfo_hz=st.cfo_hz, evm_median=st.evm_median, evm_best=st.evm_best,
            crc_ok_rate=st.crc_ok_rate, msgs_per_s=st.msgs_per_s,
            level_dbfs=st.level_dbfs, sync_locked=st.sync_locked,
            overflows=st.overflows))


__all__ = ["CHUNK_SECONDS", "Pipeline"]
