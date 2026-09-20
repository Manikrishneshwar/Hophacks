# Ink pipeline

Draw on a tablet over the local network; every drawing lands on this machine as
a PNG plus its raw stroke data, and the tablet canvas wipes itself clean after
5 seconds of inactivity so you can keep going without touching anything.

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
canvas captures itself; any pen contact resets it. `Clear` discards without
saving, `Undo` removes the last stroke. An empty canvas never triggers a
capture, so idle time costs nothing.

Once a stylus has been used the page ignores finger input, so you can rest your
palm on the screen. Only one contact draws at a time — a second finger landing
mid-stroke is ignored rather than joined to the stroke in progress.

The desktop view at <http://localhost:8000/viewer> mirrors strokes live and
shows previous captures. It is entirely optional — captures are stored whether
or not it is open.

The caretaker Android app is a real APK, not a webpage. Sideload
`android/app/build/outputs/apk/debug/app-debug.apk` onto the caregiver phone
(allow unknown sources). Open it on the same Wi-Fi, type the laptop address
printed by `run.py` (never `localhost`), and allow notifications. **Alerts**
shows each finished drawing with its image, spoken lines, and yes/no.
**Brain** is the memory graph; tap a drawing node to see the PNG. Keep the
app running so the foreground service can receive events.

A confirmed help request does **not** open the phone dialer on the pad. The
pad stays on the canvas and says help is on the way. The caretaker app gets a
high-priority emergency notification (alarm sound, vibration, full-screen
HELP) that outranks ordinary drawing alerts.

The `/caretaker` webpage still exists as a fallback. Startup still prints a
QR code for it.

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

Each record's `analysis` field is filled after recognition: spoken `text`,
`tag`, optional `detail` (soup, tea, …), and `confirmed`.

## Step 2 hook

Every saved capture is handed to `process_capture` in `server/pipeline.py`.
That ranks the drawing, returns a spoken sentence (or `None` to stay silent),
then the tablet confirms it.

```python
def process_capture(image: Path, data: dict[str, Any]) -> str | None:
    # image: path to the PNG; image.read_bytes() for the raw bytes
    # data:  {"points": [[x, y], ...], "polylines": [[[x, y], ...], ...], "strokes": int}
```

`points` is every point of every stroke in one flat list, in the order drawn.
`polylines` is the same points still grouped by stroke — a cross and a cup
cannot be told apart once the boundaries are gone. Pressure and timestamps are
dropped. The coordinates are the smoothed ones, matching the PNG; the raw
tremor samples are still in the capture's `.json` if you need them.

It runs on a worker thread *after* the upload has been answered, so a slow API
call never holds up the tablet or risks its upload timing out. An exception is
logged and swallowed rather than taking the server down.

A capture logs something like:

```
[capture] 2026-09-19T02-46-21-390.png  4 strokes  42,849 bytes
[pipeline] image   2026-09-19T02-46-21-390.png (42,849 bytes)
[pipeline] strokes 4
[pipeline] points  104, first=[150, 180], last=[671.9, 510.7]
[pipeline] top     'water'  fallback=False
[pipeline] text    'I would like a glass of water, please.'
[analysis] 2026-09-19T02-46-21-390: I would like a glass of water, please.
```

If Gemini is down or the tag list has no match, it falls back to `SAMPLE_TEXT`
so the tablet still speaks. Tests set `INK_RECOGNITION=0` to skip ranking.

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

The sentence is always shown on the canvas as well as spoken. The `Sound`
button suppresses speech along with the stroke tones; the text stays on screen
either way.

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
| `INK_IDLE_TIMEOUT_MS` | `5000` | Inactivity before auto-capture |
| `INK_SMOOTHING` | `medium` | Tremor filter: `off`, `light`, `medium`, `strong` |
| `INK_TAP_WINDOW_MS` | `420` | Double-tap window, and the delay on a yes |
| `INK_TAP_ALWAYS_LISTEN` | `0` | `1` makes an empty canvas always accept taps |
| `INK_PORT` | `8000` | Port to serve on |
| `INK_DATA_DIR` | `./data` | Where captures are written |
| `INK_CONFIRM_TIMEOUT_S` | `60` | How long a spoken sentence waits to be confirmed |
| `INK_SPEECH` | `1` | `0` skips synthesis; the tablet still speaks the text |
| `INK_RECOGNITION` | `1` | `0` skips Gemini ranking and speaks `SAMPLE_TEXT` |
| `GEMINI_TIMEOUT_S` | `15` | Budget for one call, shared across every model tried |
| `GEMINI_MODELS` | `gemini-3.5-flash-lite,gemini-3.6-flash` | Tried in order until one answers |
| `GEMINI_MODEL` | unset | Pins the primary; the defaults stay behind it |
| `GEMINI_SLOW_TIMEOUT_S` | `120` | Budget for background work, where nobody is waiting |
| `XAI_API_KEY` | unset | Grok, tried after every Gemini model refuses |
| `XAI_FALLBACK` | `1` | `0` skips xAI even with a key set |
| `XAI_MODEL` | `grok-4.20-0309-non-reasoning` | Fast enough for the live budget |
| `XAI_SLOW_MODEL` | `grok-4.6` | Background work only; better, but 42-86s |
| `LOCAL_VISION` | `1` | `0` skips the on-machine model and goes straight to templates |
| `LOCAL_VISION_MODEL` | `gemma3:12b` | Ollama model read when every Gemini model fails |
| `LOCAL_VISION_TIMEOUT_S` | `25` | Budget for the local read, separate from Gemini's |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Where the Ollama daemon listens |
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
.\.venv\Scripts\python.exe scripts\recognize_test.py # ranking harness, no Gemini credits
.\.venv\Scripts\python.exe scripts\digit_test.py     # geometric 1 / 2 and shape grading
.\.venv\Scripts\python.exe scripts\model_chain_test.py # model fallback, no Gemini credits
.\.venv\Scripts\python.exe scripts\local_vision_test.py # on-machine fallback, no Ollama needed
.\.venv\Scripts\python.exe scripts\xai_chain_test.py   # the Grok tier, stubbed, no credits
.\.venv\Scripts\python.exe scripts\eval_benchmark.py selftest # catalog, scoring, HTML report
.\.venv\Scripts\python.exe scripts\speech_test.py    # synthesis, caching, fallback, confirm
.\.venv\Scripts\python.exe scripts\caretaker_test.py # caregiver phone gets image + yes/no
.\.venv\Scripts\python.exe scripts\emergency_test.py # help alerts the caretaker, pad does not dial
.\.venv\Scripts\python.exe scripts\build_caretaker_apk.py # debug APK for the caregiver phone
.\.venv\Scripts\python.exe scripts\gemini_key_test.py # GEMINI_API_KEY actually reaches Gemini
.\.venv\Scripts\python.exe scripts\smoke_test.py     # HTTP path, against a running server
.\.venv\Scripts\python.exe scripts\show_data.py      # list what has been captured
.\.venv\Scripts\python.exe scripts\screenshots.py    # renders .preview/*.png
```

The browser test needs Chromium: `python -m playwright install chromium`.

## Layout

```
run.py              launcher: LAN address, QR codes for tablet and caretaker
server/config.py    settings
server/storage.py   disk writes, the JSONL index, and the database seam
server/app.py       routes and the WebSocket fan-out
server/pipeline.py  the step 2 hook: drawing in, text out
server/speech.py    ElevenLabs synthesis and its on-disk cache
web/canvas.html     tablet drawing surface
web/viewer.html     optional desktop view
web/caretaker.html  caregiver web fallback
web/brain.html      second-brain memory graph
android/            native caretaker app (Alerts + Brain tabs)
memory_graph.py     nodes and weighted links behind /brain
stroke_geometry.py  reads a 1 or 2, and grades game shapes, without a model
local_vision.py     reads the drawing on this machine when Gemini is unreachable
xai_backend.py      Grok as a second vendor between Gemini and the local model
```

## Intent recognition and memory

Captured strokes and the PNG go through Gemini first. It ranks tags from the
drawing (history is only a weak prior) and writes the sentence spoken on the
pad, naming the object when it can see one. If every Gemini model faults, xAI's
Grok is asked the same question; if that fails too, a vision model on this
machine reads the PNG; and only if that is unavailable do the stroke templates in
`drawings_db.json` answer. The last tier needs no key and no network at all.

Which one answered is recorded, as `source` on each candidate (`gemini`, `xai`,
`local`, `feature_fallback`) and as `fallback_used`, so a weaker reading is never
presented with Gemini's authority.

Every Gemini call walks a chain of models, `gemini-3.5-flash-lite` then
`gemini-3.6-flash`, stopping at the first that answers. The lite model leads on
availability rather than paper quality: `gemini-3.8-flash` allows only 20
free-tier requests a day, which one demo session spends, and a model that has
stopped answering ranks nothing at all. On the three labelled drawings in
`eval/` the lite model gets all three intents right, though it is vaguer about
the specific object, which the follow-up questions exist to pin down.

The chain also covers a model being withdrawn or overloaded, which happens:
`gemini-2.5-flash` is still listed by the API but 404s for new keys, and flash
models return 503 under load. A rejected key is not retried down the chain,
since it would fail identically every time. The whole chain shares the one `GEMINI_TIMEOUT_S` budget, so the pad
never waits longer than a single attempt used to. Because the API refuses any
per-request deadline under 10s, at the default 15s the primary gets the full
budget and the fallback covers quick refusals; raise the budget past 20s and the
two are split evenly, which also rescues a hang. `scripts\gemini_key_test.py`
pings every model in the chain and says which ones your key can use.

### A second vendor before falling back locally

`XAI_API_KEY` adds xAI's Grok between Gemini and the on-machine model. It is a
different vendor on a different quota, and it hooks into the shared `_generate`,
so every call benefits rather than ranking alone. It is also strong enough to be
handed the real prompt with all of its patient context, which the local model is
not. A missing Gemini key is no longer fatal while this is configured.

Model choice here is about latency. Measured on the `eval/` drawings with the
real ranking prompt:

| Model | Correct | Time |
| --- | --- | --- |
| `grok-4.6` | 3/3 | 42-86s |
| `grok-4.20-0309-non-reasoning` | 2/3 | 2.3-2.6s |
| `grok-4.3` | 0/3 | 13-18s |

The pad has to speak inside `GEMINI_TIMEOUT_S`, so the fast one serves captures
even though `grok-4.6` is plainly better at the task, and is the only model
anywhere that reads the pizza drawing correctly. `grok-4.6` is used instead for
the two calls nobody waits on, the end-of-day compression and the memory-graph
patch, which get `GEMINI_SLOW_TIMEOUT_S` because they run after the pad has
already spoken. `python xai_backend.py check` confirms the key and lists models.

### Reading the drawing on this machine

The free tier is small enough that one demo can spend it, so when every Gemini
model has refused, `local_vision.py` sends the same PNG to a vision model served
by [Ollama](https://ollama.com) on this machine. Install it without root and pull
the model:

```bash
curl -fL https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst \
  -o /tmp/ollama.tar.zst
tar --zstd -xf /tmp/ollama.tar.zst -C ~/.local
~/.local/bin/ollama serve &
~/.local/bin/ollama pull gemma3:12b
python local_vision.py check
```

`gemma3:12b` needs about 8GB of VRAM and was picked by measurement on the three
labelled drawings in `eval/`:

| Model | Correct | Warm | Notes |
| --- | --- | --- | --- |
| `gemma3:12b` | 2/3 | 2.0s | Names what it sees, so `detail` is usually filled |
| `gemma4` | 2/3 | 1.2s | Fastest, but vague and usually leaves `detail` empty |
| `qwen2.5vl:7b` | 1/3 | 4.9s | Read the cup as a bowl |

All three fit a 12GB card. `gemma4` is quicker but describes a cup as "a finger
drawing", and `detail` is what lets the pad name the object and decide whether a
follow-up is needed, so the slower one wins. With no GPU this whole tier is far
slower than the budget allows and `LOCAL_VISION=0` is the better setting.

Three things about this tier are deliberate. It does not reuse the prompt
`rank_drawing_tags` sends: given the full 4.5KB of patient context a 7B-12B model
stops looking at the picture and answers from the history, so the local prompt is
cut back to the tag list and the image, losing history as a tie-breaker. Its
guesses carry a low likelihood and are flagged `fallback_used`, because the model
is right often enough to be worth asking about and wrong often enough that the
pad must not assert: it tagged the cross drawing `help` while describing it as
"a snake". And the sentence it writes is only spoken if it survives
`usable_sentence`, which rejects the placeholder echoes these models produce
(gemma4 returned the schema's own `"I would like ..., please."` verbatim); a
rejected one falls back to the curated phrase for the tag.

Resist enriching the local prompt without measuring. Two rewrites that read
better both lost accuracy: building the per-tag hints from the catalog's own
descriptions and aliases repeats "cup" three times for water and pushed gemma3 to
2/3 down to 1/3, and offering an "unsure" tag made gemma4 decline all three
drawings, including a cup it had just described correctly.

The model stays resident (`keep_alive: -1`) and is warmed on a background thread
when the recogniser is first built, because the initial 8GB load into VRAM takes
around 27s and no capture can wait that long.

### Seeding the templates

`drawings_db.json` is gitignored, so it starts empty on a fresh clone and the
bottom tier can recognise nothing at all. Fill it once:

```bash
python scripts/seed_drawings.py
```

That takes the human-checked captures from `eval/catalog.json` for the tags they
cover and draws plain outlines for the rest. The synthetic ones are guesses at a
shape and much weaker than a real drawing; record real ones through the pad and
re-run to replace them. Re-running only updates, never duplicates.

A no tap retries the next guess. After a
yes, follow-ups only run when the request is still generic (water does not
ask tea; help offers to alert the named caretaker). The pad stays on the
canvas and does not open a phone dialer; the caretaker app gets an emergency
notification instead. Then the pad speaks a caregiver closing
(`I'll get you some water.`).

A drawing that is not a care need is no longer forced into food, water, help,
or rest. Mountains, a sun, a book, or a little scene is `story`; a smile, a
face, or a heart is `talk`. The pad offers a short story or some company, and
after a yes it actually tells one. That is what the landscape drawings were
asking for when they used to come back as "I would like to lie down."

A handwritten 1 or 2 starts the drawing game, and that one is measured rather
than asked about. `stroke_geometry.py` reads a 1 as a single tall upright
near-straight stroke and a 2 as a single open curve running from the top down
to a left-to-right baseline. A clear one skips the API call entirely, so the
game starts instantly and works with no key at all; a marginal one lets Gemini
rank as usual, and geometry then either supplies a digit Gemini missed or
refuses one it claimed on something no character can be, such as a closed loop.
Each attempt in the game is graded the same way when the API is unavailable, on
corner count and circularity.

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

The database. `CaptureRecord.analysis` is the field the recognition result
belongs in, and `CaptureStore.sinks` is where a Postgres/TigerData sink plugs in
without touching the capture path.
