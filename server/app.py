"""Local server for the tablet drawing pipeline.

Routes:
    GET  /            redirect to the tablet canvas
    GET  /canvas      the tablet drawing surface
    GET  /viewer      optional desktop view; nothing depends on it being open
    POST /api/capture receive a finished drawing
    GET  /api/config  client settings (idle timeout, background)
    GET  /api/captures recent capture metadata
    GET  /captures/*  the stored PNG and stroke files
    WS   /ws          live channel, shared by tablet and viewers
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .storage import store

app = FastAPI(title="Ink Pipeline")

CLIENT_VERSION = "2"


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Make the tablet revalidate the page and its assets on every load.

    Without this a tablet can sit on cached JavaScript for hours: the socket
    reconnects by itself after a server restart, so the page looks perfectly
    healthy while running code that predates the restart.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path in ("/", "/canvas", "/viewer"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


class Hub:
    """Fan-out of live events to whoever happens to be listening.

    Viewers are strictly optional. With nobody connected this degrades to a
    no-op, which is why the capture path never waits on it.
    """

    def __init__(self) -> None:
        self._viewers: set[WebSocket] = set()
        self._tablets: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, socket: WebSocket, role: str) -> None:
        async with self._lock:
            (self._tablets if role == "tablet" else self._viewers).add(socket)

    async def leave(self, socket: WebSocket) -> None:
        async with self._lock:
            self._viewers.discard(socket)
            self._tablets.discard(socket)

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @property
    def tablet_count(self) -> int:
        return len(self._tablets)

    async def to_viewers(self, message: dict[str, Any]) -> None:
        await self._send(list(self._viewers), message)

    async def to_tablets(self, message: dict[str, Any]) -> None:
        """Unused in step 1. Step 2 pushes synthesized speech through here."""
        await self._send(list(self._tablets), message)

    async def _send(self, sockets: list[WebSocket], message: dict[str, Any]) -> None:
        if not sockets:
            return
        payload = json.dumps(message)
        dead = []
        for socket in sockets:
            try:
                await socket.send_text(payload)
            except Exception:  # noqa: BLE001 - a dropped client is routine
                dead.append(socket)
        for socket in dead:
            await self.leave(socket)


hub = Hub()

# Questions waiting on a tap from the tablet, keyed by question id.
pending_questions: dict[str, asyncio.Future[str]] = {}


async def ask_tablet(question: str, timeout: float = 120.0, context: dict[str, Any] | None = None) -> str:
    """Put a yes/no question on the tablet and wait for the tap answer.

    This is the call step 2 makes to confirm an API result. Raises TimeoutError
    if nobody answers, and RuntimeError if no tablet is connected to ask.
    """
    if hub.tablet_count == 0:
        raise RuntimeError("no tablet connected")

    question_id = uuid4().hex[:12]
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    pending_questions[question_id] = future

    message = {"type": "ask", "id": question_id, "question": question, "context": context or {}}
    await hub.to_tablets(message)
    await hub.to_viewers(message)

    try:
        return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        cancel = {"type": "ask_cancel", "id": question_id}
        await hub.to_tablets(cancel)
        await hub.to_viewers(cancel)
        raise
    finally:
        pending_questions.pop(question_id, None)


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/canvas")


@app.get("/canvas")
async def canvas_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "canvas.html")


@app.get("/viewer")
async def viewer_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "viewer.html")


@app.get("/api/config")
async def client_config() -> dict[str, Any]:
    return {
        "idle_timeout_ms": config.IDLE_TIMEOUT_MS,
        "background": config.CANVAS_BACKGROUND,
        "tap_window_ms": config.TAP_WINDOW_MS,
        "tap_always_listen": config.TAP_ALWAYS_LISTEN,
    }


@app.post("/api/ask")
async def ask(payload: dict[str, Any] = Body(default={})) -> JSONResponse:
    """Ask the tablet a yes/no question and block until it is tapped out."""
    question = str(payload.get("question", "")).strip()
    timeout = float(payload.get("timeout", 120))

    try:
        answer = await ask_tablet(question, timeout, payload.get("context"))
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "no answer before timeout"}, status_code=408)

    return JSONResponse({"ok": True, "question": question, "answer": answer})


@app.get("/api/answers")
async def list_answers(limit: int = 20) -> dict[str, Any]:
    return {"answers": store.recent_answers(limit)}


@app.get("/api/captures")
async def list_captures(limit: int = 50) -> dict[str, Any]:
    return {"captures": store.recent(limit)}


@app.post("/api/capture")
async def create_capture(
    image: UploadFile = File(...),
    strokes: str = Form("{}"),
    meta: str = Form("{}"),
) -> JSONResponse:
    png = await image.read()
    if not png:
        return JSONResponse({"error": "empty image"}, status_code=400)

    try:
        stroke_data = json.loads(strokes)
        meta_data = json.loads(meta)
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"bad json: {exc}"}, status_code=400)

    # Disk write happens first and synchronously; the broadcast is best-effort.
    record = await asyncio.to_thread(store.save, png, stroke_data, meta_data)
    await hub.to_viewers({"type": "capture", "record": record.as_dict()})

    print(f"[capture] {record.id}.png  {record.stroke_count} strokes  {record.png_bytes:,} bytes")
    return JSONResponse({"ok": True, "record": record.as_dict()})


async def handle_answer(message: dict[str, Any]) -> None:
    """A tap answer arrived from the tablet."""
    value = message.get("value")
    if value not in ("yes", "no"):
        return

    question_id = str(message.get("id", ""))
    question = str(message.get("question", ""))
    await asyncio.to_thread(
        store.save_answer,
        question_id,
        question,
        value,
        str(message.get("session_id", "unknown")),
        message.get("context"),
    )

    future = pending_questions.get(question_id)
    if future and not future.done():
        future.set_result(value)

    print(f"[answer]  {value.upper():<3} {question or '(no question text)'}")


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket, role: str = "viewer") -> None:
    await socket.accept()
    await hub.join(socket, role)
    try:
        if role == "tablet":
            await socket.send_text(json.dumps({
                "type": "welcome",
                "viewers": hub.viewer_count,
                "version": CLIENT_VERSION,
            }))
        while True:
            raw = await socket.receive_text()
            if role != "tablet":
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if message.get("type") == "answer":
                await handle_answer(message)

            # Live stroke mirroring. Relayed untouched; if no viewer is open it
            # is dropped, which costs nothing.
            await hub.to_viewers(message)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(socket)


config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/captures", StaticFiles(directory=config.CAPTURE_DIR), name="captures")
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
