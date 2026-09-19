#!/usr/bin/env python3
"""Send a local image + text prompt to the Gemini API.

Usage:
  source .venv/bin/activate
  cp .env.example .env   # then add your GEMINI_API_KEY
  python test_gemini_image.py --image path/to/photo.jpg
  python test_gemini_image.py --image photo.png --prompt "What objects are in this photo?"
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from google.genai import types

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_PROMPT = "Describe this image in a few sentences."
PLACEHOLDER_KEYS = {"your-api-key-here", "changeme", "todo"}


def load_api_key() -> str | None:
    raw = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not raw:
        return None
    return raw.strip().strip("'\"")


def key_looks_like_project_id(api_key: str) -> bool:
    return api_key.startswith("gen-lang-client-") or api_key.startswith("projects/")


def guess_mime_type(image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type and mime_type.startswith("image/"):
        return mime_type
    raise SystemExit(
        f"Could not infer an image MIME type from {image_path}. "
        "Use a common image extension such as .jpg, .png, .webp, or .gif."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Try Gemini with a local image and a text prompt."
    )
    parser.add_argument("--image", required=True, type=Path, help="Path to a local image")
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help=f'Text prompt (default: "{DEFAULT_PROMPT}")',
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        help=f"Gemini model name (default: {DEFAULT_MODEL})",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    api_key = load_api_key()
    if not api_key or api_key.lower() in PLACEHOLDER_KEYS:
        print(
            "Missing API key. Copy a Gemini API key from "
            "https://aistudio.google.com/apikey into .env as GEMINI_API_KEY.",
            file=sys.stderr,
        )
        return 1
    if key_looks_like_project_id(api_key):
        print(
            "GEMINI_API_KEY looks like an AI Studio project ID "
            "(gen-lang-client-...), not an API key. Open "
            "https://aistudio.google.com/apikey and paste the key itself "
            "(it usually starts with AIza or AQ.).",
            file=sys.stderr,
        )
        return 1

    image_path = args.image.expanduser().resolve()
    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 1

    mime_type = guess_mime_type(image_path)
    image_bytes = image_path.read_bytes()

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=args.model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                args.prompt,
            ],
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
    except errors.ClientError as exc:
        print(f"Gemini request failed: {exc}", file=sys.stderr)
        if "API_KEY_INVALID" in str(exc):
            print(
                "The key was rejected. Create or copy a valid Gemini API key "
                "from https://aistudio.google.com/apikey and update .env.",
                file=sys.stderr,
            )
        return 1

    text = (response.text or "").strip()
    if not text:
        print("Gemini returned an empty response.", file=sys.stderr)
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
