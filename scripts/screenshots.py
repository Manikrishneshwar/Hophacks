"""Render the two pages with a sample drawing into .preview/ for a quick look."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from math import cos, sin
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".preview"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-shot-"))
    base = f"http://127.0.0.1:{port}"

    env = {**os.environ, "INK_DATA_DIR": str(data_dir), "INK_IDLE_TIMEOUT_MS": "20000",
           "INK_RECOGNITION": "0", "PYTHONUTF8": "1"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/api/config", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            viewer = browser.new_page(viewport={"width": 1280, "height": 860})
            viewer.goto(f"{base}/viewer")
            viewer.wait_for_selector("#dot.on", timeout=10000)

            tablet = browser.new_context(viewport={"width": 1180, "height": 820},
                                         device_scale_factor=2, has_touch=True).new_page()
            tablet.goto(f"{base}/canvas")
            tablet.wait_for_selector("#dot.on", timeout=10000)

            # "hello" in a loose cursive-ish scrawl, plus an underline.
            def draw(points):
                tablet.mouse.move(*points[0])
                tablet.mouse.down()
                for pt in points[1:]:
                    tablet.mouse.move(*pt)
                tablet.mouse.up()

            draw([(220, 260 + 180 * sin(i / 9)) for i in range(0, 60)]
                 + [(220 + i * 9, 300 + 90 * cos(i / 7)) for i in range(0, 70)])
            draw([(240 + i * 8, 470 + 6 * sin(i / 3)) for i in range(0, 80)])
            draw([(300 + i * 6, 560) for i in range(0, 100)])

            time.sleep(0.8)
            tablet.screenshot(path=str(OUT / "tablet.png"))

            # One capture to populate the gallery, then redraw so the live pane
            # shows something too.
            tablet.click("#send")
            viewer.wait_for_selector("#gallery figure", timeout=15000)
            draw([(240 + i * 8, 470 + 6 * sin(i / 3)) for i in range(0, 80)])
            draw([(220 + i * 9, 300 + 90 * cos(i / 7)) for i in range(0, 70)])
            time.sleep(0.5)
            viewer.screenshot(path=str(OUT / "viewer.png"))

            # The tap-to-answer prompt, on a cleared canvas.
            tablet.click("#clear")
            # Deliberately not returning the promise ask() hands back, since
            # evaluate would then wait for the question to be answered.
            tablet.evaluate("() => { window.ink.ask('I read that as: 240 mL of reagent A. Correct?'); }")
            time.sleep(0.4)
            tablet.screenshot(path=str(OUT / "prompt.png"))

            browser.close()
        print(f"wrote {OUT / 'tablet.png'} and {OUT / 'viewer.png'}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
