# Intent recognition and memory

The tablet capture path (step 1) is the eyes-free input. This layer turns a
drawing into a small set of likely intents and keeps a running memory of the
person and the day’s interactions.

A handwritten **1** or **2** is not a need: the pad offers a short drawing
game (circle, then square, then triangle). Misses get a spoken nudge to retry;
three misses skip to the next shape. Three matches ends the game.

## How a drawing becomes an answer

```text
finger strokes on the pad
        |
        v
PNG + strokes
        |
        +--> Gemini ranks tags from the drawing  -->  likelihood + spoken sentence
        |         (drawing first; history is a weak prior)
        |
        +--> local templates (drawings_db.json)  -->  only if Gemini fails or times out
                    |
                    v
              spoken on the pad, then a yes/no tap
                    |
                    v
         on yes: today's note + live graph update
```

1. Gemini reads the PNG and `drawing_tags.json` (`yes` / `no` are excluded; those are taps). History graphs are a weak tie-breaker only.
2. The spoken sentence may name the object (`I would like an apple, please.`). If Gemini already named a specific, follow-ups are skipped.
3. Local feature compare against `drawings_db.json` always runs so it is ready, but those scores are **not** multiplied into Gemini's ranks.
4. If the API faults or exceeds `GEMINI_TIMEOUT_S` (default 15s): `final_weight = rank_weight × feature_score` from the templates.
5. A **no** tap retries the next Gemini candidate (up to three guesses), still looking at the PNG. After a **yes**, follow-ups only run when the request is still generic. Confirmed water does not ask tea. Help offers to call the named caretaker. A caregiver closing is spoken next (`I'll get you some water.` / `Rest easy.`). Memory is written after that round; the LLM graph patch runs in the background.

Tablet stroke files (`[x, y, pressure, ms]`) are accepted; only `x` and `y` are used.

## Memory layer

Text history still exists. On top of it, every interaction updates a local
knowledge graph (`memory_graph.json`) that you can open at `/brain`.

| File / view | Role |
| --- | --- |
| `prompt.txt` | Main system prompt |
| `patient_data.json` | Stable patient details |
| `compressed_history.txt` | Running summary for the whole run |
| `monthly_events/YYYY-MM/YYYY-MM-DD/daily_history.txt` | That day’s notes |
| `memory_graph.json` | Second-brain nodes and weighted links |
| `http://localhost:8000/brain` | Force-directed graph for judges |

Nodes are the person, intents, drawings, days, events, and interests. There are
two brains: **Today** (that day's events and how they connect) and **History**
(the running graph). Scroll to zoom, drag to move. After each interaction Gemini
can add nodes and links; those updated graphs are fed into the next prompt.

```powershell
.\.venv\Scripts\python.exe memory_graph.py seed --demo
.\.venv\Scripts\python.exe run.py
# then open http://localhost:8000/brain
```

## Scripts

| Script | Role |
| --- | --- |
| `recognize.py` | Gemini ranking; local templates only on timeout/fail |
| `scripts/recognize_test.py` | Ranking harness (no API): Gemini wins, timeout fallback |
| `scripts/eval_benchmark.py` | Label pad drawings into `eval/` and score ranking for judges |
| `drawing_features.py` | Store and compare stroke templates |
| `drawing_tags.py` | Add or update the living tag list |
| `gemini_session.py` | Chat, daily history, end-of-day compression |
| `test_gemini_image.py` | Image + prompt smoke test |

```powershell
.\.venv\Scripts\python.exe recognize.py demo --offline
.\.venv\Scripts\python.exe recognize.py interpret examples/sample_strokes.json --image examples/test2_water.png
.\.venv\Scripts\python.exe drawing_features.py add cup strokes.json --label cup
.\.venv\Scripts\python.exe drawing_tags.py add bathroom --label bathroom --drawings toilet
.\.venv\Scripts\python.exe gemini_session.py chat "I am thirsty"
.\.venv\Scripts\python.exe gemini_session.py end-of-day
.\.venv\Scripts\python.exe scripts\eval_benchmark.py collect
.\.venv\Scripts\python.exe scripts\eval_benchmark.py run
```

The tablet capture path calls this from `server/pipeline.py`. Each saved drawing
is passed to `IntentRecognizer().interpret(...)`; the top tag becomes the
sentence spoken on the phone.

## Python

```python
from recognize import IntentRecognizer

recognizer = IntentRecognizer()
result = recognizer.interpret(strokes, image="data/captures/....png")
print(result.top_tag, result.fallback_used)
```
