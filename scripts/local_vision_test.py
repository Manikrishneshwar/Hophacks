"""Check the local vision fallback without Ollama running.

    python scripts/local_vision_test.py

A stub stands in for the HTTP call so every branch is reachable offline: a good
answer being shaped like a Gemini ranking, a tag outside the catalog being
refused, the daemon being absent, and the recogniser preferring the local model
over templates but still reaching templates when it is switched off.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import local_vision  # noqa: E402
import recognize  # noqa: E402

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"FAIL   {message}")


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


TAGS = [
    {"id": "water", "label": "water"},
    {"id": "food", "label": "food"},
    {"id": "help", "label": "help"},
    {"id": "rest", "label": "rest"},
]


class StubPost:
    """Stands in for `_post`, recording the body it was handed."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.bodies: list[dict] = []

    def __call__(self, path, body, timeout):
        self.bodies.append(body)
        if isinstance(self.response, Exception):
            raise self.response
        return {"response": json.dumps(self.response)}


def with_post(stub, fn):
    original = local_vision._post
    local_vision._post = stub
    try:
        return fn()
    finally:
        local_vision._post = original


def sample_png() -> Path:
    found = sorted((ROOT / "eval" / "drawings").glob("*.png"))
    return found[0] if found else ROOT / "missing.png"


def test_good_answer() -> None:
    stub = StubPost({"seen": "A cup", "tag": "water", "detail": "cup",
                     "spoken": "I would like water, please."})
    out = with_post(stub, lambda: local_vision.rank_tags(TAGS, image=sample_png()))

    check(len(out["rankings"]) == 1, f"expected one ranking, got {out['rankings']}")
    row = out["rankings"][0]
    check(row["tag_id"] == "water", f"expected water, got {row['tag_id']!r}")
    check(row["detail"] == "cup", f"expected detail cup, got {row['detail']!r}")
    check(out["seen"] == "A cup", f"seen not carried through: {out['seen']!r}")
    check(out["spoken"].startswith("I would like water"), f"spoken lost: {out['spoken']!r}")
    # Digits stay with geometry, which reads them offline and repeatably.
    check(out["digit"] == "", f"local model should not claim a digit, got {out['digit']!r}")
    check(
        row["likelihood"] == local_vision.LOCAL_LIKELIHOOD,
        f"local guesses must stay low-confidence, got {row['likelihood']}",
    )

    body = stub.bodies[0]
    check(body["images"] and isinstance(body["images"][0], str), "the PNG was not attached")
    check(body["format"] == "json", "json mode was not requested")
    check(
        body["keep_alive"] == -1 and not isinstance(body["keep_alive"], str),
        f"keep_alive must be the number -1, got {body['keep_alive']!r}",
    )
    # The production prompt is ~4.5k chars and makes the model ignore the image.
    check(
        len(body["prompt"]) < 1200,
        f"prompt grew to {len(body['prompt'])} chars; it must stay image-first",
    )
    for tag in TAGS:
        check(tag["id"] in body["prompt"], f"tag {tag['id']} missing from the prompt")


def test_unknown_tag_refused() -> None:
    stub = StubPost({"seen": "a snake", "tag": "snake", "spoken": "hiss"})
    try:
        with_post(stub, lambda: local_vision.rank_tags(TAGS, image=sample_png()))
    except RuntimeError as exc:
        check("snake" in str(exc), f"error should name the bad tag, got {exc}")
    else:
        fail("a tag outside the catalog should raise, not be passed on")


def test_empty_tags_refused() -> None:
    try:
        local_vision.rank_tags([], image=sample_png())
    except RuntimeError:
        pass
    else:
        fail("ranking with no tags should raise")


def test_unavailable_when_daemon_absent() -> None:
    original = local_vision.base_url
    # A port nothing listens on, so `available` has to fail by connection.
    local_vision.base_url = lambda: "http://127.0.0.1:9"
    try:
        check(not local_vision.available(), "available() must be False with no daemon")
    finally:
        local_vision.base_url = original


def test_disabled_by_env() -> None:
    import os

    previous = os.environ.get("LOCAL_VISION")
    os.environ["LOCAL_VISION"] = "0"
    try:
        check(not local_vision.enabled(), "LOCAL_VISION=0 must disable the fallback")
        check(not local_vision.available(), "available() must respect LOCAL_VISION=0")
    finally:
        if previous is None:
            os.environ.pop("LOCAL_VISION", None)
        else:
            os.environ["LOCAL_VISION"] = previous


class StubModel:
    """A Gemini stand-in whose ranking call always fails."""

    def __init__(self) -> None:
        self.models = ("stub",)
        self.calls = 0

    def rank_drawing_tags(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED (stub)")

    def context_bundle(self):
        return {}


def build_recognizer() -> recognize.IntentRecognizer:
    return recognize.IntentRecognizer(root=ROOT, model=StubModel())


def test_local_preferred_over_templates() -> None:
    """With Gemini down, a reachable local model should answer before templates."""
    calls: list[str] = []

    def fake_rank(tags, *, image, top_k=5):
        calls.append("local")
        return {
            "rankings": [
                {"tag_id": "water", "likelihood": local_vision.LOCAL_LIKELIHOOD,
                 "reason": "stub", "spoken": "I would like water, please.", "detail": "cup"}
            ],
            "spoken": "I would like water, please.",
            "seen": "A cup",
            "digit": "",
        }

    original_available = local_vision.available
    original_rank = local_vision.rank_tags
    local_vision.available = lambda: True
    local_vision.rank_tags = fake_rank
    try:
        result = build_recognizer().interpret(
            {"strokes": []}, image=sample_png(), update_memory=False
        )
    finally:
        local_vision.available = original_available
        local_vision.rank_tags = original_rank

    check(calls == ["local"], f"local vision should have been asked once, got {calls}")
    check(result.top_tag == "water", f"expected water from the local model, got {result.top_tag}")
    check(result.fallback_used, "a local answer must still be flagged as a fallback")
    check(
        bool(result.candidates) and result.candidates[0].source == "local",
        "the candidate must be marked source=local so the UI does not trust it as Gemini",
    )


def test_templates_used_when_local_absent() -> None:
    """Switched off, the chain must still reach the template matcher."""
    calls: list[str] = []

    original_available = local_vision.available
    original_rank = local_vision.rank_tags
    local_vision.available = lambda: False
    local_vision.rank_tags = lambda *a, **k: calls.append("local")
    try:
        result = build_recognizer().interpret(
            {"strokes": []}, image=sample_png(), update_memory=False
        )
    finally:
        local_vision.available = original_available
        local_vision.rank_tags = original_rank

    check(not calls, "local vision must not be called when unavailable")
    check(result.fallback_used, "expected the template fallback to be flagged")


def test_bad_strokes_do_not_sink_the_capture() -> None:
    """Unusable strokes must still leave the image paths able to answer."""
    original_available = local_vision.available
    original_rank = local_vision.rank_tags
    local_vision.available = lambda: True
    local_vision.rank_tags = lambda tags, *, image, top_k=5: {
        "rankings": [{"tag_id": "help", "likelihood": 0.4, "reason": "stub",
                      "spoken": "I would like help, please.", "detail": ""}],
        "spoken": "I would like help, please.",
        "seen": "a cross",
        "digit": "",
    }
    try:
        result = build_recognizer().interpret(
            {"strokes": "nonsense"}, image=sample_png(), update_memory=False
        )
    except Exception as exc:  # noqa: BLE001 - that is the regression under test
        fail(f"unusable strokes should not raise, got {exc!r}")
        return
    finally:
        local_vision.available = original_available
        local_vision.rank_tags = original_rank

    check(result.top_tag == "help", f"expected the image path to answer, got {result.top_tag}")


def main() -> int:
    if not sample_png().exists():
        print("SKIP   no PNG in eval/drawings to test with")
        return 0
    for test in (
        test_good_answer,
        test_unknown_tag_refused,
        test_empty_tags_refused,
        test_unavailable_when_daemon_absent,
        test_disabled_by_env,
        test_local_preferred_over_templates,
        test_templates_used_when_local_absent,
        test_bad_strokes_do_not_sink_the_capture,
    ):
        test()
        print(f"ran    {test.__name__}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nall local vision checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
