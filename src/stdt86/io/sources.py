from __future__ import annotations

import socket
import struct
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import numpy as np

_RTL_SET_FREQ = 0x01
_RTL_SET_SAMPLE_RATE = 0x02
_RTL_SET_GAIN_MODE = 0x03
_RTL_SET_AGC_MODE = 0x08


def _cu8_to_complex(raw: bytes) -> np.ndarray:
    x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    x = (x - 127.5) / 127.5
    return (x[0::2] + 1j * x[1::2]).astype(np.complex64)


def _cf32_to_complex(raw: bytes) -> np.ndarray:
    x = np.frombuffer(raw, dtype=np.float32)
    return (x[0::2] + 1j * x[1::2]).astype(np.complex64)


def _cs16_to_complex(raw: bytes) -> np.ndarray:
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return (x[0::2] + 1j * x[1::2]).astype(np.complex64)


_FORMATS = {
    "cu8": (2, _cu8_to_complex),
    "cs16": (4, _cs16_to_complex),
    "cf32": (8, _cf32_to_complex),
}


class IQSource(Protocol):

    fs: float
    lossy: bool

    def read(self, n: int) -> np.ndarray | None:
        ...

    def close(self) -> None: ...


class _SocketSource:

    def __init__(self, host: str, port: int, fs: float, fmt: str = "cu8",
                 timeout: float = 10.0) -> None:
        if fmt not in _FORMATS:
            raise ValueError(f"fmt は {'/'.join(_FORMATS)}（{fmt} 受領）")
        self.fs = float(fs)
        self.lossy = True
        self._bps, self._conv = _FORMATS[fmt]
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._pending = b""

    def _recv_exact(self, nbytes: int) -> bytes | None:
        buf = bytearray(self._pending)
        self._pending = b""
        while len(buf) < nbytes:
            try:
                chunk = self._sock.recv(min(65536, nbytes - len(buf)))
            except (TimeoutError, OSError):
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def read(self, n: int) -> np.ndarray | None:
        raw = self._recv_exact(n * self._bps)
        if raw is None:
            return None
        return self._conv(raw)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class RawTcpSource(_SocketSource):
    pass


class RtlTcpSource(_SocketSource):

    def __init__(self, host: str, port: int, fs: float,
                 freq_hz: float | None = None, agc: bool = True,
                 timeout: float = 10.0) -> None:
        super().__init__(host, port, fs, fmt="cu8", timeout=timeout)
        header = self._recv_exact(12)
        if header is None or header[:4] != b"RTL0":
            self.close()
            raise ConnectionError("rtl_tcp ヘッダ（RTL0）を受信できませんでした。")
        self.tuner_type = struct.unpack(">I", header[4:8])[0]
        self.tuner_gain_count = struct.unpack(">I", header[8:12])[0]
        self.center_hz = float(freq_hz) if freq_hz is not None else None
        self._cmd(_RTL_SET_SAMPLE_RATE, int(fs))
        if freq_hz is not None:
            self._cmd(_RTL_SET_FREQ, int(freq_hz))
        self._cmd(_RTL_SET_GAIN_MODE, 0 if agc else 1)
        self._cmd(_RTL_SET_AGC_MODE, 1 if agc else 0)

    def _cmd(self, cmd: int, value: int) -> None:
        self._sock.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))


class FileReplaySource:

    def __init__(self, path: str | Path, fs: float | None = None,
                 fmt: str = "auto", realtime: bool = True, speed: float = 1.0,
                 loop: bool = False) -> None:
        from stdt86.io.iq_loader import load_iq

        self._samples, self.fs = load_iq(path, fmt=fmt, fs=fs)
        self.lossy = False
        self.realtime = realtime
        self.speed = speed
        self.loop = loop
        self._pos = 0
        self._t0: float | None = None
        self._sent = 0
        self._closed = False

    def read(self, n: int) -> np.ndarray | None:
        if self._closed:
            return None
        if self._pos >= len(self._samples):
            if not self.loop:
                return None
            self._pos = 0
        end = min(self._pos + n, len(self._samples))
        out = self._samples[self._pos: end]
        self._pos = end
        if self.realtime:
            if self._t0 is None:
                self._t0 = time.monotonic()
            self._sent += len(out)
            due = self._t0 + self._sent / (self.fs * self.speed)
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return out

    def close(self) -> None:
        self._closed = True
        self._samples = np.zeros(0, dtype=np.complex64)


def open_source(spec: str, fs: float | None = None, freq_hz: float | None = None,
                fmt: str = "auto", realtime: bool = True,
                speed: float = 1.0) -> IQSource:
    if spec.startswith(("rtltcp://", "tcp://")):
        u = urlparse(spec)
        if u.hostname is None or u.port is None:
            raise ValueError(f"ソース URI に host:port が必要です: {spec}")
        if fs is None:
            raise ValueError("ネットワークソースには --fs（サンプルレート）が必須です。")
        if spec.startswith("rtltcp://"):
            return RtlTcpSource(u.hostname, u.port, fs, freq_hz=freq_hz)
        return RawTcpSource(u.hostname, u.port, fs,
                            fmt="cf32" if fmt == "auto" else fmt)
    return FileReplaySource(spec, fs=fs, fmt=fmt, realtime=realtime, speed=speed)


__all__ = [
    "FileReplaySource",
    "IQSource",
    "RawTcpSource",
    "RtlTcpSource",
    "open_source",
]
