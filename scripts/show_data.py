"""Print what has been captured so far.

    python scripts/show_data.py [limit]

Reads the dataset the same way a training script would: straight off
index.jsonl, cross-checked against the files on disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config  # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def main() -> int:
    if not config.INDEX_PATH.exists():
        print(f"No captures yet. Nothing at {config.INDEX_PATH}")
        return 0

    with config.INDEX_PATH.open(encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]

    print(f"{config.CAPTURE_DIR}\n{len(records)} capture(s) in the index\n")
    header = f"{'timestamp id':<28} {'trigger':<8} {'strokes':>7} {'points':>7} {'drawn':>8} {'png':>9}  analysis"
    print(header)
    print("-" * len(header))

    missing = []
    for record in records[-LIMIT:]:
        png = config.CAPTURE_DIR / record["png"]
        strokes = config.CAPTURE_DIR / record["strokes"]
        if not png.exists():
            missing.append(record["png"])
        if not strokes.exists():
            missing.append(record["strokes"])
        print(
            f"{record['id']:<28} {record['trigger']:<8} {record['stroke_count']:>7} "
            f"{record['point_count']:>7} {record['duration_ms'] / 1000:>7.1f}s "
            f"{record['png_bytes'] / 1024:>7.1f}kB  {record['analysis'] or '-'}"
        )

    total_points = sum(r["point_count"] for r in records)
    total_bytes = sum(r["png_bytes"] for r in records)
    sessions = {r["session_id"] for r in records}
    print(
        f"\n{len(sessions)} session(s), {total_points:,} points, "
        f"{total_bytes / 1024 / 1024:.2f} MB of PNGs"
    )

    if missing:
        print(f"\nWARNING: {len(missing)} indexed file(s) missing from disk: {missing[:5]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
