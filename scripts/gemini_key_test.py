"""Check that GEMINI_API_KEY in .env can actually reach Gemini.

    python scripts/gemini_key_test.py

Does not print the key. Pings every model in the chain the recogniser would
try, so a pass here means ranking can talk to the API too, and a partial pass
says which name this key cannot use.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from google.genai import types

from gemini_session import (
    PLACEHOLDER_KEYS,
    key_looks_like_project_id,
    load_api_key,
    resolve_model_chain,
)


def mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-2:]} ({len(key)} chars)"


def ping(client: genai.Client, model: str) -> tuple[bool, str]:
    """Send one word to `model`. Returns (worked, what to print)."""
    try:
        response = client.models.generate_content(
            model=model,
            contents='Reply with the single word "pong".',
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except errors.ClientError as exc:
        detail = str(exc)
        if "API_KEY_INVALID" in detail or "API key not valid" in detail:
            return False, "the key was rejected"
        if "NOT_FOUND" in detail or "not found" in detail.lower():
            return False, "this key cannot use that model name"
        return False, f"rejected: {detail[:90]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"never reached Gemini: {exc!r}"

    text = (response.text or "").strip()
    if not text:
        return False, "empty response body"
    return True, text


def main() -> int:
    load_dotenv(ROOT / ".env")
    key = load_api_key()
    models = resolve_model_chain()

    if not key or key.lower() in PLACEHOLDER_KEYS:
        print("FAIL  GEMINI_API_KEY is missing. Copy .env.example to .env and paste a key")
        print("      from https://aistudio.google.com/apikey")
        return 1
    if key_looks_like_project_id(key):
        print("FAIL  GEMINI_API_KEY looks like a project id, not a key.")
        print("      The value should start with AIza or AQ.")
        return 1

    print(f"key    {mask(key)}")
    print(f"chain  {', '.join(models)}")
    print()

    client = genai.Client(api_key=key)
    working = []
    for model in models:
        worked, detail = ping(client, model)
        print(f"{'ok  ' if worked else 'FAIL'}   {model:<22} {detail}")
        if worked:
            working.append(model)

    print()
    if not working:
        print("FAIL  no model in the chain works with this key")
        print("      Create a key at https://aistudio.google.com/apikey, or set GEMINI_MODELS")
        print("      in .env to names this key can use, comma separated.")
        return 1
    if len(working) < len(models):
        dead = [model for model in models if model not in working]
        print(f"ok     {len(working)} of {len(models)} work; {', '.join(dead)} would be skipped")
        return 0
    print(f"ok     every model in the chain works ({len(working)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
