from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from stdt86.dsp.stream_frontend import _NCO, _Decimator
from stdt86.server import events
from stdt86.server.audio import target_filename_part

TARGET_FS = 40_000.0
PREROLL_S = 5.0
TAIL_S = 2.0
MAX_RECORD_S = 600.0
_FLUSH_EVERY_S = 10.0


class IQRecorder:

    def __init__(self, fs: float, f0: float, log_dir: str | Path,
                 emit: Callable[[dict], None], state,
                 sidecar_refresh: Callable[[int], None] | None = None,
                 preroll_s: float = PREROLL_S, tail_s: float = TAIL_S,
                 max_record_s: float = MAX_RECORD_S) -> None:
        self.fs = float(fs)
        self.f0 = float(f0)
        self.log_dir = Path(log_dir).resolve()
        self.emit = emit
        self.state = state
        self.sidecar_refresh = sidecar_refresh
        decim = max(1, int(self.fs // TARGET_FS))
        self.fs_out = self.fs / decim
        self._nco = _NCO(self.f0, self.fs)
        self._decim = _Decimator(self.fs, decim)
        self.preroll = int(preroll_s * self.fs_out)
        self.tail = int(tail_s * self.fs_out)
        self.max_samples = int(max_record_s * self.fs_out)
        self._ring: list[np.ndarray] = []
        self._ring_n = 0
        self._rec: dict | None = None
        self.dropped = 0
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="stdt86-iqrec")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=10.0)


    def push(self, chunk: np.ndarray) -> None:
        try:
            self._q.put_nowait(("iq", chunk))
        except queue.Full:
            self.dropped += 1

    def window_opened(self, window_id: int, target: dict | None) -> None:
        self._q.put(("open", (window_id, target)))

    def window_closed(self, window_id: int) -> None:
        self._q.put(("close", window_id))


    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                if self._rec is not None:
                    self._finalize()
                return
            kind, payload = item
            if kind == "iq":
                self._on_samples(payload)
            elif kind == "open":
                self._on_open(*payload)
            elif kind == "close":
                self._on_close(payload)

    def _on_samples(self, chunk: np.ndarray) -> None:
        x = self._decim.process(
            self._nco.mix(np.asarray(chunk, dtype=np.complex64)))
        if not len(x):
            return
        rec = self._rec
        if rec is not None:
            rec["parts"].append(x)
            rec["n"] += len(x)
            rec["since_write"] += len(x)
            if rec["n"] >= self.max_samples:
                rec["note"] = f"録音上限 {self.max_samples / self.fs_out:.0f}s 到達"
                self._finalize()
                return
            if rec["tail_left"] is not None:
                rec["tail_left"] -= len(x)
                if rec["tail_left"] <= 0:
                    self._finalize()
                    return
            if rec["since_write"] >= _FLUSH_EVERY_S * self.fs_out:
                self._write(final=False)
        else:
            self._ring.append(x)
            self._ring_n += len(x)
            while self._ring and self._ring_n - len(self._ring[0]) >= self.preroll:
                self._ring_n -= len(self._ring[0])
                self._ring.pop(0)

    def _on_open(self, window_id: int, target: dict | None) -> None:
        if self._rec is not None:
            self._finalize()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        part = target_filename_part(target)
        path = self.log_dir / f"{stamp}_broadcast{window_id}{part}_iq.wav"
        self._rec = {
            "wid": window_id, "path": str(path),
            "parts": list(self._ring), "n": self._ring_n,
            "since_write": 0, "tail_left": None, "note": "",
        }
        self._ring = []
        self._ring_n = 0
        self.emit(events.log_event(
            getattr(self.state, "t", 0.0),
            f"通報 #{window_id} の IQ を {path} へ保存します"
            f"（{self.fs_out:.0f}Hz 複素, プリロール {self.preroll / self.fs_out:.0f}s）"))

    def _on_close(self, window_id: int) -> None:
        if self._rec is None or self._rec["wid"] != window_id:
            return
        self._rec["tail_left"] = self.tail

    def _write(self, final: bool) -> None:
        import soundfile as sf

        rec = self._rec
        if rec is None or not rec["parts"]:
            return
        rec["since_write"] = 0
        try:
            x = np.concatenate(rec["parts"])
            peak = float(np.max(np.abs(np.concatenate([x.real, x.imag]))))
            scale = 32000.0 / peak if peak > 0 else 1.0
            data = np.stack([x.real, x.imag], axis=1) * scale
            self.log_dir.mkdir(parents=True, exist_ok=True)
            sf.write(rec["path"], data.astype(np.int16),
                     int(round(self.fs_out)), subtype="PCM_16")
        except Exception as exc:
            rec["note"] = f"IQ 保存失敗: {type(exc).__name__}: {exc}"
        note = rec["note"]
        if self.dropped:
            note = (f"{note} / " if note else "") + \
                f"入力取りこぼし {self.dropped} チャンク（録音に隙間あり）"
        info = {
            "path": rec["path"],
            "seconds": round(rec["n"] / self.fs_out, 1),
            "fs": round(self.fs_out, 1),
            "done": final,
            "note": note,
        }
        self.state.window_iq(rec["wid"], info)
        self.emit({"type": "iq_status", "window_id": rec["wid"], **info})

    def _finalize(self) -> None:
        rec = self._rec
        if rec is None:
            return
        self._write(final=True)
        self._rec = None
        if self.sidecar_refresh is not None:
            self.sidecar_refresh(rec["wid"])


__all__ = ["IQRecorder", "MAX_RECORD_S", "PREROLL_S", "TAIL_S", "TARGET_FS"]
