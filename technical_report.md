# Ink pipeline — technical report

This document is the architecture of the project as it stands. It describes
what each piece does, how control moves from a finger on the pad to a spoken
line and a caretaker alert, and the feature templates that back recognition
when a model is unavailable.

For how to run it, see [readME.md](readME.md). For recognition-only notes and
scripts, see [docs/recognition.md](docs/recognition.md).

---

## 1. What it is

An eyes-free communication pad for someone who cannot speak easily and may
have tremor. They draw on an Android tablet. A laptop on the same Wi-Fi
stores the drawing, reads it, speaks a short first-person sentence, and asks
yes or no by tap. A caregiver phone watches the same session.

The pad is not a general chat. It maps a drawing onto a small tag set:

| Tag | Meaning | Typical ink |
| --- | --- | --- |
| `food` | Wants something to eat | apple, pizza wedge, bowl |
| `water` | Wants a drink | cup, glass, bottle |
| `help` | Needs a caregiver now | handwritten H, plus, cross, telephone |
| `rest` | Wants to rest | bed, pillow |
| `story` | Wants a short story about the drawing | scenery, book, music, animal |
| `talk` | Wants company | smile, face, heart |
| `play` | Starts the shape game | handwritten 1 or 2, geometry only |

`yes` and `no` are taps on an empty canvas, never drawings.

---

## 2. System shape

Three devices, one process on the laptop.

```text
  Android tablet                    Laptop                         Caregiver phone
  (Chrome, /canvas)                 (uvicorn, run.py)              (native APK)
        |                                 |                               |
        |  PNG + strokes                  |                               |
        |  POST /api/capture  ----------->|                               |
        |                                 |  WS /ws?role=caretaker        |
        |  WS /ws?role=tablet             |------------------------------>|
        |<----- speak / ask / game -------|  event + PNG + emergency      |
        |  tap yes/no -------------------->|                               |
```

Nothing is exposed to the internet. The tablet cannot use `localhost`;
`run.py` prints the laptop's LAN address and a QR code.

| Surface | URL / artifact | Role |
| --- | --- | --- |
| Tablet pad | `/canvas` (`web/canvas.html`) | Draw, hear, tap |
| Desktop viewer | `/viewer` | Optional live mirror |
| Memory graph | `/brain` | Today + history nodes |
| Caretaker web | `/caretaker` | Fallback if the APK is not installed |
| Caretaker app | `android/` debug APK | Alerts, Brain, Today, emergency |

WebSocket roles on `/ws`: `tablet`, `viewer`, `caretaker`. The hub fans
captures, spoken lines, questions, game prompts, and graph updates to whoever
is connected.

---

## 3. Repository layout

```text
run.py                  LAN address, QR codes, uvicorn
server/config.py        env + .env, smoothing presets, timeouts
server/storage.py       PNG + stroke JSON + index.jsonl + answers.jsonl
server/app.py           HTTP, WebSocket hub, analyse → converse
server/pipeline.py      process_capture, spoken lines, follow-ups, closings
server/speech.py        ElevenLabs, data/tts/ cache, never raises
server/shape_game.py    in-process circle / square / triangle session

recognize.py            IntentRecognizer: geometry, Gemini, blend, fallbacks
gemini_session.py       Gemini chain, ranking prompt, history, graph patch
xai_backend.py          Grok over HTTP, inside _generate
local_vision.py         Ollama vision when the cloud vendors fail
drawing_features.py     stroke descriptors + drawings_db.json store
drawing_tags.py         living tag catalog (drawing_tags.json)
stroke_geometry.py      LLM-free 1 / 2 and shape grading
memory_graph.py         nodes and weighted links (memory_graph.json)

web/                    canvas, viewer, caretaker, brain
android/                Kotlin caretaker: Alerts, Brain, Today
data/priors/            labelled repeats used as feature templates
data/captures/          live PNGs and stroke files
drawings_db.json        stored descriptors
drawing_tags.json       tags, aliases, drawing_ids
scripts/                tests, seed, APK build, eval
```

Python on Linux is `.venv/bin/python`. On Windows it is
`.\.venv\Scripts\python.exe`. A bare `python` is the system interpreter and
does not have the project dependencies.

---

## 4. Flow of control

This is the live path from ink to a confirmed request.

### 4.1 Capture (tablet → disk)

1. Finger contact on `/canvas` is smoothed with a One Euro filter
   (`INK_SMOOTHING`, default `medium`). Each stroke keeps a `raw` array of
   unfiltered points; tremor is never discarded.
2. After `INK_IDLE_TIMEOUT_MS` (default 5000) with no contact, the canvas
   flattens itself onto white, POSTs the PNG and stroke JSON to
   `/api/capture`, and wipes.
3. `create_capture` writes disk first (`CaptureStore.save`):
   - `data/captures/<id>.png`
   - `data/captures/<id>.json`
   - one line on `data/index.jsonl`
   then answers the tablet. Recognition is scheduled after the response so a
   slow model cannot time out the upload.
4. Viewers get `{"type": "capture", "record": ...}` immediately.

Stroke points are `[x, y, pressure, ms]` in CSS pixels. The PNG is `dpr`
times larger. Pressure is always 0.5; the tool is always `touch`. `t` is a
shared clock across strokes in one capture.

### 4.2 Analyse

`analyse(record, strokes)` builds a payload
`{points, polylines, strokes}` (smoothed `x,y` only) and calls
`pipeline.process_capture(image, payload)` on a worker thread.

If a shape game is already open for that tablet session, the capture is
graded as a game attempt and never ranked as a need.

### 4.3 `process_capture`

```text
INK_RECOGNITION=0  -->  SAMPLE_TEXT  ("I would like a glass of water, please.")
        |
        v
IntentRecognizer.interpret(strokes, image)
        |
        +-- geometry says 1 or 2 with high confidence
        |         --> tag play, spoken "Let's play a drawing game."
        |             (only if digit_source == "geometry")
        |
        +-- else top tag + spoken sentence + optional detail
                  --> CaptureResult
```

A stored digit prior must not start the game. An open apple scores about
0.75 against a stored `two*`, which is not a 2. The game starts only when
`digit in {1, 2}` and `digit_source == "geometry"`.

### 4.4 Recognition inside `interpret`

```text
parse strokes
    |
    +--> stroke_geometry.detect_digit
    |         fast_path (confidence >= 0.85)  --> return digit, skip models
    |         else keep the guess for later (assist / veto)
    |
    +--> DrawingFeatureStore.score_all   (always, so a blend is ready)
    |
    +--> GeminiPatientModel.rank_drawing_tags(PNG, tag catalog)
    |         timeout GEMINI_TIMEOUT_S (default 15s)
    |         on success: _from_gemini + optional unique-strong blend
    |         on hang/exception: _without_gemini
    |
    +--> _settle_digit: veto / assist / keep Gemini's claimed digit
    |
    v
RecognitionResult  (top_tag, spoken, candidates, digit, digit_source, ...)
```

Gemini itself walks a vendor chain *inside* `_generate`:

```text
gemini-3.5-flash-lite
        |
        | 404 / 429 / 400 / invalid
        v
gemini-3.6-flash
        |
        | still a fast refusal, XAI_API_KEY set, >= ~3.5s left
        v
xAI Grok   (XAI_MODEL, ~2.5s)
        |
        | Gemini hung (budget already gone) or Grok missing/failed
        v
local_vision.py  (Ollama, gemma3:12b)     only from _without_gemini
        |
        | Ollama down or LOCAL_VISION=0
        v
templates  (_from_features on drawings_db.json)
```

A missing or bad `GEMINI_API_KEY` is not fatal while `XAI_API_KEY` is set.
A hang burns the 15s budget; Grok is then skipped and the local model runs.
`XAI_FALLBACK=0` disables Grok.

Each candidate records `source`: `gemini`, `xai`, `local`, or
`feature_fallback`. `fallback_used` is true for local and templates so the
caretaker view does not present those with Gemini's authority.

### 4.5 Confirm, follow up, close

`converse` speaks the top sentence and asks **"Did I get that right?"**

- **no** — retry the next candidate (up to `MAX_GUESSES` = 3) with
  "Is this better?"
- **yes** — `refine_details`, then a closing (or an emergency alert)
- three nos — "I am not sure. Could you draw that again?"

Follow-ups run only when `needs_followup` is true:

| Confirmed tag | Follow-up |
| --- | --- |
| `food` | dish only if the spoken line did not already name one |
| `water` | none if they already said water / tea / coffee / juice |
| `help` | always offer to call the named caretaker first |
| `rest` | none |
| `story` / `talk` | none; `companion_for` tells a story or a kind remark |

`detail` is a *variety of the need* only for food and water
(`DETAIL_REFINABLE_TAGS`). For help it is empty unless the drawing is a
telephone (`call`). A letter, a cross, or a hand is ink, not a kind of help.

A yes on `call` / `caretaker` does **not** open `tel:` on the pad. The pad
says help is on the way. The caretaker app gets `kind=emergency`
(high-priority channel, alarm, full-screen HELP).

Memory is written after the round. The LLM graph patch runs in the
background so the pad never waits on it.

### 4.6 Shape game

Started only from a geometric 1 or 2, after they tap yes on
"Play a game?".

- Digit 1: circle → square → triangle
- Digit 2: square → triangle → circle
- Three matches end the game. Three misses on one shape skip to the next.
- Grading is `stroke_geometry.grade` (corner count + circularity). Gemini
  is not required to win.

---

## 5. Feature templates

Templates do two jobs: they can break a Gemini near-tie, and they are the
last fallback when every vision model is down.

### 5.1 How a drawing becomes a descriptor

`drawing_features.build_descriptors` turns raw pad strokes into a fixed
vector. Incoming formats accepted: capture `.json` stroke dicts, flat
`points`, nested polylines, `{x,y}` objects.

1. **Parse** to `[[(x, y), ...], ...]` (`parse_strokes`).
2. **Normalize** into the unit square, letterboxed so aspect ratio is kept
   (`normalize_strokes`).
3. **Resample** to `point_count` points (default 100, allowed 100–500).
   Points are allocated across strokes by path length
   (`allocate_counts` / `resample_polyline`).
4. **Describe**:

| Field | What it is |
| --- | --- |
| `resampled_points` | 100 `(x, y)` samples, the shape itself |
| `resampled_strokes` | same samples still grouped by stroke |
| `direction_histogram` | 8-bin `atan2` histogram of consecutive steps |
| `occupancy_grid` | 8×8 density of the normalized points |
| `stroke_count` | number of parsed strokes |
| `stroke_lengths` | length of each resampled stroke |
| `aspect_ratio` | raw bounding-box width / height |
| `path_length` | length of the flattened resampled path |
| `mean_curvature` | mean turning angle along the path |
| `start`, `end` | first and last resampled points |

Tremor is why these features do not use raw arc length as a shape score.
Shake adds length without moving the gesture. Comparison uses the
normalized, resampled points and histograms, not perimeter.

### 5.2 Comparison score

`compare_descriptors` returns a weighted mix:

| Term | Weight | Metric |
| --- | --- | --- |
| shape | 0.50 | `1 - min(RMSE_forward, RMSE_reversed) / √2` |
| direction | 0.20 | histogram intersection |
| occupancy | 0.20 | `1 - L2(grids) / √2` |
| strokes | 0.10 | `1 - |n1 − n2| / max(n1, n2)` |

Reversing the point order lets a stroke drawn the other way still match.
`DrawingFeatureStore.compare` / `score_all` ranks every stored drawing by
this score.

### 5.3 The store

`drawings_db.json` is a `DrawingFeatureStore`:

```json
{
  "point_count": 100,
  "drawings": {
    "apple1": {
      "id": "apple1",
      "label": "apple",
      "metadata": {
        "origin": "prior",
        "tag_id": "food",
        "detail": "apple"
      },
      "descriptors": { "...": "see §5.1" }
    }
  }
}
```

`drawing_tags.json` links tags to those ids (`drawing_ids`) and to aliases
(`apple`, `pizza`, `call`, `phone`, `music`, …).
`feature_score_for_tag` takes the best match whose id, label, or
`metadata.tag_id` belongs to the tag. Drawings with `tag_id == "_digit"`
are skipped when scoring spoken tags.

### 5.4 How templates are seeded

`scripts/seed_drawings.py` fills the store, in order of trust:

1. **`data/priors/`** — this person's repeated drawings, named
   `apple1`, `call3`, … Prefix map:

   | Prefix | Tag | Detail |
   | --- | --- | --- |
   | `apple` | `food` | `apple` |
   | `pizza` | `food` | `pizza` |
   | `water` | `water` | (empty) |
   | `call` | `help` | `call` |
   | `music` | `story` | `music` |
   | `one` | `_digit` | `1` |
   | `two` | `_digit` | `2` |

2. **`eval/catalog.json`** — only for a tag the priors do not cover.
3. **Synthetic outlines** — `bed` (rest), `check` (yes), `xmark` (no).
   Cartoon `cup` / `bowl` / `cross` are skipped when priors already cover
   water / food / help.

Re-running updates existing ids and deletes leftovers. Tag `drawing_ids`
are rewritten to the prior set.

Current store (after seed): apple×7, pizza×6, water×6, call×10, music×6,
one×8, two×6, plus synthetic `bed`, `check`, `xmark`.

Hold-one-out of the priors (`scripts/priors_test.py`) is a tag-level
nearest-neighbor check with a 60% spoken-tag floor. Digit priors are
stored for comparison but are not a spoken tag and do not start the game.

### 5.5 How templates affect a live rank

Feature compare **always** runs. Mixing into Gemini is conservative:

```text
FEATURE_BLEND_MIN    = 0.6     must be this close to a stored drawing
FEATURE_BLEND_WEIGHT = 0.3     blend only that one candidate
```

Blend only when **exactly one** Gemini candidate has
`feature_score >= 0.6`:

```text
final_weight = 0.7 × likelihood  +  0.3 × feature_score
```

Two strong matches (apple vs cup, both round) are ignored. A 0.95 food
rank still beats a cup template. Templates cannot invent a tag Gemini
omitted.

When Gemini and local vision both fail, `_from_features` maps the best
matches onto tags:

```text
final_weight = RANK_WEIGHTS[rank] × feature_score
RANK_WEIGHTS = (1.0, 0.8, 0.6, 0.4, 0.2)
```

`_digit` and `yes` / `no` are skipped. Detail comes from metadata, but
`1` / `2` / the tag id itself are dropped.

---

## 6. Geometry (no model)

`stroke_geometry.py` is deterministic and uses CSS-pixel strokes.

### 6.1 Digit detection

A stroke shorter than 45% of the longest is decoration (flag, serif, rest
dot). A main stroke shorter than 40 px is a speck.

**1** — one tall upright near-straight stroke (elongation ≥ 2, tilt ≤ 30°,
straightness ≥ 0.78). An optional short bar underneath may be a base
serif, not a cross.

**2** — one open curve from the top down to a left-to-right baseline
(not closed, not too straight, turning between 120° and 400°).

**Veto** (the drawing cannot be a character): ends closer than 18% of the
bounding-box diagonal (closed loop: cup, apple), or more than three
substantial strokes. A veto discards a Gemini-claimed digit *and* that
ranking, because the model was answering the digit question.

| Confidence | Effect |
| --- | --- |
| ≥ 0.85 (`FAST_PATH`) | skip Gemini; game may start |
| ≥ 0.60 (`ASSIST`) | fill in a digit Gemini left empty |
| veto | drop a claimed digit |

Never measure a stroke by raw arc length here. Tremor inflates it.
Measurements use a fitted axis or a coarsely resampled copy.

### 6.2 Shape grading

`classify_shape` / `grade` compare a drawing to circle, square, or
triangle on corner count and circularity after resampling to 64 points.

| Shape | Ideal corners | Ideal circularity |
| --- | --- | --- |
| circle | 0 | 1.00 |
| square | 4 | 0.785 |
| triangle | 3 | 0.605 |

Minimum score 0.45. Used by the game when Gemini is down, and by the
live game path as well.

---

## 7. Model prompts and harness

### 7.1 Ranking (Gemini / Grok)

`GeminiPatientModel.rank_drawing_tags` sends the PNG plus a short mapping:

- apple / pizza / sandwich / bowl → `food`
- cup / glass / bottle / tap → `water`
- cross / plus / handwritten H / telephone → `help`
- bed / pillow → `rest`
- scenery / book / animal → `story`
- smile / face / heart → `talk`
- handwritten 1 or 2 → `digit` field only; an apple or cup is not a digit

Spoken for food/water is first person and may name the object. Spoken for
help is exactly **"I need help, please."** Help `detail` is empty unless
the drawing is a telephone (`call`). A single letter is never a detail.

Returned JSON: rankings with `tag_id`, `likelihood`, `spoken`, `detail`,
plus top-level `spoken`, `seen`, `digit`. `_usable_detail` strips letters,
tag initials, and help details other than `call` / `caretaker`.

A ranking under `MIN_USABLE_LIKELIHOOD` (0.25) is treated as no answer.
Real readings land at 0.8+; 0.05 is what arrives when the model answered
a different question.

History graphs are a weak tie-breaker only. `yes` / `no` are excluded
from the catalog.

### 7.2 Local vision

`local_vision.py` does **not** reuse the production prompt. A 7B–12B
model given 4.5 KB of patient context stops looking at the image. The
local prompt is the tag list and the picture. Answers carry
`LOCAL_LIKELIHOOD` (0.4) and `source="local"`.

`usable_sentence` rejects placeholder echoes (`"I would like ..., please."`).
A rejected line falls back to the curated `PHRASES` entry.

Default model `gemma3:12b` on Ollama (`keep_alive: -1`, the number, not
the string). Warmed in the background when the recogniser is first built
(~27 s VRAM load).

### 7.3 Spoken-line hygiene

`pipeline._spoken_text` uses the model sentence when it is clean. For
`help`, a line that says "with …" or names a standalone letter is replaced
with `PHRASES["help"]`. That is what stopped **"I need help with h"**
after an H drawing: Gemini had put `detail: "h"`, and
`already_named` used to be a substring test (`"h" in "help"`), which
skipped the caretaker call.

`already_named` is now a word-boundary match and ignores tokens shorter
than two characters. `is_specific` rejects a single letter and the tag
initial.

### 7.4 Harness

`scripts/recognize_test.py` does not call Gemini. A `FakeModel` supplies
ranks so the suite can assert:

- a 0.95 food rank still beats a blended water template
- a unique strong prior can break a 0.55 / 0.50 near-tie
- templates cannot invent a tag Gemini omitted
- API failure and timeout use `drawings_db.json`
- `"h"` is not already named inside "I need help, please."
- an H drawing still offers to call the caretaker
- help never closes with "I'll help with the h"
- ranking drops letter / hand help details; `call` is kept
- an open apple is not turned into a 2
- a closed loop vetoes a claimed digit
- a 0.05 ranking falls through

Other tests: `priors_test.py`, `digit_test.py`, `model_chain_test.py`,
`local_vision_test.py`, `xai_chain_test.py`, plus the capture / tap /
speech / caretaker / emergency browser tests in `scripts/`.

---

## 8. Speech

`speech.synthesise(text)` runs on the laptop. The API key never leaves
this machine. MP3s are cached under `data/tts/` by
`(model, voice, text)`. A missing key, a dead network, or a refused
request returns `None`; the tablet then uses `speechSynthesis`. The
function never raises.

Default model `eleven_flash_v2_5`. The canvas shows "Powered by
ElevenLabs" only when their audio actually played.

---

## 9. Memory

Two layers, both local.

**Text.** `prompt.txt`, `patient_data.json`, `compressed_history.txt`,
and `monthly_events/YYYY-MM/YYYY-MM-DD/daily_history.txt`.

**Graph.** `memory_graph.json` via `memory_graph.py`. Node types:
person, intent, drawing, interest, preference, day, event, theme.
`/brain` shows two scopes: **Today** and **History**. After a confirmed
round, `confirm_memory` writes the live graph; `update_graphs_with_llm`
patches it later with `GEMINI_SLOW_TIMEOUT_S` (or Grok `XAI_SLOW_MODEL`,
`grok-4.6`).

`/brain` listens on the viewer WebSocket for `graph` updates. It does
not auto-seed demo data on GET.

---

## 10. Caretaker

Native app in `android/` (`org.hophacks.inkcaretaker`).

| Tab | Source |
| --- | --- |
| Alerts | `/api/caretaker/events` + live WS events (5 at a time, Load more) |
| Brain | `/brain?embed=1` |
| Today | `/api/caretaker/stats` — confirmed request counts for the day |

`HubService` is a foreground service: WebSocket `/ws?role=caretaker`,
25 s ping, reconnect with backoff. Help is a separate
`IMPORTANCE_HIGH` channel with alarm + `EmergencyActivity`. Ordinary
drawing alerts do not use that channel.

`CLIENT_VERSION` (canvas + `app.py`) and `CARETAKER_VERSION` (web
fallback + `app.py`) must be bumped together on frontend changes. The
native app does not use the version check.

Build: `scripts/build_caretaker_apk.py` →
`android/app/build/outputs/apk/debug/app-debug.apk`.

---

## 11. Configuration (recognition-related)

| Variable | Default | Role |
| --- | --- | --- |
| `INK_RECOGNITION` | `1` | `0` skips ranking, speaks `SAMPLE_TEXT` |
| `GEMINI_TIMEOUT_S` | `15` | whole Gemini+xAI capture budget |
| `GEMINI_MODELS` | lite, then 3.6-flash | tried in order |
| `GEMINI_SLOW_TIMEOUT_S` | `120` | history compress + graph patch |
| `XAI_API_KEY` | unset | Grok after a fast Gemini refusal |
| `XAI_MODEL` | `grok-4.20-0309-non-reasoning` | live path |
| `XAI_SLOW_MODEL` | `grok-4.6` | background only (42–86 s) |
| `LOCAL_VISION` | `1` | `0` goes straight to templates |
| `LOCAL_VISION_MODEL` | `gemma3:12b` | Ollama |
| `LOCAL_VISION_TIMEOUT_S` | `25` | separate from Gemini's budget |
| `INK_SMOOTHING` | `medium` | One Euro: off / light / medium / strong |
| `INK_CONFIRM_TIMEOUT_S` | `60` | how long a question waits for a tap |

`.env` is gitignored. Real environment variables win over it.

The API refuses a per-request deadline under 10 s. At the default 15 s
the primary gets the full budget; a quick 400/429 still leaves room for
the next model. Raise past 20 s if a hang must be survivable.

---

## 12. Persistence

| Path | Contents |
| --- | --- |
| `data/captures/<id>.png` | flattened drawing, white background |
| `data/captures/<id>.json` | strokes with `points` and `raw` |
| `data/index.jsonl` | one `CaptureRecord` per line; `analysis` filled after recognition |
| `data/answers.jsonl` | one tap per line, with capture id and spoken text in `context` |
| `data/tts/<hash>.mp3` | cached speech |
| `drawings_db.json` | feature templates |
| `drawing_tags.json` | tag catalog |
| `memory_graph.json` | second brain |

The key `strokes` is a list of stroke dicts inside a capture `.json`, and
a filename string inside `index.jsonl`.

`CaptureStore.sinks` is the unused seam for a Postgres / Tiger Data
writer. `INK_DATABASE_URL` exists; nothing is wired.

---

## 13. Design constraints that the code encodes

- The pad must speak inside ~15 s. Accuracy that takes 40+ s (Grok 4.6
  on the live path) is reserved for work after the pad has spoken.
- A small local model given patient history ignores the image. Local
  vision gets tags + PNG only.
- Feature blend is unique-strong only. Hold-one-out of the priors is not
  perfect; two round objects must not fight.
- Digit templates must not start the game. Geometry is the only starter.
- Help is a call for a person, not a label of the ink. H / plus / cross
  confirm as help and then offer to call the caretaker.
- Speech and analysis exceptions are swallowed. Going silent or taking
  the server down is worse than a weaker guess.
- Raw tremor samples stay on disk. Clinical interest may be the shake,
  not the smoothed line.
