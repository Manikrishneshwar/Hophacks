"""Check the xAI tier of the chain without calling xAI.

    python scripts/xai_chain_test.py

Stubs stand in for both vendors so every branch is reachable offline: xAI
rescuing a chain where every Gemini model refused, xAI being skipped when Gemini
answered, a missing key leaving the old error intact, the PNG surviving the
translation into OpenAI-shaped messages, and a budget too small to bother with.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import xai_backend  # noqa: E402
from gemini_session import GeminiPatientModel, resolve_model_chain  # noqa: E402
from google.genai import types  # noqa: E402

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"FAIL   {message}")


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


class StubModels:
    """Every Gemini model raises, unless told to answer."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.tried: list[str] = []

    def generate_content(self, *, model, contents, config):
        self.tried.append(model)
        if self.answer is None:
            raise RuntimeError("503 UNAVAILABLE model is overloaded")

        class Response:
            text = self.answer

        return Response()


class StubClient:
    def __init__(self, answer: str | None = None) -> None:
        self.models = StubModels(answer)


def build(answer: str | None = None) -> tuple:
    session = GeminiPatientModel.__new__(GeminiPatientModel)
    session.models = resolve_model_chain()
    client = StubClient(answer)
    session._client = client
    return session, client


class StubPost:
    def __init__(self, reply: str = '{"ok": true}') -> None:
        self.reply = reply
        self.bodies: list[dict] = []
        self.timeouts: list[float] = []

    def __call__(self, body, timeout):
        self.bodies.append(body)
        self.timeouts.append(timeout)
        return {"choices": [{"message": {"content": self.reply}}]}


def with_xai(stub, key, fn):
    original_post = xai_backend._post
    previous = os.environ.get("XAI_API_KEY")
    os.environ.pop("XAI_FALLBACK", None)
    if key is None:
        os.environ.pop("XAI_API_KEY", None)
    else:
        os.environ["XAI_API_KEY"] = key
    xai_backend._post = stub
    try:
        return fn()
    finally:
        xai_backend._post = original_post
        if previous is None:
            os.environ.pop("XAI_API_KEY", None)
        else:
            os.environ["XAI_API_KEY"] = previous


def test_xai_rescues_a_dead_chain() -> None:
    print("\n-- xai answers when every Gemini model refused --")
    session, client = build()
    stub = StubPost('{"rescued": true}')
    text = with_xai(stub, "xai-test", lambda: session._generate(["rank this"], json_mode=True))

    check(text == '{"rescued": true}', f"expected the xai reply, got {text!r}")
    check(
        len(client.models.tried) == len(session.models),
        f"every Gemini model should have been tried first, got {client.models.tried}",
    )
    check(len(stub.bodies) == 1, f"xai should be called once, got {len(stub.bodies)}")
    check(
        stub.bodies[0].get("response_format") == {"type": "json_object"},
        "json mode must carry over to xai, or the ranking will not parse",
    )
    print("ok     xai rescued the chain")


def test_xai_not_called_when_gemini_answers() -> None:
    print("\n-- xai stays unused when Gemini works --")
    session, _ = build(answer='{"from": "gemini"}')
    stub = StubPost()
    text = with_xai(stub, "xai-test", lambda: session._generate(["rank this"]))

    check(text == '{"from": "gemini"}', f"expected the Gemini reply, got {text!r}")
    check(not stub.bodies, "xai must not be called when a Gemini model answered")
    print("ok     xai left alone")


def test_no_key_keeps_the_gemini_error() -> None:
    print("\n-- without a key the original failure is still raised --")
    session, _ = build()
    stub = StubPost()
    try:
        with_xai(stub, None, lambda: session._generate(["rank this"]))
    except RuntimeError as exc:
        check(
            "overloaded" in str(exc) or "503" in str(exc),
            f"the Gemini error should survive, got {exc}",
        )
    else:
        fail("a dead chain with no xai key must raise")
    check(not stub.bodies, "xai must not be called without a key")
    print("ok     error preserved")


def test_image_survives_translation() -> None:
    print("\n-- the PNG reaches xai as a data url --")
    found = sorted((ROOT / "eval" / "drawings").glob("*.png"))
    if not found:
        print("SKIP   no PNG in eval/drawings")
        return
    raw = found[0].read_bytes()
    part = types.Part.from_bytes(data=raw, mime_type="image/png")

    session, _ = build()
    stub = StubPost()
    with_xai(stub, "xai-test", lambda: session._generate([part, "describe it"]))

    content = stub.bodies[0]["messages"][0]["content"]
    texts = [b for b in content if b["type"] == "text"]
    images = [b for b in content if b["type"] == "image_url"]
    check(len(images) == 1, f"expected one image block, got {len(images)}")
    check("describe it" in texts[0]["text"], "the prompt text was lost")
    url = images[0]["image_url"]["url"]
    check(url.startswith("data:image/png;base64,"), f"wrong data url prefix: {url[:32]}")
    check(
        base64.b64decode(url.split(",", 1)[1]) == raw,
        "the PNG bytes changed on the way through",
    )
    print("ok     image and prompt both carried")


def test_unhurried_lane_uses_the_better_model() -> None:
    print("\n-- background work gets the slower, better model --")
    session, _ = build()

    fast = StubPost()
    with_xai(fast, "xai-test", lambda: session._generate(["rank this"]))
    slow = StubPost()
    with_xai(slow, "xai-test", lambda: session._generate(["summarise"], unhurried=True))

    check(
        fast.bodies[0]["model"] == xai_backend.DEFAULT_MODEL,
        f"the live path must stay fast, got {fast.bodies[0]['model']!r}",
    )
    check(
        slow.bodies[0]["model"] == xai_backend.SLOW_MODEL,
        f"background work should use {xai_backend.SLOW_MODEL}, got {slow.bodies[0]['model']!r}",
    )
    # Nobody is waiting, so the deadline must not be the pad's 15s.
    check(
        slow.timeouts[0] > fast.timeouts[0],
        f"unhurried deadline {slow.timeouts[0]:.0f}s should exceed live {fast.timeouts[0]:.0f}s",
    )
    print(
        f"ok     live {fast.bodies[0]['model']} at {fast.timeouts[0]:.0f}s, "
        f"background {slow.bodies[0]['model']} at {slow.timeouts[0]:.0f}s"
    )


def test_tiny_budget_is_refused() -> None:
    print("\n-- too little time left to bother --")
    stub = StubPost()

    def call():
        return xai_backend.generate(["hi"], timeout=xai_backend.MIN_DEADLINE_S - 0.5)

    try:
        with_xai(stub, "xai-test", call)
    except RuntimeError as exc:
        check("needs" in str(exc), f"expected a budget complaint, got {exc}")
    else:
        fail("a budget under the floor should be refused, not attempted")
    check(not stub.bodies, "no request should be sent with no time left")
    print("ok     refused without sending")


def main() -> int:
    for test in (
        test_xai_rescues_a_dead_chain,
        test_xai_not_called_when_gemini_answers,
        test_no_key_keeps_the_gemini_error,
        test_image_survives_translation,
        test_unhurried_lane_uses_the_better_model,
        test_tiny_budget_is_refused,
    ):
        test()
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all xai chain checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
