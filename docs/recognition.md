# Intent recognition and memory

The tablet capture path (step 1) is the eyes-free input. This layer turns a
drawing into a small set of likely intents and keeps a running memory of the
person and the day’s interactions.

This is a prototype, not a medical device. It does not diagnose or treat anyone.

## How a drawing becomes an answer

```text
finger strokes on the pad
        |
        v
resample to a fixed point count (default 100)
        |
        +--> offline feature compare  -->  feature_score for each template
        |
        +--> Gemini ranks tags        -->  top 5 tags + likelihood
                    |                     (skipped if API fails / times out)
                    v
        rank_weight x likelihood x feature_score
                    |
                    v
              top 5 final weights
                    |
                    v
         memory layer writes today's note
```

1. Incoming stroke points can be any length. They are resampled to **100 points** (configurable 100–500).
2. Those points become a JSON descriptor and are compared to `drawings_db.json`. This path is local and is the **fallback**.
3. Gemini reads `drawing_tags.json`, optional PNG, patient files, and the offline matches, then ranks the **top 5 tags**.
4. Ranks 1–5 get weights `(1.0, 0.8, 0.6, 0.4, 0.2)`.
5. `final_weight = rank_weight × llm_likelihood × feature_score`
6. On API failure: `final_weight = rank_weight × feature_score`

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
| `recognize.py` | Fuse features + tag ranking |
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
```

## Python

```python
from recognize import IntentRecognizer

recognizer = IntentRecognizer()
result = recognizer.interpret(strokes, image="data/captures/....png")
print(result.top_tag, result.fallback_used)
```
