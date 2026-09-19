#!/usr/bin/env python3
"""Answer a Gemini-shaped request with xAI's Grok instead, over plain HTTP.

This sits between the Gemini model chain and the on-machine model: a different
vendor, so a Google outage or a spent Google quota does not reach it, and unlike
`local_vision.py` it is strong enough to be handed the real production prompt
with all of its patient context. That is why it hooks into `_generate` and so
covers every call (ranking, follow-ups, closings, grading) rather than ranking
alone.

The API is OpenAI-shaped, so `urllib` is enough and no dependency is added.

Model choice is about latency, not accuracy. Measured on the three labelled
drawings in `eval/` with the real ranking prompt:

  grok-4.6                       3/3 in 42-86s   (reasoning; the only model
                                                  anywhere that reads the pizza)
  grok-4.20-0309-non-reasoning   2/3 in 2.3-2.6s
  grok-4.3                       0/3 in 13-18s

The pad has to speak inside `GEMINI_TIMEOUT_S`, so the fast non-reasoning model
is the default despite grok-4.6 being plainly better at the task. grok-4.6 is
worth pinning for offline work that is not on the clock, such as the end-of-day
summary.

Usage:
  python xai_backend.py check
  python xai_backend.py rank sketch.png
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINT = "https://api.x.ai/v1/chat/completions"
MODELS_ENDPOINT = "https://api.x.ai/v1/models"

# Fast enough to answer inside what a refused Gemini call leaves behind.
DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"

# For work nobody is waiting on: the end-of-day compression and the memory-graph
# patch, which both run after the pad has already spoken. It reads these drawings
# better than anything else tried, including every Gemini model, and the minute it
# takes to reason costs nothing there.
SLOW_MODEL = "grok-4.6"

# Below this there is no point starting: the fast model needs about 2.4s and a
# request that cannot finish only delays the local model behind it.
MIN_DEADLINE_S = 3.5


def api_key() -> str:
    return (os.environ.get("XAI_API_KEY") or "").strip()


def model_name(*, unhurried: bool = False) -> str:
    if unhurried:
        return os.environ.get("XAI_SLOW_MODEL") or SLOW_MODEL
    return os.environ.get("XAI_MODEL") or DEFAULT_MODEL


def enabled() -> bool:
    flag = (os.environ.get("XAI_FALLBACK") or "").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return False
    return bool(api_key())


def _post(body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        ENDPOINT,
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # The reason is in the body, never the status line.
        detail = exc.read().decode(errors="replace")[:200].strip()
        raise RuntimeError(f"xai {exc.code}: {detail}") from exc


def as_messages(contents: list[Any]) -> list[dict[str, Any]]:
    """Flatten google-genai `contents` into one OpenAI-shaped user message.

    Strings are the prompt; a `Part` carrying `inline_data` is the PNG, which is
    resent as a data URL. Anything else is ignored rather than guessed at.
    """
    text_parts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for item in contents:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None and getattr(blob, "data", None):
            mime = getattr(blob, "mime_type", None) or "image/png"
            encoded = base64.b64encode(blob.data).decode()
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(text_parts)}]
    content.extend(blocks)
    return [{"role": "user", "content": content}]


def generate(
    contents: list[Any],
    *,
    json_mode: bool = False,
    temperature: float | None = None,
    timeout: float,
    unhurried: bool = False,
) -> str:
    """Mirror `GeminiPatientModel._generate`: same input, same string back."""
    if not enabled():
        raise RuntimeError("no XAI_API_KEY set")
    if timeout < MIN_DEADLINE_S:
        raise RuntimeError(f"only {timeout:.1f}s left, xai needs {MIN_DEADLINE_S:g}s")

    body: dict[str, Any] = {
        "model": model_name(unhurried=unhurried),
        "messages": as_messages(contents),
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    payload = _post(body, timeout)
    try:
        text = (payload["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"xai returned no usable choice: {str(payload)[:160]}") from exc
    if not text:
        raise RuntimeError("xai returned an empty response")
    return text


def available_models() -> list[str]:
    request = urllib.request.Request(
        MODELS_ENDPOINT, headers={"Authorization": f"Bearer {api_key()}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return [str(row.get("id")) for row in json.loads(response.read()).get("data", [])]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="confirm the key works and list models")
    rank = sub.add_parser("rank", help="read one PNG through the real ranking prompt")
    rank.add_argument("image", type=Path)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from gemini_session import load_dotenv

    load_dotenv(root / ".env")

    if not api_key():
        print("XAI_API_KEY is not set; add it to .env")
        return 1

    if args.command == "check":
        print(f"model  {model_name()}")
        try:
            models = available_models()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"usable False ({exc})")
            return 1
        print(f"usable {model_name() in models}")
        for name in models:
            print(f"  {name}")
        return 0

    from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore
    from gemini_session import GeminiPatientModel

    catalog = [
        tag
        for tag in DrawingTagStore(root / DEFAULT_TAGS_PATH).catalog_for_model()
        if tag["id"] not in {"yes", "no"}
    ]
    model = GeminiPatientModel(root=root)
    # Force the xAI path regardless of whether Gemini would have answered.
    model.models = ()
    print(json.dumps(model.rank_drawing_tags(catalog, image=args.image, top_k=5), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
