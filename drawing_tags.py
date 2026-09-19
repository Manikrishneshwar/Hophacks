#!/usr/bin/env python3
"""Store and update drawing intent tags.

Tags are the vocabulary the pad can express (water, help, pain, and so on).
Each tag can point at one or more stored drawing templates in drawings_db.json.
The file is rewritten whenever a tag is added or updated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TAGS_PATH = Path("drawing_tags.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DrawingTagStore:
    """JSON catalog of communication tags linked to drawing templates."""

    def __init__(self, path: str | Path = DEFAULT_TAGS_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"updated_at": None, "tags": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        self._data["updated_at"] = loaded.get("updated_at")
        self._data["tags"] = loaded.get("tags", {})

    def save(self) -> None:
        self._data["updated_at"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get(self, tag_id: str) -> dict[str, Any]:
        try:
            return self._data["tags"][tag_id]
        except KeyError as exc:
            raise KeyError(f"Unknown tag {tag_id!r}") from exc

    def list_tags(self) -> list[dict[str, Any]]:
        return [self._data["tags"][key] for key in sorted(self._data["tags"])]

    def catalog_for_model(self) -> list[dict[str, Any]]:
        """Compact tag list sent to Gemini for ranking."""
        rows = []
        for tag in self.list_tags():
            rows.append(
                {
                    "id": tag["id"],
                    "label": tag.get("label") or tag["id"],
                    "description": tag.get("description") or "",
                    "aliases": tag.get("aliases") or [],
                    "drawing_ids": tag.get("drawing_ids") or [],
                }
            )
        return rows

    def add(
        self,
        tag_id: str,
        *,
        label: str | None = None,
        description: str = "",
        aliases: list[str] | None = None,
        drawing_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if tag_id in self._data["tags"]:
            raise ValueError(f"Tag {tag_id!r} already exists; use update()")
        now = utc_now()
        record = {
            "id": tag_id,
            "label": label or tag_id,
            "description": description,
            "aliases": aliases or [],
            "drawing_ids": drawing_ids or [],
            "created_at": now,
            "updated_at": now,
        }
        self._data["tags"][tag_id] = record
        self.save()
        return record

    def update(
        self,
        tag_id: str,
        *,
        label: str | None = None,
        description: str | None = None,
        aliases: list[str] | None = None,
        drawing_ids: list[str] | None = None,
        add_drawing_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        record = self.get(tag_id)
        if label is not None:
            record["label"] = label
        if description is not None:
            record["description"] = description
        if aliases is not None:
            record["aliases"] = aliases
        if drawing_ids is not None:
            record["drawing_ids"] = drawing_ids
        if add_drawing_ids:
            merged = list(record.get("drawing_ids") or [])
            for drawing_id in add_drawing_ids:
                if drawing_id not in merged:
                    merged.append(drawing_id)
            record["drawing_ids"] = merged
        record["updated_at"] = utc_now()
        self._data["tags"][tag_id] = record
        self.save()
        return record

    def upsert(
        self,
        tag_id: str,
        *,
        label: str | None = None,
        description: str | None = None,
        aliases: list[str] | None = None,
        drawing_ids: list[str] | None = None,
        add_drawing_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if tag_id in self._data["tags"]:
            return self.update(
                tag_id,
                label=label,
                description=description,
                aliases=aliases,
                drawing_ids=drawing_ids,
                add_drawing_ids=add_drawing_ids,
            )
        return self.add(
            tag_id,
            label=label,
            description=description or "",
            aliases=aliases,
            drawing_ids=drawing_ids or add_drawing_ids,
        )

    def names_for(self, tag: dict[str, Any]) -> set[str]:
        names = {tag["id"], tag.get("label") or ""}
        names.update(tag.get("aliases") or [])
        names.update(tag.get("drawing_ids") or [])
        return {name.lower() for name in names if name}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update the drawing tag catalog")
    parser.add_argument("--tags", type=Path, default=DEFAULT_TAGS_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create a tag")
    add.add_argument("tag_id")
    add.add_argument("--label")
    add.add_argument("--description", default="")
    add.add_argument("--aliases", default="", help="Comma-separated aliases")
    add.add_argument("--drawings", default="", help="Comma-separated drawing template IDs")

    update = sub.add_parser("update", help="Update an existing tag")
    update.add_argument("tag_id")
    update.add_argument("--label")
    update.add_argument("--description")
    update.add_argument("--aliases")
    update.add_argument("--drawings")
    update.add_argument("--add-drawings", default="", help="Append drawing IDs")

    sub.add_parser("list", help="Print all tags")
    return parser


def _split(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def main() -> int:
    args = build_parser().parse_args()
    store = DrawingTagStore(args.tags)
    if args.command == "list":
        print(json.dumps(store.list_tags(), indent=2))
        return 0
    if args.command == "add":
        record = store.add(
            args.tag_id,
            label=args.label,
            description=args.description,
            aliases=_split(args.aliases) or [],
            drawing_ids=_split(args.drawings) or [],
        )
        print(json.dumps(record, indent=2))
        return 0
    record = store.update(
        args.tag_id,
        label=args.label,
        description=args.description,
        aliases=_split(args.aliases),
        drawing_ids=_split(args.drawings),
        add_drawing_ids=_split(args.add_drawings) or [],
    )
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
