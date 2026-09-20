"""Turn step 2's text into audio the tablet can play.

`synthesise` is deliberately total: it returns a clip when there is audio and
None when there is not, and never raises. A missing key, a dead network or a
refused request all end up as None, which the tablet reads as "say this with
your own voice engine". Losing the nice voice is a cosmetic failure; going
silent is not.

Clips live in this process only. The pad plays them from `/tts/<id>` and they
are forgotten on restart. Repeating a sentence in the same run reuses the
bytes so a demo does not spend credits twice. Nothing is written under
`data/tts/`.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config

# Free plans are limited to mp3 at 128 kbps, which every Android browser plays.
OUTPUT_FORMAT = "mp3_44100_128"

# Lower stability lets the voice follow the punctuation of a short request;
# similarity keeps it recognisably the chosen voice.
VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.8}

# A long session should not keep every line. Oldest clips drop first.
_MAX_CLIPS = 48


@dataclass(frozen=True)
class SpeechClip:
    name: str
    audio: bytes


_clips: dict[str, SpeechClip] = {}


def clip_name(text: str) -> str:
    """Stable id for a sentence. Voice and model are part of the key."""
    key = f"{config.ELEVENLABS_MODEL}\n{config.ELEVENLABS_VOICE_ID}\n{text}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.mp3"


def get(name: str) -> SpeechClip | None:
    return _clips.get(name)


def _remember(clip: SpeechClip) -> SpeechClip:
    _clips[clip.name] = clip
    extra = len(_clips) - _MAX_CLIPS
    if extra > 0:
        for key in list(_clips)[:extra]:
            del _clips[key]
    return clip


def synthesise(text: str) -> SpeechClip | None:
    """Return playable MP3 bytes for `text`, or None if the tablet must speak it."""
    text = (text or "").strip()
    if not text:
        return None
    if not config.SPEECH_ENABLED:
        return None
    if not config.ELEVENLABS_API_KEY:
        print("[speech] no ELEVENLABS_API_KEY, leaving it to the tablet")
        return None

    name = clip_name(text)
    cached = _clips.get(name)
    if cached is not None:
        print(f"[speech] cached  {name}  {len(text)} chars, 0 credits")
        return cached

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

    clip = _remember(SpeechClip(name=name, audio=audio))
    print(f"[speech] {name}  {len(text)} chars, {len(audio):,} bytes, "
          f"model {config.ELEVENLABS_MODEL}")
    return clip
