#!/usr/bin/env python3
"""Rank a pad drawing with Gemini; use local templates only as fallback.

Gemini sees the PNG and returns tag likelihoods plus a spoken sentence.
Those ranks are used as-is. The drawing-feature database is compared every
time so it is ready, but it only becomes the answer if Gemini fails or
exceeds GEMINI_TIMEOUT_S (default 15).

Usage:
  python recognize.py interpret strokes.json
  python recognize.py interpret strokes.json --image sketch.png
  python recognize.py interpret strokes.json --offline
  python recognize.py demo
"""

from __future__ import annotations

import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from drawing_features import DrawingFeatureStore, load_strokes_file
from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore
from gemini_session import GeminiPatientModel, utc_now

DEFAULT_DB_PATH = Path("drawings_db.json")
RANK_WEIGHTS = (1.0, 0.8, 0.6, 0.4, 0.2)
TOP_K = 5
SKIP_RANK_TAGS = frozenset({"yes", "no"})
DEFAULT_TIMEOUT_S = 15.0


@dataclass
class Candidate:
    tag_id: str
    label: str
    rank: int
    rank_weight: float
    likelihood: float
    feature_score: float
    final_weight: float
    matched_drawing_id: str | None = None
    reason: str = ""
    source: str = "gemini"
    spoken: str = ""
    detail: str = ""


@dataclass
class RecognitionResult:
    top_tag: str | None
    fallback_used: bool
    candidates: list[Candidate] = field(default_factory=list)
    feature_matches: list[dict[str, Any]] = field(default_factory=list)
    spoken: str = ""
    seen: str = ""
    digit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_tag": self.top_tag,
            "fallback_used": self.fallback_used,
            "spoken": self.spoken,
            "candidates": [asdict(item) for item in self.candidates],
            "feature_matches": self.feature_matches,
        }


def feature_score_for_tag(
    tag: dict[str, Any],
    feature_matches: list[dict[str, Any]],
    tag_store: DrawingTagStore,
) -> tuple[float, str | None]:
    names = tag_store.names_for(tag)
    linked = set(tag.get("drawing_ids") or [])
    best = 0.0
    best_id = None
    for match in feature_matches:
        drawing_id = match.get("id")
        label = (match.get("label") or "").lower()
        if drawing_id in linked or drawing_id in names or label in names:
            score = float(match.get("score") or 0.0)
            if score > best:
                best = score
                best_id = drawing_id
    return best, best_id


class IntentRecognizer:
    """Compare a pad drawing, rank tags, and fuse the two scores."""

    def __init__(
        self,
        *,
        root: str | Path = ".",
        drawings_db: str | Path = DEFAULT_DB_PATH,
        tags_path: str | Path = DEFAULT_TAGS_PATH,
        point_count: int = 100,
        model: GeminiPatientModel | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.features = DrawingFeatureStore(self.root / drawings_db, point_count=point_count)
        self.tags = DrawingTagStore(self.root / tags_path)
        self.model = model or GeminiPatientModel(root=self.root)

    def interpret(
        self,
        strokes: Any,
        *,
        image: str | Path | None = None,
        offline: bool = False,
        update_memory: bool = True,
        top_k: int = TOP_K,
    ) -> RecognitionResult:
        feature_matches = self.features.score_all(strokes)
        ranked_features = feature_matches[:top_k]
        catalog = [
            tag for tag in self.tags.catalog_for_model() if tag["id"] not in SKIP_RANK_TAGS
        ]

        if offline:
            result = self._from_features(ranked_features)
        else:
            timeout = float(os.environ.get("GEMINI_TIMEOUT_S", DEFAULT_TIMEOUT_S))
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                ranked = pool.submit(
                    self.model.rank_drawing_tags,
                    catalog,
                    image=image,
                    top_k=top_k,
                ).result(timeout=timeout)
                result = self._from_gemini(ranked.get("rankings") or [], feature_matches)
                digit = str(ranked.get("digit") or "").strip()
                result.digit = digit if digit in {"1", "2"} else ""
                if not result.candidates and not result.digit:
                    raise RuntimeError("Gemini returned no valid tag rankings")
                overall = str(ranked.get("spoken") or "").strip()
                if result.candidates:
                    first_id = (ranked.get("rankings") or [{}])[0].get("tag_id")
                    if not result.candidates[0].spoken and overall:
                        if result.top_tag == first_id or not first_id:
                            result.candidates[0].spoken = overall
                    result.spoken = result.candidates[0].spoken or overall
                result.seen = str(ranked.get("seen") or "").strip()
            except TimeoutError:
                print(f"[recognize] Gemini ranking timed out after {timeout:g}s")
                result = self._from_features(ranked_features)
            except Exception as exc:  # noqa: BLE001 - local features are the fallback
                print(f"[recognize] Gemini ranking failed: {exc!r}")
                result = self._from_features(ranked_features)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        if update_memory:
            self._write_memory(result)
        return result

    def _from_gemini(
        self,
        rankings: list[dict[str, Any]],
        feature_matches: list[dict[str, Any]],
    ) -> RecognitionResult:
        """Use Gemini likelihoods as the score. Feature scores are recorded, not multiplied in."""
        candidates = []
        for index, row in enumerate(rankings[:TOP_K]):
            try:
                tag = self.tags.get(row["tag_id"])
            except KeyError:
                continue
            feature_score, drawing_id = feature_score_for_tag(tag, feature_matches, self.tags)
            likelihood = float(row.get("likelihood") or 0.0)
            candidates.append(
                Candidate(
                    tag_id=tag["id"],
                    label=tag.get("label") or tag["id"],
                    rank=index + 1,
                    rank_weight=1.0,
                    likelihood=likelihood,
                    feature_score=feature_score,
                    final_weight=round(likelihood, 6),
                    matched_drawing_id=drawing_id,
                    reason=row.get("reason") or "",
                    source="gemini",
                    spoken=str(row.get("spoken") or "").strip(),
                    detail=str(row.get("detail") or "").strip().lower(),
                )
            )
        candidates.sort(key=lambda item: item.final_weight, reverse=True)
        for index, item in enumerate(candidates):
            item.rank = index + 1
        top = candidates[0] if candidates else None
        return RecognitionResult(
            top_tag=top.tag_id if top else None,
            fallback_used=False,
            candidates=candidates,
            feature_matches=feature_matches[:TOP_K],
            spoken=top.spoken if top else "",
        )

    def _from_features(self, feature_matches: list[dict[str, Any]]) -> RecognitionResult:
        """Map the best drawing templates onto tags when Gemini is unavailable."""
        candidates = []
        used_tags: set[str] = set()
        for index, match in enumerate(feature_matches[:TOP_K]):
            tag = self._tag_for_drawing(match)
            if tag is None or tag["id"] in used_tags or tag["id"] in SKIP_RANK_TAGS:
                continue
            used_tags.add(tag["id"])
            rank = len(candidates)
            rank_weight = RANK_WEIGHTS[rank]
            score = float(match.get("score") or 0.0)
            candidates.append(
                Candidate(
                    tag_id=tag["id"],
                    label=tag.get("label") or tag["id"],
                    rank=rank + 1,
                    rank_weight=rank_weight,
                    likelihood=1.0,
                    feature_score=score,
                    final_weight=round(rank_weight * score, 6),
                    matched_drawing_id=match.get("id"),
                    reason="Offline feature fallback",
                    source="feature_fallback",
                )
            )
        top = candidates[0].tag_id if candidates else None
        return RecognitionResult(
            top_tag=top,
            fallback_used=True,
            candidates=candidates,
            feature_matches=feature_matches[:TOP_K],
        )

    def _tag_for_drawing(self, match: dict[str, Any]) -> dict[str, Any] | None:
        drawing_id = match.get("id")
        label = (match.get("label") or "").lower()
        for tag in self.tags.list_tags():
            names = self.tags.names_for(tag)
            if drawing_id in (tag.get("drawing_ids") or []) or drawing_id in names or label in names:
                return tag
        return None

    def _write_memory(self, result: RecognitionResult) -> None:
        if not result.candidates:
            return
        lines = [
            f"Recognized intent: {result.top_tag or 'unknown'}",
            f"Fallback used: {result.fallback_used}",
            "Top candidates:",
        ]
        for item in result.candidates:
            lines.append(
                f"- {item.tag_id}: final={item.final_weight:.3f} "
                f"rank_w={item.rank_weight:.1f} likelihood={item.likelihood:.2f} "
                f"feature={item.feature_score:.3f} ({item.source})"
            )
        top = result.candidates[0]
        self.model.graph.ensure_seed(self.model.root)
        self.model.graph.record_intent(
            top.tag_id,
            score=top.final_weight,
            source=top.source,
            drawing_id=top.matched_drawing_id,
            note=f"top of {len(result.candidates)} candidates",
        )
        self.model.daily_graph().record_intent(
            top.tag_id,
            score=top.final_weight,
            source=top.source,
            drawing_id=top.matched_drawing_id,
            note=f"top of {len(result.candidates)} candidates",
        )
        self.model.append_daily_note("\n".join(lines), user_message="pad drawing")

    def confirm(
        self,
        result: RecognitionResult,
        *,
        spoken: str,
        capture_id: str,
        accepted: bool,
    ) -> str:
        """Write journal + graph only after the person has tapped yes or given up."""
        if not result.candidates:
            return ""
        top = next((item for item in result.candidates if item.tag_id == result.top_tag), result.candidates[0])
        if accepted:
            note = spoken.strip() or f"Confirmed {top.tag_id}"
            self.model.graph.ensure_seed(self.model.root)
            self.model.graph.record_intent(
                top.tag_id,
                score=max(top.final_weight, 0.4),
                source=top.source,
                confirmed=True,
                drawing_id=top.matched_drawing_id,
                note=note,
            )
            self.model.daily_graph().record_intent(
                top.tag_id,
                score=max(top.final_weight, 0.4),
                source=top.source,
                confirmed=True,
                drawing_id=top.matched_drawing_id,
                note=note,
            )
            journal = f"Confirmed intent {top.tag_id}: {note}"
        else:
            journal = (
                f"Guesses rejected for capture {capture_id}: "
                + ", ".join(item.tag_id for item in result.candidates[:3])
            )
        path = self.model.ensure_daily_history()
        block = [
            f"## {utc_now().isoformat()}",
            f"User: pad drawing {capture_id}",
            f"Summary: {journal}",
            "",
        ]
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(block) + "\n")
        return journal if accepted else ""

    def patch_graphs(self, *, capture_id: str, journal: str) -> None:
        """Optional LLM links on top of the local graph write. May be slow."""
        if not journal:
            return
        self.model.update_graphs_with_llm(f"pad drawing {capture_id}", journal)


def _circle(n: int = 120, radius: float = 20.0, origin: tuple[float, float] = (0.0, 0.0)) -> list[list[float]]:
    return [
        [origin[0] + radius * math.cos(2 * math.pi * i / n), origin[1] + radius * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]


def _bowl(n: int = 80) -> list[list[float]]:
    return [[-20 + 40 * i / (n - 1), 8 * math.sin(math.pi * i / (n - 1))] for i in range(n)]


def _cross() -> list[list[list[float]]]:
    return [
        [[0, -12], [0, 12]],
        [[-12, 0], [12, 0]],
    ]


def _bed() -> list[list[list[float]]]:
    return [[[-16, 0], [16, 0], [16, 8], [-16, 8], [-16, 0]]]


def _check() -> list[list[float]]:
    return [[-10, 0], [-3, -8], [12, 10]]


def _xmark() -> list[list[list[float]]]:
    return [[[-10, -10], [10, 10]], [[-10, 10], [10, -10]]]


def seed_example_templates(features: DrawingFeatureStore) -> None:
    templates = {
        "cup": [_circle()],
        "bowl": [_bowl()],
        "cross": _cross(),
        "bed": _bed(),
        "check": [_check()],
        "xmark": _xmark(),
    }
    for drawing_id, strokes in templates.items():
        features.upsert(strokes, drawing_id, label=drawing_id)


def run_demo(root: Path, offline: bool) -> RecognitionResult:
    features = DrawingFeatureStore(root / DEFAULT_DB_PATH)
    seed_example_templates(features)
    recognizer = IntentRecognizer(root=root)
    query = [_circle(n=90, radius=18.0, origin=(4.0, -2.0))]
    return recognizer.interpret(query, offline=offline, update_memory=False)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path("."))

    parser = argparse.ArgumentParser(description="Recognize a pad drawing")
    sub = parser.add_subparsers(dest="command", required=True)

    interpret = sub.add_parser("interpret", parents=[common], help="Score strokes against tags")
    interpret.add_argument("strokes", type=Path)
    interpret.add_argument("--image", type=Path)
    interpret.add_argument("--offline", action="store_true", help="Skip Gemini and use features only")
    interpret.add_argument("--no-memory", action="store_true", help="Do not append daily_history")

    demo = sub.add_parser(
        "demo",
        parents=[common],
        help="Seed example templates and recognize a circle-like cup",
    )
    demo.add_argument("--offline", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "demo":
        result = run_demo(args.root, offline=args.offline)
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    recognizer = IntentRecognizer(root=args.root)
    strokes = load_strokes_file(args.strokes)
    result = recognizer.interpret(
        strokes,
        image=args.image,
        offline=args.offline,
        update_memory=not args.no_memory,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
