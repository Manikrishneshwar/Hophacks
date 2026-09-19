"""Explain the contents of one stroke file.

    python scripts/show_strokes.py [capture_id]

With no argument it picks the capture with the most points, which is usually
the most interesting one to look at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config  # noqa: E402


def main() -> int:
    files = sorted(config.CAPTURE_DIR.glob("*.json"))
    if not files:
        print(f"No stroke files in {config.CAPTURE_DIR}")
        return 1

    if len(sys.argv) > 1:
        wanted = sys.argv[1].removesuffix(".json").removesuffix(".png")
        matches = [f for f in files if f.stem == wanted]
        if not matches:
            print(f"No capture named {wanted}")
            return 1
        path = matches[0]
    else:
        path = max(files, key=lambda f: json.loads(f.read_text(encoding="utf-8"))["point_count"])

    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"file      {path.name}")
    print(f"canvas    {data['width']} x {data['height']} CSS px, device pixel ratio {data['dpr']}")
    print(f"totals    {data['stroke_count']} stroke(s), {data['point_count']} point(s), "
          f"{data['duration_ms']} ms from first contact to capture")
    print(f"trigger   {data['trigger']}")

    print("\nper stroke")
    for i, stroke in enumerate(data["strokes"]):
        points = stroke["points"]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        pressures = [p[2] for p in points]
        print(
            f"  [{i}] tool={stroke['tool']:<5} points={len(points):<5} "
            f"t={points[0][3]}..{points[-1][3]} ms  "
            f"x={min(xs):.0f}..{max(xs):.0f}  y={min(ys):.0f}..{max(ys):.0f}  "
            f"pressure={min(pressures):.2f}..{max(pressures):.2f}"
        )

    first = data["strokes"][0]["points"]
    print(f"\nfirst points of stroke 0, as [x, y, pressure, ms]")
    for point in first[:8]:
        print(f"  {point}")
    if len(first) > 8:
        print(f"  ... {len(first) - 8} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
