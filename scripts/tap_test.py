"""Exercise the tap-to-answer gesture in a real browser.

    python scripts/tap_test.py

Covers the happy paths (one tap yes, two taps no) and the ways it could
misfire: taps while no question is pending must still draw, a real drawing must
never be read as an answer, and taps on an inked canvas must not answer.
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f"server never came up at {url}")


def ask_async(base: str, question: str, result: dict) -> threading.Thread:
    """Fire POST /api/ask on a background thread; it blocks until answered."""
    def run() -> None:
        request = urllib.request.Request(
            f"{base}/api/ask",
            data=json.dumps({"question": question, "timeout": 30}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result.update(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            result.update(json.loads(exc.read() or b"{}"))
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-tap-"))
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "INK_DATA_DIR": str(data_dir), "INK_PORT": str(port),
           "INK_IDLE_TIMEOUT_MS": "60000", "INK_RECOGNITION": "0", "PYTHONUTF8": "1"}

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    try:
        wait_for(f"{base}/api/config")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": 1024, "height": 768},
                                       device_scale_factor=2, has_touch=True).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            def tap(x=500, y=380) -> None:
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.up()

            def scribble() -> None:
                page.mouse.move(300, 300)
                page.mouse.down()
                for i in range(30):
                    page.mouse.move(300 + i * 10, 300 + i * 4)
                page.mouse.up()

            # 1. No question pending: a tap must draw a dot, not vanish.
            tap()
            time.sleep(0.7)
            drew = page.evaluate("strokes.length")
            print(f"tap, no question     strokes={drew} (expected 1: it drew a dot)")
            check(drew == 1, "tap with no question pending was swallowed instead of drawing")
            check(page.evaluate("window.ink.answer") is None, "tap registered an answer with no question")
            page.click("#clear")

            # 2. Question pending, one tap: yes.
            result: dict = {}
            thread = ask_async(base, "Is this correct?", result)
            page.wait_for_selector("#prompt:not([hidden])", timeout=10000)
            print(f"prompt shown         {page.inner_text('#prompt-question')!r}")
            tap()
            thread.join(timeout=20)
            answer = page.evaluate("window.ink.answer")
            print(f"one tap              window.ink.answer={answer!r}  server={result.get('answer')!r}")
            check(answer == "yes", f"one tap gave {answer!r}, expected 'yes'")
            check(result.get("answer") == "yes", f"server received {result.get('answer')!r}")
            check(page.evaluate("strokes.length") == 0, "the answering tap left a dot on the canvas")
            check(page.is_hidden("#prompt"), "prompt stayed visible after answering")

            # 3. Question pending, two taps: no.
            result = {}
            thread = ask_async(base, "Should I try again?", result)
            page.wait_for_selector("#prompt:not([hidden])", timeout=10000)
            tap()
            time.sleep(0.12)
            tap(505, 384)
            thread.join(timeout=20)
            answer = page.evaluate("window.ink.answer")
            print(f"two taps             window.ink.answer={answer!r}  server={result.get('answer')!r}")
            check(answer == "no", f"two taps gave {answer!r}, expected 'no'")
            check(result.get("answer") == "no", f"server received {result.get('answer')!r}")
            check(page.evaluate("strokes.length") == 0, "the answering taps left dots on the canvas")

            # 4. Question pending, but the user draws: must not answer.
            result = {}
            thread = ask_async(base, "Drawing should not answer this", result)
            page.wait_for_selector("#prompt:not([hidden])", timeout=10000)
            scribble()
            time.sleep(0.8)
            print(f"drew while pending   strokes={page.evaluate('strokes.length')}, "
                  f"pending={page.evaluate('window.ink.pending')}")
            check(page.evaluate("strokes.length") == 1, "the drawing was swallowed as a tap")
            check(page.evaluate("window.ink.pending") is True, "drawing resolved the question")

            # 5. A tap answers even with ink on the canvas, and leaves it alone.
            tap(700, 500)
            thread.join(timeout=20)
            print(f"tap over ink         answer={page.evaluate('window.ink.answer')!r}, "
                  f"strokes still={page.evaluate('strokes.length')}")
            check(page.evaluate("window.ink.answer") == "yes",
                  "a tap over existing ink did not answer the question")
            check(page.evaluate("strokes.length") == 1,
                  "answering over existing ink disturbed the drawing")
            page.click("#clear")

            # 6. A second finger must not be joined to the first one's stroke.
            page.touchscreen.tap(200, 200)  # warm up touch input
            page.click("#clear")
            multi = page.evaluate("""() => {
              const board = document.getElementById('board');
              const send = (type, id, x, y) => board.dispatchEvent(new PointerEvent(type, {
                pointerId: id, pointerType: 'touch', clientX: x, clientY: y,
                pressure: 0.5, bubbles: true, cancelable: true,
              }));
              send('pointerdown', 1, 200, 200);
              send('pointermove', 1, 260, 200);
              send('pointerdown', 2, 800, 600);   // second finger lands
              send('pointermove', 2, 860, 600);   // and moves far away
              send('pointermove', 1, 320, 200);
              send('pointerup', 2, 860, 600);
              send('pointerup', 1, 320, 200);
              return strokes.map(s => s.points.map(p => [p[0], p[1]]));
            }""")
            xs = [x for stroke in multi for x, _ in stroke]
            print(f"two fingers          {len(multi)} stroke(s), x range {min(xs):.0f}..{max(xs):.0f}")
            check(len(multi) == 1, f"second finger created extra strokes: {len(multi)}")
            check(max(xs) < 500, f"stroke jumped to the second finger at x={max(xs):.0f}")

            check(not errors, f"page errors: {errors}")
            browser.close()

        answers = [json.loads(line) for line in
                   (data_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"\nanswers.jsonl        {len(answers)} record(s): "
              f"{[a['answer'] for a in answers]}")
        check(len(answers) == 3, f"expected 3 logged answers, got {len(answers)}")
        check([a["answer"] for a in answers] == ["yes", "no", "yes"], "logged answers are wrong")
        check(all(a["question"] for a in answers), "an answer was logged without its question")

        pngs = list((data_dir / "captures").glob("*.png"))
        check(not pngs, f"taps should not have produced captures, found {[p.name for p in pngs]}")

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
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
