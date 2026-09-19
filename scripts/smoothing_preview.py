"""Render the same tremulous input at each smoothing level into .preview/.

    python scripts/smoothing_preview.py

Draws one shaky line per preset on a single canvas so the settings can be
compared side by side before choosing one.
"""

from __future__ import annotations

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
OUT = ROOT / ".preview"

sys.path.insert(0, str(ROOT))
from server import config  # noqa: E402

# Taken from the server so the picture cannot drift from the real presets.
PRESETS = list(config.SMOOTHING_PRESETS.items())

# Replays a shaky horizontal drag at a given height, after swapping the page's
# active smoothing parameters. Paced in real time so the tremor really is 8 Hz.
DRAW = """async ([params, baseline]) => {
  SMOOTHING = params;
  const board = document.getElementById('board');
  const send = (type, x, y) => board.dispatchEvent(new PointerEvent(type, {
    pointerId: 1, pointerType: 'touch', clientX: x, clientY: y,
    pressure: 0.5, bubbles: true, cancelable: true,
  }));
  const frame = () => new Promise(r => requestAnimationFrame(r));

  const t0 = performance.now();
  send('pointerdown', 150, baseline);
  let x = 150;
  for (let i = 1; i <= 100; i++) {
    await frame();
    x = 150 + i * 8;
    const wobble = 9 * Math.sin(2 * Math.PI * 8 * ((performance.now() - t0) / 1000));
    send('pointermove', x, baseline + wobble);
  }
  send('pointerup', x, baseline);
}"""

LABEL = """([text, y]) => {
  const board = document.getElementById('board');
  const c = board.getContext('2d');
  c.save();
  c.setTransform(1, 0, 0, 1, 0, 0);
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  c.scale(dpr, dpr);
  c.fillStyle = '#5b9dff';
  c.font = '600 15px system-ui, sans-serif';
  c.fillText(text, 30, y);
  c.restore();
}"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-smooth-shot-"))
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "INK_DATA_DIR": str(data_dir), "INK_IDLE_TIMEOUT_MS": "600000",
           "INK_RECOGNITION": "0", "PYTHONUTF8": "1"}

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"{base}/api/config", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": 1024, "height": 620},
                                       device_scale_factor=2, has_touch=True).new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            for i, (name, params) in enumerate(PRESETS):
                baseline = 90 + i * 120
                page.evaluate(LABEL, [f"smoothing: {name}", baseline - 42])
                page.evaluate(DRAW, [params, baseline])

            page.screenshot(path=str(OUT / "smoothing.png"))
            browser.close()

        print(f"wrote {OUT / 'smoothing.png'}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
