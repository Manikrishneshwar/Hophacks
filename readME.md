# Sillow

**A sensing pillow.** Rest a finger, draw what you need, hear it spoken, tap yes
or no. A caregiver phone already has the picture.

Sillow is an eyes-free communication surface for someone who cannot speak easily
and may have tremor. There is no keyboard and no grid of buttons. One finger on
a tablet, a laptop on the same Wi-Fi, and a caretaker Android app. Nothing is
exposed to the internet.

| You draw | Sillow understands |
| --- | --- |
| Cup, glass, bottle | Water |
| Apple, pizza, bowl | Food (named when it can see it) |
| H, plus, cross, telephone | Help — then offer to call the named caretaker |
| Bed | Rest |
| Garden, mountains, music | A short story |
| Smile, face, heart | Company |
| A clear handwritten 1 or 2 | A circle / square / triangle game |

Help never opens a phone dialer on the pad. The pad stays on the canvas and
says help is on the way. The caretaker app gets a high-priority alarm and a
full-screen HELP screen.

Architecture, control flow, templates, and model fallbacks:
[technical_report.md](technical_report.md).
Devpost copy: [devpost.md](devpost.md).
Recognition scripts: [docs/recognition.md](docs/recognition.md).

---

## Quick start

```bash
git clone https://github.com/Manikrishneshwar/Hophacks.git
cd Hophacks
python -m venv .venv
```

Linux / macOS:

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Always use the venv interpreter. A bare `python` is the system one, which
lacks these packages and has already started a second server on port 8000.

Startup prints a LAN URL and QR codes. Point the **tablet** camera at the
tablet QR (never `localhost` — that is the tablet itself). Sideload
`android/app/build/outputs/apk/debug/app-debug.apk` on the caregiver phone,
enter the same LAN address, and allow notifications.

Put keys in a gitignored `.env` in the project root:

```
GEMINI_API_KEY=...
XAI_API_KEY=...          # optional; Grok after a fast Gemini refusal
ELEVENLABS_API_KEY=...   # optional; without it the tablet's own voice speaks
```

If the tablet cannot reach the page, the host firewall is blocking port 8000.
Windows (admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "InkPipeline" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private
```

Linux: `ufw allow 8000/tcp` (or the equivalent firewalld rule).

---

## Using it

**Pad.** Draw. The countdown in the bottom bar is time until capture; any
contact resets it. After five seconds of stillness the canvas saves a PNG plus
strokes and wipes itself. `Clear` discards, `Undo` removes the last stroke. An
empty canvas never captures.

Once a stylus has been used, fingers stop drawing so a palm can rest on the
screen. Only one contact draws at a time.

The laptop speaks a short first-person sentence (*I would like an apple,
please.*) and asks **Did I get that right?** One tap on the empty canvas is
yes, two taps is no. A no retries the next guess (up to three). After a yes,
follow-ups only run when the request is still generic (named water does not
ask tea; help offers to call Jordan). Then a caregiver closing.

**Viewer** (optional): <http://localhost:8000/viewer> mirrors strokes and
shows previous captures.

**Caretaker app.** Alerts (drawing, spoken lines, yes/no), Brain (memory
graph; tap a drawing node for the PNG), Today (confirmed request counts).
Keep the app open so the foreground service stays connected. `/caretaker` is
a web fallback; startup still prints a QR for it.

**Brain:** <http://localhost:8000/brain> — people, intents, drawings, days.
**Load sample week** plants a two-week story for judges before any live
captures.

Android is the target. iPadOS has no `navigator.vibrate`.

---

## While drawing

A soft tone on stroke start, a brighter shorter one on end, and a brief
vibration on start — confirmation without looking. `Sound` mutes tones and
speech; the spoken sentence still appears on screen. Tones are Web Audio, not
files.

**The first stroke after a page load has neither sound nor vibration.** That
is browser policy: `AudioContext` starts suspended, and Chrome wants a
completed tap before `navigator.vibrate`. Tap `Sound` (or any toolbar button)
once to unlock both.

Tremor goes through a One Euro filter (`INK_SMOOTHING`: `off`, `light`,
`medium` default, `strong`). Slow shake is smoothed hard; fast deliberate
strokes stay sharp. `beta` is much smaller than textbook One Euro values,
because tremor is itself fast. **Raw samples are kept** on each stroke as
`raw` — the shake may be the clinically interesting signal.

```bash
INK_SMOOTHING=strong .venv/bin/python run.py
.venv/bin/python scripts/smoothing_preview.py   # writes .preview/smoothing.png
```

---

## Yes and no

On an **empty** canvas, while a question is pending: one stationary tap is
yes, two is no. Three or more are ignored. Anything that travels or lasts is
drawing, so existing ink is left alone. `INK_TAP_WINDOW_MS` (default 420) is
both the double-tap window and the delay before a lone tap becomes yes.

```bash
.venv/bin/python scripts/ask.py "Is this the right reading?"
```

Answers append to `data/answers.jsonl` with the capture id and spoken text in
`context`.

---

## How a drawing is read

```text
idle capture (PNG + strokes)
        │
        ▼
geometry: a clear 1 / 2 starts the shape game (no API)
        │
        ▼
Gemini (lite → 3.6-flash) ranks tags from the PNG
        │  fast refusal
        ▼
xAI Grok (same prompt)
        │
        ▼
Ollama gemma3:12b (tags + image only)
        │
        ▼
templates in drawings_db.json (this person's priors)
        │
        ▼
speak → tap yes/no → follow-up if needed → closing
        │
        ▼
memory graph + daily note
```

History is a weak prior. Templates can break a Gemini near-tie when exactly
one candidate matches a stored drawing; they cannot invent a tag the model
omitted. Digit templates do not start the game (an open apple looks like a
2). A letter H is help, not “help with h.”

Seed or refresh templates:

```bash
.venv/bin/python scripts/seed_drawings.py
.venv/bin/python scripts/priors_test.py
```

Speech is ElevenLabs **on this machine**, so the API key never leaves the
laptop. Clips stay in memory and are served from `/tts/<id>`. No key: the
tablet uses its own voice. The free tier needs visible attribution; “Powered
by ElevenLabs” shows only when their audio actually played.

---

## What gets stored

```
data/captures/<id>.png     drawing, flattened onto white
data/captures/<id>.json    strokes: [x, y, pressure, ms] plus raw tremor
data/index.jsonl           one CaptureRecord per line (`analysis` after ranking)
data/answers.jsonl         one tap per line
```

Ids are server-minted local timestamps, so a skewed tablet clock cannot
misorder files. Coordinates are CSS pixels; the PNG is `dpr` times larger.

---

## Configuration

`.env` is read at startup (gitignored). Real environment variables win.

| Variable | Default | Meaning |
| --- | --- | --- |
| `INK_IDLE_TIMEOUT_MS` | `5000` | Inactivity before auto-capture |
| `INK_SMOOTHING` | `medium` | `off`, `light`, `medium`, `strong` |
| `INK_TAP_WINDOW_MS` | `420` | Double-tap window / delay on yes |
| `INK_PORT` | `8000` | Serve port |
| `INK_DATA_DIR` | `./data` | Captures |
| `INK_CONFIRM_TIMEOUT_S` | `60` | How long a question waits for a tap |
| `INK_SPEECH` | `1` | `0` skips ElevenLabs; device voice still speaks |
| `INK_RECOGNITION` | `1` | `0` skips ranking, speaks a stock water line |
| `GEMINI_TIMEOUT_S` | `15` | Whole Gemini + xAI capture budget |
| `GEMINI_MODELS` | lite, then 3.6-flash | Tried in order |
| `XAI_API_KEY` | unset | Grok after a fast Gemini refusal |
| `XAI_MODEL` | `grok-4.20-0309-non-reasoning` | Live path |
| `XAI_SLOW_MODEL` | `grok-4.6` | Background only (42–86s) |
| `LOCAL_VISION` | `1` | `0` goes straight to templates |
| `LOCAL_VISION_MODEL` | `gemma3:12b` | Ollama |
| `ELEVENLABS_API_KEY` | unset | Device voice if missing |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | Or `eleven_multilingual_v2` |

---

## Tests and tools

Browser tests need Chromium: `.venv/bin/python -m pip install -r requirements-dev.txt`
then `.venv/bin/python -m playwright install chromium`.

```bash
.venv/bin/python scripts/recognize_test.py      # ranking harness, no API credits
.venv/bin/python scripts/digit_test.py          # geometric 1 / 2 and shape grading
.venv/bin/python scripts/emergency_test.py      # help alerts the caretaker, no dialer
.venv/bin/python scripts/build_caretaker_apk.py # debug APK
.venv/bin/python scripts/browser_test.py        # capture loop (own server)
.venv/bin/python scripts/tap_test.py
.venv/bin/python scripts/smoothing_test.py
.venv/bin/python scripts/speech_test.py
.venv/bin/python scripts/caretaker_test.py
.venv/bin/python scripts/priors_test.py
.venv/bin/python scripts/model_chain_test.py
.venv/bin/python scripts/local_vision_test.py
.venv/bin/python scripts/xai_chain_test.py
.venv/bin/python scripts/gemini_key_test.py
.venv/bin/python scripts/show_data.py
```

---

## Layout

```
run.py                 LAN address, QR codes
server/                capture, converse, speech, shape game
web/                   canvas, viewer, caretaker, brain
android/               native caretaker (Alerts, Brain, Today)
recognize.py           ranking, prior blend, template fallback
drawing_features.py    stroke descriptors → drawings_db.json
stroke_geometry.py     1 / 2 and game shapes, no model
local_vision.py        Ollama when the cloud vendors fail
xai_backend.py         Grok between Gemini and local vision
memory_graph.py        /brain
```

`CaptureStore.sinks` is the unused seam for Postgres / Tiger Data.
`INK_DATABASE_URL` exists; nothing is wired yet.
