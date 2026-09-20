#!/usr/bin/env python3
"""Second-brain memory graph for patient context.

Nodes (person, intents, drawings, days, events, interests) and weighted edges
are stored in memory_graph.json. The same payload drives the /brain view.

Usage:
  python memory_graph.py show
  python memory_graph.py seed
  python memory_graph.py seed --demo
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_GRAPH_PATH = Path("memory_graph.json")
NODE_TYPES = ("person", "intent", "drawing", "interest", "preference", "day", "event", "theme")

TYPE_COLORS = {
    "person": "#5b9dff",
    "intent": "#46c98b",
    "drawing": "#5ec8d4",
    "interest": "#8b7cff",
    "preference": "#c9a46b",
    "day": "#7b8494",
    "event": "#ef8f5f",
    "theme": "#d47cb0",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text or "node"


class MemoryGraph:
    """Local knowledge graph that grows with each pad interaction."""

    def __init__(self, path: str | Path = DEFAULT_GRAPH_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {
            "updated_at": None,
            "nodes": {},
            "edges": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        self._data["updated_at"] = loaded.get("updated_at")
        self._data["nodes"] = loaded.get("nodes", {})
        self._data["edges"] = loaded.get("edges", {})

    def save(self) -> None:
        self._data["updated_at"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self._data["nodes"].get(node_id)

    def upsert_node(
        self,
        node_id: str,
        *,
        ntype: str,
        label: str,
        weight: float = 1.0,
        **props: Any,
    ) -> dict[str, Any]:
        now = utc_now()
        node = self._data["nodes"].get(node_id)
        if node is None:
            node = {
                "id": node_id,
                "type": ntype,
                "label": label,
                "weight": 0.0,
                "created_at": now,
                "last_seen": now,
                "props": {},
            }
        node["label"] = label
        node["type"] = ntype
        node["weight"] = round(float(node.get("weight") or 0.0) + weight, 4)
        node["last_seen"] = now
        node.setdefault("props", {}).update({k: v for k, v in props.items() if v is not None})
        self._data["nodes"][node_id] = node
        return node

    def link(
        self,
        src: str,
        relation: str,
        target: str,
        *,
        weight: float = 1.0,
        **props: Any,
    ) -> dict[str, Any]:
        edge_id = f"{src}|{relation}|{target}"
        now = utc_now()
        edge = self._data["edges"].get(edge_id)
        if edge is None:
            edge = {
                "id": edge_id,
                "source": src,
                "relation": relation,
                "target": target,
                "weight": 0.0,
                "created_at": now,
                "last_seen": now,
                "props": {},
            }
        edge["weight"] = round(float(edge.get("weight") or 0.0) + weight, 4)
        edge["last_seen"] = now
        edge.setdefault("props", {}).update({k: v for k, v in props.items() if v is not None})
        self._data["edges"][edge_id] = edge
        return edge

    def person_id(self) -> str:
        for node in self._data["nodes"].values():
            if node["type"] == "person":
                return node["id"]
        return "person:patient"

    def seed_from_patient(self, patient: dict[str, Any]) -> None:
        name = patient.get("preferred_name") or patient.get("name") or "Patient"
        person = "person:patient"
        self.upsert_node(
            person,
            ntype="person",
            label=str(name),
            weight=0,
            full_name=patient.get("name"),
            age=patient.get("age"),
            notes=patient.get("notes"),
        )
        prefs = patient.get("preferences") or {}
        if prefs.get("tone"):
            pref_id = "preference:tone"
            self.upsert_node(pref_id, ntype="preference", label=str(prefs["tone"]), weight=0)
            self.link(person, "prefers", pref_id, weight=0)
        for interest in prefs.get("interests") or []:
            interest_id = f"interest:{slug(str(interest))}"
            self.upsert_node(interest_id, ntype="interest", label=str(interest), weight=0)
            self.link(person, "interested_in", interest_id, weight=0)
        comm = patient.get("communication") or {}
        for intent in comm.get("typical_intents") or []:
            intent_id = f"intent:{slug(str(intent))}"
            self.upsert_node(intent_id, ntype="intent", label=str(intent), weight=0)
            self.link(person, "uses_intent", intent_id, weight=0)
        self.save()

    def seed_from_tags(self, tags: list[dict[str, Any]]) -> None:
        person = self.person_id()
        for tag in tags:
            intent_id = f"intent:{slug(tag['id'])}"
            self.upsert_node(
                intent_id,
                ntype="intent",
                label=tag.get("label") or tag["id"],
                weight=0,
                description=tag.get("description"),
                aliases=tag.get("aliases") or [],
            )
            self.link(person, "uses_intent", intent_id, weight=0)
            for drawing_id in tag.get("drawing_ids") or []:
                draw = f"drawing:{slug(str(drawing_id))}"
                self.upsert_node(draw, ntype="drawing", label=str(drawing_id), weight=0)
                self.link(intent_id, "expressed_by", draw, weight=0)
        self.save()

    def ensure_seed(self, root: str | Path = ".") -> None:
        """Create the skeleton from patient + tags if the graph is empty."""
        root_path = Path(root)
        if self._data["nodes"]:
            return
        patient_path = root_path / "patient_data.json"
        tags_path = root_path / "drawing_tags.json"
        if patient_path.exists():
            self.seed_from_patient(json.loads(patient_path.read_text(encoding="utf-8")))
        if tags_path.exists():
            tags = json.loads(tags_path.read_text(encoding="utf-8")).get("tags", {})
            self.seed_from_tags(list(tags.values()) if isinstance(tags, dict) else tags)

    def _day_id(self, on: date | None = None) -> str:
        day = on or date.today()
        day_id = f"day:{day.isoformat()}"
        self.upsert_node(day_id, ntype="day", label=day.strftime("%b %d"), weight=0, iso=day.isoformat())
        self.link(self.person_id(), "active_on", day_id, weight=0)
        return day_id

    def record_intent(
        self,
        tag_id: str,
        *,
        score: float = 1.0,
        source: str = "fused",
        confirmed: bool | None = None,
        drawing_id: str | None = None,
        on: date | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        person = self.person_id()
        intent_id = f"intent:{slug(tag_id)}"
        day_id = self._day_id(on)
        self.upsert_node(intent_id, ntype="intent", label=tag_id, weight=score)
        self.link(person, "requested", intent_id, weight=score, source=source)
        self.link(intent_id, "occurred_on", day_id, weight=score)
        if drawing_id:
            draw = f"drawing:{slug(drawing_id)}"
            self.upsert_node(draw, ntype="drawing", label=drawing_id, weight=score)
            self.link(intent_id, "expressed_by", draw, weight=score)
        event_id = f"event:{utc_now()}:{slug(tag_id)}:{uuid.uuid4().hex[:8]}"
        label = f"{tag_id} ({score:.2f})"
        self.upsert_node(
            event_id,
            ntype="event",
            label=label,
            weight=score,
            source=source,
            confirmed=confirmed,
            note=note[:200],
            iso_date=(on or date.today()).isoformat(),
        )
        self.link(person, "had_event", event_id, weight=score)
        self.link(event_id, "about", intent_id, weight=score)
        self.link(event_id, "on_day", day_id, weight=score)
        self.save()
        return self.get_node(event_id) or {}

    def resolve_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return "theme:unknown"
        if text in self._data["nodes"]:
            return text
        for prefix in NODE_TYPES:
            candidate = f"{prefix}:{slug(text)}"
            if candidate in self._data["nodes"]:
                return candidate
        if ":" in text:
            return text
        return f"theme:{slug(text)}"

    def apply_patch(self, patch: dict[str, Any] | None) -> None:
        """Merge LLM-proposed nodes and edges into this graph."""
        if not patch:
            return
        for node in patch.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            label = str(node.get("label") or node.get("id") or "theme").strip()
            ntype = str(node.get("type") or "theme")
            if ntype not in NODE_TYPES:
                ntype = "theme"
            node_id = self.resolve_id(str(node.get("id") or f"{ntype}:{slug(label)}"))
            try:
                weight = float(node.get("weight") or 0.5)
            except (TypeError, ValueError):
                weight = 0.5
            self.upsert_node(
                node_id,
                ntype=ntype,
                label=label,
                weight=max(0.1, min(2.0, weight)),
                note=str(node.get("note") or "")[:240] or None,
            )
        for edge in patch.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            src = self.resolve_id(str(edge.get("from") or edge.get("source") or ""))
            dst = self.resolve_id(str(edge.get("to") or edge.get("target") or ""))
            if not src or not dst or src == dst:
                continue
            if src not in self._data["nodes"]:
                self.upsert_node(src, ntype="theme", label=src.split(":")[-1], weight=0.2)
            if dst not in self._data["nodes"]:
                self.upsert_node(dst, ntype="theme", label=dst.split(":")[-1], weight=0.2)
            relation = slug(str(edge.get("relation") or "related")).replace("-", "_") or "related"
            try:
                weight = float(edge.get("weight") or 0.5)
            except (TypeError, ValueError):
                weight = 0.5
            self.link(src, relation, dst, weight=max(0.1, min(2.0, weight)))
        self.save()

    def compact_for_llm(self, max_events: int = 12) -> dict[str, Any]:
        nodes = list(self._data["nodes"].values())
        events = [node for node in nodes if node["type"] == "event"]
        events.sort(key=lambda node: node.get("last_seen") or "", reverse=True)
        keep = [node for node in nodes if node["type"] != "event"] + events[:max_events]
        keep_ids = {node["id"] for node in keep}
        edges = [
            edge
            for edge in self._data["edges"].values()
            if edge["source"] in keep_ids and edge["target"] in keep_ids
        ]
        return {
            "nodes": [
                {
                    "id": node["id"],
                    "type": node["type"],
                    "label": node["label"],
                    "weight": node.get("weight", 0),
                }
                for node in keep
            ],
            "edges": [
                {
                    "from": edge["source"],
                    "relation": edge["relation"],
                    "to": edge["target"],
                    "weight": edge.get("weight", 0),
                }
                for edge in edges
            ],
        }

    def context_summary(self, limit: int = 8) -> str:
        """Short graph readout for Gemini prompts."""
        intents = [
            node
            for node in self._data["nodes"].values()
            if node["type"] == "intent" and node.get("weight", 0) > 0
        ]
        intents.sort(key=lambda node: node.get("weight", 0), reverse=True)
        events = [
            node
            for node in self._data["nodes"].values()
            if node["type"] == "event"
        ]
        events.sort(key=lambda node: node.get("last_seen") or "", reverse=True)
        links = sorted(
            self._data["edges"].values(),
            key=lambda edge: edge.get("weight", 0),
            reverse=True,
        )
        if not intents and not events:
            return "Memory graph has no recorded events yet."
        parts = []
        if intents:
            parts.append("Frequent intents:")
            for node in intents[:limit]:
                parts.append(f"- {node['label']} (weight {node['weight']:.2f})")
        if events:
            parts.append("Recent events:")
            for node in events[:limit]:
                parts.append(f"- {node['label']}")
        if links:
            parts.append("Strongest links:")
            for edge in links[:limit]:
                src = self._data["nodes"].get(edge["source"], {})
                dst = self._data["nodes"].get(edge["target"], {})
                parts.append(
                    f"- {src.get('label', edge['source'])} {edge['relation']} "
                    f"{dst.get('label', edge['target'])} ({edge.get('weight', 0):.2f})"
                )
        return "\n".join(parts)

    def record_interaction(
        self,
        user_message: str,
        summary: str,
        *,
        on: date | None = None,
    ) -> None:
        person = self.person_id()
        day_id = self._day_id(on)
        text = f"{user_message} {summary}".lower()
        mentioned = []
        for node in list(self._data["nodes"].values()):
            if node["type"] not in {"intent", "interest"}:
                continue
            names = {node["label"].lower(), node["id"].split(":", 1)[-1]}
            names.update(str(alias).lower() for alias in (node.get("props") or {}).get("aliases") or [])
            if any(name and name in text for name in names):
                mentioned.append(node["id"])
                self.upsert_node(node["id"], ntype=node["type"], label=node["label"], weight=0.35)
                relation = "requested" if node["type"] == "intent" else "mentioned"
                self.link(person, relation, node["id"], weight=0.35)

        event_id = f"event:{utc_now()}:note:{uuid.uuid4().hex[:8]}"
        snippet = (summary or user_message).strip().replace("\n", " ")[:80]
        self.upsert_node(event_id, ntype="event", label=snippet or "note", weight=0.4, note=summary[:240], iso_date=(on or date.today()).isoformat())
        self.link(person, "had_event", event_id, weight=0.4)
        self.link(event_id, "on_day", day_id, weight=0.4)
        for node_id in mentioned:
            self.link(event_id, "about", node_id, weight=0.4)
        self.save()

    def vis_payload(self, on: date | None = None) -> dict[str, Any]:
        keep: set[str] | None = None
        if on is not None:
            day_id = f"day:{on.isoformat()}"
            iso = on.isoformat()
            keep = set()
            for node in self._data["nodes"].values():
                ntype = node["type"]
                if ntype in {"person", "intent", "drawing", "interest", "preference", "theme"}:
                    keep.add(node["id"])
                elif node["id"] == day_id:
                    keep.add(node["id"])
                elif ntype == "event":
                    iso_date = str((node.get("props") or {}).get("iso_date") or "")
                    linked = any(
                        day_id in (edge["source"], edge["target"])
                        and node["id"] in (edge["source"], edge["target"])
                        for edge in self._data["edges"].values()
                    )
                    if iso_date == iso or linked:
                        keep.add(node["id"])
            for edge in self._data["edges"].values():
                if day_id in (edge["source"], edge["target"]):
                    other = edge["target"] if edge["source"] == day_id else edge["source"]
                    keep.add(other)
                    keep.add(day_id)
        nodes = []
        for node in self._data["nodes"].values():
            if keep is not None and node["id"] not in keep:
                continue
            weight = max(float(node.get("weight") or 0.0), 0.0)
            size = 14 + min(36, weight * 8)
            if node["type"] == "person":
                size = 42
            if node["type"] == "event":
                size = 10 + min(16, weight * 4)
            props = dict(node.get("props") or {})
            image = None
            if node["type"] == "drawing":
                label = str(node.get("label") or "")
                if "T" in label:
                    image = f"/captures/{label}.png"
                    props.setdefault("image", image)
            nodes.append(
                {
                    "id": node["id"],
                    "label": node["label"],
                    "group": node["type"],
                    "value": size,
                    "title": f"{node['type']}: {node['label']}",
                    "color": TYPE_COLORS.get(node["type"], "#8b93a3"),
                    "font": {"color": "#e7e9ee", "size": 13 if node["type"] != "event" else 11},
                    "weight": weight,
                    "last_seen": node.get("last_seen"),
                    "props": props,
                    "image": image,
                }
            )
        edges = []
        for edge in self._data["edges"].values():
            if keep is not None and (edge["source"] not in keep or edge["target"] not in keep):
                continue
            weight = max(float(edge.get("weight") or 0.15), 0.15)
            edges.append(
                {
                    "id": edge["id"],
                    "from": edge["source"],
                    "to": edge["target"],
                    "label": edge["relation"].replace("_", " "),
                    "value": weight,
                    "width": 0.6 + min(6.0, weight * 1.4),
                    "color": {"color": "#3a4252", "highlight": "#5b9dff"},
                    "font": {"color": "#8b93a3", "size": 10, "align": "middle"},
                    "weight": weight,
                    "relation": edge["relation"],
                }
            )
        counts: dict[str, int] = {}
        for node in nodes:
            group = node["group"]
            counts[group] = counts.get(group, 0) + 1
        return {
            "updated_at": self._data.get("updated_at"),
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "by_type": counts,
            },
            "legend": [{"type": key, "color": TYPE_COLORS[key]} for key in NODE_TYPES if key in counts],
            "nodes": nodes,
            "edges": edges,
        }

    def seed_demo_day(self) -> None:
        """Today-only slice for the daily brain."""
        today = date.today()
        story = [
            (today, "water", 0.92, "cup", "Drew a cup this morning"),
            (today, "yes", 0.8, "check", "Confirmed water"),
            (today, "food", 0.55, "bowl", "Mentioned being hungry later"),
        ]
        for on, tag, score, drawing, note in story:
            self.record_intent(
                tag,
                score=score,
                source="demo",
                confirmed=tag in {"water", "yes"},
                drawing_id=drawing,
                on=on,
                note=note,
            )
        water = "intent:water"
        food = "intent:food"
        yes = "intent:yes"
        if water in self._data["nodes"] and yes in self._data["nodes"]:
            self.link(yes, "confirmed", water, weight=0.9)
        if water in self._data["nodes"] and food in self._data["nodes"]:
            self.link(water, "followed_by", food, weight=0.4)
        self.save()

    def seed_demo_week(self) -> None:
        """Fictional week of activity so judges see a populated second brain."""
        today = date.today()
        story = [
            (today - timedelta(days=4), "water", 0.9, "cup", "Asked for water after a walk"),
            (today - timedelta(days=4), "food", 0.7, "bowl", "Wanted lunch"),
            (today - timedelta(days=3), "water", 0.85, "cup", "Thirsty mid-morning"),
            (today - timedelta(days=3), "rest", 0.6, "bed", "Needed a nap"),
            (today - timedelta(days=2), "help", 0.95, "cross", "Called for a caregiver"),
            (today - timedelta(days=2), "water", 0.8, "cup", "Water after help resolved"),
            (today - timedelta(days=1), "food", 0.75, "bowl", "Hungry in the evening"),
            (today - timedelta(days=1), "rest", 0.7, "bed", "Ready to sleep"),
            (today, "water", 0.92, "cup", "Drew a cup this morning"),
            (today, "yes", 0.65, "check", "Confirmed water"),
        ]
        garden = "interest:gardening"
        if garden in self._data["nodes"]:
            self.upsert_node(garden, ntype="interest", label="gardening", weight=1.2)
            self.link(self.person_id(), "mentioned", garden, weight=1.2)
        for on, tag, score, drawing, note in story:
            self.record_intent(
                tag,
                score=score,
                source="demo",
                confirmed=tag in {"water", "help", "yes"},
                drawing_id=drawing,
                on=on,
                note=note,
            )
        self.save()


def load_root_graph(root: str | Path = ".") -> MemoryGraph:
    root_path = Path(root)
    graph = MemoryGraph(root_path / DEFAULT_GRAPH_PATH)
    graph.ensure_seed(root_path)
    return graph


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patient second-brain memory graph")
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="Print graph stats and vis payload summary")
    seed = sub.add_parser("seed", help="Build skeleton from patient_data + tags")
    seed.add_argument("--demo", action="store_true", help="Add a fictional week of activity")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    graph = load_root_graph(args.root)
    if args.command == "seed":
        graph.ensure_seed(args.root)
        if args.demo:
            graph.seed_demo_week()
        print(json.dumps(graph.vis_payload()["stats"], indent=2))
        return 0
    payload = graph.vis_payload()
    print(json.dumps({"updated_at": payload["updated_at"], "stats": payload["stats"]}, indent=2))
    print(graph.context_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
