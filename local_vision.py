#!/usr/bin/env python3
"""Read a drawing with a vision model running on this machine, via Ollama.

This is the last thing tried before falling back to stroke templates, and the
only link in the chain that keeps working with no key, no network, and no
quota. It exists because the Gemini free tier is small enough that one demo
session can exhaust it.

The prompt here is deliberately not the one `rank_drawing_tags` sends. A 7B-12B
model given the full production prompt answers from the patient history and
stops looking at the picture: routing the real prompt to qwen2.5vl:7b returned
"I would like an apple, please" for a cup, a cross, and a pizza alike. Stripping
the context back to the tag list and the image fixes that, at the cost of losing
the history as a tie-breaker, which is the right trade for a fallback.

A local answer is never as good as a Gemini one, so `LOCAL_LIKELIHOOD` stays low
enough that the pad keeps treating the guess as a question to confirm.

Usage:
  python local_vision.py check
  python local_vision.py warm
  python local_vision.py rank sketch.png
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

DEFAULT_URL = "http://127.0.0.1:11434"

# Measured on the three labelled eval drawings: gemma3:12b read 2 of 3 and
# answered in about 2.5s warm, against 1 of 3 and 5.7s for qwen2.5vl:7b. It also
# described what it saw correctly ("A cup") where qwen said "a bowl".
DEFAULT_MODEL = "gemma3:12b"

# Loading 8GB into VRAM takes about 27s, which would blow the recognition
# budget on the first capture. -1 tells Ollama to keep the model resident
# instead of unloading it after its usual five idle minutes. Must stay a number:
# sent as the string "-1" the daemon reads it as a duration and rejects it with
# `time: missing unit in duration "-1"`.
KEEP_ALIVE = -1

# 4096 is what the compact prompt was measured at. Ollama's default rejects the
# production prompt outright ("request (4377 tokens) exceeds the available
# context size (4096)"), so this only holds while the prompt stays small.
DEFAULT_NUM_CTX = 4096

# Generous next to Gemini's budget because this runs after Gemini already failed,
# and a slow local answer still beats no answer.
DEFAULT_TIMEOUT_S = 25.0

# Whether the daemon is up at all. Short, because the point is to fail fast and
# let templates answer rather than add latency to an already-failed capture.
PROBE_TIMEOUT_S = 1.5

# Low on purpose. The model is right often enough to be worth asking about and
# wrong often enough that the pad must not assert: it called the cross drawing
# "a snake" and still tagged it help.
LOCAL_LIKELIHOOD = 0.4

# No longer offered in the prompt, because advertising it made gemma4 decline
# every drawing. Still honoured if a model volunteers it, since a model saying it
# cannot read the shape is worth believing.
UNSURE = "unsure"


def base_url() -> str:
    return (os.environ.get("OLLAMA_URL") or DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get("LOCAL_VISION_MODEL") or DEFAULT_MODEL


def enabled() -> bool:
    """Off only when asked. Absent config means "use it if it is there"."""
    flag = (os.environ.get("LOCAL_VISION") or "").strip().lower()
    return flag not in {"0", "false", "off", "no"}


def timeout_s() -> float:
    try:
        return float(os.environ.get("LOCAL_VISION_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _post(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url()}{path}",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Ollama explains itself in the body and says nothing in the status line,
        # so without this every misconfiguration looks like a bare "400".
        detail = exc.read().decode(errors="replace")[:200].strip()
        raise RuntimeError(f"ollama {exc.code}: {detail}") from exc


def available() -> bool:
    """True when the daemon answers and the model is already pulled."""
    if not enabled():
        return False
    try:
        request = urllib.request.Request(f"{base_url()}/api/tags")
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as response:
            installed = json.loads(response.read()).get("models") or []
    except (urllib.error.URLError, OSError, ValueError):
        return False
    wanted = model_name()
    names = {str(entry.get("name") or "") for entry in installed}
    # Ollama reports "gemma3:12b"; accept a bare "gemma3" as naming the same pull.
    return wanted in names or any(name.split(":")[0] == wanted for name in names)


def warm() -> bool:
    """Load the model into VRAM so the first real capture is not the slow one."""
    if not available():
        return False
    try:
        _post(
            "/api/generate",
            {"model": model_name(), "prompt": "", "keep_alive": KEEP_ALIVE},
            timeout=max(timeout_s(), 90.0),
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[local_vision] warm failed: {exc!r}")
        return False
    return True


def build_prompt(tags: list[dict[str, Any]]) -> str:
    """Kept deliberately blunt, because every attempt to enrich it measured worse.

    Two rewrites were tried on the `eval/` drawings and both lost accuracy:

    - Building the per-tag hints out of the catalog's own `description`,
      `drawing_ids` and `aliases` reads better but repeats "cup" three times for
      water, and gemma3 then tagged all three drawings water with detail "cup":
      2/3 down to 1/3.
    - Offering an "unsure" tag so the model could decline instead of guessing
      made gemma4 decline all three, including a cup it had just described as
      "a simple, open-topped" vessel. A model this size takes the exit every time.

    So the mapping sentences stay written out, and the model is always made to
    choose. Change this only against measurements, not on how it reads.
    """
    listing = "\n".join(f"- {tag['id']}: {tag.get('label') or tag['id']}" for tag in tags)
    return (
        "This is a finger drawing on a communication pad for someone who cannot speak.\n"
        "Say what the drawing shows, then pick the one tag that best matches it.\n"
        "Food of any kind (apple, pizza, sandwich, bowl) is food. A cup, glass, bottle "
        "or tap is water. A cross, plus or phone is help. A bed or pillow is rest.\n"
        "Mountains, a sun, a tree, a house, a book, or a little scene is story. "
        "A smile, a face, or a heart is talk. A pizza wedge or apple is still food. "
        "Do not force scenery into rest or water, and do not call food a heart.\n"
        f"Tags:\n{listing}\n"
        'Return JSON only: {"seen":"what it shows","tag":"one id from the list",'
        '"detail":"specific object like apple or tea",'
        '"spoken":"for food water help rest a first-person request; '
        'for story or talk an offer such as Want a short story?"}'
    )


def _image_b64(image: str | Path) -> str:
    return base64.b64encode(Path(image).read_bytes()).decode()


def usable_sentence(text: str) -> str:
    """Keep the model's sentence only if it is fit to speak, else return "".

    An empty result is not a loss: the pipeline then falls back to the curated
    `PHRASES` line for the tag, which is better written than anything a 12B model
    produces. This mainly catches the schema example being echoed back verbatim,
    which gemma4 did, leaving the pad about to say "I would like ..., please."
    """
    line = " ".join((text or "").split())
    if len(line) < 8 or len(line) > 140:
        return ""
    if "..." in line or "…" in line:
        return ""
    lowered = line.lower()
    # Placeholder-shaped or meta answers rather than a request.
    if any(marker in lowered for marker in ("<", ">", "{", "}", "insert", "object]", "tag]")):
        return ""
    # Must read as the patient speaking, which every curated phrase does.
    if not any(lowered.startswith(start) for start in ("i ", "i'", "please", "can i", "could i")):
        return ""
    return line


def rank_tags(
    tags: list[dict[str, Any]],
    *,
    image: str | Path,
    top_k: int = 5,
) -> dict[str, Any]:
    """Read the drawing locally, shaped like `rank_drawing_tags` so callers match.

    Raises on anything unusable, which leaves the caller on its template path.
    """
    del top_k  # The local prompt commits to one tag; there is no ranking to trim.
    if not tags:
        raise RuntimeError("no tags to rank")
    if image is None:
        raise RuntimeError("local vision needs the rendered PNG")
    allowed = {tag["id"]: tag for tag in tags}

    try:
        num_ctx = int(os.environ.get("LOCAL_VISION_NUM_CTX", DEFAULT_NUM_CTX))
    except ValueError:
        num_ctx = DEFAULT_NUM_CTX

    payload = _post(
        "/api/generate",
        {
            "model": model_name(),
            "prompt": build_prompt(tags),
            "images": [_image_b64(image)],
            "stream": False,
            "format": "json",
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0.1, "num_ctx": num_ctx},
        },
        timeout=timeout_s(),
    )
    parsed = json.loads(payload.get("response") or "{}")

    tag_id = str(parsed.get("tag") or "").strip().lower()
    seen = str(parsed.get("seen") or "").strip()
    if tag_id == UNSURE:
        # Taking the model at its word costs nothing: the caller drops to
        # templates, which is where an unreadable drawing belongs anyway.
        raise RuntimeError(f"local model could not read the drawing: {seen or 'no description'}")
    if tag_id not in allowed:
        raise RuntimeError(f"local model chose an unknown tag {tag_id!r}")

    detail = str(parsed.get("detail") or "").strip().lower()
    raw_spoken = str(parsed.get("spoken") or "")
    # Story and talk are offers from the pad, not first-person requests, so the
    # usual sentence gate would throw them away.
    if tag_id in {"story", "talk"}:
        spoken = " ".join(raw_spoken.split())
        if len(spoken) < 8 or "..." in spoken or "…" in spoken:
            spoken = ""
    else:
        spoken = usable_sentence(raw_spoken)
    return {
        "rankings": [
            {
                "tag_id": tag_id,
                "likelihood": LOCAL_LIKELIHOOD,
                "reason": f"read on this machine by {model_name()}: {seen or 'no description'}",
                "spoken": spoken,
                "detail": detail,
            }
        ],
        "spoken": spoken,
        "seen": seen,
        # Digits are geometry's job, and it does them offline and repeatably.
        # Asking a 12B model to also spot a 1 or a 2 only adds a way to be wrong.
        "digit": "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="report whether the local model can be reached")
    sub.add_parser("warm", help="load the model into VRAM")
    rank = sub.add_parser("rank", help="read one PNG and print the ranking")
    rank.add_argument("image", type=Path)
    args = parser.parse_args(argv)

    if args.command == "check":
        ok = available()
        print(f"url    {base_url()}")
        print(f"model  {model_name()}")
        print(f"usable {ok}")
        return 0 if ok else 1

    if args.command == "warm":
        ok = warm()
        print(f"warm   {ok}")
        return 0 if ok else 1

    from drawing_tags import DEFAULT_TAGS_PATH, DrawingTagStore

    catalog = [
        tag
        for tag in DrawingTagStore(DEFAULT_TAGS_PATH).catalog_for_model()
        if tag["id"] not in {"yes", "no"}
    ]
    print(json.dumps(rank_tags(catalog, image=args.image), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
