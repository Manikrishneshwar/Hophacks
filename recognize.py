#!/usr/bin/env python3
"""Rank a pad drawing with Gemini, then with a local model, then with templates.

Gemini sees the PNG and returns tag likelihoods plus a spoken sentence.
A close match against the labelled priors can break a near-tie among those
ranks, but it cannot invent a tag Gemini omitted or overturn a clear winner.
If every Gemini model fails or the call exceeds GEMINI_TIMEOUT_S (default 15),
a vision model on this machine reads the PNG instead (see local_vision.py).
Templates answer only when both of those are unavailable.

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

import local_vision
import stroke_geometry
from drawing_features import DrawingFeatureStore, load_strokes_file
from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore
from gemini_session import GeminiPatientModel, utc_now

DEFAULT_DB_PATH = Path("drawings_db.json")
RANK_WEIGHTS = (1.0, 0.8, 0.6, 0.4, 0.2)
TOP_K = 5
SKIP_RANK_TAGS = frozenset({"yes", "no"})
DEFAULT_TIMEOUT_S = 15.0

# Below this a ranking is treated as no answer at all, and the next tier is asked.
# A model that has actually read the drawing returns 0.8 or more; the values this
# rejects are what comes back when it answered a different question, which is
# 0.05 to 0.10 in practice. Must stay under `local_vision.LOCAL_LIKELIHOOD`, or
# the on-machine model's own answers would be thrown away as well.
MIN_USABLE_LIKELIHOOD = 0.25

# When a new drawing is close to a stored prior, mix that score into Gemini's
# rank. Weak feature hits stay ignored so a cartoon cup cannot overturn a 0.95
# food reading of an apple. 0.3 is enough to break a 0.55/0.50 tie.
FEATURE_BLEND_MIN = 0.6
FEATURE_BLEND_WEIGHT = 0.3


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
    # "geometry", "gemini", or "" when no digit was read.
    # Geometry is repeatable; a Gemini one is not.
    digit_source: str = ""
    digit_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_tag": self.top_tag,
            "fallback_used": self.fallback_used,
            "spoken": self.spoken,
            "digit": self.digit,
            "digit_source": self.digit_source,
            "digit_reason": self.digit_reason,
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
    tag_id = tag["id"]
    best = 0.0
    best_id = None
    for match in feature_matches:
        drawing_id = match.get("id")
        label = (match.get("label") or "").lower()
        meta = match.get("metadata") or {}
        meta_tag = str(meta.get("tag_id") or "")
        if meta_tag == "_digit":
            continue
        if (
            drawing_id in linked
            or drawing_id in names
            or label in names
            or meta_tag == tag_id
        ):
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
        local_digit = stroke_geometry.detect_digit(strokes)
        try:
            feature_matches = self.features.score_all(strokes)
        except ValueError as exc:
            # Unusable stroke data must not sink the whole capture: the PNG is
            # what Gemini and the local model read, and either can still answer
            # without a single parsed point. Only templates need the strokes,
            # and they are the fallback of last resort anyway. Reachable only
            # once the template database has entries, since an empty one never
            # looks at the strokes at all.
            print(f"[recognize] stroke features unavailable: {exc}")
            feature_matches = []
        ranked_features = feature_matches[:top_k]
        catalog = [
            tag for tag in self.tags.catalog_for_model() if tag["id"] not in SKIP_RANK_TAGS
        ]

        # An unmistakable 1 or 2 needs no model. Answering from geometry keeps
        # the game instant and reachable with no key and no network, and no
        # candidates are invented for a drawing that was never an intent.
        if local_digit.fast_path:
            print(f"[recognize] digit   {local_digit.digit} from geometry: {local_digit.reason}")
            result = RecognitionResult(
                top_tag=None,
                fallback_used=False,
                feature_matches=ranked_features,
                digit=local_digit.digit,
                digit_source="geometry",
                digit_reason=local_digit.reason,
            )
            if update_memory:
                self._write_memory(result)
            return result

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
                vendor = str(ranked.get("vendor") or "").strip()
                if vendor and vendor != "gemini":
                    for item in result.candidates:
                        item.source = vendor
                digit = str(ranked.get("digit") or "").strip()
                result.digit = digit if digit in {"1", "2"} else ""

                # A claimed digit that geometry refuses means the model was
                # answering the digit question, not the intent one, so whatever
                # ranking came back beside it was never really about the drawing.
                # Long curved shapes such as a telephone handset trigger this,
                # and the leftover tag arrives with a likelihood near zero.
                if result.digit and local_digit.veto:
                    raise RuntimeError(
                        f"claimed digit {result.digit} refused by geometry "
                        f"({local_digit.reason}); its ranking is not trustworthy either"
                    )
                if not result.candidates and not result.digit:
                    raise RuntimeError("Gemini returned no valid tag rankings")
                # A real reading of a drawing scores 0.8 or better. Speaking a 0.05
                # guess aloud with full confidence is how a handset became "I would
                # like a glass of water". A digit needs no tag, so it is exempt.
                if result.candidates and not result.digit:
                    best = result.candidates[0].likelihood
                    if best < MIN_USABLE_LIKELIHOOD:
                        raise RuntimeError(
                            f"top tag {result.top_tag!r} scored only {best:.2f}, "
                            f"under the {MIN_USABLE_LIKELIHOOD:g} floor"
                        )
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
                result = self._without_gemini(catalog, ranked_features, feature_matches, image)
            except Exception as exc:  # noqa: BLE001 - local features are the fallback
                print(f"[recognize] Gemini ranking failed: {exc!r}")
                result = self._without_gemini(catalog, ranked_features, feature_matches, image)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        self._settle_digit(result, local_digit)

        if update_memory:
            self._write_memory(result)
        return result

    def _without_gemini(
        self,
        catalog: list[dict[str, Any]],
        ranked_features: list[dict[str, Any]],
        feature_matches: list[dict[str, Any]],
        image: str | Path | None,
    ) -> RecognitionResult:
        """Read the drawing on this machine, and only then fall back to templates.

        Templates are a poor last resort: they can only recognise a drawing
        someone already seeded, and an unseeded database answers every capture
        the same way. A local vision model at least looks at the picture.
        """
        if image is not None and local_vision.available():
            try:
                ranked = local_vision.rank_tags(catalog, image=image)
            except Exception as exc:  # noqa: BLE001 - templates are the next fallback
                print(f"[recognize] local vision failed: {exc!r}")
            else:
                result = self._from_gemini(ranked.get("rankings") or [], feature_matches)
                if result.candidates:
                    for item in result.candidates:
                        item.source = "local"
                    # Flagged as a fallback so the caretaker view and the logs do
                    # not present a local guess with Gemini's authority.
                    result.fallback_used = True
                    result.spoken = result.candidates[0].spoken
                    result.seen = str(ranked.get("seen") or "").strip()
                    print(
                        f"[recognize] local   {result.top_tag!r} from "
                        f"{local_vision.model_name()}: {result.seen or 'no description'}"
                    )
                    return result
                print("[recognize] local vision returned nothing usable")
        return self._from_features(ranked_features)

    def _settle_digit(
        self,
        result: RecognitionResult,
        local: stroke_geometry.DigitGuess,
    ) -> None:
        """Reconcile the digit Gemini reported with what the strokes measure.

        Geometry is the authority on whether a drawing can be a digit at all, so
        a veto wins: that fires when the main stroke closes on itself or there
        are too many strokes, which a cup or a face does and a character never
        does. Otherwise geometry only ever adds a digit Gemini left empty, which
        is the common failure now that the digit is one field in a prompt
        otherwise devoted to intents.
        """
        if result.digit and local.veto:
            print(f"[recognize] digit   dropped Gemini {result.digit}: {local.reason}")
            result.digit = ""
            result.digit_reason = local.reason
            return
        if result.digit:
            result.digit_source = "gemini"
            result.digit_reason = "reported by Gemini"
            return
        if local.assists:
            print(f"[recognize] digit   {local.digit} from geometry: {local.reason}")
            result.digit = local.digit
            result.digit_source = "geometry"
            result.digit_reason = local.reason
            return
        result.digit_reason = local.reason

    def _from_gemini(
        self,
        rankings: list[dict[str, Any]],
        feature_matches: list[dict[str, Any]],
    ) -> RecognitionResult:
        """Gemini likelihoods first; one close prior can break a near-tie.

        Feature scores used to be recorded and ignored. That was right while the
        database was a handful of cartoon outlines. The labelled priors are
        close enough to this person's hand that a 0.55/0.50 Gemini split is
        worth breaking when exactly one candidate matches a stored drawing.
        Two strong matches stay ignored, and a 0.95 food rank still beats a
        cup template.
        """
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
        # Only mix a prior in when exactly one Gemini candidate is close to a
        # stored drawing. Two strong matches (apple vs cup, both round) are
        # noise, and hold-one-out on the priors is well under perfect.
        strong = [item for item in candidates if item.feature_score >= FEATURE_BLEND_MIN]
        if len(strong) == 1:
            item = strong[0]
            item.final_weight = round(
                (1.0 - FEATURE_BLEND_WEIGHT) * item.likelihood
                + FEATURE_BLEND_WEIGHT * item.feature_score,
                6,
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
        for match in feature_matches:
            if len(candidates) >= TOP_K:
                break
            tag = self._tag_for_drawing(match)
            if tag is None or tag["id"] in used_tags or tag["id"] in SKIP_RANK_TAGS:
                continue
            used_tags.add(tag["id"])
            rank = len(candidates)
            rank_weight = RANK_WEIGHTS[rank]
            score = float(match.get("score") or 0.0)
            meta = match.get("metadata") or {}
            detail = str(meta.get("detail") or "").strip().lower()
            if detail in {"", "1", "2"} or detail == tag["id"]:
                detail = ""
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
                    detail=detail,
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
        meta = match.get("metadata") or {}
        meta_tag = str(meta.get("tag_id") or "").strip()
        if meta_tag == "_digit":
            return None
        if meta_tag:
            try:
                return self.tags.get(meta_tag)
            except KeyError:
                pass
        drawing_id = match.get("id")
        label = (match.get("label") or "").lower()
        if label in {"1", "2"}:
            return None
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
