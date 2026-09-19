"""Runtime settings, overridable through environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """Read .env into the environment without adding a dependency.

    Real environment variables win, so you can override a .env value for a
    single run from the shell.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()

WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("INK_DATA_DIR", ROOT / "data"))
CAPTURE_DIR = DATA_DIR / "captures"
INDEX_PATH = DATA_DIR / "index.jsonl"
ANSWERS_PATH = DATA_DIR / "answers.jsonl"

HOST = os.environ.get("INK_HOST", "0.0.0.0")
PORT = int(os.environ.get("INK_PORT", "8000"))

# How long the canvas may sit untouched before it is captured and wiped.
IDLE_TIMEOUT_MS = int(os.environ.get("INK_IDLE_TIMEOUT_MS", "20000"))

# Captures are flattened onto a white background so they are usable as-is by
# vision models in step 2; transparent PNGs tend to render as black.
CANVAS_BACKGROUND = "#ffffff"

# Tap-to-answer on an empty canvas: one tap yes, two taps no. The window is how
# long a first tap waits for a second before it is read as a yes, so it is the
# latency you feel when answering yes. Too short and a slow double tap reads as
# a yes; too long and every yes feels sluggish.
TAP_WINDOW_MS = int(os.environ.get("INK_TAP_WINDOW_MS", "420"))

# Taps only answer while a question is actually pending. Set this to 1 to have
# an empty canvas always listen, at the cost of stippled dots being eaten.
TAP_ALWAYS_LISTEN = os.environ.get("INK_TAP_ALWAYS_LISTEN", "0") == "1"

# Step 2 seam: when a database URL is present the storage layer will also write
# capture metadata to Postgres/TigerData. Unset means disk-only.
DATABASE_URL = os.environ.get("INK_DATABASE_URL", "")
