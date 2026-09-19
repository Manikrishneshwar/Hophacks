"""Check that a finished drawing reaches the caretaker phone.

    python scripts/caretaker_test.py

Starts its own server. Opens the caretaker page and the tablet together,
draws one stroke, confirms with a yes tap, and asserts the caretaker got
the PNG, the spoken line, and the yes — without live stroke traffic.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.pipeline import SAMPLE_TEXT  # noqa: E402

STUB_PLAYBACK = """
HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
if (window.speechSynthesis) {
  window.speechSynthesis.speak = () => {};
}
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_server(base: str) -> None:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{base}/api/config", timeout=2).read()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)


def get_json(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def tap_yes(page) -> None:
    page.mouse.move(500, 400)
    page.mouse.down()
    page.mouse.up()
    time.sleep(1.0)


def main() -> int:
    from playwright.sync_api import sync_playwright

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-caretaker-"))
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "INK_DATA_DIR": str(data_dir),
        "INK_IDLE_TIMEOUT_MS": "600000",
        "INK_SMOOTHING": "off",
        "INK_RECOGNITION": "0",
        "INK_SPEECH": "0",
        "INK_CONFIRM_TIMEOUT_S": "40",
        "PYTHONUTF8": "1",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    try:
        wait_for_server(base)

        home = urllib.request.urlopen(f"{base}/caretaker", timeout=5)
        check(home.status == 200, "/caretaker did not serve")
        bootstrap = get_json(f"{base}/api/caretaker/events")
        check("events" in bootstrap and "patient" in bootstrap,
              "/api/caretaker/events is missing events or patient")
        print(f"patient          {bootstrap.get('patient', {}).get('name')!r}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            phone = browser.new_context(viewport={"width": 390, "height": 844},
                                        has_touch=True).new_page()
            phone.goto(f"{base}/caretaker")
            phone.wait_for_function("window.caretaker && window.caretaker.connected",
                                    timeout=10000)

            tablet = browser.new_context(viewport={"width": 1024, "height": 768},
                                         has_touch=True)
            tablet.add_init_script(STUB_PLAYBACK)
            page = tablet.new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            page.mouse.move(150, 200)
            page.mouse.down()
            for i in range(1, 11):
                page.mouse.move(150 + i * 40, 200)
            page.mouse.up()
            page.evaluate("() => capture('manual')")

            page.wait_for_selector("#prompt:not([hidden])", timeout=20000)
            tap_yes(page)

            phone.wait_for_function(
                "window.caretaker.last && window.caretaker.last.answer === 'yes'",
                timeout=20000,
            )
            event = phone.evaluate("window.caretaker.last")
            types = phone.evaluate("window.caretaker.messages.map(m => m.type)")
            print(f"event            id={event.get('id')}  answer={event.get('answer')}")
            print(f"text             {event.get('text')!r}")
            print(f"image            {event.get('image')}")
            print(f"prompts          {event.get('prompts')}")
            print(f"ws types         {types}")

            check(event.get("answer") == "yes", f"expected yes, got {event.get('answer')!r}")
            check(event.get("text") == SAMPLE_TEXT,
                  f"caretaker text was {event.get('text')!r}")
            check(isinstance(event.get("image"), str) and event["image"].startswith("/captures/")
                  and event["image"].endswith(".png"),
                  f"image url is {event.get('image')!r}")
            check(SAMPLE_TEXT in (event.get("prompts") or []),
                  "spoken prompt did not reach the caretaker")
            check(any((row or {}).get("answer") == "yes" for row in (event.get("answers") or [])),
                  "yes tap was missing from answers")
            leak = [kind for kind in types if kind not in ("welcome", "caretaker")]
            check(not leak, f"caretaker socket saw live traffic: {leak}")

            card = phone.locator(f'article[data-id="{event["id"]}"]')
            check(card.count() == 1, "the event card is not on the page")
            if card.count():
                src = card.locator("img").get_attribute("src")
                check(src == event["image"], f"card image is {src!r}")
                check("YES" in card.inner_text(), "card is missing the YES badge")

            history = get_json(f"{base}/api/caretaker/events")["events"]
            print(f"history          {len(history)} event(s)")
            check(bool(history) and history[0]["id"] == event["id"],
                  "finished event is not at the top of /api/caretaker/events")
            check(history[0].get("answer") == "yes",
                  "history lost the yes")

            browser.close()
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
