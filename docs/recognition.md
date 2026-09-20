# Intent recognition — operator notes

How to run, seed, and test the ranking path. The architecture, control flow,
feature weights, and prompt rules are in
[technical_report.md](../technical_report.md). How to start the pad is in
[readME.md](../readME.md).

A handwritten **1** or **2** is not a need: the pad offers a short drawing
game (circle, then square, then triangle). Misses get a spoken nudge; three
misses skip to the next shape. Three matches ends the game. Only geometry may
start that game.

## What happens to a capture

```text
finger strokes on the pad
        |
        v
PNG + strokes
        |
        +--> geometry: clear 1 / 2 skips the models
        |
        +--> Gemini ranks tags from the drawing  -->  likelihood + spoken
        |         (drawing first; history is a weak prior)
        |
        +--> xAI Grok (xai_backend.py)  -->  if every Gemini model fails fast
        |
        +--> local vision (local_vision.py)  -->  if xAI fails too
        |
        +--> templates (drawings_db.json)  -->  last resort
                    |
                    v
              spoken on the pad, then a yes/no tap
                    |
                    v
         on yes: follow-ups if still generic, then closing + memory
```

1. Gemini reads the PNG and `drawing_tags.json` (`yes` / `no` are taps).
2. Food or water may name the object (`I would like an apple, please.`). Help
   is always `I need help, please.`; `detail` stays empty unless the drawing
   is a telephone (`call`). A letter H is help, not a kind of help.
3. Feature compare against `drawings_db.json` always runs. A match of 0.6 or
   better is mixed into Gemini's rank (30%) only when exactly one candidate is
   that close. Templates cannot invent a tag Gemini omitted.
4. Grok uses the same prompt and schema, from inside `_generate`. Only a fast
   Gemini refusal leaves budget. `XAI_MODEL` is the fast non-reasoning model;
   `XAI_SLOW_MODEL` (`grok-4.6`) is for work after the pad has spoken.
5. Local vision is image + tag list only. Its guess carries `LOCAL_LIKELIHOOD`
   and `source="local"`.
6. Templates are seeded from `data/priors/` by `scripts/seed_drawings.py`.
   Digit priors stay in the store but do not start the game.
7. A **no** retries the next candidate (up to three). After a **yes**,
   follow-ups stay on the confirmed intent. Help offers to call the named
   caretaker. Then a caregiver closing. The LLM graph patch runs later.

Tablet stroke files (`[x, y, pressure, ms]`) are accepted; only `x` and `y`
are used for features and geometry.

## Memory

| File / view | Role |
| --- | --- |
| `prompt.txt` | Main system prompt |
| `patient_data.json` | Stable patient details, including caretaker name |
| `compressed_history.txt` | Running summary |
| `monthly_events/YYYY-MM/YYYY-MM-DD/daily_history.txt` | That day's notes |
| `memory_graph.json` | Second-brain nodes and weighted links |
| `http://localhost:8000/brain` | Force-directed graph |

```bash
.venv/bin/python memory_graph.py seed --demo
.venv/bin/python run.py
# then open http://localhost:8000/brain
```

## Local vision (optional)

When every Gemini model has refused, `local_vision.py` sends the PNG to
[Ollama](https://ollama.com). Install without root and pull the model:

```bash
curl -fL https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst \
  -o /tmp/ollama.tar.zst
tar --zstd -xf /tmp/ollama.tar.zst -C ~/.local
~/.local/bin/ollama serve &
~/.local/bin/ollama pull gemma3:12b
.venv/bin/python local_vision.py check
```

`gemma3:12b` needs about 8 GB of VRAM. With no GPU, set `LOCAL_VISION=0`.
Do not enrich `local_vision.build_prompt` without re-measuring against
`eval/`; two cleaner rewrites both lost accuracy.

## Seed the templates

```bash
.venv/bin/python scripts/seed_drawings.py
.venv/bin/python scripts/priors_test.py
```

Sources, in order: `data/priors/` (this person's repeats), then
`eval/catalog.json` for uncovered tags, then synthetic `bed` / `check` /
`xmark`. Digit files (`one*`, `two*`) are stored as `_digit` and skipped
when scoring spoken tags.

## Scripts

| Script | Role |
| --- | --- |
| `recognize.py` | Gemini ranking, prior blend on near-ties, templates last |
| `scripts/recognize_test.py` | Ranking harness (no API) |
| `scripts/priors_test.py` | Hold-one-out of `data/priors/` |
| `scripts/seed_drawings.py` | Fill `drawings_db.json` |
| `scripts/digit_test.py` | Geometric 1 / 2 and shape grading |
| `stroke_geometry.py` | Digit and game-shape geometry |
| `scripts/eval_benchmark.py` | Label pad drawings and score ranking |
| `drawing_features.py` | Store and compare stroke templates |
| `drawing_tags.py` | Add or update the living tag list |
| `gemini_session.py` | Chat, daily history, end-of-day compression |

```bash
.venv/bin/python recognize.py demo --offline
.venv/bin/python recognize.py interpret examples/sample_strokes.json --image examples/test2_water.png
.venv/bin/python drawing_features.py add cup strokes.json --label cup
.venv/bin/python drawing_tags.py add bathroom --label bathroom --drawings toilet
.venv/bin/python gemini_session.py chat "I am thirsty"
.venv/bin/python gemini_session.py end-of-day
.venv/bin/python local_vision.py check
.venv/bin/python xai_backend.py check
.venv/bin/python scripts/seed_drawings.py
.venv/bin/python scripts/priors_test.py
.venv/bin/python scripts/eval_benchmark.py collect
.venv/bin/python scripts/eval_benchmark.py run
```

## From Python

```python
from recognize import IntentRecognizer

recognizer = IntentRecognizer()
result = recognizer.interpret(strokes, image="data/captures/....png")
print(result.top_tag, result.fallback_used, result.digit_source)
```

The tablet capture path calls this from `server/pipeline.py`.
