"""Check that tremor smoothing works and that the raw signal survives.

    python scripts/smoothing_test.py

Feeds a straight horizontal drag with a simulated 8 Hz tremor superimposed, then
measures how far the stored points deviate from the intended straight line,
before and after filtering.
"""

from __future__ import annotations

import json
import math
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

TREMOR_AMPLITUDE = 9.0   # pixels of wobble either side of the line
TREMOR_HZ = 8.0


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


# Injected in the page: replay a tremulous drag through real pointer events,
# paced in real time at roughly 60 Hz. The pacing matters — the filter reads
# each event's timestamp, so replaying instantly would misrepresent the tremor
# frequency and understate how much gets removed.
SHAKY_DRAG = """async () => {
  const board = document.getElementById('board');
  const send = (type, x, y) => board.dispatchEvent(new PointerEvent(type, {
    pointerId: 1, pointerType: 'touch', clientX: x, clientY: y,
    pressure: 0.5, bubbles: true, cancelable: true,
  }));
  const frame = () => new Promise(r => requestAnimationFrame(r));
  const BASELINE = 400;
  const steps = 110;

  const t0 = performance.now();
  send('pointerdown', 120, BASELINE);
  let lastX = 120;
  for (let i = 1; i <= steps; i++) {
    await frame();
    const elapsed = (performance.now() - t0) / 1000;
    lastX = 120 + i * 6;
    send('pointermove', lastX, BASELINE + AMPLITUDE * Math.sin(2 * Math.PI * HZ * elapsed));
  }
  send('pointerup', lastX, BASELINE);
  return strokes[0];
}"""


def deviation(points: list[list[float]], baseline: float) -> float:
    """Root-mean-square distance from the intended straight line."""
    return math.sqrt(sum((p[1] - baseline) ** 2 for p in points) / len(points))


def run(smoothing: str) -> dict:
    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-smooth-"))
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "INK_DATA_DIR": str(data_dir), "INK_SMOOTHING": smoothing,
           "INK_IDLE_TIMEOUT_MS": "60000", "INK_RECOGNITION": "0", "PYTHONUTF8": "1"}

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(f"{base}/api/config")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": 1024, "height": 768},
                                       has_touch=True).new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)
            script = SHAKY_DRAG.replace("AMPLITUDE", str(TREMOR_AMPLITUDE)).replace("HZ", str(TREMOR_HZ))
            stroke = page.evaluate(script)
            browser.close()
        return stroke
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


def main() -> int:
    failures: list[str] = []
    baseline = 400.0

    print(f"input: straight drag with {TREMOR_AMPLITUDE:.0f}px of {TREMOR_HZ:.0f}Hz tremor "
          f"(RMS of a pure sine would be {TREMOR_AMPLITUDE / math.sqrt(2):.1f}px)\n")

    results = {}
    for level in ("off", "light", "medium", "strong"):
        stroke = run(level)
        points = stroke["points"]
        wobble = deviation(points, baseline)
        results[level] = wobble

        raw = stroke.get("raw")
        raw_note = f"raw kept: {len(raw)} samples" if raw else "raw not stored"
        print(f"  {level:<7} wobble {wobble:5.2f} px   points {len(points):4}   {raw_note}")

        if level == "off":
            if raw is not None:
                failures.append("raw samples stored even with smoothing off (pure duplication)")
            if wobble < 4.0:
                failures.append(f"unsmoothed wobble was only {wobble:.2f}px; the test input is too gentle")
        else:
            if not raw:
                failures.append(f"{level}: raw samples were not preserved")
            elif deviation(raw, baseline) < wobble:
                failures.append(f"{level}: stored raw is smoother than the filtered points")
            if stroke.get("smoothing") != level:
                failures.append(f"{level}: stroke labelled {stroke.get('smoothing')!r}")

    if not (results["off"] > results["light"] > results["medium"] > results["strong"]):
        failures.append(f"stronger settings did not reduce wobble monotonically: {results}")

    reduction = 100 * (1 - results["medium"] / results["off"])
    print(f"\nmedium removes {reduction:.0f}% of the tremor")
    if reduction < 70:
        failures.append(f"medium only removed {reduction:.0f}% of the tremor")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
