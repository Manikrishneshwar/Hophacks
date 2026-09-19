#!/usr/bin/env python3
"""Fill drawings_db.json so the template fallback can answer something.

Templates are the last resort in the chain, reached when Gemini and the local
vision model are both unavailable. An empty database makes that resort useless:
`score_all` returns nothing, no candidate carries a feature score, and every
capture gets the same generic sentence no matter what was drawn.

Two sources, in order of trust:

1. Real captures listed in eval/catalog.json, which carry a human-checked tag.
   These are how someone actually draws on this pad, tremor and all.
2. Synthetic outlines for tags no capture covers yet. These are honest guesses
   at a shape and are much weaker than a real drawing; record real ones through
   the pad and re-run this to replace them.

Re-running is safe: existing ids are updated rather than duplicated.

Usage:
  python scripts/seed_drawings.py
  python scripts/seed_drawings.py --db drawings_db.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drawing_features import DrawingFeatureStore, parse_strokes  # noqa: E402
from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore  # noqa: E402


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 24) -> list[list[float]]:
    span = end - start
    return [
        [cx + r * math.cos(start + span * i / steps), cy + r * math.sin(start + span * i / steps)]
        for i in range(steps + 1)
    ]


def _line(x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> list[list[float]]:
    return [[x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps] for i in range(steps + 1)]


def synthetic() -> dict[str, list[list[list[float]]]]:
    """Canonical outlines, in pad-like pixel coordinates with y increasing down."""
    return {
        # Tapered tumbler plus a waterline, which is what the real cup capture
        # looks like and what people tend to draw for a drink.
        "cup": [
            _line(120, 90, 135, 240) + _line(135, 240, 225, 240) + _line(225, 240, 240, 90),
            _line(120, 90, 240, 90),
        ],
        # Open bowl: a half circle with a rim across the top.
        "bowl": [
            _arc(180, 150, 70, 0.0, math.pi),
            _line(110, 150, 250, 150),
        ],
        "cross": [
            _line(180, 70, 180, 250),
            _line(95, 160, 265, 160),
        ],
        # Mattress with a headboard on the left and a pillow on it.
        "bed": [
            _line(90, 230, 90, 140) + _line(90, 230, 280, 230) + _line(280, 230, 280, 180),
            _line(100, 175, 155, 175),
        ],
        "check": [
            _line(110, 165, 160, 225) + _line(160, 225, 260, 100),
        ],
        "xmark": [
            _line(110, 100, 255, 245),
            _line(255, 100, 110, 245),
        ],
    }


def from_catalog(tags: DrawingTagStore, eval_root: Path) -> dict[str, Any]:
    """Real strokes keyed by the drawing_id their tag points at."""
    catalog_path = eval_root / "catalog.json"
    if not catalog_path.exists():
        return {}
    items = json.loads(catalog_path.read_text()).get("items") or []
    picked: dict[str, Any] = {}
    for item in items:
        tag_id = str(item.get("tag") or "").strip()
        strokes_rel = item.get("strokes")
        if not tag_id or not strokes_rel:
            continue
        try:
            tag = tags.get(tag_id)
        except KeyError:
            continue
        drawing_ids = tag.get("drawing_ids") or []
        if not drawing_ids:
            continue
        strokes_path = eval_root / strokes_rel
        if not strokes_path.exists():
            continue
        strokes = parse_strokes(json.loads(strokes_path.read_text()))
        if not strokes:
            continue
        # First capture for a tag wins, so a re-run is deterministic.
        picked.setdefault(
            drawing_ids[0],
            {
                "strokes": strokes,
                "tag_id": tag_id,
                "note": str(item.get("note") or "").strip(),
                "source_id": str(item.get("id") or "").strip(),
            },
        )
    return picked


def seed(db_path: Path, tags_path: Path, eval_root: Path, *, dry_run: bool = False) -> int:
    tags = DrawingTagStore(tags_path)
    store = DrawingFeatureStore(db_path)
    real = from_catalog(tags, eval_root)
    shapes = synthetic()

    wanted: list[tuple[str, str, Any, dict[str, Any]]] = []
    for tag in tags.catalog_for_model():
        tag_id = tag["id"]
        for drawing_id in tag.get("drawing_ids") or []:
            if drawing_id in real:
                entry = real[drawing_id]
                wanted.append(
                    (
                        drawing_id,
                        tag_id,
                        entry["strokes"],
                        {
                            "origin": "capture",
                            "tag_id": tag_id,
                            "source_id": entry["source_id"],
                            "note": entry["note"],
                        },
                    )
                )
            elif drawing_id in shapes:
                wanted.append(
                    (
                        drawing_id,
                        tag_id,
                        shapes[drawing_id],
                        {
                            "origin": "synthetic",
                            "tag_id": tag_id,
                            "note": "drawn by script, replace with a real capture",
                        },
                    )
                )
            else:
                print(f"  {drawing_id:<8} SKIP    no capture and no synthetic outline")

    existing = set(store.list_ids())
    for drawing_id, tag_id, strokes, metadata in wanted:
        action = "update" if drawing_id in existing else "add"
        print(
            f"  {drawing_id:<8} {action:<7} tag={tag_id:<6} "
            f"{metadata['origin']:<9} strokes={len(strokes)}"
        )
        if dry_run:
            continue
        if action == "update":
            store.update(drawing_id, strokes, label=tag_id, metadata=metadata)
        else:
            store.add(strokes, drawing_id, label=tag_id, metadata=metadata)

    if dry_run:
        print("dry run, nothing written")
        return 0
    store.save()
    print(f"wrote {db_path} with {len(store.list_ids())} drawings")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "drawings_db.json")
    parser.add_argument("--tags", type=Path, default=ROOT / DEFAULT_TAGS_PATH)
    parser.add_argument("--eval", type=Path, default=ROOT / "eval")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return seed(args.db, args.tags, args.eval, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
