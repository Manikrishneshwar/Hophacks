"""Step 2: what happens to a capture once it is on disk.

`process_capture` ranks the drawing with Gemini and returns a spoken sentence
(plus the ranking, so confirmation can retry). Local drawing templates are
used only when Gemini fails or times out. `SAMPLE_TEXT` is the last fallback
when ranking is empty or `INK_RECOGNITION=0`.

It runs on a worker thread after the upload has already been answered, so it is
free to block on a slow network call.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

# Stands in when ranking has nothing to say, and for tests that disable
# recognition so they are not calling Gemini.
SAMPLE_TEXT = "I would like a glass of water, please."

# Last resort if Gemini does not return a spoken line.
PHRASES = {
    "water": SAMPLE_TEXT,
    "food": "I would like something to eat, please.",
    "help": "I need help, please.",
    "rest": "I would like to rest, please.",
    "yes": "Yes.",
    "no": "No.",
}

MAX_GUESSES = 3
MAX_FOLLOWUPS = 2
FOLLOWUP_TAGS = {"food", "water", "help", "rest"}
GENERIC_DETAILS = {"", "food", "water", "help", "rest", "drink", "eat", "yes", "no"}
CLOSING_TIMEOUT_S = 8.0
FOLLOWUP_BLOCKLIST = {
    "food": {"tea", "water", "coffee", "drink", "juice"},
    "water": {"soup", "pizza", "sandwich", "apple", "food", "oatmeal", "toast", "tea", "coffee"},
    "help": {"soup", "tea", "pizza", "water", "apple", "sandwich"},
    "rest": {"soup", "tea", "pizza", "water", "apple"},
}

NAMED_DRINKS = ("water", "tea", "coffee", "juice")
NAMED_FOODS = ("apple", "pizza", "soup", "sandwich", "toast", "oatmeal")

FALLBACK_CLOSINGS = {
    "food": "I'll get that for you.",
    "water": "I'll get you some water.",
    "help": "Someone is on the way.",
    "rest": "Rest easy.",
}


def caretaker() -> dict[str, Any]:
    path = config.ROOT / "patient_data.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    info = data.get("caretaker")
    return info if isinstance(info, dict) else {}


def caretaker_followup() -> dict[str, str]:
    info = caretaker()
    name = str(info.get("name") or "your caretaker").strip()
    first = name.split()[0] if name else "them"
    return {
        "spoken": f"Should I call {name}?",
        "question": f"Call {first}?",
        "detail": "call",
    }


def default_followups(tag_id: str | None) -> list[dict[str, str]]:
    if tag_id == "food":
        return [
            {"spoken": "Would you like soup?", "question": "Soup?", "detail": "soup"},
            {"spoken": "A sandwich instead?", "question": "Sandwich?", "detail": "sandwich"},
        ]
    if tag_id == "help":
        return [
            caretaker_followup(),
            {"spoken": "Do you need the bathroom?", "question": "Bathroom?", "detail": "bathroom"},
        ]
    return []


def is_specific(detail: str | None, tag_id: str | None = None) -> bool:
    token = (detail or "").strip().lower()
    return bool(token) and token not in GENERIC_DETAILS and token != (tag_id or "")


def already_named(spoken: str, detail: str | None) -> bool:
    token = (detail or "").strip().lower()
    if not token:
        return False
    return token in (spoken or "").lower()


def needs_followup(tag_id: str | None, spoken: str, detail: str | None = None) -> bool:
    """False when the request is already specific enough for a real assistant."""
    if tag_id not in FOLLOWUP_TAGS:
        return False
    text = (spoken or "").lower()
    if already_named(spoken, detail) and is_specific(detail, tag_id):
        return False
    if tag_id == "water":
        return not any(word in text for word in NAMED_DRINKS)
    if tag_id == "food":
        return not any(word in text for word in NAMED_FOODS)
    if tag_id == "help":
        return "call" not in text
    if tag_id == "rest":
        return False
    return False


def question_for(detail: str) -> str:
    label = " ".join((detail or "").split())
    if not label:
        return "?"
    if not label.endswith("?"):
        label = f"{label[0].upper()}{label[1:]}?"
    return label


def fallback_closing(tag_id: str | None, detail: str | None = None) -> str:
    token = (detail or "").strip().lower()
    if token in {"call", "caretaker"}:
        name = str(caretaker().get("name") or "your caretaker").strip()
        return f"Calling {name} now."
    if is_specific(detail, tag_id):
        name = (detail or "").strip()
        if tag_id == "food":
            return f"I'll get you the {name}."
        if tag_id == "water":
            return f"I'll get you some {name}."
        if tag_id == "help":
            return f"I'll help with the {name}."
        if tag_id == "rest":
            return "Rest easy."
    if tag_id == "water":
        return "I'll get you some water."
    return FALLBACK_CLOSINGS.get(tag_id or "", "Okay.")


def _followup_allowed(tag_id: str | None, detail: str, spoken: str = "") -> bool:
    token = (detail or "").strip().lower()
    if not token:
        return False
    blocked = set(FOLLOWUP_BLOCKLIST.get(tag_id or "", set()))
    text = (spoken or "").lower()
    if tag_id == "water" and "water" in text:
        blocked.update({"tea", "coffee", "juice"})
    return token not in blocked


_recognizer = None


@dataclass
class CaptureResult:
    text: str
    tag_id: str | None = None
    reason: str = ""
    fallback_used: bool = False
    candidates: list[Any] = field(default_factory=list)
    recognition: Any = None
    detail: str | None = None


def as_capture_result(raw: CaptureResult | str | None) -> CaptureResult | None:
    if raw is None:
        return None
    if isinstance(raw, CaptureResult):
        return raw
    text = str(raw).strip()
    return CaptureResult(text=text) if text else None


def build_payload(strokes: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a capture's strokes into the shape `process_capture` expects.

    `points` is every `[x, y]` in drawing order. `polylines` keeps the same
    points grouped by stroke, because a cross and a cup are indistinguishable
    once the boundaries are gone. Pressure and timestamps are dropped.
    """
    polylines: list[list[list[float]]] = []
    for stroke in strokes:
        points = stroke["points"] if isinstance(stroke, dict) else stroke
        line = [[point[0], point[1]] for point in points]
        if line:
            polylines.append(line)
    return {
        "points": [point for line in polylines for point in line],
        "polylines": polylines,
        "strokes": len(strokes),
    }


def _get_recognizer():
    """Build the recogniser once per process; the tag catalog is on disk."""
    global _recognizer
    if _recognizer is None:
        if str(config.ROOT) not in sys.path:
            sys.path.insert(0, str(config.ROOT))
        from recognize import IntentRecognizer

        _recognizer = IntentRecognizer(root=config.ROOT)
    return _recognizer


def _clean_spoken(text: str) -> str:
    line = " ".join((text or "").split())
    if not line:
        return ""
    for mark in (".", "!", "?"):
        if mark in line:
            line = line.split(mark, 1)[0] + (mark if mark != "?" else ".")
            break
    if line.endswith("?"):
        line = line[:-1].rstrip() + "."
    return line[:140]


def phrase_for(tag_id: str | None, label: str | None = None) -> str:
    if tag_id and tag_id in PHRASES:
        return PHRASES[tag_id]
    if label:
        return f"I need {label}."
    return SAMPLE_TEXT


def spoken_for(
    tag_id: str,
    *,
    reason: str = "",
    label: str | None = None,
    image: Path | None = None,
    prepared: str = "",
) -> str:
    cleaned = _clean_spoken(prepared)
    if cleaned:
        return cleaned
    fallback = phrase_for(tag_id, label)
    if not config.RECOGNITION_ENABLED:
        return fallback
    try:
        text = _clean_spoken(
            _get_recognizer().model.spoken_for_tag(
                tag_id, reason=reason, label=label, image=image
            )
        )
        return text or fallback
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] spoken_for {tag_id} failed: {exc!r}")
        return fallback


def follow_ups_for(
    tag_id: str | None,
    *,
    spoken: str = "",
    seen: str = "",
    image: Path | None = None,
) -> list[dict[str, str]]:
    """Assistant-style yes/no follow-ups after a broad intent is confirmed."""
    if tag_id not in FOLLOWUP_TAGS or not needs_followup(tag_id, spoken):
        return []
    fallback = default_followups(tag_id)[:MAX_FOLLOWUPS]
    if not config.RECOGNITION_ENABLED:
        return fallback
    try:
        rows = _get_recognizer().model.follow_ups_for_intent(
            tag_id, spoken=spoken, seen=seen, image=image, limit=MAX_FOLLOWUPS
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] follow-ups for {tag_id} failed: {exc!r}")
        return fallback
    cleaned = []
    for row in rows:
        spoken_line = " ".join((row.get("spoken") or "").split())[:140]
        question = " ".join((row.get("question") or "").split())
        detail = (row.get("detail") or "").strip().lower()
        if not spoken_line or not detail or not _followup_allowed(tag_id, detail, spoken):
            continue
        if not question.endswith("?"):
            question = f"{question or detail.capitalize()}?"
        cleaned.append({"spoken": spoken_line, "question": question, "detail": detail})
        if len(cleaned) >= MAX_FOLLOWUPS:
            break
    if tag_id == "help":
        call = caretaker_followup()
        cleaned = [call] + [item for item in cleaned if item["detail"] != "call"]
        cleaned = cleaned[:MAX_FOLLOWUPS]
    return cleaned or fallback


def _spoken_text(result: Any) -> str:
    cleaned = _clean_spoken(getattr(result, "spoken", "") or "")
    if cleaned:
        return cleaned
    tag_id = result.top_tag
    label = None
    for candidate in result.candidates:
        if candidate.tag_id == tag_id:
            label = candidate.label
            break
    return phrase_for(tag_id, label)


def closing_for(
    tag_id: str | None,
    *,
    detail: str | None = None,
    spoken: str = "",
) -> str:
    """Caregiver wrap-up after a yes. Local fallback if Gemini is slow or down."""
    fallback = fallback_closing(tag_id, detail)
    if not tag_id or not config.RECOGNITION_ENABLED:
        return fallback if tag_id else ""
    if (detail or "").strip().lower() in {"call", "caretaker"}:
        return fallback
    if tag_id == "water":
        return fallback
    if is_specific(detail, tag_id):
        return fallback
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        text = pool.submit(
            _get_recognizer().model.closing_for_need,
            tag_id,
            detail=detail or "",
            spoken=spoken,
        ).result(timeout=CLOSING_TIMEOUT_S)
        return _clean_spoken(text) or fallback
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] closing for {tag_id} failed: {exc!r}")
        return fallback
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def followup_from_detail(detail: str) -> dict[str, str]:
    name = (detail or "").strip()
    return {
        "spoken": f"Is that {name}?",
        "question": question_for(name),
        "detail": name.lower(),
    }


def confirm_memory(result: CaptureResult, *, accepted: bool, capture_id: str) -> str:
    recognition = result.recognition
    if recognition is None:
        return ""
    return _get_recognizer().confirm(
        recognition,
        spoken=result.text if not result.detail else f"{result.text} ({result.detail})",
        capture_id=capture_id,
        accepted=accepted,
    ) or ""


def patch_graphs(*, capture_id: str, journal: str) -> None:
    if not journal:
        return
    _get_recognizer().patch_graphs(capture_id=capture_id, journal=journal)


def grade_shape(image: Path, target: str) -> dict[str, Any]:
    """Ask Gemini if the drawing matches the prompted shape."""
    from . import shape_game

    fallback = {"match": False, "seen": "", "spoken": shape_game.NUDGES.get(target, "Try again.")}
    if not config.RECOGNITION_ENABLED:
        return fallback
    timeout = float(config.GEMINI_TIMEOUT_S)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        graded = pool.submit(
            _get_recognizer().model.grade_shape, target, image=image
        ).result(timeout=timeout)
        spoken = _clean_spoken(graded.get("spoken") or "")
        return {
            "match": bool(graded.get("match")),
            "seen": str(graded.get("seen") or "").strip(),
            "spoken": spoken,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] grade_shape failed: {exc!r}")
        return fallback
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def process_capture(image: Path, data: dict[str, Any]) -> CaptureResult | str | None:
    """Turn a captured drawing into text.

    Args:
        image: path to the PNG. Use `image.read_bytes()` for the raw bytes.
        data: `{"points": [[x, y], ...], "polylines": [[[x, y], ...], ...],
              "strokes": int}`, as built above.

    Returns:
        A CaptureResult to speak, a plain string (tests), or None to stay silent.
    """
    points = data["points"]
    strokes = data["strokes"]
    polylines = data.get("polylines") or []

    print(f"[pipeline] image   {image.name} ({image.stat().st_size:,} bytes)")
    print(f"[pipeline] strokes {strokes}")
    print(f"[pipeline] points  {len(points)}, first={points[0] if points else None}, "
          f"last={points[-1] if points else None}")

    if not points:
        return None

    if not config.RECOGNITION_ENABLED:
        print(f"[pipeline] text    {SAMPLE_TEXT!r} (recognition off)")
        return SAMPLE_TEXT

    try:
        result = _get_recognizer().interpret(
            {"strokes": polylines} if polylines else {"points": points},
            image=image,
            update_memory=False,
        )
    except Exception as exc:  # noqa: BLE001 - speech should still happen
        print(f"[pipeline] recognition failed: {exc!r}")
        print(f"[pipeline] text    {SAMPLE_TEXT!r}")
        return SAMPLE_TEXT

    digit = getattr(result, "digit", "") or ""
    if digit in {"1", "2"}:
        print(f"[pipeline] digit   {digit}  (shape game)")
        return CaptureResult(
            text="Let's play a drawing game.",
            tag_id="play",
            detail=digit,
            fallback_used=result.fallback_used,
            candidates=list(result.candidates),
            recognition=result,
        )

    text = _spoken_text(result)
    top = next((item for item in result.candidates if item.tag_id == result.top_tag), None)
    detail = (getattr(top, "detail", None) or "").strip() or None
    if not is_specific(detail, result.top_tag):
        detail = None
    print(f"[pipeline] top     {result.top_tag!r}  fallback={result.fallback_used}")
    print(f"[pipeline] text    {text!r}")
    if detail:
        print(f"[pipeline] detail  {detail!r}")
    return CaptureResult(
        text=text,
        tag_id=result.top_tag,
        reason=result.candidates[0].reason if result.candidates else "",
        fallback_used=result.fallback_used,
        candidates=list(result.candidates),
        recognition=result,
        detail=detail,
    )
