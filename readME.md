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
palm on the screen.

The desktop view at <http://localhost:8000/viewer> mirrors strokes live and
shows previous captures. It is entirely optional — captures are stored whether
or not it is open.

## Answering yes/no by tapping

On an **empty** canvas, one tap means yes and two taps mean no. There is no
button; the whole canvas is the answer surface.

This only listens while a question is actually pending, which is what keeps it
from eating deliberate taps — stippling a field of dots on a blank canvas still
draws dots, because nothing is asking anything. While a question is up, a
banner shows the question and the tap legend.

Anything that isn't a quick, stationary contact is treated as drawing, and ink
on the canvas suspends answering entirely (the banner then says to clear first).
So a question can sit pending while you draw; you answer it once the canvas is
blank again.

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

## Configuration

Set these in the environment before starting:

| Variable | Default | Meaning |
| --- | --- | --- |
| `INK_IDLE_TIMEOUT_MS` | `20000` | Inactivity before auto-capture |
| `INK_TAP_WINDOW_MS` | `420` | Double-tap window, and the delay on a yes |
| `INK_TAP_ALWAYS_LISTEN` | `0` | `1` makes an empty canvas always accept taps |
| `INK_PORT` | `8000` | Port to serve on |
| `INK_DATA_DIR` | `./data` | Where captures are written |
| `INK_DATABASE_URL` | unset | Postgres/TigerData connection string |

## Tests

```powershell
.\.venv\Scripts\python.exe scripts\browser_test.py   # capture loop, starts its own server
.\.venv\Scripts\python.exe scripts\tap_test.py       # tap answers, starts its own server
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
web/canvas.html     tablet drawing surface
web/viewer.html     optional desktop view
```

## Step 2: intent recognition and memory

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

Text-to-speech on the tablet is still open. The tablet WebSocket is
bidirectional and `hub.to_tablets()` is wired but unused, so speech can be
pushed down the existing connection; `CaptureRecord.analysis` is the field the
recognition result belongs in.
