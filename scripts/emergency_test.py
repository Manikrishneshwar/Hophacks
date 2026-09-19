"""Check help no longer dials, and an emergency payload is marked urgent.

    python scripts/emergency_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import CaretakerLog  # noqa: E402
from server.pipeline import fallback_closing  # noqa: E402
from server.storage import CaptureRecord  # noqa: E402
from server.pipeline import CaptureResult  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    canvas = (ROOT / "web" / "canvas.js").read_text(encoding="utf-8")
    check("tel:" not in canvas, "canvas.js still opens a tel: link")
    check("Help is on the way" in canvas, "canvas toast no longer says help is on the way")
    print("canvas           no tel: dialer")

    closing = fallback_closing("help", "call")
    print(f"closing          {closing!r}")
    check("Calling" not in closing, f"closing still dials: {closing!r}")
    check("let" in closing.lower() or "help" in closing.lower(), f"unexpected closing: {closing!r}")

    record = CaptureRecord(
        id="2026-09-19T00-00-00-000",
        created_at="2026-09-19T00:00:00.000-04:00",
        session_id="test",
        trigger="idle",
        png="2026-09-19T00-00-00-000.png",
        strokes="2026-09-19T00-00-00-000.json",
        width=100,
        height=100,
        dpr=1,
        stroke_count=1,
        point_count=3,
        duration_ms=100,
        idle_timeout_ms=5000,
        png_bytes=12,
    )
    log = CaretakerLog(record)
    log.call = {"name": "Jordan Chen", "phone": "+15555550100", "relation": "daughter"}
    log.spoken("I've let Jordan Chen know. Help is on the way.")
    message = log.to_message(CaptureResult(text="I need help, please.", tag_id="help", detail="call"), confirmed=True)
    print(f"payload          kind={message['kind']} priority={message['priority']}")
    check(message["kind"] == "emergency", f"kind is {message['kind']!r}")
    check(message["priority"] == "emergency", f"priority is {message['priority']!r}")
    check(message["image"].endswith(".png"), f"image is {message['image']!r}")
    check(message["call"]["name"] == "Jordan Chen", "call info missing")

    brain = (ROOT / "web" / "brain.js").read_text(encoding="utf-8")
    check("embed" in brain, "brain.js has no embed mode")
    check("side-image" in (ROOT / "web" / "brain.html").read_text(encoding="utf-8"),
          "brain.html is missing the drawing image")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
