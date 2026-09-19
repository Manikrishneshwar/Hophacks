# Ink pipeline

Draw on a tablet over the local network; every drawing lands on this machine as
a PNG plus its raw stroke data, and the tablet canvas wipes itself clean after
20 seconds of inactivity so you can keep going without touching anything.

## Setup from scratch

On a machine that has never seen this project:

```powershell
git clone https://github.com/Manikrishneshwar/Hophacks.git
cd Hophacks

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Optional, only to run the browser tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Always call the interpreter as `.\.venv\Scripts\python.exe` rather than plain
`python`. A bare `python` picks up the system interpreter, which does not have
these packages, and the resulting failures look like application bugs.

## Run it

```powershell
.\.venv\Scripts\python.exe run.py
```

Startup prints a LAN URL and a QR code. Point the tablet's camera at the QR
code, or type the URL. The tablet cannot use `localhost` — that would resolve to
the tablet itself — so it uses this machine's Wi-Fi address instead. Nothing is
exposed to the internet; both devices just have to be on the same network.

If the tablet cannot reach the page, Windows Firewall is almost certainly
blocking the port. Run this once in an **administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "InkPipeline" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private
```

## Using it

On the tablet, draw. The countdown in the bottom bar shows how long until the
canvas captures itself; any pen contact resets it. `Send now` captures
immediately, `Clear` discards without saving. An empty canvas never triggers a
capture, so idle time costs nothing.

Once a stylus has been used the page ignores finger input, so you can rest your
palm on the screen. Only one contact draws at a time — a second finger landing
mid-stroke is ignored rather than joined to the stroke in progress.

The desktop view at <http://localhost:8000/viewer> mirrors strokes live and
shows previous captures. It is entirely optional — captures are stored whether
or not it is open.

The second-brain view at <http://localhost:8000/brain> is the patient memory
graph: people, intents, drawings, and days linked together. Use **Load sample
week** if you want a populated graph for judges before any live captures.

## Feedback while drawing

The tablet plays a soft tone when a stroke starts and a brighter, shorter one
when it ends, and vibrates briefly on stroke start. This is confirmation that
the contact registered, without the user having to look for ink. The `Sound`
button in the bottom bar mutes both, and the choice is remembered.

Tones are synthesised with the Web Audio API rather than loaded from files, so
there is nothing to download and no delay on the first play.

**The first stroke after a page load has neither sound nor vibration.** This is
browser policy, and known. An `AudioContext` starts suspended and `resume()` is
asynchronous, so it is not running yet when that first tone tries to play. And
Chrome requires *sticky* user activation for `navigator.vibrate` — a tap that
has already completed — so the first contact on a fresh page is always blocked.
Tapping any toolbar button before drawing unlocks both; the `Sound` button does
it explicitly and plays a test tone.

Vibration uses `navigator.vibrate`, which **iPadOS does not implement at all**.
On an iPad neither the sounds nor the vibration work. Android is the target.

Only the tablet page does this. The desktop viewer is always silent.

## Tremor smoothing

Intended for users with unsteady hands, including stroke patients. Pointer input
runs through a One Euro filter: a low-pass whose cutoff rises with pointer
speed, so slow shaky movement is smoothed hard while deliberate fast strokes
stay responsive.

Set the level with `INK_SMOOTHING`, one of `off`, `light`, `medium` (default) or
`strong`. Against a simulated 8 Hz tremor, `medium` removes about 80% of the
wobble and `strong` about 91%.

```powershell
$env:INK_SMOOTHING = "strong"
.\.venv\Scripts\python.exe run.py
```

See them compared side by side:

```powershell
.\.venv\Scripts\python.exe scripts\smoothing_preview.py   # writes .preview/smoothing.png
```

The trade-off is lag: heavier smoothing makes the line trail further behind the
fingertip, which is visible in that image as the smoothed lines ending slightly
short. `strong` is worth it for a pronounced tremor and sluggish otherwise.

**The raw samples are kept.** When smoothing is active each stroke carries a
`raw` array of unfiltered points alongside the filtered `points`, and a
`smoothing` field naming the preset. For stroke patients the tremor may itself
be the signal of interest, so it is never discarded — and it means a capture can
be re-filtered later with different settings.

The tuning deliberately uses a much smaller `beta` than typical One Euro
examples. The usual values assume fast movement is intentional and should stay
sharp, but tremor is itself fast, so a large `beta` raises the cutoff exactly
when the shake is worst and waves it through.

## Answering yes/no by tapping

On an **empty** canvas, one tap means yes and two taps mean no. There is no
button; the whole canvas is the answer surface.

This only listens while a question is actually pending, which is what keeps it
from eating deliberate taps — stippling a field of dots still draws dots,
because nothing is asking anything. While a question is up, a banner shows the
question and the tap legend.

Anything that isn't a quick, stationary contact is treated as drawing, so you
can answer without clearing first; existing ink is left untouched.

Ask a question and wait for the answer:

```powershell
.\.venv\Scripts\python.exe scripts\ask.py "Is this the right reading?"
```

From Python, which is how step 2 will do it:

```python
from server.app import ask_tablet

answer = await ask_tablet("Is this the right reading?", timeout=120)  # 'yes' | 'no'
```

From the tablet's own JavaScript console, the state lives on `window.ink`:

```js
ink.answer            // 'yes' | 'no' | null - the most recent answer
ink.pending           // is a question waiting right now
ink.question          // its text
await ink.ask('Ready?')   // put a question up, resolves to 'yes' or 'no'
ink.onAnswer = (value, question) => { ... }
```

Answers append to `data/answers.jsonl`, one JSON object per line, independent of
the capture index because a question doesn't necessarily belong to a drawing.

Three or more taps in quick succession are rejected rather than guessed at, and
the question stays up. Tune the double-tap window with `INK_TAP_WINDOW_MS`; it
is also the delay before a single tap resolves as yes.

## What gets stored

```
data/captures/2026-09-19T00-22-31-287.png    the drawing, flattened onto white
data/captures/2026-09-19T00-22-31-287.json   the strokes that produced it
data/index.jsonl                             one metadata record per capture
data/answers.jsonl                           one record per tap answer
data/tts/<hash>.mp3                          cached speech, keyed by sentence
```

Filenames are local timestamps to the millisecond, so they sort chronologically
as plain strings. The server mints them, so a tablet with a skewed clock cannot
misorder them.

The stroke file holds every point as `[x, y, pressure, ms_since_first_point]` in
CSS-pixel space, which is enough to re-render the drawing at any resolution or
line weight later, or to train on the drawing motion rather than the picture.

`index.jsonl` is the dataset manifest — read it line by line to enumerate
everything:

```python
import json
with open("data/index.jsonl", encoding="utf-8") as f:
    records = [json.loads(line) for line in f if line.strip()]
```

Each record carries an `analysis` field, currently `null`, reserved for the text
that step 2's API returns for that image.

## Step 2 hook

Every saved capture is handed to `process_capture` in `server/pipeline.py`.
That function is a placeholder — replace its body with the real API call. It
returns the text to be spoken, or `None` for nothing.

```python
def process_capture(image: Path, data: dict[str, Any]) -> str | None:
    # image: path to the PNG; image.read_bytes() for the raw bytes
    # data:  {"points": [[x, y], ...], "strokes": int}
```

`points` is every point of every stroke in one flat list, in the order drawn.
Stroke boundaries are not marked, only counted — pressure and timestamps are
dropped. The coordinates are the smoothed ones, matching the PNG; the raw
tremor samples are still in the capture's `.json` if you need them.

It runs on a worker thread *after* the upload has been answered, so a slow API
call never holds up the tablet or risks its upload timing out. An exception is
logged and swallowed rather than taking the server down.

A capture with four strokes logs:

```
[capture] 2026-09-19T02-46-21-390.png  4 strokes  42,849 bytes
[pipeline] image   2026-09-19T02-46-21-390.png (42,849 bytes)
[pipeline] strokes 4
[pipeline] points  104, first=[150, 180], last=[671.9, 510.7]
[pipeline] text    'I would like a glass of water, please.'
[analysis] 2026-09-19T02-46-21-390: I would like a glass of water, please.
```

Until the real call is in, it returns `SAMPLE_TEXT` from the top of the file, so
the speech half below has a fixed sentence to work against.

The returned text also appears under that capture's thumbnail in the viewer.

## Speaking the result

Whatever `process_capture` returns is spoken on the tablet and then confirmed
with a tap, which closes the loop: the user sees and hears what was understood
and says yes or no without typing.

Synthesis is ElevenLabs, and it happens **here, not on the tablet**, so the API
key never leaves this machine. The MP3 is cached under `data/tts/` keyed by text,
voice and model, so repeating a sentence costs no credits. The tablet is handed
only a local URL.

```
ELEVENLABS_API_KEY=sk-...
```

Put that in a `.env` file in the project root; it is gitignored. Without a key
nothing breaks — the tablet reads the sentence with its own voice engine
instead, which is why a demo on a dead Wi-Fi network still talks.

The sentence is always shown on the canvas as well as spoken, and `Replay` in
the bottom bar says it again. The `Sound` button suppresses speech along with
the stroke tones; the text stays on screen either way.

The free ElevenLabs tier requires visible attribution, so "Powered by
ElevenLabs" appears under the sentence whenever their audio was used. It is not
shown when the device's own voice spoke, because then it isn't theirs.

`eleven_flash_v2_5` is the default model: roughly half a credit per character
and the quickest to return. `eleven_multilingual_v2` sounds warmer at a full
credit per character. On the free 10,000 credits a month, a 40-character
sentence works out to about 500 of them on flash, 250 on multilingual.

A capture that gets spoken and confirmed logs:

```
[speech] 6b1f3d...mp3  38 chars, 23,414 bytes, model eleven_flash_v2_5
[answer]  YES Did I get that right?
[analysis] 2026-09-19T02-46-21-390 confirmed: yes
```

The tap lands in `data/answers.jsonl` with the capture id and the spoken text in
its `context`, so a yes/no is always traceable to the drawing that caused it.

## Configuration

Set these in the environment before starting:

| Variable | Default | Meaning |
| --- | --- | --- |
| `INK_IDLE_TIMEOUT_MS` | `20000` | Inactivity before auto-capture |
| `INK_SMOOTHING` | `medium` | Tremor filter: `off`, `light`, `medium`, `strong` |
| `INK_TAP_WINDOW_MS` | `420` | Double-tap window, and the delay on a yes |
| `INK_TAP_ALWAYS_LISTEN` | `0` | `1` makes an empty canvas always accept taps |
| `INK_PORT` | `8000` | Port to serve on |
| `INK_DATA_DIR` | `./data` | Where captures are written |
| `INK_CONFIRM_TIMEOUT_S` | `60` | How long a spoken sentence waits to be confirmed |
| `INK_SPEECH` | `1` | `0` skips synthesis; the tablet still speaks the text |
| `ELEVENLABS_API_KEY` | unset | Without it the tablet's own voice is used |
| `ELEVENLABS_VOICE_ID` | Rachel | Any premade voice; the shared library needs a paid plan |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | `eleven_multilingual_v2` for quality over cost |
| `INK_DATABASE_URL` | unset | Postgres/TigerData connection string |

`.env` in the project root is read at startup and is gitignored, which is where
the key belongs. Real environment variables win over it, so a single run can be
overridden from the shell.

## Tests

```powershell
.\.venv\Scripts\python.exe scripts\browser_test.py   # capture loop, starts its own server
.\.venv\Scripts\python.exe scripts\tap_test.py       # tap answers and multi-touch
.\.venv\Scripts\python.exe scripts\smoothing_test.py # tremor filter effectiveness
.\.venv\Scripts\python.exe scripts\pipeline_test.py  # what step 2 receives
.\.venv\Scripts\python.exe scripts\speech_test.py    # synthesis, caching, fallback, confirm
.\.venv\Scripts\python.exe scripts\smoke_test.py     # HTTP path, against a running server
.\.venv\Scripts\python.exe scripts\show_data.py      # list what has been captured
.\.venv\Scripts\python.exe scripts\screenshots.py    # renders .preview/*.png
```

The browser test needs Chromium: `python -m playwright install chromium`.

## Layout

```
run.py              launcher: LAN address, QR code, firewall check
server/config.py    settings
server/storage.py   disk writes, the JSONL index, and the database seam
server/app.py       routes and the WebSocket fan-out
server/pipeline.py  the step 2 hook: drawing in, text out
server/speech.py    ElevenLabs synthesis and its on-disk cache
web/canvas.html     tablet drawing surface
web/viewer.html     optional desktop view
web/brain.html      second-brain memory graph
memory_graph.py     nodes and weighted links behind /brain
```

## Intent recognition and memory

Captured strokes (and optional PNG) go through an eyes-free recognition layer
for people who can only move one or two fingers. Offline feature compare always
runs against stored drawing templates. Gemini then ranks the top 5 tags from
`drawing_tags.json`. Those ranks get decreasing weights and are multiplied by
the feature score:

```text
final_weight = rank_weight × llm_likelihood × feature_score
```

If the API faults or times out, the feature scores alone are the answer. A
memory layer then appends today's note under `monthly_events/` and, at end of
day, rewrites `compressed_history.txt`.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# add GEMINI_API_KEY to .env

.\.venv\Scripts\python.exe recognize.py demo --offline
.\.venv\Scripts\python.exe recognize.py interpret examples/sample_strokes.json
.\.venv\Scripts\python.exe gemini_session.py chat "I am thirsty"
.\.venv\Scripts\python.exe gemini_session.py end-of-day
```

Stroke files from `data/captures/*.json` work here: each point only needs `x`
and `y`. Full design notes are in [docs/recognition.md](docs/recognition.md).

## Still to come

The capture path does not call the recogniser yet: `process_capture` still
returns `SAMPLE_TEXT`, and `IntentRecognizer().interpret(strokes, image=...)` is
what belongs in its place. Everything downstream of that return value — speech,
display on the canvas, the confirming tap — already works.

Then the database. `CaptureRecord.analysis` is the field the recognition result
belongs in, and `CaptureStore.sinks` is where a Postgres/TigerData sink plugs in
without touching the capture path.
