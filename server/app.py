"""Local server for the tablet drawing pipeline.

Routes:
    GET  /            redirect to the tablet canvas
    GET  /canvas      the tablet drawing surface
    GET  /viewer      optional desktop view; nothing depends on it being open
    GET  /brain       second-brain memory graph for judges and caregivers
    POST /api/capture receive a finished drawing
    GET  /api/config  client settings (idle timeout, background)
    GET  /api/captures recent capture metadata
    GET  /captures/*  the stored PNG and stroke files
    GET  /tts/*       synthesised speech, served so the API key stays here
    WS   /ws          live channel, shared by tablet and viewers
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from typing import Any

from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, pipeline, shape_game, speech
from .storage import CaptureRecord, store

# Background tasks are held here; asyncio only keeps weak references, so a task
# without one can be garbage collected mid-flight.
_background: set[asyncio.Task[Any]] = set()


def schedule(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# The recognition and memory modules live at the repository root rather than in
# this package, so the root has to be importable however the server was started.
if str(config.ROOT) not in sys.path:
    sys.path.insert(0, str(config.ROOT))
from memory_graph import MemoryGraph  # noqa: E402

app = FastAPI(title="Ink Pipeline")

CLIENT_VERSION = "9"


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Make the tablet revalidate the page and its assets on every load.

    Without this a tablet can sit on cached JavaScript for hours: the socket
    reconnects by itself after a server restart, so the page looks perfectly
    healthy while running code that predates the restart.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path in ("/", "/canvas", "/viewer", "/brain"):
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
        """Push speech and questions to connected tablets."""
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


@app.get("/brain")
async def brain_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "brain.html")


def _history_graph() -> MemoryGraph:
    graph = MemoryGraph(config.ROOT / "memory_graph.json")
    graph.ensure_seed(config.ROOT)
    return graph


def _daily_graph() -> MemoryGraph:
    day = date.today()
    path = config.ROOT / "monthly_events" / day.strftime("%Y-%m") / day.isoformat() / "daily_graph.json"
    graph = MemoryGraph(path)
    graph.ensure_seed(config.ROOT)
    return graph


@app.get("/api/memory-graph")
async def memory_graph_payload(scope: str = "history") -> dict[str, Any]:
    if scope == "daily":
        graph = _daily_graph()
        payload = graph.vis_payload(on=date.today())
        payload["scope"] = "daily"
        return payload
    graph = _history_graph()
    payload = graph.vis_payload()
    payload["scope"] = "history"
    return payload


@app.post("/api/memory-graph/demo")
async def memory_graph_demo() -> dict[str, Any]:
    history = _history_graph()
    daily = _daily_graph()
    history.seed_demo_week()
    daily.seed_demo_day()
    payload = {
        "daily": daily.vis_payload(on=date.today()),
        "history": history.vis_payload(),
    }
    await hub.to_viewers({"type": "graph", "daily": payload["daily"], "history": payload["history"]})
    return payload


@app.get("/api/config")
async def client_config() -> dict[str, Any]:
    return {
        "idle_timeout_ms": config.IDLE_TIMEOUT_MS,
        "background": config.CANVAS_BACKGROUND,
        "tap_window_ms": config.TAP_WINDOW_MS,
        "tap_always_listen": config.TAP_ALWAYS_LISTEN,
        "smoothing": config.SMOOTHING,
        "smoothing_params": config.SMOOTHING_PRESETS[config.SMOOTHING],
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

    # Step 2 runs after this response goes back, so a slow API call cannot hold
    # up the tablet or risk its upload timing out.
    schedule(analyse(record, stroke_data.get("strokes", [])))

    return JSONResponse({"ok": True, "record": record.as_dict()})


async def analyse(record: CaptureRecord, strokes: list[dict[str, Any]]) -> None:
    payload = pipeline.build_payload(strokes)
    image = config.CAPTURE_DIR / record.png

    if shape_game.get(record.session_id):
        await play_round(record, image)
        return

    try:
        raw = await asyncio.to_thread(pipeline.process_capture, image, payload)
    except Exception as exc:  # noqa: BLE001 - a broken analysis must not kill the server
        print(f"[analysis] {record.id} failed: {exc!r}")
        return

    result = pipeline.as_capture_result(raw)
    if not result or not result.text:
        return

    print(f"[analysis] {record.id}: {result.text}")
    await hub.to_viewers({"type": "analysis", "id": record.id, "text": result.text})
    if result.tag_id == "play":
        await offer_game(record, result)
        return
    await converse(record, result)


async def offer_game(record: CaptureRecord, result: pipeline.CaptureResult) -> None:
    """A handwritten 1 or 2 can start the shape-drawing game."""
    answer = await speak_and_ask(
        record,
        result.text,
        "Play a game?",
        tag_id="play",
    )
    if answer != "yes":
        if answer == "no":
            await speak_only(record, "Okay.")
        await persist_analysis(record, result, confirmed=False)
        await push_game(None)
        return

    game = shape_game.start(record.session_id, digit=result.detail or "1")
    print(f"[game] {record.session_id} start {game.target} (digit {result.detail})")
    await speak_game(record, game.prompt(), game.target)
    await persist_analysis(record, result, confirmed=True)


async def play_round(record: CaptureRecord, image: Any) -> None:
    game = shape_game.get(record.session_id)
    if game is None:
        return
    target = game.target
    graded = await asyncio.to_thread(pipeline.grade_shape, image, target)
    if graded["match"]:
        spoken, done = shape_game.succeed(record.session_id)
        print(f"[game] {record.session_id} matched {target}")
    else:
        spoken, done = shape_game.fail(record.session_id, graded.get("spoken") or "")
        print(f"[game] {record.session_id} miss {target}  {spoken}")

    next_target = None if done else (shape_game.get(record.session_id).target if shape_game.get(record.session_id) else None)
    await speak_game(record, spoken, next_target)
    await persist_analysis(
        record,
        pipeline.CaptureResult(
            text=spoken,
            tag_id="game",
            detail=target,
        ),
        confirmed=graded["match"],
    )
    await hub.to_viewers({"type": "analysis", "id": record.id, "text": spoken})


async def speak_game(record: CaptureRecord, text: str, target: str | None) -> None:
    await speak_only(record, text)
    await push_game(target)


async def push_game(target: str | None) -> None:
    await hub.to_tablets({"type": "game", "target": target})
    await hub.to_viewers({"type": "game", "target": target})


async def push_graphs() -> None:
    """Tell any open /brain page to redraw from disk."""
    daily = await asyncio.to_thread(lambda: _daily_graph().vis_payload(on=date.today()))
    history = await asyncio.to_thread(lambda: _history_graph().vis_payload())
    daily["scope"] = "daily"
    history["scope"] = "history"
    await hub.to_viewers({"type": "graph", "daily": daily, "history": history})


async def converse(record: CaptureRecord, result: pipeline.CaptureResult) -> None:
    """Speak a guess, confirm it, retry a couple of times, then write memory."""
    candidates = list(result.candidates)
    guesses = candidates[: pipeline.MAX_GUESSES] if candidates else [None]
    accepted = False
    spoken = result.text

    for index, candidate in enumerate(guesses):
        if candidate is not None and index > 0:
            image = config.CAPTURE_DIR / record.png
            spoken = await asyncio.to_thread(
                pipeline.spoken_for,
                candidate.tag_id,
                reason=candidate.reason or "previous guess was rejected",
                label=candidate.label,
                image=image,
                prepared=getattr(candidate, "spoken", "") or "",
            )
            result.text = spoken
            result.tag_id = candidate.tag_id
            result.reason = candidate.reason
            raw_detail = (getattr(candidate, "detail", None) or "").strip() or None
            result.detail = raw_detail if pipeline.is_specific(raw_detail, candidate.tag_id) else None
            if result.recognition is not None:
                result.recognition.top_tag = candidate.tag_id
                result.recognition.spoken = spoken
            await hub.to_viewers({"type": "analysis", "id": record.id, "text": spoken})

        question = "Did I get that right?" if index == 0 else "Is this better?"
        answer = await speak_and_ask(record, spoken, question, tag_id=result.tag_id)
        if answer is None:
            await persist_analysis(record, result, confirmed=False)
            return
        if answer == "yes":
            accepted = True
            break
        print(f"[analysis] {record.id} rejected guess {index + 1}: {spoken}")

    if accepted:
        await refine_details(record, result)
        if (result.detail or "").lower() in {"call", "caretaker"}:
            await place_caretaker_call(record, result)
        else:
            closing = await asyncio.to_thread(
                pipeline.closing_for,
                result.tag_id,
                detail=result.detail,
                spoken=result.text,
            )
            if closing:
                print(f"[analysis] {record.id} closing: {closing}")
                await speak_only(record, closing)

    if not accepted and candidates:
        spoken = "I am not sure. Could you draw that again?"
        result.text = spoken
        await speak_only(record, spoken)

    journal = ""
    try:
        journal = await asyncio.to_thread(
            pipeline.confirm_memory,
            result,
            accepted=accepted,
            capture_id=record.id,
        )
        await persist_analysis(record, result, confirmed=accepted)
        await push_graphs()
    except Exception as exc:  # noqa: BLE001
        print(f"[analysis] {record.id} memory update failed: {exc!r}")

    if accepted and journal:
        schedule(patch_graphs_later(record, journal))

    print(f"[analysis] {record.id} confirmed: {'yes' if accepted else 'no'}")


async def refine_details(record: CaptureRecord, result: pipeline.CaptureResult) -> None:
    """After a yes, ask only what a real assistant would still need to know."""
    if not pipeline.needs_followup(result.tag_id, result.text, result.detail):
        return

    if pipeline.is_specific(result.detail, result.tag_id):
        followups = [pipeline.followup_from_detail(result.detail)]
    else:
        seen = ""
        if result.recognition is not None:
            seen = getattr(result.recognition, "seen", "") or ""
        followups = await asyncio.to_thread(
            pipeline.follow_ups_for,
            result.tag_id,
            spoken=result.text,
            seen=seen,
            image=config.CAPTURE_DIR / record.png,
        )
    if not followups:
        return

    print(f"[analysis] {record.id} follow-ups: "
          + ", ".join(item["detail"] for item in followups))

    for item in followups:
        line = item.get("spoken") or item["question"]
        await hub.to_viewers({"type": "analysis", "id": record.id, "text": line})
        answer = await speak_and_ask(
            record,
            line,
            item["question"],
            tag_id=result.tag_id,
        )
        if answer is None:
            return
        if answer == "yes":
            result.text = line
            result.detail = item["detail"]
            print(f"[analysis] {record.id} detail: {item['detail']}")
            return
        print(f"[analysis] {record.id} not {item['detail']}")


async def place_caretaker_call(record: CaptureRecord, result: pipeline.CaptureResult) -> None:
    info = pipeline.caretaker()
    name = str(info.get("name") or "your caretaker").strip()
    phone = str(info.get("phone") or "").strip()
    relation = str(info.get("relation") or "").strip()
    spoken = f"Calling {name} now."
    print(f"[call] {name}  {phone or '(no number)'}")
    await speak_only(record, spoken)
    await hub.to_tablets({
        "type": "call",
        "name": name,
        "phone": phone,
        "relation": relation,
    })
    await hub.to_viewers({
        "type": "call",
        "name": name,
        "phone": phone,
        "relation": relation,
        "id": record.id,
    })
    result.text = spoken


async def patch_graphs_later(record: CaptureRecord, journal: str) -> None:
    """LLM graph links run after the tablet has already heard the closing."""
    try:
        await asyncio.to_thread(
            pipeline.patch_graphs, capture_id=record.id, journal=journal
        )
        await push_graphs()
    except Exception as exc:  # noqa: BLE001
        print(f"[recognize] graph patch skipped: {exc!r}")


async def persist_analysis(
    record: CaptureRecord,
    result: pipeline.CaptureResult,
    *,
    confirmed: bool,
) -> None:
    await asyncio.to_thread(
        store.update_analysis,
        record.id,
        {
            "text": result.text,
            "tag": result.tag_id,
            "detail": result.detail,
            "confirmed": confirmed,
        },
    )


async def speak_only(record: CaptureRecord, text: str) -> None:
    audio = await asyncio.to_thread(speech.synthesise, text)
    await hub.to_tablets({
        "type": "speak",
        "id": record.id,
        "text": text,
        "url": f"/tts/{audio.name}" if audio else None,
    })


async def speak_and_ask(
    record: CaptureRecord,
    text: str,
    question: str,
    *,
    tag_id: str | None = None,
) -> str | None:
    await speak_only(record, text)
    try:
        return await ask_tablet(
            question,
            timeout=config.CONFIRM_TIMEOUT_S,
            context={"capture": record.id, "text": text, "tag": tag_id},
        )
    except RuntimeError:
        print(f"[analysis] {record.id} spoken to nobody: no tablet connected")
        return None
    except asyncio.TimeoutError:
        print(f"[analysis] {record.id} left unconfirmed")
        return None


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
        else:
            await socket.send_text(json.dumps({"type": "welcome", "role": role}))
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
config.TTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/captures", StaticFiles(directory=config.CAPTURE_DIR), name="captures")
app.mount("/tts", StaticFiles(directory=config.TTS_DIR), name="tts")
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
