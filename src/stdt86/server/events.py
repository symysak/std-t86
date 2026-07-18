from __future__ import annotations

import numpy as np

from stdt86.control.channel import ControlMessage


def control_msg_event(t: float, pos: int, msg: ControlMessage) -> dict:
    return {
        "type": "control_msg",
        "t": round(t, 3),
        "pos": pos,
        "msg_type": msg.msg_type,
        "name": msg.msg_type_name,
        "channel": msg.channel,
        "crc_ok": msg.crc_ok,
        "raw_hex": msg.raw_hex,
        "fields": {k: v for k, v in msg.fields.items()
                   if isinstance(v, (str, int, float, bool, list))},
    }


def tch_second_event(t: float, counts: dict[str, int]) -> dict:
    return {"type": "tch_second", "t": round(t, 1), "counts": counts}


def broadcast_event(kind: str, t: float, window_id: int,
                    wall_ms: int | None = None,
                    target: dict | None = None) -> dict:
    return {"type": f"broadcast_{kind}", "t": round(t, 3), "window_id": window_id,
            "wall_ms": wall_ms, "target": target}


def quality_event(t: float, *, cfo_hz: float | None, evm_median: float | None,
                  evm_best: float | None, crc_ok_rate: float | None,
                  msgs_per_s: float, level_dbfs: float | None,
                  sync_locked: bool, overflows: int) -> dict:
    return {
        "type": "quality",
        "t": round(t, 1),
        "cfo_hz": None if cfo_hz is None else round(cfo_hz, 1),
        "evm_median": None if evm_median is None else round(evm_median, 1),
        "evm_best": None if evm_best is None else round(evm_best, 1),
        "crc_ok_rate": None if crc_ok_rate is None else round(crc_ok_rate, 3),
        "msgs_per_s": round(msgs_per_s, 1),
        "level_dbfs": None if level_dbfs is None else round(level_dbfs, 1),
        "sync_locked": sync_locked,
        "overflows": overflows,
    }


def constellation_event(t: float, slots: list[np.ndarray],
                        max_points: int = 600) -> dict:
    if slots:
        syms = np.concatenate(slots)
    else:
        syms = np.zeros(0, dtype=np.complex64)
    if len(syms) > max_points:
        idx = np.linspace(0, len(syms) - 1, max_points).astype(int)
        syms = syms[idx]
    pts = np.stack([syms.real, syms.imag], axis=1) if len(syms) else np.zeros((0, 2))
    return {
        "type": "constellation",
        "t": round(t, 1),
        "points": np.round(pts, 3).tolist(),
    }


def audio_status_event(window_id: int, *, frames: int, crc7_ok: int,
                       decoded_seconds: float, decode_attempted: bool,
                       note: str = "", wav_path: str | None = None,
                       filled: int = 0) -> dict:
    return {
        "type": "audio_status",
        "window_id": window_id,
        "frames": frames,
        "crc7_ok": crc7_ok,
        "filled": filled,
        "decoded_seconds": round(decoded_seconds, 1),
        "decode_attempted": decode_attempted,
        "note": note,
        "wav_path": wav_path,
    }


def log_event(t: float, text: str) -> dict:
    return {"type": "log", "t": round(t, 2), "text": text}


__all__ = [
    "audio_status_event",
    "broadcast_event",
    "control_msg_event",
    "log_event",
    "quality_event",
    "tch_second_event",
]
