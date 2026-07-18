from __future__ import annotations

import io
import queue
import re
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from stdt86.server import events
from stdt86.server.state import LiveState

BATCH_FRAMES = 250
SAMPLE_RATE = 16_000


def target_filename_part(target: dict | None) -> str:
    if not target:
        return ""
    kind = target.get("kind")
    if kind == "all":
        return "_一斉"
    if kind == "selective":
        ids = target.get("effective_ids") or target.get("ids") or []
        part = "子局" + "-".join(str(i) for i in ids)
        if len(part) > 24:
            part = part[:24] + "他"
        return "_" + re.sub(r"[^\w\-一-龯ぁ-んァ-ヶ]", "-", part)
    return ""


def pcm16_wav_bytes(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


class AudioWorker:

    def __init__(self, seed: int, state: LiveState,
                 emit: Callable[[dict], None],
                 pcm_sink: Callable[[int, np.ndarray], None],
                 log_dir: str | Path | None = "logs") -> None:
        self.seed = seed
        self.state = state
        self.emit = emit
        self.pcm_sink = pcm_sink
        self.log_dir = Path(log_dir).resolve() if log_dir else None
        self._q: queue.Queue = queue.Queue()
        self._pending: dict[int, list[np.ndarray | None]] = {}
        self._targets: dict[int, dict | None] = {}
        self._gaps: dict[int, object] = {}
        self._plc: dict[int, tuple[np.ndarray | None, int]] = {}
        self._stats: dict[int, dict] = {}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="stdt86-audio")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=10.0)


    def push_burst(self, window_id: int, bits: np.ndarray, pos: int) -> None:
        self._q.put(("burst", window_id, (bits, pos)))

    def set_window_target(self, window_id: int, target: dict | None) -> None:
        self._targets[window_id] = target

    def window_closed(self, window_id: int) -> None:
        self._q.put(("close", window_id, None))


    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                for wid in list(self._pending):
                    self._decode_batch(wid)
                return
            kind, wid, payload = item
            if kind == "burst":
                from stdt86.codec import s_codec

                bits, pos = payload
                gaps = self._gaps.setdefault(wid, s_codec.SlotGapTracker())
                missing = gaps.step(pos)
                pend = self._pending.setdefault(wid, [])
                if missing is None:
                    self._stat(wid)["stale"] += 1
                else:
                    pend.extend([None] * missing)
                    pend.append(bits)
                if len(pend) >= BATCH_FRAMES:
                    self._decode_batch(wid)
            elif kind == "close":
                self._decode_batch(wid)

    def _stat(self, wid: int) -> dict:
        return self._stats.setdefault(
            wid, {"frames": 0, "crc7_ok": 0, "filled": 0, "stale": 0,
                  "decoded_seconds": 0.0,
                  "decode_attempted": False, "note": "", "wav_path": None,
                  "pcm": []})

    def _write_wav(self, wid: int, st: dict) -> str:
        if self.log_dir is None or not st["pcm"]:
            return ""
        try:
            if st["wav_path"] is None:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                part = target_filename_part(self._targets.get(wid))
                st["wav_path"] = str(
                    self.log_dir / f"{stamp}_broadcast{wid}{part}.wav")
                self.emit(events.log_event(
                    getattr(self.state, "t", 0.0),
                    f"通報 #{wid} の音声を {st['wav_path']} へ保存します"))
            Path(st["wav_path"]).write_bytes(
                pcm16_wav_bytes(np.concatenate(st["pcm"])))
            self._write_sidecar(wid, st)
            return ""
        except Exception as exc:
            return f"WAV 保存失敗: {type(exc).__name__}: {exc}"

    def _write_sidecar(self, wid: int, st: dict) -> None:
        from stdt86.control import channel as ch

        target = self._targets.get(wid)
        win = self.state.window_info(wid) or {}

        def wall(ms):
            return ("—" if ms is None else time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)))

        lines = [f"通報 #{wid}", ""]
        if target:
            lines.append(f"報知対象: {target['label']}")
            lines.append(f"  種別: {target['kind']}"
                         " (all=一斉 / selective=群・個別 / unknown=不明)")
            ids = target.get("ids") or []
            lines.append("  子局識別番号(生値): "
                         + ("、".join(str(i) for i in ids) if ids else "—"))
            eff = target.get("effective_ids") or []
            vb = target.get("valid_bits")
            if eff and (vb or eff != ids):
                lines.append("  子局識別番号(マスク後): "
                             + "、".join(str(i) for i in eff)
                             + (f"（有効ビット数 {vb}）" if vb else ""))
            if target.get("call_no") is not None:
                lines.append(f"  呼番号: {target['call_no']}")
            if target.get("note"):
                lines.append(f"  注記: {target['note']}")
            lines.append("  ※ 一斉の判定はマスク後 全0 のみ"
                         "（§2.5 番号計画: 全0=呼出先指定なし）。その他=群/個別呼出")
        else:
            lines.append("報知対象: 不明（通報開始指示から取得できず）")
        code = self.state.municipality_code or self.state.municipal_code
        if code:
            name = ch.city_for_municipal_code(code)
            lines.append(f"市区町村: {name or '?'}（コード {code}）")
        lines += [
            f"開始（受信機実時刻）: {wall(win.get('wall_start'))}",
            f"終了（受信機実時刻）: {wall(win.get('wall_end'))}",
            "",
            f"音声フレーム: {st['frames']} / CRC7一致: {st['crc7_ok']}"
            f" / 欠落補間: {st['filled']}",
            f"デコード秒数: {st['decoded_seconds']:.1f}",
            f"WAV: {st['wav_path']}",
        ]
        Path(st["wav_path"] + ".txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def _decode_batch(self, wid: int) -> None:
        entries = self._pending.pop(wid, [])
        if not entries:
            return
        st = self._stat(wid)
        n_real = sum(1 for e in entries if e is not None)
        st["frames"] += n_real
        st["filled"] += len(entries) - n_real
        note = ""
        try:
            from stdt86.codec import g7221, s_codec

            frames, fers = s_codec.decode_tch_frames_gapped(entries, self.seed)
            st["crc7_ok"] += int(np.sum(fers == 0))
            last_good, run = self._plc.get(wid, (None, 0))
            concealed = s_codec.conceal_frame_errors(
                frames, fers, last_good=last_good, run=run)
            good = np.flatnonzero(fers == 0)
            if len(good):
                self._plc[wid] = (frames[int(good[-1])].copy(),
                                  len(fers) - 1 - int(good[-1]))
            else:
                self._plc[wid] = (last_good, run + len(fers))
            pcm = g7221.decode(concealed, scodec=True)
            st["decode_attempted"] = True
            st["decoded_seconds"] += len(pcm) / 16000.0
            pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
            st["pcm"].append(pcm16)
            self.pcm_sink(wid, pcm16)
            if st["crc7_ok"] < st["frames"] * 0.5:
                note = "CRC7 不一致多数 — 実運用波の伝送路 FEC は §5.4 と異なるため想定内"
        except Exception as exc:
            note = f"デコード失敗: {type(exc).__name__}: {exc}"
        wav_err = self._write_wav(wid, st)
        if wav_err:
            note = f"{note} / {wav_err}" if note else wav_err
        st["note"] = note
        status = {k: v for k, v in st.items() if k != "pcm"}
        self.state.audio_status(wid, status)
        self.emit(events.audio_status_event(
            wid, frames=st["frames"], crc7_ok=st["crc7_ok"],
            decoded_seconds=st["decoded_seconds"],
            decode_attempted=st["decode_attempted"], note=note,
            wav_path=st["wav_path"], filled=st["filled"]))


    def window_pcm(self, window_id: int) -> np.ndarray | None:
        st = self._stats.get(window_id)
        if not st or not st["pcm"]:
            return None
        return np.concatenate(st["pcm"])


__all__ = ["AudioWorker", "BATCH_FRAMES", "SAMPLE_RATE", "pcm16_wav_bytes"]
