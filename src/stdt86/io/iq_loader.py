from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import numpy as np


class IQFormat(StrEnum):

    CU8 = "cu8"
    CF32 = "cf32"
    WAV = "wav"


_EXT_MAP: dict[str, IQFormat] = {
    ".cu8": IQFormat.CU8,
    ".bin": IQFormat.CU8,
    ".cf32": IQFormat.CF32,
    ".fc32": IQFormat.CF32,
    ".iq": IQFormat.CF32,
    ".wav": IQFormat.WAV,
}


def detect_format(path: str | Path) -> IQFormat:
    ext = Path(path).suffix.lower()
    try:
        return _EXT_MAP[ext]
    except KeyError as exc:
        raise ValueError(
            f"拡張子 '{ext}' から I/Q 形式を判別できません。fmt= で明示してください "
            f"(対応: {sorted(_EXT_MAP)})"
        ) from exc


def load_iq(
    path: str | Path,
    fmt: IQFormat | str = "auto",
    fs: float | None = None,
    max_seconds: float | None = None,
    offset_seconds: float = 0.0,
) -> tuple[np.ndarray, float]:
    path = Path(path)
    resolved = detect_format(path) if fmt == "auto" else IQFormat(fmt)

    if resolved is IQFormat.WAV:
        return _load_wav(path, fs, max_seconds=max_seconds, offset_seconds=offset_seconds)

    if fs is None:
        raise ValueError(
            f"{resolved.value} は生 I/Q なので fs（録音サンプルレート [Hz]）が必須です。"
        )
    if resolved is IQFormat.CU8:
        return _load_cu8(path), float(fs)
    return _load_cf32(path), float(fs)


def _load_cu8(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    iq = raw.astype(np.float32)
    iq = (iq - 127.5) / 127.5
    return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)


def _load_cf32(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.float32)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def _load_wav(
    path: Path, fs: float | None,
    max_seconds: float | None = None, offset_seconds: float = 0.0,
) -> tuple[np.ndarray, float]:
    import soundfile as sf

    if max_seconds is not None or offset_seconds:
        info = sf.info(path)
        start = int(offset_seconds * info.samplerate)
        frames = int(max_seconds * info.samplerate) if max_seconds else -1
        data, file_fs = sf.read(path, dtype="float32", always_2d=True,
                                start=start, frames=frames)
    else:
        data, file_fs = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] < 2:
        raise ValueError("WAV は I=L, Q=R のステレオが必要です（モノラルは非対応）。")
    if fs is not None and abs(file_fs - fs) > 1e-6:
        raise ValueError(f"WAV のヘッダレート {file_fs} Hz と指定 fs {fs} Hz が不一致です。")
    samples = (data[:, 0] + 1j * data[:, 1]).astype(np.complex64)
    return samples, float(file_fs)
