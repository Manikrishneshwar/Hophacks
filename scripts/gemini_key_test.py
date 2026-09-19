"""Check that GEMINI_API_KEY in .env can actually reach Gemini.

    python scripts/gemini_key_test.py

Does not print the key. Sends a one-word ping with the same model the
recogniser uses, so a pass here means ranking can talk to the API too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from google.genai import types

from gemini_session import DEFAULT_MODEL, PLACEHOLDER_KEYS, key_looks_like_project_id, load_api_key


def mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-2:]} ({len(key)} chars)"


def main() -> int:
    load_dotenv(ROOT / ".env")
    key = load_api_key()
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    if not key or key.lower() in PLACEHOLDER_KEYS:
        print("FAIL  GEMINI_API_KEY is missing. Copy .env.example to .env and paste a key")
        print("      from https://aistudio.google.com/apikey")
        return 1
    if key_looks_like_project_id(key):
        print("FAIL  GEMINI_API_KEY looks like a project id, not a key.")
        print("      The value should start with AIza or AQ.")
        return 1

    print(f"key    {mask(key)}")
    print(f"model  {model}")

    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model,
            contents='Reply with the single word "pong".',
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except errors.ClientError as exc:
        detail = str(exc)
        print(f"FAIL  Gemini rejected the request: {detail}")
        if "API_KEY_INVALID" in detail or "API key not valid" in detail:
            print("      The key was rejected. Create a new one at https://aistudio.google.com/apikey")
        elif "NOT_FOUND" in detail or "not found" in detail.lower():
            print("      That model name is not available on this key. Set GEMINI_MODEL in .env")
            print("      to something this key can use, for example gemini-2.5-flash.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  request never reached Gemini: {exc!r}")
        return 1

    text = (response.text or "").strip()
    if not text:
        print("FAIL  Gemini returned an empty body")
        return 1

    print(f"reply  {text}")
    print("ok     key works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
