from __future__ import annotations

import asyncio
import contextlib
import queue
import struct
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from stdt86.server.pipeline import Pipeline

_STATIC = Path(__file__).parent / "static"


class _Hub:

    def __init__(self, maxsize: int = 512) -> None:
        self.maxsize = maxsize
        self.clients: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self.clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def publish(self, item) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(item)


async def _drain(src: queue.Queue, hub: _Hub, stop: asyncio.Event,
                 transform=None) -> None:
    loop = asyncio.get_running_loop()

    def _get():
        try:
            return src.get(timeout=0.5)
        except queue.Empty:
            return _get

    while not stop.is_set():
        item = await loop.run_in_executor(None, _get)
        if item is _get:
            continue
        hub.publish(transform(item) if transform else item)


def create_app(pipeline: Pipeline) -> FastAPI:
    hub = _Hub()
    audio_hub = _Hub(maxsize=64)
    stop = asyncio.Event()
    _audio_seq = {"n": 0}

    def _pcm_frame(item: tuple[int, np.ndarray]) -> bytes:
        wid, pcm = item
        _audio_seq["n"] += 1
        return struct.pack("<II", wid, _audio_seq["n"]) + pcm.tobytes()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.start()
        tasks = [
            asyncio.create_task(_drain(pipeline.event_q, hub, stop)),
            asyncio.create_task(_drain(pipeline.pcm_q, audio_hub, stop,
                                       transform=_pcm_frame)),
        ]
        yield
        stop.set()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        pipeline.stop()

    app = FastAPI(title="STD-T86 live receiver", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html",
                            headers={"Cache-Control": "no-cache"})

    def _snapshot() -> dict:
        return pipeline.state.snapshot()

    @app.get("/api/status")
    async def status():
        return JSONResponse(_snapshot())

    @app.post("/api/squelch")
    async def set_squelch(enabled: bool):
        return JSONResponse({"squelch_enabled": pipeline.set_squelch(enabled)})

    @app.post("/api/broadcast_strict")
    async def set_broadcast_strict(enabled: bool):
        return JSONResponse(
            {"broadcast_strict": pipeline.set_broadcast_strict(enabled)})

    @app.post("/api/cfo/reset")
    async def reset_cfo():
        pipeline.request_cfo_reset()
        return JSONResponse({"ok": True})

    @app.get("/api/audio/{window_id}.wav")
    async def audio_wav(window_id: int):
        pcm = pipeline.audio.window_pcm(window_id)
        if pcm is None:
            return JSONResponse({"error": "この通報ウィンドウの音声はありません"},
                                status_code=404)
        from stdt86.server.audio import pcm16_wav_bytes

        return Response(pcm16_wav_bytes(pcm), media_type="audio/wav")

    @app.websocket("/ws")
    async def ws_events(ws: WebSocket):
        await ws.accept()
        await ws.send_json(_snapshot())
        q = hub.register()
        try:
            while True:
                ev = await q.get()
                await ws.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unregister(q)

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket):
        await ws.accept()
        q = audio_hub.register()
        try:
            while True:
                frame = await q.get()
                await ws.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            audio_hub.unregister(q)

    return app


__all__ = ["create_app"]
