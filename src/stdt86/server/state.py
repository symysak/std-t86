from __future__ import annotations

import threading
from collections import Counter, deque

from stdt86.control import channel as ch


class LiveState:

    def __init__(self, seed: int | None, municipal_code: int | None,
                 recent_msgs: int = 2000, log_lines: int = 500) -> None:
        self.seed = seed
        self.municipal_code = municipal_code
        self.municipality_code: int | None = None
        self.f0_hz: float | None = None
        self.center_hz: float | None = None
        self._lock = threading.Lock()
        self._msgs: deque[ch.ControlMessage] = deque(maxlen=recent_msgs)
        self._log: deque[dict] = deque(maxlen=log_lines)
        self._tch_counts: Counter[str] = Counter()
        self.t = 0.0
        self.cfo_hz: float | None = None
        self.evm_median: float | None = None
        self.evm_best: float | None = None
        self.level_dbfs: float | None = None
        self.msgs_per_s = 0.0
        self.crc_ok_rate: float | None = None
        self.sync_locked = False
        self.overflows = 0
        self.squelch_enabled = True
        self.broadcast_strict = True
        self.broadcast: dict = {"active": False, "window_id": None, "started_t": None}
        self.windows: dict[int, dict] = {}
        self.source_desc = ""


    def add_control(self, msg: ch.ControlMessage) -> None:
        with self._lock:
            self._msgs.append(msg)

    def add_tch(self, ctype: str) -> None:
        with self._lock:
            self._tch_counts[ctype] += 1

    def add_log(self, ev: dict) -> None:
        with self._lock:
            self._log.append(ev)

    def has_control(self) -> bool:
        with self._lock:
            return bool(self._msgs) or bool(self._tch_counts)

    def reset_control(self, clear_broadcast: bool = False) -> None:
        with self._lock:
            self._msgs.clear()
            self._tch_counts.clear()
            self.crc_ok_rate = None
            self.msgs_per_s = 0.0
            self.evm_median = None
            self.evm_best = None
            if clear_broadcast:
                self.broadcast = {"active": False, "window_id": None,
                                  "started_t": None}

    def broadcast_started(self, window_id: int, t: float,
                          wall_ms: int | None = None,
                          target: dict | None = None) -> None:
        with self._lock:
            self.broadcast = {"active": True, "window_id": window_id, "started_t": t}
            self.windows[window_id] = {"t_start": t, "t_end": None, "audio": None,
                                       "wall_start": wall_ms, "wall_end": None,
                                       "target": target}

    def broadcast_target(self, window_id: int, target: dict | None) -> None:
        with self._lock:
            if window_id in self.windows:
                self.windows[window_id]["target"] = target

    def broadcast_ended(self, window_id: int, t: float,
                        wall_ms: int | None = None) -> None:
        with self._lock:
            if self.broadcast.get("window_id") == window_id:
                self.broadcast = {"active": False, "window_id": None, "started_t": None}
            if window_id in self.windows:
                self.windows[window_id]["t_end"] = t
                self.windows[window_id]["wall_end"] = wall_ms

    def audio_status(self, window_id: int, status: dict) -> None:
        with self._lock:
            self.windows.setdefault(
                window_id, {"t_start": None, "t_end": None, "audio": None}
            )["audio"] = status

    def window_iq(self, window_id: int, info: dict) -> None:
        with self._lock:
            self.windows.setdefault(
                window_id, {"t_start": None, "t_end": None, "audio": None}
            )["iq"] = info

    def window_info(self, window_id: int) -> dict | None:
        with self._lock:
            w = self.windows.get(window_id)
            return dict(w) if w is not None else None


    def control_summary(self) -> dict:
        with self._lock:
            msgs = list(self._msgs)
            seed = self.seed
            muni_full = self.municipality_code
        if seed is None:
            return {"seed": None, "searching": True, "municipality": None,
                    "candidates": [], "type_counts": {}, "manufacturers": {},
                    "slot_usage": [], "broadcast_active": False,
                    "total": 0, "valid": 0, "crc_ok": 0}
        s = ch.summarize(msgs, seed, municipal_code=self.municipal_code)
        if muni_full is not None and not s.get("municipality"):
            name = ch.city_for_municipal_code(muni_full)
            s["municipality"] = (f"{name}（FACCH確定 {muni_full}）" if name
                                 else f"コード {muni_full}（FACCH確定）")
        s["type_counts"] = dict(s["type_counts"])
        s.pop("recent", None)
        return s

    def snapshot(self) -> dict:
        summary = self.control_summary()
        with self._lock:
            return {
                "type": "snapshot",
                "t": round(self.t, 1),
                "source": self.source_desc,
                "control": summary,
                "tuning": {
                    "f0_hz": self.f0_hz,
                    "center_hz": self.center_hz,
                    "seed": self.seed,
                    "municipality_code": self.municipality_code,
                },
                "broadcast": dict(self.broadcast),
                "squelch_enabled": self.squelch_enabled,
                "broadcast_strict": self.broadcast_strict,
                "windows": {str(k): dict(v) for k, v in self.windows.items()},
                "tch_counts": dict(self._tch_counts),
                "quality": {
                    "cfo_hz": self.cfo_hz,
                    "evm_median": self.evm_median,
                    "evm_best": self.evm_best,
                    "crc_ok_rate": self.crc_ok_rate,
                    "msgs_per_s": self.msgs_per_s,
                    "level_dbfs": self.level_dbfs,
                    "sync_locked": self.sync_locked,
                    "overflows": self.overflows,
                },
                "recent_log": list(self._log),
            }


__all__ = ["LiveState"]
