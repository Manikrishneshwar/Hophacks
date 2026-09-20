#!/usr/bin/env python3
"""Fill drawings_db.json from labelled priors, eval captures, then synthetics.

Templates are the last resort in the chain, and they also nudge Gemini's ranks
when a new drawing is close to one already stored. An empty database makes both
of those useless.

Sources, in order of trust:

1. `data/priors/` — repeated drawings of the same object, named `apple1`,
   `call3`, and so on. These are how this person actually draws.
2. Real captures listed in eval/catalog.json.
3. Synthetic outlines for tags no real drawing covers yet (bed, check, xmark).

Re-running is safe: existing ids are updated rather than duplicated. Tag
`drawing_ids` are rewritten to match whatever was stored.

Usage:
  python scripts/seed_drawings.py
  python scripts/seed_drawings.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drawing_features import DrawingFeatureStore, parse_strokes  # noqa: E402
from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore  # noqa: E402

PRIOR_STEM = re.compile(r"^([a-zA-Z]+)(\d+)$")

# Prefix on the prior filename → (tag_id, detail). `_digit` is not a spoken
# intent: those drawings only help recover a 1 or a 2 when geometry is unsure.
PRIOR_MAP: dict[str, tuple[str, str]] = {
    "apple": ("food", "apple"),
    "pizza": ("food", "pizza"),
    "water": ("water", ""),
    "call": ("help", "call"),
    "music": ("story", "music"),
    "one": ("_digit", "1"),
    "two": ("_digit", "2"),
}


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 24) -> list[list[float]]:
    span = end - start
    return [
        [cx + r * math.cos(start + span * i / steps), cy + r * math.sin(start + span * i / steps)]
        for i in range(steps + 1)
    ]


def _line(x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> list[list[float]]:
    return [[x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps] for i in range(steps + 1)]


def synthetic() -> dict[str, list[list[list[float]]]]:
    """Canonical outlines for tags the priors do not cover."""
    return {
        "cup": [
            _line(120, 90, 135, 240) + _line(135, 240, 225, 240) + _line(225, 240, 240, 90),
            _line(120, 90, 240, 90),
        ],
        "bowl": [
            _arc(180, 150, 70, 0.0, math.pi),
            _line(110, 150, 250, 150),
        ],
        "cross": [
            _line(180, 70, 180, 250),
            _line(95, 160, 265, 160),
        ],
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


def from_priors(priors_dir: Path) -> list[dict[str, Any]]:
    """Every labelled prior that still has parseable strokes."""
    if not priors_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(priors_dir.glob("*.json")):
        stem = path.stem
        parsed = PRIOR_STEM.match(stem)
        if not parsed:
            print(f"  {stem:<10} SKIP    name is not prefix+number")
            continue
        prefix = parsed.group(1).lower()
        if prefix not in PRIOR_MAP:
            print(f"  {stem:<10} SKIP    unknown prior prefix {prefix!r}")
            continue
        tag_id, detail = PRIOR_MAP[prefix]
        try:
            strokes = parse_strokes(json.loads(path.read_text()))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"  {stem:<10} SKIP    {exc}")
            continue
        if not strokes:
            print(f"  {stem:<10} SKIP    no points")
            continue
        rows.append(
            {
                "drawing_id": stem,
                "tag_id": tag_id,
                "detail": detail,
                "strokes": strokes,
                "metadata": {
                    "origin": "prior",
                    "tag_id": tag_id,
                    "detail": detail,
                    "digit": detail if tag_id == "_digit" else "",
                    "source": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                },
            }
        )
    return rows


def from_catalog(tags: DrawingTagStore, eval_root: Path) -> list[dict[str, Any]]:
    """Eval captures, keyed by the first drawing_id on their tag, as a backup."""
    catalog_path = eval_root / "catalog.json"
    if not catalog_path.exists():
        return []
    items = json.loads(catalog_path.read_text()).get("items") or []
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        tag_id = str(item.get("tag") or "").strip()
        strokes_rel = item.get("strokes")
        if not tag_id or not strokes_rel or tag_id in {"play", "yes", "no"}:
            continue
        try:
            tag = tags.get(tag_id)
        except KeyError:
            continue
        drawing_ids = [d for d in (tag.get("drawing_ids") or []) if d]
        drawing_id = drawing_ids[0] if drawing_ids else f"eval-{item.get('id')}"
        if drawing_id in seen_ids:
            continue
        strokes_path = eval_root / strokes_rel
        if not strokes_path.exists():
            continue
        try:
            strokes = parse_strokes(json.loads(strokes_path.read_text()))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not strokes:
            continue
        seen_ids.add(drawing_id)
        rows.append(
            {
                "drawing_id": drawing_id,
                "tag_id": tag_id,
                "detail": str(item.get("detail") or "").strip(),
                "strokes": strokes,
                "metadata": {
                    "origin": "eval",
                    "tag_id": tag_id,
                    "detail": str(item.get("detail") or "").strip(),
                    "source_id": str(item.get("id") or "").strip(),
                },
            }
        )
    return rows


def seed(
    db_path: Path,
    tags_path: Path,
    eval_root: Path,
    priors_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    tags = DrawingTagStore(tags_path)
    store = DrawingFeatureStore(db_path)
    wanted: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(row: dict[str, Any]) -> None:
        drawing_id = row["drawing_id"]
        if drawing_id in seen:
            return
        seen.add(drawing_id)
        wanted.append(row)

    for row in from_priors(priors_dir):
        take(row)
    covered = {row["tag_id"] for row in wanted if row["tag_id"] != "_digit"}
    for row in from_catalog(tags, eval_root):
        if row["tag_id"] in covered:
            continue
        take(row)
    covered = {row["tag_id"] for row in wanted if row["tag_id"] != "_digit"}
    synthetic_tag = {
        "cup": "water",
        "bowl": "food",
        "cross": "help",
        "bed": "rest",
        "check": "yes",
        "xmark": "no",
    }
    for drawing_id, strokes in synthetic().items():
        if drawing_id in seen:
            continue
        tag_id = synthetic_tag[drawing_id]
        if tag_id in covered:
            print(f"  {drawing_id:<10} skip    tag={tag_id:<7} synthetic; priors already cover it")
            continue
        take(
            {
                "drawing_id": drawing_id,
                "tag_id": tag_id,
                "detail": "",
                "strokes": strokes,
                "metadata": {
                    "origin": "synthetic",
                    "tag_id": tag_id,
                    "note": "drawn by script, replace with a real capture",
                },
            }
        )

    existing = set(store.list_ids())
    linked: dict[str, list[str]] = {}
    for row in wanted:
        drawing_id = row["drawing_id"]
        tag_id = row["tag_id"]
        action = "update" if drawing_id in existing else "add"
        print(
            f"  {drawing_id:<10} {action:<7} tag={tag_id:<7} "
            f"{row['metadata']['origin']:<9} strokes={len(row['strokes'])}"
        )
        if tag_id not in {"_digit", "yes", "no"}:
            linked.setdefault(tag_id, []).append(drawing_id)
        if dry_run:
            continue
        label = row["detail"] or (tag_id if tag_id != "_digit" else row["detail"])
        if action == "update":
            store.update(
                drawing_id,
                row["strokes"],
                label=label or tag_id,
                metadata=row["metadata"],
            )
        else:
            store.add(
                row["strokes"],
                drawing_id,
                label=label or tag_id,
                metadata=row["metadata"],
            )

    leftovers = [drawing_id for drawing_id in store.list_ids() if drawing_id not in seen]
    for drawing_id in leftovers:
        print(f"  {drawing_id:<10} delete  leftover, not in this seed")
        if not dry_run:
            store.delete(drawing_id)

    if dry_run:
        print("dry run, nothing written")
        return 0

    # Priors replace the old single-id lists (cup, bowl, …) so scoring looks at
    # how this person actually draws, not a one-stroke cartoon of a cup.
    for tag in tags.list_tags():
        tag_id = tag["id"]
        ids = linked.get(tag_id, [])
        if tag_id == "yes":
            ids = ["check"]
        elif tag_id == "no":
            ids = ["xmark"]
        aliases = list(tag.get("aliases") or [])
        extras = {"food": ["apple", "pizza"], "help": ["call", "phone"], "story": ["music"]}.get(
            tag_id, []
        )
        for name in extras:
            if name not in aliases:
                aliases.append(name)
        if ids or extras:
            tags.update(
                tag_id,
                drawing_ids=ids or list(tag.get("drawing_ids") or []),
                aliases=aliases,
            )

    store.save()
    tags.save()
    print(f"wrote {db_path} with {len(store.list_ids())} drawings")
    print(f"updated {tags_path} drawing_ids")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "drawings_db.json")
    parser.add_argument("--tags", type=Path, default=ROOT / DEFAULT_TAGS_PATH)
    parser.add_argument("--eval", type=Path, default=ROOT / "eval")
    parser.add_argument("--priors", type=Path, default=ROOT / "data" / "priors")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return seed(args.db, args.tags, args.eval, args.priors, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
