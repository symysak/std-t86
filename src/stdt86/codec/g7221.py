from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

BITS_PER_FRAME_16K = 320
SAMPLE_RATE = 16000


def _binary(name: str) -> str:
    env = os.environ.get("STDT86_G7221_DIR")
    names = [name, name + ".exe"]
    candidates: list[Path] = []
    dirs = []
    if env:
        dirs.append(Path(env))
    dirs.append(Path(__file__).resolve().parents[3] / "build" / "g7221")
    for d in dirs:
        candidates += [d / n for n in names]
    for n in names:
        which = shutil.which(n)
        if which:
            candidates.append(Path(which))
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        f"{name} が見つかりません。`bash scripts/build_g7221.sh`"
        f"（Windows は `pwsh scripts/build_g7221.ps1`）を実行してください "
        f"(または STDT86_G7221_DIR を設定)。"
    )


def frames_to_packed(frames_bits: np.ndarray) -> bytes:
    frames_bits = np.asarray(frames_bits, dtype=np.uint8)
    if frames_bits.ndim != 2 or frames_bits.shape[1] != BITS_PER_FRAME_16K:
        raise ValueError(f"frames は (n, {BITS_PER_FRAME_16K}) が必要です。")
    words = frames_bits.reshape(-1, 16)
    packed = np.zeros(len(words), dtype="<u2")
    for k in range(16):
        packed |= words[:, k].astype("<u2") << (15 - k)
    return packed.tobytes()


def decode(frames_bits: np.ndarray, rate: int = 16000, scodec: bool = False) -> np.ndarray:
    packed = frames_to_packed(frames_bits)
    dec = _binary("g7221_sep_decode" if scodec else "g7221_decode")
    env = dict(os.environ)
    if scodec:
        env["STDT86_SCODEC"] = "1"
    with tempfile.TemporaryDirectory() as d:
        bit = Path(d) / "in.bit"
        pcm = Path(d) / "out.pcm"
        bit.write_bytes(packed)
        subprocess.run(
            [dec, "0", str(bit), str(pcm), str(rate), "7000"],
            check=True,
            capture_output=True,
            env=env,
        )
        raw = np.fromfile(pcm, dtype="<i2")
    return raw.astype(np.float32) / 32768.0


def adaptive_multiplex(frames_bits: np.ndarray) -> np.ndarray:
    frames_bits = np.asarray(frames_bits, dtype=np.uint8)
    packed = frames_to_packed(frames_bits)
    dec = _binary("g7221_sep_decode")
    env = dict(os.environ)
    env["STDT86_SCODEC"] = "2"
    with tempfile.TemporaryDirectory() as d:
        bit = Path(d) / "in.bit"
        pcm = Path(d) / "out.pcm"
        mux = Path(d) / "mux.txt"
        env["STDT86_MUX_OUT"] = str(mux)
        bit.write_bytes(packed)
        subprocess.run(
            [dec, "0", str(bit), str(pcm), str(SAMPLE_RATE), "7000"],
            check=True,
            capture_output=True,
            env=env,
        )
        lines = mux.read_text().splitlines()
    mi = np.array([[int(c) for c in ln] for ln in lines], dtype=np.uint8)
    if mi.shape != frames_bits.shape:
        raise RuntimeError(f"適応多重化の出力形状が不正: {mi.shape} != {frames_bits.shape}")
    return mi


def encode(pcm: np.ndarray, rate: int = 16000) -> np.ndarray:
    pcm = np.asarray(pcm)
    if pcm.dtype != np.int16:
        pcm = np.clip(pcm, -1.0, 1.0)
        pcm = (pcm * 32767).astype("<i2")
    enc = _binary("g7221_encode")
    with tempfile.TemporaryDirectory() as d:
        pin = Path(d) / "in.pcm"
        bout = Path(d) / "out.bit"
        pcm.astype("<i2").tofile(pin)
        subprocess.run(
            [enc, "0", str(pin), str(bout), str(rate), "7000"],
            check=True,
            capture_output=True,
        )
        words = np.fromfile(bout, dtype="<u2")
    nbits = rate // 50
    bits = np.zeros((len(words), 16), dtype=np.uint8)
    for k in range(16):
        bits[:, k] = (words >> (15 - k)) & 1
    return bits.reshape(-1, nbits)
