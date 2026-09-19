"""Step 2: what happens to a capture once it is on disk.

`process_capture` is a placeholder. Replace its body with the real API call and
whatever checking you want around it; nothing else needs to change, because the
server only cares that it returns the text to be spoken (or None for nothing).

It runs on a worker thread after the upload has already been answered, so it is
free to block on a slow network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Stands in for whatever the real API returns, so the speech half of step 2 has
# something to say while `process_capture` is still a placeholder.
SAMPLE_TEXT = "I would like a glass of water, please."


def build_payload(strokes: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a capture's strokes into the shape `process_capture` expects.

    Every point of every stroke ends up in one list as `[x, y]`, in the order
    they were drawn, so the sequence carries the timing implicitly and pressure
    and timestamps are dropped. Stroke boundaries are not marked; only the
    count survives.
    """
    return {
        "points": [[point[0], point[1]] for stroke in strokes for point in stroke["points"]],
        "strokes": len(strokes),
    }


def process_capture(image: Path, data: dict[str, Any]) -> str | None:
    """Turn a captured drawing into text.

    Args:
        image: path to the PNG. Use `image.read_bytes()` for the raw bytes.
        data: `{"points": [[x, y], ...], "strokes": int}`, as built above.

    Returns:
        The text to speak, or None to stay silent.

    -- REPLACE EVERYTHING BELOW WITH THE REAL API CALL --
    """
    points = data["points"]
    strokes = data["strokes"]

    print(f"[pipeline] image   {image.name} ({image.stat().st_size:,} bytes)")
    print(f"[pipeline] strokes {strokes}")
    print(f"[pipeline] points  {len(points)}, first={points[0] if points else None}, "
          f"last={points[-1] if points else None}")
    print(f"[pipeline] text    {SAMPLE_TEXT!r}")

    return SAMPLE_TEXT
