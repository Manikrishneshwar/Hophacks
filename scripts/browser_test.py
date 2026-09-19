"""Drive the real canvas page in a browser and assert the idle capture fires.

    python scripts/browser_test.py

Starts its own server with a short idle timeout in a throwaway data directory,
opens the viewer and the canvas, scribbles with a simulated stylus, and checks
that the drawing is captured, stored and mirrored to the viewer.
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
IDLE_MS = 3000


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


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-test-"))
    base = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "INK_DATA_DIR": str(data_dir),
        "INK_PORT": str(port),
        "INK_HOST": "127.0.0.1",
        "INK_IDLE_TIMEOUT_MS": str(IDLE_MS),
        "PYTHONUTF8": "1",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    failures: list[str] = []
    try:
        wait_for(f"{base}/api/config")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            viewer = browser.new_page()
            viewer.goto(f"{base}/viewer")
            viewer.wait_for_selector("#dot.on", timeout=10000)

            tablet = browser.new_context(
                viewport={"width": 1024, "height": 768},
                device_scale_factor=2,
                has_touch=True,
            ).new_page()
            errors: list[str] = []
            tablet.on("pageerror", lambda e: errors.append(str(e)))
            tablet.goto(f"{base}/canvas")
            tablet.wait_for_selector("#dot.on", timeout=10000)

            # Scribble with a stylus-like pointer.
            tablet.mouse.move(200, 300)
            tablet.mouse.down()
            for i in range(40):
                tablet.mouse.move(200 + i * 12, 300 + (i % 7) * 14)
            tablet.mouse.up()

            stroke_count = tablet.evaluate("strokes.length")
            point_count = tablet.evaluate("strokes[0] ? strokes[0].points.length : 0")
            print(f"drawing          {stroke_count} stroke(s), {point_count} points")
            if stroke_count != 1 or point_count < 10:
                failures.append("stroke was not recorded from pointer input")

            # The countdown should be visibly ticking down.
            time.sleep(0.6)
            countdown = tablet.inner_text("#countdown")
            print(f"countdown        showing {countdown!r}")
            if not countdown.endswith("s"):
                failures.append("idle countdown is not displayed")

            # The viewer should have mirrored the strokes live.
            mirrored = viewer.evaluate("inked")
            print(f"live mirror      viewer inked={mirrored}")
            if not mirrored:
                failures.append("viewer did not receive live strokes")

            # Now wait out the idle timeout and let it capture itself.
            print(f"waiting          {IDLE_MS / 1000:g}s of inactivity ...")
            viewer.wait_for_selector("#gallery figure", timeout=IDLE_MS + 15000)

            if tablet.evaluate("strokes.length") != 0:
                failures.append("canvas was not cleared after capture")
            else:
                print("canvas cleared   yes")

            caption = viewer.inner_text("#gallery figure figcaption")
            print(f"viewer gallery   {caption}")
            if "idle" not in caption:
                failures.append("capture was not attributed to the idle trigger")

            if errors:
                failures.append(f"page errors: {errors}")

            browser.close()

        pngs = sorted(p for p in (data_dir / "captures").glob("*.png"))
        jsons = sorted(p for p in (data_dir / "captures").glob("*.json"))
        print(f"on disk          {[p.name for p in pngs]}")
        if len(pngs) != 1 or pngs[0].stat().st_size < 1000:
            failures.append("expected exactly one non-trivial PNG on disk")
        if len(jsons) != 1:
            failures.append("expected exactly one stroke file on disk")

        index = [json.loads(line) for line in
                 (data_dir / "index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"index.jsonl      {len(index)} record(s): "
              f"{index[0]['stroke_count']} strokes, {index[0]['point_count']} points, "
              f"{index[0]['width']}x{index[0]['height']} @{index[0]['dpr']}x, "
              f"{index[0]['duration_ms']}ms, trigger={index[0]['trigger']}")
        if len(index) != 1 or index[0]["trigger"] != "idle":
            failures.append("index does not hold one idle-triggered record")

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
