"""Turn step 2's text into an audio file the tablet can play.

`synthesise` is deliberately total: it returns a path when there is audio and
None when there is not, and never raises. A missing key, a dead network or a
refused request all end up as None, which the tablet reads as "say this with
your own voice engine". Losing the nice voice is a cosmetic failure; going
silent is not.

Files are cached under `data/tts/` keyed by text, voice and model, so repeating
a sentence during a demo costs no credits.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config

# Free plans are limited to mp3 at 128 kbps, which every Android browser plays.
OUTPUT_FORMAT = "mp3_44100_128"

# Lower stability lets the voice follow the punctuation of a short request;
# similarity keeps it recognisably the chosen voice.
VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.8}


def cache_path(text: str) -> Path:
    """Where `text` would be cached. Voice and model are part of the key, so
    changing either in `.env` produces new files instead of stale audio."""
    key = f"{config.ELEVENLABS_MODEL}\n{config.ELEVENLABS_VOICE_ID}\n{text}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return config.TTS_DIR / f"{digest}.mp3"


def synthesise(text: str) -> Path | None:
    """Return a playable MP3 for `text`, or None if the tablet must speak it."""
    text = (text or "").strip()
    if not text:
        return None
    if not config.SPEECH_ENABLED:
        return None
    if not config.ELEVENLABS_API_KEY:
        print("[speech] no ELEVENLABS_API_KEY, leaving it to the tablet")
        return None

    path = cache_path(text)
    if path.exists() and path.stat().st_size > 0:
        print(f"[speech] cached  {path.name}  {len(text)} chars, 0 credits")
        return path

    url = (f"{config.ELEVENLABS_BASE_URL.rstrip('/')}/v1/text-to-speech/"
           f"{config.ELEVENLABS_VOICE_ID}?output_format={OUTPUT_FORMAT}")
    body = json.dumps({
        "text": text,
        "model_id": config.ELEVENLABS_MODEL,
        "voice_settings": VOICE_SETTINGS,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    })

    try:
        with urllib.request.urlopen(request, timeout=config.ELEVENLABS_TIMEOUT_S) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        # The body carries the actual reason: wrong key, no credits, bad voice.
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"[speech] HTTP {exc.code} from ElevenLabs: {detail}")
        return None
    except Exception as exc:  # noqa: BLE001 - offline laptop, DNS, timeout
        print(f"[speech] request failed: {exc!r}")
        return None

    if not audio:
        print("[speech] ElevenLabs returned no audio")
        return None

    # Write beside the target and rename, so a half-written file is never
    # served or mistaken for a cache hit.
    config.TTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_bytes(audio)
    temporary.replace(path)

    print(f"[speech] {path.name}  {len(text)} chars, {len(audio):,} bytes, "
          f"model {config.ELEVENLABS_MODEL}")
    return path
