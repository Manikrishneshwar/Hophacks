"""Check that a capture is spoken on the tablet and confirmed with a tap.

    python scripts/speech_test.py

Runs the pipeline twice over. First with a stub standing in for the ElevenLabs
API, to prove the server synthesises the sentence, caches it, serves it to the
tablet and keeps the key to itself. Then with no key at all, to prove the phone
falls back to its own voice engine rather than going quiet.

Playback itself is stubbed inside the page. Headless Chromium's media stack is
not what is under test, and satisfying its decoder would mean shipping a real
MP3 just to hear silence.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.pipeline import SAMPLE_TEXT  # noqa: E402

API_KEY = "sk-not-a-real-key"
VOICE = "voice-under-test"

# Recording HTMLMediaElement.play instead of letting it run, and speechSynthesis
# alongside it, is what makes the two paths distinguishable from the outside.
STUB_PLAYBACK = """
window.__played = [];
window.__spoken = [];
HTMLMediaElement.prototype.play = function () {
  window.__played.push(this.src);
  return Promise.resolve();
};
if (window.speechSynthesis) {
  window.speechSynthesis.speak = (utterance) => window.__spoken.push(utterance.text);
}
"""


class FakeElevenLabs(BaseHTTPRequestHandler):
    """Answers the text-to-speech endpoint and remembers what it was asked."""

    calls: list[dict[str, str]] = []
    audio = b"ID3\x04\x00\x00\x00" + b"\x00" * 4096

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeElevenLabs.calls.append({
            "path": self.path,
            "key": self.headers.get("xi-api-key", ""),
            "text": body.get("text", ""),
            "model": body.get("model_id", ""),
        })
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(self.audio)))
        self.end_headers()
        self.wfile.write(self.audio)

    def log_message(self, *args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_server(env: dict[str, str], port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def wait_for_server(base: str) -> None:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{base}/api/config", timeout=2).read()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)


def get_json(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def draw_and_speak(page, base: str, y: int) -> dict | None:
    """Draw one stroke, send it, and return what the tablet ended up speaking."""
    page.evaluate("ink.speech = null")
    before = len(get_json(f"{base}/api/captures")["captures"])

    page.mouse.move(150, y)
    page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(150 + i * 40, y)
    page.mouse.up()
    page.click("#send")

    # The canvas clears itself before the upload leaves, so wait on the server.
    for _ in range(60):
        if len(get_json(f"{base}/api/captures")["captures"]) > before:
            break
        time.sleep(0.25)

    try:
        page.wait_for_function("ink.speech !== null", timeout=20000)
    except Exception:  # noqa: BLE001 - reported as a failure by the caller
        return None
    return page.evaluate("ink.speech")


def tap_yes(page) -> None:
    """One quick, still contact on the empty canvas, then let the window settle."""
    page.mouse.move(500, 400)
    page.mouse.down()
    page.mouse.up()
    time.sleep(1.0)


def main() -> int:  # noqa: C901 - a linear script, read top to bottom
    from playwright.sync_api import sync_playwright

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    api_port = free_port()
    api = ThreadingHTTPServer(("127.0.0.1", api_port), FakeElevenLabs)
    threading.Thread(target=api.serve_forever, daemon=True).start()

    data_dirs = [Path(tempfile.mkdtemp(prefix="ink-speech-")) for _ in range(2)]
    ports = [free_port(), free_port()]
    servers: list[subprocess.Popen[bytes]] = []

    def phase_env(data_dir: Path, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "INK_DATA_DIR": str(data_dir),
            "INK_IDLE_TIMEOUT_MS": "600000",   # only manual sends in this test
            "INK_SMOOTHING": "off",
            "PYTHONUTF8": "1",
            **extra,
        }

    try:
        # ---------------------------------------------------------- with a key
        base = f"http://127.0.0.1:{ports[0]}"
        servers.append(start_server(phase_env(
            data_dirs[0],
            ELEVENLABS_API_KEY=API_KEY,
            ELEVENLABS_BASE_URL=f"http://127.0.0.1:{api_port}",
            ELEVENLABS_VOICE_ID=VOICE,
            INK_CONFIRM_TIMEOUT_S="40",
        ), ports[0]))
        wait_for_server(base)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1024, "height": 768},
                                          has_touch=True)
            context.add_init_script(STUB_PLAYBACK)
            page = context.new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            spoken = draw_and_speak(page, base, 200)
            print(f"spoken           {spoken}")
            check(spoken is not None, "the tablet was never told to speak")

            if spoken:
                check(spoken["text"] == SAMPLE_TEXT,
                      f"tablet was given {spoken['text']!r}, not the pipeline text")
                check(spoken["via"] == "elevenlabs",
                      f"tablet spoke via {spoken['via']!r} despite the key being set")
                check(bool(spoken["url"]) and spoken["url"].startswith("/tts/"),
                      f"audio url is {spoken['url']!r}")

            played = page.evaluate("window.__played")
            print(f"played           {played}")
            check(len(played) == 1 and played[0].endswith(spoken["url"] if spoken else "?"),
                  f"the page did not play the served audio: {played}")

            print(f"api calls        {len(FakeElevenLabs.calls)}")
            check(len(FakeElevenLabs.calls) == 1,
                  f"expected one synthesis call, got {len(FakeElevenLabs.calls)}")
            if FakeElevenLabs.calls:
                call = FakeElevenLabs.calls[0]
                print(f"api request      {call['path']}  model={call['model']}")
                check(call["text"] == SAMPLE_TEXT, f"synthesised {call['text']!r}")
                check(call["key"] == API_KEY, "the API key did not reach ElevenLabs")
                check(VOICE in call["path"], f"wrong voice in {call['path']}")

            # The audio must be reachable from the tablet, byte for byte.
            served = urllib.request.urlopen(f"{base}{spoken['url']}", timeout=5).read() if spoken else b""
            check(served == FakeElevenLabs.audio,
                  f"/tts served {len(served)} bytes, stub returned {len(FakeElevenLabs.audio)}")

            # And the key must not be anywhere the tablet can see.
            page_side = urllib.request.urlopen(f"{base}/api/config", timeout=5).read().decode()
            check(API_KEY not in page_side and "elevenlabs" not in page_side.lower(),
                  "/api/config leaks the speech credentials to the tablet")
            check(all("api.elevenlabs.io" not in url for url in played),
                  "the tablet fetched audio from ElevenLabs directly")

            # The sentence is on screen, with the attribution the free tier needs.
            print(f"on screen        {page.inner_text('#speech-text')!r} / "
                  f"{page.inner_text('#speech-source')!r}")
            check(page.inner_text("#speech-text") == SAMPLE_TEXT,
                  "the spoken sentence is not shown on the canvas")
            check("ElevenLabs" in page.inner_text("#speech-source"),
                  "the ElevenLabs attribution is missing")

            # Confirmation: the question is up, and one tap answers it.
            page.wait_for_selector("#prompt:not([hidden])", timeout=10000)
            question = page.inner_text("#prompt-question")
            print(f"question         {question!r}")
            check("right" in question.lower(), f"unexpected confirmation question: {question!r}")

            tap_yes(page)
            answers = get_json(f"{base}/api/answers")["answers"]
            print(f"answer logged    {answers[0] if answers else None}")
            check(bool(answers) and answers[0]["answer"] == "yes",
                  "the confirming tap was not recorded")
            if answers:
                context_data = answers[0].get("context") or {}
                check(context_data.get("text") == SAMPLE_TEXT,
                      f"answer context lost the spoken text: {context_data}")
                check(bool(context_data.get("capture")),
                      f"answer context lost the capture id: {context_data}")

            # Same sentence again: cache hit, no second call, same file.
            repeat = draw_and_speak(page, base, 400)
            print(f"repeat           {repeat['url'] if repeat else None}  "
                  f"api calls still {len(FakeElevenLabs.calls)}")
            check(len(FakeElevenLabs.calls) == 1,
                  "a repeated sentence went back to the API instead of the cache")
            check(bool(repeat) and spoken and repeat["url"] == spoken["url"],
                  "the cached audio was served under a different url")
            cached = list((data_dirs[0] / "tts").glob("*.mp3"))
            check(len(cached) == 1, f"expected one cached mp3, found {len(cached)}")

            browser.close()

        # ------------------------------------------------------- with no key
        base = f"http://127.0.0.1:{ports[1]}"
        servers.append(start_server(phase_env(
            data_dirs[1],
            ELEVENLABS_API_KEY="",
            ELEVENLABS_BASE_URL=f"http://127.0.0.1:{api_port}",
        ), ports[1]))
        wait_for_server(base)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1024, "height": 768},
                                          has_touch=True)
            context.add_init_script(STUB_PLAYBACK)
            page = context.new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            spoken = draw_and_speak(page, base, 200)
            print(f"\nkeyless          {spoken}")
            check(spoken is not None, "the tablet was never told to speak without a key")
            if spoken:
                check(spoken["url"] is None, f"audio url should be null, got {spoken['url']!r}")
                check(spoken["via"] == "browser",
                      f"fallback used {spoken['via']!r} instead of the device voice")
            print(f"device spoke     {page.evaluate('window.__spoken')}")
            check(page.evaluate("window.__spoken") == [SAMPLE_TEXT],
                  "the device voice engine was not asked to say the sentence")
            check(len(FakeElevenLabs.calls) == 1,
                  "a keyless server still called ElevenLabs")

            browser.close()

    finally:
        for server in servers:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        api.shutdown()
        for data_dir in data_dirs:
            shutil.rmtree(data_dir, ignore_errors=True)

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
