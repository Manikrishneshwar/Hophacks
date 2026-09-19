"""Check that Gemini ranking is used as-is, and templates only kick in on failure.

    python scripts/recognize_test.py

Does not call the Gemini API. A fake model supplies ranks so we can assert
that a high cup/template score cannot overturn a food ranking, that yes/no
are not sent to the model, and that a timeout or exception falls back to
drawings_db.json.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drawing_features import DrawingFeatureStore  # noqa: E402
from recognize import IntentRecognizer, seed_example_templates  # noqa: E402
from server import pipeline  # noqa: E402


class FakeModel:
    def __init__(self, rankings, spoken="", delay=0.0, error=None, digit=""):
        self.rankings = rankings
        self.spoken = spoken
        self.delay = delay
        self.error = error
        self.digit = digit
        self.received_tags: list[str] | None = None

    def rank_drawing_tags(self, tags, *, image=None, top_k=5, **_kwargs):
        self.received_tags = [tag["id"] for tag in tags]
        if self.error is not None:
            raise self.error
        if self.delay:
            time.sleep(self.delay)
        return {
            "rankings": self.rankings,
            "spoken": self.spoken,
            "seen": "an apple",
            "digit": self.digit,
        }


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    raise SystemExit(1)


def make_recognizer(tmp: Path, model: FakeModel) -> IntentRecognizer:
    tags = tmp / "drawing_tags.json"
    shutil.copy(ROOT / "drawing_tags.json", tags)
    db = tmp / "drawings_db.json"
    features = DrawingFeatureStore(db)
    seed_example_templates(features)
    return IntentRecognizer(
        root=tmp,
        drawings_db="drawings_db.json",
        tags_path="drawing_tags.json",
        model=model,
    )


def circle_strokes():
    import math

    return [
        [
            [20 * math.cos(2 * math.pi * i / 90), 20 * math.sin(2 * math.pi * i / 90)]
            for i in range(90)
        ]
    ]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ink-rank-"))
    query = circle_strokes()

    fake = FakeModel(
        rankings=[
            {
                "tag_id": "food",
                "likelihood": 0.95,
                "reason": "apple",
                "spoken": "I would like an apple, please.",
                "detail": "apple",
            },
            {
                "tag_id": "water",
                "likelihood": 0.40,
                "reason": "round",
                "spoken": "I would like a drink, please.",
                "detail": "",
            },
        ],
        spoken="I would like an apple, please.",
    )
    recognizer = make_recognizer(tmp, fake)
    result = recognizer.interpret(query, update_memory=False)

    if fake.received_tags is None:
        fail("Gemini was not asked to rank")
    if "yes" in fake.received_tags or "no" in fake.received_tags:
        fail(f"yes/no were sent to ranking: {fake.received_tags}")
    if result.fallback_used:
        fail("Gemini ranking was treated as a fallback")
    if result.top_tag != "food":
        fail(f"expected food from Gemini, got {result.top_tag!r}")
    if result.spoken != "I would like an apple, please.":
        fail(f"spoken line was {result.spoken!r}")
    if result.candidates[0].detail != "apple":
        fail(f"detail was {result.candidates[0].detail!r}")
    if result.candidates[0].source != "gemini":
        fail(f"source was {result.candidates[0].source!r}")
    water = next(item for item in result.candidates if item.tag_id == "water")
    if result.candidates[0].final_weight <= water.final_weight:
        fail("template scores appear to have overturned Gemini's food rank")
    print("ok     Gemini rank is used as-is (templates do not fuse)")

    boom = FakeModel(rankings=[], error=RuntimeError("api down"))
    recognizer = make_recognizer(tmp, boom)
    result = recognizer.interpret(query, update_memory=False)
    if not result.fallback_used:
        fail("API failure did not fall back to templates")
    if result.top_tag != "water":
        fail(f"circle fallback should be water/cup, got {result.top_tag!r}")
    print("ok     API failure uses drawings_db.json")

    previous = os.environ.get("GEMINI_TIMEOUT_S")
    os.environ["GEMINI_TIMEOUT_S"] = "0.2"
    try:
        slow = FakeModel(rankings=[], delay=0.6)
        recognizer = make_recognizer(tmp, slow)
        started = time.time()
        result = recognizer.interpret(query, update_memory=False)
        elapsed = time.time() - started
    finally:
        if previous is None:
            os.environ.pop("GEMINI_TIMEOUT_S", None)
        else:
            os.environ["GEMINI_TIMEOUT_S"] = previous
    if elapsed > 1.0:
        fail(f"timeout waited {elapsed:.1f}s; should return around 0.2s")
    if not result.fallback_used:
        fail("slow Gemini did not fall back to templates")
    print("ok     timeout uses drawings_db.json")

    if not pipeline.already_named("I would like an apple, please.", "apple"):
        fail("apple should count as already named")
    if pipeline.already_named("I would like some food, please.", "pizza"):
        fail("generic food should still follow up on pizza")
    if pipeline.fallback_closing("food", "pizza") != "I'll get you the pizza.":
        fail(f"unexpected food closing: {pipeline.fallback_closing('food', 'pizza')!r}")
    if pipeline.fallback_closing("rest") != "Rest easy.":
        fail("rest closing should be Rest easy.")
    if pipeline.needs_followup("water", "I would like a glass of water, please."):
        fail("confirmed water should not follow up with tea")
    if pipeline.needs_followup("food", "I would like a slice of pizza, please."):
        fail("named pizza should not ask soup")
    if not pipeline.needs_followup("help", "I need help, please."):
        fail("generic help should offer to call the caretaker")
    if pipeline._followup_allowed("food", "tea"):
        fail("food follow-ups must not include tea")
    if pipeline._followup_allowed("water", "tea", "I would like a glass of water, please."):
        fail("water follow-ups must not include tea after water")
    if not pipeline._followup_allowed("food", "pizza"):
        fail("food follow-ups should allow pizza")
    call = pipeline.caretaker_followup()
    if call["detail"] != "call" or "Jordan" not in call["question"]:
        fail(f"help should offer to call the caretaker, got {call}")
    print("ok     yes-path follow-up and closing helpers")

    from server import shape_game

    shape_game.end("test-game")
    game = shape_game.start("test-game", digit="1")
    if game.target != "circle":
        fail(f"digit 1 should start with circle, got {game.target}")
    spoken, done = shape_game.succeed("test-game")
    if done or "square" not in spoken:
        fail(f"after a circle, expected square prompt, got {spoken!r} done={done}")
    spoken, done = shape_game.fail("test-game", "A bit wobbly")
    if done or "wobbly" not in spoken.lower():
        fail(f"first miss should nudge, got {spoken!r}")
    for _ in range(shape_game.MAX_ATTEMPTS - 1):
        spoken, done = shape_game.fail("test-game")
    if done or "triangle" not in spoken:
        fail(f"too many misses should skip to triangle, got {spoken!r} done={done}")
    spoken, done = shape_game.succeed("test-game")
    spoken, done = shape_game.succeed("test-game")
    if not done or "enough" not in spoken.lower():
        fail(f"three successes should end the game, got {spoken!r} done={done}")
    if shape_game.get("test-game") is not None:
        fail("ended game should be cleared")

    game = shape_game.start("test-game", digit="2")
    if game.target != "square":
        fail(f"digit 2 should start with square, got {game.target}")
    shape_game.end("test-game")
    print("ok     shape game 1/2 flow")

    digit_fake = FakeModel(
        rankings=[
            {
                "tag_id": "food",
                "likelihood": 0.2,
                "reason": "not food",
                "spoken": "",
                "detail": "",
            }
        ],
        spoken="",
        digit="1",
    )
    recognizer = make_recognizer(tmp, digit_fake)
    result = recognizer.interpret(query, update_memory=False)
    if result.digit != "1":
        fail(f"expected digit 1, got {result.digit!r}")
    print("ok     handwritten 1 is detected without fusing templates")

    shutil.rmtree(tmp, ignore_errors=True)
    print("all recognize harness checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
