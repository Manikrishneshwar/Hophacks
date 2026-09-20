#!/usr/bin/env python3
"""Gemini patient-session model with daily and compressed history.

Reads prompt.txt, patient_data.json, compressed_history.txt, and the
current day's daily_history.txt under monthly_events/YYYY-MM/YYYY-MM-DD/.

Each interaction updates that day's daily_history. At end of day,
compressed_history.txt is rewritten from the running summary plus today.

Usage:
  python gemini_session.py init-today
  python gemini_session.py show-context
  python gemini_session.py chat "Good morning"
  python gemini_session.py chat --image sketch.png "What did I draw?"
  python gemini_session.py end-of-day
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import xai_backend
from dotenv import load_dotenv
from google import genai
from google.genai import types

from memory_graph import MemoryGraph

# Chosen on free-tier availability, not on paper quality. `gemini-3.8-flash`
# allows 20 requests per day, which one demo session exhausts, and then every
# capture falls through to templates. The lite model answered every request and
# got all three labelled eval drawings right; it is vaguer about the specific
# object, which the follow-up questions are there to pin down anyway.
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# A different model, so it draws on a different quota pool. Verified with image
# input, which the ranking call needs. Not `gemini-2.5-flash`: it is still
# listed by the API but 404s with "no longer available to new users".
FALLBACK_MODEL = "gemini-3.6-flash"

# Tried in order, so an exhausted or overloaded primary does not take the whole
# pipeline down with it.
DEFAULT_MODEL_CHAIN = (DEFAULT_MODEL, FALLBACK_MODEL)

GEMINI_TIMEOUT_DEFAULT_S = 15.0

# Generous, because the calls that use it happen after the pad has already spoken
# and grok-4.6 spent 42-86s on the eval drawings while getting all of them right.
GEMINI_SLOW_TIMEOUT_DEFAULT_S = 120.0

# The API rejects a shorter per-request deadline outright, with
# "Manually set deadline 8s is too short. Minimum allowed deadline is 10s."
# Two real attempts therefore only fit inside a budget of 20s or more.
API_MIN_DEADLINE_S = 10.0

# Failures that would repeat identically on every model. The chain stops on
# these instead of burning the budget proving the key is still wrong.
PERMANENT_FAILURE_MARKERS = (
    "API_KEY_INVALID",
    "API key not valid",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
)

PLACEHOLDER_KEYS = {"your-api-key-here", "changeme", "todo"}
EMPTY_CONTEXT = "(empty)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_api_key() -> str | None:
    raw = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not raw:
        return None
    return raw.strip().strip("'\"")


def key_looks_like_project_id(api_key: str) -> bool:
    return api_key.startswith("gen-lang-client-") or api_key.startswith("projects/")


def resolve_model_chain(model: str | None = None) -> tuple[str, ...]:
    """Which models to try, in order.

    An explicit `model` pins exactly one, because a caller that names a model
    means it. Otherwise `GEMINI_MODELS` is a comma-separated chain, a lone
    `GEMINI_MODEL` becomes the primary with the defaults kept behind it, and
    with neither set the full default chain is used.
    """
    if model and model.strip():
        return (model.strip(),)

    listed = os.environ.get("GEMINI_MODELS", "")
    chain = tuple(dict.fromkeys(part.strip() for part in listed.split(",") if part.strip()))
    if chain:
        return chain

    primary = os.environ.get("GEMINI_MODEL", "").strip()
    if primary:
        return tuple(dict.fromkeys((primary, *DEFAULT_MODEL_CHAIN)))
    return DEFAULT_MODEL_CHAIN


def total_timeout_s() -> float:
    """The budget one Gemini call may spend, across every model it tries."""
    try:
        return max(1.0, float(os.environ.get("GEMINI_TIMEOUT_S", GEMINI_TIMEOUT_DEFAULT_S)))
    except ValueError:
        return GEMINI_TIMEOUT_DEFAULT_S


def slow_timeout_s() -> float:
    """The budget for work nobody is waiting on, so a reasoning model can finish.

    The end-of-day compression and the graph patch both run after the pad has
    spoken, where the 15s that keeps the pad responsive only truncates a better
    answer for no benefit.
    """
    try:
        return max(1.0, float(os.environ.get("GEMINI_SLOW_TIMEOUT_S", GEMINI_SLOW_TIMEOUT_DEFAULT_S)))
    except ValueError:
        return GEMINI_SLOW_TIMEOUT_DEFAULT_S


def is_permanent_failure(exc: BaseException) -> bool:
    detail = str(exc)
    return any(marker in detail for marker in PERMANENT_FAILURE_MARKERS)


def brief(exc: BaseException, limit: int = 120) -> str:
    """One short line from an API error, which is otherwise a wall of JSON."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def guess_mime_type(image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type and mime_type.startswith("image/"):
        return mime_type
    raise ValueError(f"Could not infer an image MIME type from {image_path}")


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Model did not return a JSON object")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Model JSON was not an object")
    return data


def _usable_detail(raw: str, tag_id: str) -> str:
    """Keep only a real object name. Letters and tag initials are ink, not needs."""
    token = (raw or "").strip().lower()
    if not token or token == tag_id or token == tag_id[:1]:
        return ""
    if len(token) < 2:
        return ""
    if tag_id == "help" and token not in {"call", "caretaker"}:
        return ""
    if tag_id == "rest":
        return ""
    return token


@dataclass
class InteractionResult:
    reply: str
    daily_note: str
    daily_history_path: Path


class GeminiPatientModel:
    """Gemini wrapper that keeps patient context and rolling history files."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        prompt_path: str | Path | None = None,
        patient_data_path: str | Path | None = None,
        compressed_history_path: str | Path | None = None,
        monthly_events_dir: str | Path | None = None,
        model: str | None = None,
        on_date: date | None = None,
    ) -> None:
        load_dotenv()
        self.root = Path(root).resolve()
        self.prompt_path = Path(prompt_path) if prompt_path else self.root / "prompt.txt"
        self.patient_data_path = (
            Path(patient_data_path) if patient_data_path else self.root / "patient_data.json"
        )
        self.compressed_history_path = (
            Path(compressed_history_path)
            if compressed_history_path
            else self.root / "compressed_history.txt"
        )
        self.monthly_events_dir = (
            Path(monthly_events_dir) if monthly_events_dir else self.root / "monthly_events"
        )
        self.models = resolve_model_chain(model)
        self.on_date = on_date or date.today()
        self._client: genai.Client | None = None
        self.history_graph = MemoryGraph(self.root / "memory_graph.json")
        self.graph = self.history_graph

    @property
    def model_name(self) -> str:
        """The model tried first. Logs and scripts still talk about one model."""
        return self.models[0]

    def daily_dir(self, on: date | None = None) -> Path:
        day = on or self.on_date
        return self.monthly_events_dir / day.strftime("%Y-%m") / day.isoformat()

    def daily_history_path(self, on: date | None = None) -> Path:
        return self.daily_dir(on) / "daily_history.txt"

    def ensure_daily_history(self, on: date | None = None) -> Path:
        path = self.daily_history_path(on)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        return path

    def daily_graph(self, on: date | None = None) -> MemoryGraph:
        graph = MemoryGraph(self.daily_dir(on) / "daily_graph.json")
        graph.ensure_seed(self.root)
        return graph

    def ensure_files(self) -> None:
        self.ensure_daily_history()
        if not self.compressed_history_path.exists():
            self.compressed_history_path.parent.mkdir(parents=True, exist_ok=True)
            self.compressed_history_path.write_text("", encoding="utf-8")
        self.history_graph.ensure_seed(self.root)
        self.daily_graph().ensure_seed(self.root)

    def read_prompt(self) -> str:
        text = read_text(self.prompt_path).strip()
        if not text:
            raise FileNotFoundError(f"Prompt file is missing or empty: {self.prompt_path}")
        return text

    def read_patient_data(self) -> dict[str, Any]:
        if not self.patient_data_path.is_file():
            raise FileNotFoundError(f"Patient data not found: {self.patient_data_path}")
        data = json.loads(self.patient_data_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("patient_data.json must contain a JSON object")
        return data

    def read_daily_history(self, on: date | None = None) -> str:
        return read_text(self.daily_history_path(on)).strip()

    def read_compressed_history(self) -> str:
        return read_text(self.compressed_history_path).strip()

    def context_bundle(self, on: date | None = None) -> dict[str, str]:
        patient = json.dumps(self.read_patient_data(), indent=2)
        return {
            "prompt": self.read_prompt(),
            "patient_data": patient,
            "compressed_history": self.read_compressed_history() or EMPTY_CONTEXT,
            "daily_history": self.read_daily_history(on) or EMPTY_CONTEXT,
            "memory_graph": self.history_graph.context_summary(),
            "daily_graph": self.daily_graph(on).context_summary(),
            "date": (on or self.on_date).isoformat(),
            "daily_history_path": str(self.daily_history_path(on)),
        }

    def _client_or_raise(self) -> genai.Client:
        if self._client is None:
            api_key = load_api_key()
            if not api_key or api_key.lower() in PLACEHOLDER_KEYS:
                raise RuntimeError(
                    "Missing GEMINI_API_KEY. Add it to .env from https://aistudio.google.com/apikey"
                )
            if key_looks_like_project_id(api_key):
                raise RuntimeError(
                    "GEMINI_API_KEY looks like a project ID. Use the key that starts with AIza or AQ."
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def _contents_with_image(self, prompt: str, image: str | Path | None) -> list[Any]:
        contents: list[Any] = [prompt]
        if image is None:
            return contents
        image_path = Path(image).expanduser().resolve()
        contents.insert(
            0,
            types.Part.from_bytes(
                data=image_path.read_bytes(),
                mime_type=guess_mime_type(image_path),
            ),
        )
        return contents

    @property
    def last_vendor(self) -> str:
        """Which vendor answered the most recent call, "gemini" or "xai".

        Read straight after the call that cares. Captures are handled one at a
        time on the pad, so a single slot is enough; it would need to travel with
        the request if several were ever answered at once.
        """
        return getattr(self, "_last_vendor", "")

    def _generate(
        self,
        contents: list[Any],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        unhurried: bool = False,
    ) -> str:
        config_kwargs: dict[str, Any] = {
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        # A missing or malformed Gemini key is not fatal while another vendor is
        # configured, so the complaint is held back and only raised below if xAI
        # cannot answer either. Keeping the message means a setup with no keys at
        # all still says which one to add.
        try:
            client = self._client_or_raise()
        except RuntimeError as exc:
            if not xai_backend.enabled():
                raise
            print(f"[gemini] unusable ({brief(exc)}); going straight to xai")
            client = None

        # One budget is shared across the chain, so falling back cannot make the
        # person at the pad wait longer than a single attempt already did. The
        # API's 10s floor means an even split only buys a rescue from a hanging
        # model when the budget is large enough for every link to clear it; at
        # the default 15s the primary gets the lot and the fallback covers the
        # quick refusals instead, which is what a withdrawn or overloaded model
        # actually returns.
        budget = slow_timeout_s() if unhurried else total_timeout_s()
        even_share = budget / max(1, len(self.models))
        split_evenly = even_share >= API_MIN_DEADLINE_S
        started = time.monotonic()

        last_error: Exception | None = None
        for index, model in enumerate(self.models if client is not None else ()):
            remaining = self.models[index + 1:]
            left = budget - (time.monotonic() - started)
            if index and left < API_MIN_DEADLINE_S:
                print(f"[gemini] skipping {model}: {left:.1f}s of budget left, "
                      f"the API needs {API_MIN_DEADLINE_S:g}s")
                break

            attempt_s = even_share if split_evenly else max(API_MIN_DEADLINE_S, left)
            config_kwargs["http_options"] = types.HttpOptions(timeout=int(attempt_s * 1000))
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("Gemini returned an empty response")
            except Exception as exc:  # noqa: BLE001 - the next model is the handler
                last_error = exc
                if not remaining or is_permanent_failure(exc):
                    break
                # Deliberately not naming the next model here: whether it is
                # actually tried depends on the budget left at the top of the
                # loop, and promising an attempt that gets skipped reads as a bug.
                print(f"[gemini] {model} failed ({brief(exc)})")
                continue
            if index:
                print(f"[gemini] {model} answered after {index} failed attempt(s)")
            self._last_vendor = "gemini"
            return text

        # Another vendor, so a Google-wide outage or an exhausted Google quota
        # does not reach it. Only worth starting with budget left, which is what
        # a fast refusal leaves and an exhausted one does not: `recognize` caps
        # the whole call at the same GEMINI_TIMEOUT_S, so overrunning here would
        # be cut off anyway and only delay the on-machine model behind it.
        if xai_backend.enabled():
            left = budget - (time.monotonic() - started)
            try:
                text = xai_backend.generate(
                    contents,
                    json_mode=json_mode,
                    temperature=temperature,
                    timeout=left,
                    unhurried=unhurried,
                )
            except Exception as exc:  # noqa: BLE001 - local vision is next
                print(f"[xai] {xai_backend.model_name(unhurried=unhurried)} failed ({brief(exc)})")
            else:
                print(
                    f"[xai] {xai_backend.model_name(unhurried=unhurried)} "
                    "answered after Gemini failed"
                )
                self._last_vendor = "xai"
                return text

        if last_error is None:
            raise RuntimeError("No model answered: no Gemini client and xai did not reply")
        if isinstance(last_error, RuntimeError):
            raise last_error
        raise RuntimeError(f"Gemini request failed: {last_error}") from last_error

    def _interaction_prompt(self, user_message: str, on: date | None = None) -> str:
        ctx = self.context_bundle(on)
        return (
            f"{ctx['prompt']}\n\n"
            f"## Date\n{ctx['date']}\n\n"
            f"## Patient data\n{ctx['patient_data']}\n\n"
            f"## Compressed history\n{ctx['compressed_history']}\n\n"
            f"## Daily brain (today)\n{ctx['daily_graph']}\n\n"
            f"## History brain\n{ctx['memory_graph']}\n\n"
            f"## Today's daily history\n{ctx['daily_history']}\n\n"
            f"## Current user message\n{user_message.strip()}\n"
        )

    def interact(
        self,
        user_message: str,
        *,
        image: str | Path | None = None,
        on: date | None = None,
    ) -> InteractionResult:
        """Talk to the model, then append a summarized note to daily_history."""
        if not user_message.strip():
            raise ValueError("user_message is empty")
        self.ensure_files()
        contents = self._contents_with_image(self._interaction_prompt(user_message, on), image)
        raw = self._generate(contents, json_mode=True)
        parsed = parse_json_object(raw)
        reply = str(parsed.get("reply") or "").strip()
        daily_note = str(parsed.get("daily_note") or "").strip()
        if not reply:
            reply = raw
        if not daily_note:
            daily_note = f"Interaction recorded: {user_message.strip()[:160]}"

        path = self.append_daily_note(daily_note, user_message=user_message, on=on)
        return InteractionResult(reply=reply, daily_note=daily_note, daily_history_path=path)

    def append_daily_note(
        self,
        daily_note: str,
        *,
        user_message: str | None = None,
        on: date | None = None,
    ) -> Path:
        path = self.ensure_daily_history(on)
        stamp = utc_now().isoformat()
        block = [f"## {stamp}"]
        if user_message:
            block.append(f"User: {user_message.strip()}")
        block.append(f"Summary: {daily_note.strip()}")
        block.append("")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(block) + "\n")
        self.history_graph.ensure_seed(self.root)
        daily_graph = self.daily_graph(on)
        self.history_graph.record_interaction(user_message or "", daily_note, on=on)
        daily_graph.record_interaction(user_message or "", daily_note, on=on)
        try:
            self.update_graphs_with_llm(user_message or "", daily_note, on=on)
        except Exception:
            pass
        return path

    def update_graphs_with_llm(
        self,
        user_message: str,
        daily_note: str,
        on: date | None = None,
    ) -> dict[str, Any]:
        """Ask Gemini to add/connect nodes on both brains, then persist the patch."""
        daily_graph = self.daily_graph(on)
        prompt = (
            "You maintain two memory graphs for an eyes-free assistive pad.\n"
            "The DAILY brain is only today's unfolding events and how they connect.\n"
            "The HISTORY brain is lasting facts, recurring intents, and patterns.\n"
            "Return JSON only with keys daily and history, each {nodes:[], edges:[]}.\n"
            "Node: {id, type, label, weight, note}. Types: person, intent, drawing, "
            "interest, preference, day, event, theme.\n"
            "Edge: {from, relation, to, weight}. Reuse existing ids when you can.\n"
            "Connect today's events to each other (caused, followed, confirmed, same_need).\n"
            "History edges should capture lasting patterns (often_requests, associated_with).\n"
            "Do not diagnose or invent medical conditions.\n\n"
            f"## New interaction\nUser: {user_message}\nNote: {daily_note}\n\n"
            f"## Daily graph now\n{json.dumps(daily_graph.compact_for_llm(), indent=2)}\n\n"
            f"## History graph now\n{json.dumps(self.history_graph.compact_for_llm(), indent=2)}\n"
        )
        # Runs after the tablet has already heard its closing, so it can afford
        # the slower and better model.
        parsed = parse_json_object(self._generate([prompt], json_mode=True, unhurried=True))
        daily_graph.apply_patch(parsed.get("daily") if isinstance(parsed.get("daily"), dict) else {})
        self.history_graph.apply_patch(
            parsed.get("history") if isinstance(parsed.get("history"), dict) else {}
        )
        return parsed

    def compress_history(self, on: date | None = None) -> str:
        """Rewrite compressed_history.txt from the running summary plus today's daily file."""
        self.ensure_files()
        day = on or self.on_date
        daily = self.read_daily_history(day)
        if not daily:
            raise RuntimeError(
                f"No daily history to compress for {day.isoformat()} ({self.daily_history_path(day)})"
            )

        previous = self.read_compressed_history() or EMPTY_CONTEXT
        patient = json.dumps(self.read_patient_data(), indent=2)
        prompt = (
            "You maintain a compressed running history for a patient-journal prototype.\n"
            "Write a concise updated long-term history that keeps important facts, "
            "preferences, recurring themes, and notable events.\n"
            "Drop small talk and repeated details. Do not diagnose or give medical orders.\n"
            "Return plain text only.\n\n"
            f"## Date being closed\n{day.isoformat()}\n\n"
            f"## Patient data\n{patient}\n\n"
            f"## Previous compressed history\n{previous}\n\n"
            f"## Today's daily history\n{daily}\n"
        )
        updated = self._generate([prompt], json_mode=False, unhurried=True).strip() + "\n"
        self.compressed_history_path.write_text(updated, encoding="utf-8")
        try:
            merge_prompt = (
                "Fold today's daily brain into the lasting history brain.\n"
                "Return JSON only: {\"history\": {\"nodes\": [], \"edges\": []}}.\n"
                "Promote recurring intents and keep only durable links. Do not diagnose.\n\n"
                f"## Daily brain\n{json.dumps(self.daily_graph(day).compact_for_llm(), indent=2)}\n\n"
                f"## History brain\n{json.dumps(self.history_graph.compact_for_llm(), indent=2)}\n"
            )
            parsed = parse_json_object(self._generate([merge_prompt], json_mode=True, unhurried=True))
            self.history_graph.apply_patch(
                parsed.get("history") if isinstance(parsed.get("history"), dict) else parsed
            )
        except Exception:
            pass
        return updated

    def rank_drawing_tags(
        self,
        tags: list[dict[str, Any]],
        *,
        image: str | Path | None = None,
        feature_matches: list[dict[str, Any]] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Ask Gemini to rank known tags for a new drawing. Raises on API failure.

        `feature_matches` is accepted for older callers and ignored: templates
        are the timeout fallback, not an input to the model.
        """
        del feature_matches
        if not tags:
            return {"rankings": [], "spoken": "", "seen": ""}
        allowed = {tag["id"] for tag in tags}
        ctx = self.context_bundle()
        prompt = (
            "You interpret a finger drawing from an eyes-free pad.\n"
            "LOOK AT THE IMAGE FIRST. Describe what it shows, then map it to a tag.\n"
            "An apple, pizza, sandwich, bowl, or other food is food. A cup, glass, "
            "bottle, or tap is water. A cross, plus, handwritten H, or telephone "
            "is help. A bed or pillow is rest.\n"
            "Not every drawing is a care need. Mountains, a sun, a tree, a house, a "
            "book, an animal, or any little scene is story. A smile, a face, a person, "
            "or a heart is talk. Do not force those into food, water, help, or rest.\n"
            "A triangle or pizza wedge is food, not a heart and not a scene. A single "
            "cup is water. A cross or telephone is help. Only pick story or talk when "
            "the drawing is clearly not one of those needs.\n"
            "Also look for a handwritten digit. If the drawing is clearly a 1 or a 2, "
            "set digit to \"1\" or \"2\". A tall single vertical stroke may be a 1. "
            "A 2 has a curved top and a baseline. Do not call an apple, pizza, cup, "
            "bed, telephone, or random scribble a digit. If it is not clearly 1 or 2, "
            "digit is \"\".\n"
            "Patient history is a weak tie-breaker only. Do not pick water just because "
            "they have asked for water before if the drawing is clearly something else.\n"
            "Yes and no are given by taps, never by this ranking.\n"
            "For food or water, spoken is first person, short, as the person "
            "talking to a caregiver. Name the food or drink if you can see it "
            "(apple, pizza, tea). No question, no list, no diagnosis.\n"
            "For help, spoken is exactly \"I need help, please.\" Do not name a "
            "letter, a cross, or a hand. detail is empty unless the drawing is "
            "clearly a telephone, in which case detail is \"call\".\n"
            "For rest, spoken is first person and short. detail is empty.\n"
            "For story, spoken is the pad offering: name what you see, then ask if "
            "they want a short story (That looks like mountains. Want a short story?).\n"
            "For talk, spoken is the pad offering company (That's a smile. Want some "
            "company?).\n"
            "If digit is 1 or 2, spoken can be empty; the pad will offer a drawing game.\n"
            "Return JSON only: "
            '{"seen": "what the drawing shows", "digit": "", '
            '"rankings": [{"tag_id": "...", "likelihood": 0.0, "reason": "...", '
            '"spoken": "I would like an apple, please.", "detail": "apple"}], '
            '"spoken": "I would like an apple, please."}\n'
            f"Return at most {top_k} tags, highest likelihood first. "
            "Use only tag_id values from the list.\n"
            "likelihood must be a number from 0 to 1. detail is a real object "
            "name (apple, pizza, tea), never a single letter, never the tag "
            "initial, never the tag_id itself. Empty string if unknown.\n"
            "You are not a clinician and must not diagnose.\n\n"
            f"## Date\n{ctx['date']}\n\n"
            f"## Patient data (weak prior)\n{ctx['patient_data']}\n\n"
            f"## Daily brain (today, weak prior)\n{ctx['daily_graph']}\n\n"
            f"## History brain (weak prior)\n{ctx['memory_graph']}\n\n"
            f"## Known tags\n{json.dumps(tags, indent=2)}\n"
        )
        parsed = parse_json_object(
            self._generate(self._contents_with_image(prompt, image), json_mode=True, temperature=0.2)
        )
        rows = parsed.get("rankings") or parsed.get("tags") or []
        rankings = []
        seen_ids: set[str] = set()
        for row in rows:
            tag_id = str(row.get("tag_id") or row.get("id") or "").strip()
            if not tag_id or tag_id not in allowed or tag_id in seen_ids:
                continue
            try:
                likelihood = float(row.get("likelihood", 0))
            except (TypeError, ValueError):
                likelihood = 0.0
            likelihood = min(1.0, max(0.0, likelihood))
            rankings.append(
                {
                    "tag_id": tag_id,
                    "likelihood": likelihood,
                    "reason": str(row.get("reason") or "").strip(),
                    "spoken": str(row.get("spoken") or "").strip(),
                    "detail": _usable_detail(
                        str(row.get("detail") or ""), tag_id
                    ),
                }
            )
            seen_ids.add(tag_id)
            if len(rankings) >= top_k:
                break
        if not rankings:
            raise RuntimeError("Gemini returned no valid tag rankings")
        spoken = str(parsed.get("spoken") or parsed.get("reply") or "").strip()
        if not spoken and rankings[0].get("spoken"):
            spoken = rankings[0]["spoken"]
        digit = str(parsed.get("digit") or "").strip()
        if digit not in {"1", "2"}:
            digit = ""
        return {
            "rankings": rankings,
            "spoken": spoken,
            "seen": str(parsed.get("seen") or "").strip(),
            "digit": digit,
            # Which vendor actually answered, so a Grok reading is not filed under
            # Gemini in the logs or the caretaker view.
            "vendor": self.last_vendor,
        }

    def spoken_for_tag(
        self,
        tag_id: str,
        *,
        reason: str = "",
        label: str | None = None,
        image: str | Path | None = None,
    ) -> str:
        """One sentence for a tag, used when the first guess was rejected."""
        name = label or tag_id
        ctx = self.context_bundle()
        if tag_id in {"story", "talk"}:
            voice = (
                "Speak as the pad offering something, not as the patient. "
                "Name what the drawing shows, then ask if they want a short story "
                "(story) or some company (talk). One or two short sentences."
            )
        else:
            voice = (
                "First person, short, as the person speaking to a caregiver. "
                "If there is a drawing, name what it shows when it fits this intent "
                "(apple, pizza, tea). No question, no list, no diagnosis."
            )
        prompt = (
            "Write the one sentence that should be spoken aloud on an eyes-free pad.\n"
            f"{voice} "
            "Return JSON only: {\"spoken\": \"...\"}\n\n"
            f"## Intent we are retrying\n{name} ({tag_id})\n"
            f"## Why this guess\n{reason or 'previous guess was rejected'}\n\n"
            f"## Patient data\n{ctx['patient_data']}\n"
        )
        parsed = parse_json_object(
            self._generate(self._contents_with_image(prompt, image), json_mode=True, temperature=0.2)
        )
        return str(parsed.get("spoken") or parsed.get("reply") or "").strip()

    def follow_ups_for_intent(
        self,
        tag_id: str,
        *,
        spoken: str = "",
        seen: str = "",
        image: str | Path | None = None,
        limit: int = 2,
    ) -> list[dict[str, str]]:
        """Yes/no follow-ups to pin down food, drink, help, or rest."""
        ctx = self.context_bundle()
        hour = datetime.now().hour
        prompt = (
            "You are a calm in-room assistant on an eyes-free yes/no pad.\n"
            "The person already confirmed a need. Only ask a follow-up if it would "
            "change what you do next. Speak as the assistant, not as the patient.\n"
            "Rules:\n"
            "- water: if they already asked for water, return ZERO followups. "
            "Never offer tea or coffee after water. Only ask Water? if they said "
            "'a drink' with no specific.\n"
            "- food: ask about a dish only if they said 'food' or 'something to eat' "
            "with no specific. Never offer drinks.\n"
            "- help: FIRST follow-up is always calling the named caretaker. "
            "Then bathroom if needed. Never food or drink. Never ask if the "
            "drawing is a letter, a cross, a plus, or a hand.\n"
            "- rest: return ZERO followups.\n"
            "Do not diagnose. Do not switch intents.\n"
            "Each follow-up has:\n"
            "- spoken: one short assistant question (Should I call Jordan?)\n"
            "- question: 1-3 words on the pad (Call Jordan?)\n"
            "- detail: a short label (call, soup, bathroom). Never a letter.\n"
            f"Return JSON only: {{\"followups\": [{{\"spoken\": \"...\", \"question\": \"Call Jordan?\", \"detail\": \"call\"}}]}}\n"
            f"Return at most {limit} followups. Empty list is allowed.\n\n"
            f"## Local hour (24h)\n{hour}\n\n"
            f"## Confirmed intent\n{tag_id}\n"
            f"## What we already said\n{spoken or '(none)'}\n"
            f"## What the drawing showed\n{seen or '(not described)'}\n\n"
            f"## Patient data (includes caretaker)\n{ctx['patient_data']}\n"
        )
        parsed = parse_json_object(
            self._generate(self._contents_with_image(prompt, image), json_mode=True, temperature=0.2)
        )
        rows = parsed.get("followups") or parsed.get("follow_ups") or []
        out: list[dict[str, str]] = []
        seen_details: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail = str(row.get("detail") or row.get("label") or "").strip().lower()
            spoken_line = str(row.get("spoken") or "").strip()
            question = str(row.get("question") or "").strip()
            if not spoken_line or not detail or detail in seen_details:
                continue
            if not question:
                question = f"{detail.capitalize()}?"
            if not question.endswith("?"):
                question = f"{question}?"
            out.append({"spoken": spoken_line, "question": question, "detail": detail})
            seen_details.add(detail)
            if len(out) >= limit:
                break
        return out

    def grade_shape(
        self,
        target: str,
        *,
        image: str | Path | None = None,
    ) -> dict[str, Any]:
        """Did the drawing match the prompted shape? Generous about tremor."""
        prompt = (
            "A person with limited motion and possible tremor was asked to draw "
            f"a {target} on an eyes-free pad.\n"
            "LOOK AT THE IMAGE. Decide if it is that shape, even if wobbly or incomplete.\n"
            "A shaky closed loop is a circle. Four roughly straight sides is a square. "
            "Three sides is a triangle. Be generous.\n"
            "match is false if it is clearly a different shape, a digit, or a picture "
            "of food or a cup.\n"
            "If match is false, spoken is one short coaching nudge to try the same "
            "shape again. Not a question. Do not diagnose.\n"
            "If match is true, spoken can be empty.\n"
            "Return JSON only: "
            '{"match": true, "seen": "what it shows", "spoken": ""}\n'
            f"## Target shape\n{target}\n"
        )
        parsed = parse_json_object(
            self._generate(
                self._contents_with_image(prompt, image),
                json_mode=True,
                temperature=0.2,
            )
        )
        match = parsed.get("match")
        if isinstance(match, str):
            match = match.strip().lower() in {"true", "yes", "1"}
        return {
            "match": bool(match),
            "seen": str(parsed.get("seen") or "").strip(),
            "spoken": str(parsed.get("spoken") or parsed.get("nudge") or "").strip(),
        }

    def closing_for_need(
        self,
        tag_id: str,
        *,
        detail: str = "",
        spoken: str = "",
    ) -> str:
        """Caregiver-side wrap-up after the person has tapped yes."""
        prompt = (
            "The person confirmed a need on an eyes-free pad.\n"
            "Write ONE short closing line the pad speaks next, as the caregiver "
            "or system acknowledging the action. Not in the patient's first person. "
            "Not a question. No list. No diagnosis.\n"
            "Examples: I'll order that pizza. I'll get you some water. Rest easy. "
            "Someone is on the way.\n"
            "Return JSON only: {\"spoken\": \"...\"}\n\n"
            f"## Intent\n{tag_id}\n"
            f"## Specific\n{detail or '(none)'}\n"
            f"## What they confirmed\n{spoken or '(none)'}\n"
        )
        parsed = parse_json_object(self._generate([prompt], json_mode=True, temperature=0.3))
        return str(parsed.get("spoken") or parsed.get("reply") or "").strip()

    def companion_for_drawing(
        self,
        tag_id: str,
        *,
        seen: str = "",
        detail: str = "",
        image: str | Path | None = None,
    ) -> str:
        """A short story or a bit of company after they tap yes on an open drawing."""
        if tag_id == "talk":
            ask = (
                "They drew a face, a person, or something companionable.\n"
                "Speak two short kind sentences, or one gentle joke. "
                "No question, no diagnosis, no medical advice.\n"
            )
        else:
            ask = (
                "They drew a scene, a book, an animal, or something to look at.\n"
                "Tell a warm story in three short sentences about what the drawing "
                "shows. No question, no diagnosis, no medical advice.\n"
            )
        prompt = (
            f"{ask}"
            "Return JSON only: {\"spoken\": \"...\"}\n\n"
            f"## What the drawing showed\n{seen or detail or '(look at the image)'}\n"
        )
        parsed = parse_json_object(
            self._generate(
                self._contents_with_image(prompt, image),
                json_mode=True,
                temperature=0.6,
            )
        )
        return str(parsed.get("spoken") or parsed.get("reply") or "").strip()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path("."), help="Project root with the context files")
    common.add_argument("--date", help="Override today's date as YYYY-MM-DD")
    common.add_argument("--model", help="Gemini model name")

    parser = argparse.ArgumentParser(description="Gemini patient session with history files")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-today", parents=[common], help="Create this day's empty daily_history.txt")
    sub.add_parser("show-context", parents=[common], help="Print the assembled context without calling Gemini")

    chat = sub.add_parser("chat", parents=[common], help="Send a message and update daily_history")
    chat.add_argument("message")
    chat.add_argument("--image", type=Path)

    sub.add_parser(
        "end-of-day",
        parents=[common],
        help="Update compressed_history.txt from today's daily_history",
    )
    return parser


def make_model(args: argparse.Namespace) -> GeminiPatientModel:
    on_date = date.fromisoformat(args.date) if args.date else None
    return GeminiPatientModel(root=args.root, model=args.model, on_date=on_date)


def main() -> int:
    args = build_parser().parse_args()
    model = make_model(args)

    if args.command == "init-today":
        path = model.ensure_daily_history()
        print(path)
        return 0

    if args.command == "show-context":
        model.ensure_files()
        print(json.dumps(model.context_bundle(), indent=2))
        return 0

    if args.command == "chat":
        result = model.interact(args.message, image=args.image)
        print(result.reply, flush=True)
        print(f"\n[daily note saved to {result.daily_history_path}]", file=sys.stderr)
        return 0

    if args.command == "end-of-day":
        updated = model.compress_history()
        print(updated)
        print(f"[compressed history saved to {model.compressed_history_path}]", file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
