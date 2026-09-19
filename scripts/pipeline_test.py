"""Check that a capture is handed to step 2 as a PNG plus flattened points.

    python scripts/pipeline_test.py

Draws three separate strokes in a real browser, then asserts that
`process_capture` receives the PNG and a payload whose points are flattened
across all strokes in drawing order.
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

# Stands in for server/pipeline.py: records its arguments instead of analysing.
SPY = '''
import json, os
from pathlib import Path
from typing import Any

def process_capture(image: Path, data: dict[str, Any]) -> str | None:
    Path(os.environ["SPY_OUT"]).write_text(json.dumps({
        "image_name": image.name,
        "image_exists": image.exists(),
        "image_bytes": image.stat().st_size if image.exists() else 0,
        "keys": sorted(data),
        "strokes": data.get("strokes"),
        "points": data.get("points"),
        "polylines": data.get("polylines"),
    }), encoding="utf-8")
    return "spoken text from the placeholder"
'''


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="ink-pipe-"))
    spy_out = data_dir / "spy.json"
    base = f"http://127.0.0.1:{port}"

    # Swap the placeholder for a spy by shadowing it on sys.path.
    shim_dir = Path(tempfile.mkdtemp(prefix="ink-shim-"))
    (shim_dir / "sitecustomize.py").write_text(
        "import server.pipeline as p\n"
        "ns = {}\n"
        f"exec({SPY!r}, ns)\n"
        "p.process_capture = ns['process_capture']\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "INK_DATA_DIR": str(data_dir),
        "INK_IDLE_TIMEOUT_MS": "1500",
        "INK_SMOOTHING": "off",
        "SPY_OUT": str(spy_out),
        "PYTHONPATH": f"{shim_dir}{os.pathsep}{ROOT}",
        "PYTHONUTF8": "1",
    }

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"{base}/api/config", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": 1024, "height": 768},
                                       has_touch=True).new_page()
            page.goto(f"{base}/canvas")
            page.wait_for_selector("#dot.on", timeout=10000)

            # Three strokes, drawn left to right at increasing heights so the
            # expected flat ordering is easy to state.
            expected_strokes = 3
            for s in range(expected_strokes):
                y = 200 + s * 120
                page.mouse.move(150, y)
                page.mouse.down()
                for i in range(1, 11):
                    page.mouse.move(150 + i * 40, y)
                page.mouse.up()

            drawn = page.evaluate("strokes.map(s => s.points.map(p => [p[0], p[1]]))")
            page.click("#send")
            page.wait_for_function("strokes.length === 0", timeout=15000)

            # The canvas clears itself before the PNG has even been encoded, so
            # closing the browser here would abort the upload mid-flight. Wait
            # for the server to admit it has the drawing.
            for _ in range(60):
                body = urllib.request.urlopen(f"{base}/api/captures", timeout=5).read()
                if json.loads(body)["captures"]:
                    break
                time.sleep(0.25)
            else:
                check(False, "the upload never reached the server")

            browser.close()

        for _ in range(60):
            if spy_out.exists():
                break
            time.sleep(0.25)

        if not spy_out.exists():
            print("process_capture was never called")
            return 1

        seen = json.loads(spy_out.read_text(encoding="utf-8"))
        flat = [point for stroke in drawn for point in stroke]

        print(f"image            {seen['image_name']}  exists={seen['image_exists']}  "
              f"{seen['image_bytes']:,} bytes")
        print(f"payload keys     {seen['keys']}")
        print(f"strokes          {seen['strokes']} (drew {expected_strokes})")
        print(f"points           {len(seen['points'])} flattened from "
              f"{[len(s) for s in drawn]} per stroke")
        print(f"first / last     {seen['points'][0]} ... {seen['points'][-1]}")
        print(f"shape of a point {len(seen['points'][0])} values (x, y only)")

        check(seen["image_exists"] and seen["image_bytes"] > 1000, "the PNG handed over is missing or empty")
        check(seen["image_name"].endswith(".png"), f"image is not a png: {seen['image_name']}")
        check(seen["keys"] == ["points", "polylines", "strokes"],
              f"payload keys are {seen['keys']}")
        check(len(seen.get("polylines") or []) == expected_strokes,
              f"polyline count is {len(seen.get('polylines') or [])}, drew {expected_strokes}")
        check(seen["strokes"] == expected_strokes,
              f"stroke count is {seen['strokes']}, drew {expected_strokes}")
        check(all(len(p) == 2 for p in seen["points"]), "a point carried more than x and y")
        check(seen["points"] == flat,
              "flattened points do not match the drawn order across strokes")

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(shim_dir, ignore_errors=True)

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
