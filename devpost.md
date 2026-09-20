# Devpost — Sillow

Paste each section into the matching Devpost field. Keep the tagline and “Built with” as written; the rest can be trimmed if a field has a character cap.

---

## Project name

**Sillow - sensing pillow**

## Tagline

A sensing pillow: rest a finger, draw what you need, hear it spoken, and someone is already on the way.

## Elevator (about 50 words)

Sillow is a sensing pillow — an eyes-free surface for people who cannot speak easily and may have tremor. Draw a cup, an apple, an H, or a garden. The tablet speaks a short sentence, you tap yes or no, and a caregiver phone sees every confirmed request. Help never opens a dialer on the pad — it alerts the person who can actually help.

---

## Inspiration

After a stroke, language and a steady hand can both go. Typical AAC apps still ask you to hunt through grids, type, or look at the screen. That is a lot to ask of someone who is tired, shaky, and already struggling to be understood.

We wanted the opposite of a form: something as undemanding as a pillow. Rest a hand. Draw the thing. Hear it spoken back. Tap once for yes. The caregiver should not be guessing from another room, and an emergency should not yank the person off the surface into a phone dialer.

Sillow is deliberately small: water, food, help, rest, a short story, a bit of company, or a drawing game. That constraint is the product. A general chatbot would talk past the person drawing.

---

## What it does

Three devices on the same Wi-Fi. Nothing is exposed to the internet.

**The sensing pillow (tablet).** Draw with one finger. A One Euro filter calms tremor without throwing away the raw shake (that signal is stored). After five seconds of stillness the canvas captures itself, wipes, and you can keep going. The laptop reads the drawing and the tablet speaks a first-person line: *I would like an apple, please.* One tap is yes, two taps is no. A no retries the next guess. After a yes, the pad only asks what still matters (call Jordan? soup?) and then speaks a caregiver closing.

**What a drawing means**

| You draw | The pad understands |
| --- | --- |
| Cup, glass, bottle | Water |
| Apple, pizza, bowl | Food (and names it when it can) |
| H, plus, cross, telephone | Help — then offers to call the named caretaker |
| Bed | Rest |
| Garden, mountains, music | A short story about what you drew |
| Smile, face, heart | Company |
| A clear handwritten 1 or 2 | A circle / square / triangle game |

Help is a person, not a label of the ink. A letter H is help, not “help with h.” Confirming it alerts the caretaker app with a high-priority alarm and a full-screen HELP screen. The pad stays on the canvas and says help is on the way.

**The caretaker phone.** A native Android app (Alerts, Brain, Today) plus a web fallback. Live drawings, spoken lines, yes/no, daily request counts, and the memory graph. Keep the app running; a foreground service holds the socket.

**The second brain.** Every confirmed request updates a local knowledge graph: people, intents, drawings, days, themes. `/brain` shows Today and History. Judges can load a sample two-week life for Alex — gardening, Saturday pizza, Jordan the daughter, a telephone call for help — so the graph is vivid before any live captures.

---

## How we built it

**Capture.** FastAPI + WebSockets. The tablet is a full-screen canvas. Strokes are `[x, y, pressure, time]` in CSS pixels; the PNG is what vision models see. Recognition runs *after* the upload is answered, so a slow model cannot time out the pad.

**Recognition, in order, until something can speak.**

1. Geometry (`stroke_geometry.py`) — a tall upright stroke is a 1, an open curve to a baseline is a 2. A clear one starts the game with no API call. Tremor is measured against a fitted axis, never raw arc length (shake adds length without going anywhere).
2. Gemini ranks tags from the PNG. History is a weak prior only. `gemini-3.5-flash-lite` then `gemini-3.6-flash` share one 15s budget.
3. xAI Grok, same prompt, only if Gemini refuses fast (quota, 404). The accurate reasoning model is too slow for the pad, so it is reserved for work after the person has already heard the answer.
4. A vision model on the laptop (Ollama, `gemma3:12b`) with a *short* prompt: tags + image. The full patient context drowns out the drawing at this size.
5. Stroke templates in `drawings_db.json`, seeded from this person’s repeated drawings (`data/priors/`: apples, cups, phones, …). A unique strong match can break a Gemini near-tie. Templates cannot invent a tag the model omitted. Digit templates never start the game — an open apple is too close to a stored 2.

**Conversation.** Up to three spoken guesses. Follow-ups stay on the confirmed need. Speech is ElevenLabs on the laptop so the API key never leaves the machine; clips stay in memory and the tablet gets a local URL. No key? The device voice still talks.

**Caretaker.** Kotlin app: Alerts, embedded Brain, daily stats, emergency channel. Built as a debug APK from this repo.

**Stack.** Python, FastAPI, Gemini, xAI Grok, Ollama, ElevenLabs, Kotlin, vanilla JS canvases, vis.js-style force graph, One Euro filter.

---

## Challenges we ran into

**The model names the ink, not the need.** An H drawing came back as “I need help with h” because `detail: "h"` sat inside the word “help,” which skipped the caretaker call. We now treat letters as shapes, not varieties of help.

**An apple is not a 2.** Stored digit templates scored ~0.75 against an open apple and offered the drawing game instead of food. The game starts only from geometry, and only when there is no care-need ranking beside it.

**Quota is a demo killer.** One flash model allows 20 free requests a day. We lead with the lite model on availability, walk a chain, then Grok, then a local model, then templates. The pad still speaks if the cloud is gone.

**Small local models ignore the picture.** Handed 4.5 KB of patient history, a 12B model answered “apple” for a cup, a cross, and a pizza. The local prompt is now only the tag list and the PNG.

**Tremor lies to geometry.** Arc length over distance calls a shaky line bent. We fit an axis or resample first. The One Euro `beta` is much smaller than textbook values, because tremor is fast and a large beta lets the shake through.

**The pad has 15 seconds.** `grok-4.6` read our eval drawings 3/3 — in 42–86 seconds. It cannot own the live path. Latency chose the model, not the leaderboard.

---

## Accomplishments that we're proud of

- A full loop you can demo without staring at the tablet: draw, hear, tap, caregiver alert.
- Help that pages a real phone with an alarm, instead of `tel:` on the patient’s screen.
- Recognition that degrades in public: cloud → second vendor → on-machine vision → this person’s own drawings.
- A memory graph that is a product, not a screenshot — Today vs History, drawing thumbnails, a seeded lifetime for judges.
- Company as well as care: a landscape gets a story, not “I would like to lie down.”
- Stroke files keep the raw tremor. The smoothed line is for talking; the shake may be the clinical signal later.

---

## What we learned

Eyes-free UI is a timing problem as much as a vision problem. If the pad is silent for 15 seconds, the person thinks it did not see them.

Prompting a drawing model to “name the object” will name the letter H. Constraints have to live in code, not just in the prompt.

Fallbacks are not all equal. A template that has never seen this person’s apple is worse than a vague local model that at least looked at the PNG — unless the template *is* their apple.

Judges will draw something you did not seed. The system has to say “Could you draw that again?” rather than invent a need.

---

## What's next

- A real caregiver number and on-call routing, still never dialed from the pad.
- More of this person’s priors, less cartoon geometry.
- A Postgres/Tiger Data sink behind the capture store (the seam is there; nothing is wired).
- Clinical review of the raw tremor traces.
- A calmer first-run: the first stroke after a page load still has no sound or vibration, because browsers require a prior gesture. We lived with that. Unlocking it properly would help.

---

## Built with

`python` `fastapi` `javascript` `kotlin` `android` `google-gemini` `xai` `ollama` `elevenlabs` `websockets` `computer-vision`

Optional extras if the form allows more: `accessibility` `aac` `one-euro-filter` `knowledge-graph`

---

## Links

| Field | Value |
| --- | --- |
| GitHub | https://github.com/Manikrishneshwar/Hophacks |
| Try it | Same Wi-Fi as the laptop: run `python run.py`, scan the QR code with the tablet. Sideload `android/app/build/outputs/apk/debug/app-debug.apk` on the caregiver phone. |
| Demo patient | Alex Rivera (fictional). Caretaker: Jordan Chen, daughter. |

---

## Suggested video script (~90 seconds)

1. **0:00** Tablet, one finger, no on-screen keyboard. “This is Sillow, a sensing pillow. You draw what you need.”
2. **0:10** Draw a cup. Idle capture. Pad: *I would like a glass of water, please.* One tap. Closing: *I'll get you some water.*
3. **0:25** Draw an apple. Pad names it. Caretaker phone shows the PNG and YES.
4. **0:40** Draw an H. Pad: *I need help, please.* Tap yes. *Should I call Jordan?* Tap yes. Caretaker alarm + full-screen HELP. Pad stays on the canvas.
5. **0:55** Draw hills. Pad offers a story, then tells one.
6. **1:10** `/brain` — Today and History, Jordan linked to help, garden themes.
7. **1:25** “If the cloud is down, it still speaks. The key never leaves the laptop.”

---

## Image captions (for Devpost gallery)

1. The pad: empty canvas, countdown, one-finger drawing.
2. Spoken line on screen: *I would like an apple, please.* plus yes/no prompt.
3. Caretaker Alerts tab with drawing thumbnail and YES.
4. Emergency HELP activity on the caregiver phone.
5. Memory graph: Alex, Jordan, water, garden, Saturday pizza.
6. (Optional) Laptop terminal: QR codes for tablet and caretaker.

---

## One-line for social / HopHacks table card

Sillow — a sensing pillow. Draw a cup, hear “I would like some water,” tap yes. Help calls Jordan, not the phone dialer.
