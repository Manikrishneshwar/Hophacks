"""Check the Gemini model fallback chain without calling Gemini.

    python scripts/model_chain_test.py

A stub client stands in for the SDK so every branch is reachable offline: the
first model answering, the second rescuing a failure, a rejected key stopping
the chain instead of retrying, and the shared timeout budget being split.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gemini_session  # noqa: E402
from gemini_session import (
    API_MIN_DEADLINE_S,
    DEFAULT_MODEL_CHAIN,
    FALLBACK_MODEL,
    GeminiPatientModel,
    resolve_model_chain,
)

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"FAIL   {message}")


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


class StubResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class StubModels:
    """Answers or raises per model name, and records what it was asked."""

    def __init__(self, behaviour: dict[str, object], delay: float = 0.0) -> None:
        self.behaviour = behaviour
        self.delay = delay
        self.tried: list[str] = []
        self.timeouts: list[int | None] = []

    def generate_content(self, *, model, contents, config):
        self.tried.append(model)
        http_options = getattr(config, "http_options", None)
        self.timeouts.append(getattr(http_options, "timeout", None))
        if self.delay:
            time.sleep(self.delay)
        outcome = self.behaviour.get(model, "pong")
        if isinstance(outcome, Exception):
            raise outcome
        return StubResponse(str(outcome))


class StubClient:
    def __init__(self, behaviour: dict[str, object], delay: float = 0.0) -> None:
        self.models = StubModels(behaviour, delay)


def make_model(
    behaviour: dict[str, object],
    *,
    model: str | None = None,
    delay: float = 0.0,
) -> tuple:
    session = GeminiPatientModel.__new__(GeminiPatientModel)
    session.models = resolve_model_chain(model)
    client = StubClient(behaviour, delay)
    session._client = client
    return session, client


def clear_env() -> None:
    for name in ("GEMINI_MODEL", "GEMINI_MODELS", "GEMINI_TIMEOUT_S"):
        os.environ.pop(name, None)
    # These tests are about the Gemini chain alone and must not touch the network.
    # Building the session through `__new__` already skips the `load_dotenv()` that
    # would expose a real key, but an exported one would still leak in and let xAI
    # rescue the cases that are supposed to fail.
    os.environ["XAI_FALLBACK"] = "0"


def test_resolution() -> None:
    print("\n-- which models get tried --")
    clear_env()
    check(
        resolve_model_chain() == DEFAULT_MODEL_CHAIN,
        f"default chain should be {DEFAULT_MODEL_CHAIN}, got {resolve_model_chain()}",
    )

    check(
        resolve_model_chain("gemini-pinned") == ("gemini-pinned",),
        "an explicit model should pin exactly one",
    )

    os.environ["GEMINI_MODELS"] = " a , b ,, a "
    check(
        resolve_model_chain() == ("a", "b"),
        f"GEMINI_MODELS should parse and dedupe, got {resolve_model_chain()}",
    )
    del os.environ["GEMINI_MODELS"]

    os.environ["GEMINI_MODEL"] = "gemini-chosen"
    chain = resolve_model_chain()
    check(chain[0] == "gemini-chosen", f"a lone GEMINI_MODEL should lead, got {chain}")
    check(len(chain) > 1, f"a fallback should stay behind it, got {chain}")
    del os.environ["GEMINI_MODEL"]
    print(f"ok     default {DEFAULT_MODEL_CHAIN}, pin, list and primary+fallback all resolve")


def test_first_model_answers() -> None:
    print("\n-- the first model answering --")
    clear_env()
    session, client = make_model({})
    text = session._generate(["hello"])
    check(text == "pong", f"expected the stub reply, got {text!r}")
    check(
        client.models.tried == [DEFAULT_MODEL_CHAIN[0]],
        f"only the first model should be called, tried {client.models.tried}",
    )
    print(f"ok     answered by {client.models.tried[0]} with no fallback")


def test_second_model_rescues() -> None:
    print("\n-- a failure falling through to the next model --")
    clear_env()
    for label, outcome in (
        ("an API error", RuntimeError("503 UNAVAILABLE model is overloaded")),
        ("an empty body", ""),
    ):
        session, client = make_model({DEFAULT_MODEL_CHAIN[0]: outcome})
        text = session._generate(["hello"])
        check(text == "pong", f"{label}: expected the fallback reply, got {text!r}")
        check(
            client.models.tried == list(DEFAULT_MODEL_CHAIN[:2]),
            f"{label}: both models should be tried, tried {client.models.tried}",
        )
        print(f"ok     {label:<14} fell through to {FALLBACK_MODEL}")


def test_permanent_failure_stops() -> None:
    print("\n-- a rejected key must not be retried on every model --")
    clear_env()
    rejected = RuntimeError("400 INVALID_ARGUMENT API_KEY_INVALID: API key not valid")
    session, client = make_model(dict.fromkeys(DEFAULT_MODEL_CHAIN, rejected))
    try:
        session._generate(["hello"])
    except RuntimeError as exc:
        check("API_KEY_INVALID" in str(exc), f"the reason should survive, got {exc}")
    else:
        fail("a rejected key should raise")
    check(
        len(client.models.tried) == 1,
        f"the chain should stop after the first rejection, tried {client.models.tried}",
    )
    print("ok     stopped after one attempt instead of burning the budget")


def test_all_models_fail() -> None:
    print("\n-- every model failing --")
    clear_env()
    session, client = make_model(
        dict.fromkeys(DEFAULT_MODEL_CHAIN, RuntimeError("503 UNAVAILABLE"))
    )
    try:
        session._generate(["hello"])
    except RuntimeError:
        pass
    else:
        fail("an exhausted chain should raise")
    check(
        client.models.tried == list(DEFAULT_MODEL_CHAIN),
        f"every model should have been tried, tried {client.models.tried}",
    )
    print(f"ok     tried all {len(DEFAULT_MODEL_CHAIN)} then raised")


def test_deadlines_clear_the_api_floor() -> None:
    print("\n-- every deadline must clear the API's 10s floor --")
    clear_env()
    # The API refuses anything shorter: "Manually set deadline 8s is too short."
    # A chain long enough to make an even split illegal must not send one.
    for budget, chain in (("15", None), ("15", "a,b,c,d,e,f"), ("40", None)):
        clear_env()
        os.environ["GEMINI_TIMEOUT_S"] = budget
        if chain:
            os.environ["GEMINI_MODELS"] = chain
        models = resolve_model_chain()
        session, client = make_model(dict.fromkeys(models[:-1], RuntimeError("503 UNAVAILABLE")))
        session._generate(["hello"])
        deadlines = [(value or 0) / 1000.0 for value in client.models.timeouts]
        too_short = [value for value in deadlines if value < API_MIN_DEADLINE_S]
        if too_short:
            fail(f"budget {budget}s over {len(models)} models sent {too_short} < {API_MIN_DEADLINE_S}s")
        else:
            print(f"ok     budget {budget:>2}s over {len(models)} models: "
                  f"deadlines {[round(value, 1) for value in deadlines]}")
    clear_env()


def test_generous_budget_splits_evenly() -> None:
    print("\n-- a budget big enough for both splits, so a hang is survivable --")
    clear_env()
    os.environ["GEMINI_TIMEOUT_S"] = "40"
    session, client = make_model({DEFAULT_MODEL_CHAIN[0]: RuntimeError("503 UNAVAILABLE")})
    session._generate(["hello"])
    deadlines = [(value or 0) / 1000.0 for value in client.models.timeouts]
    check(len(deadlines) == 2, f"both models should be tried, got {deadlines}")
    check(
        sum(deadlines) <= 40.01,
        f"the split should stay inside the budget, it sums to {sum(deadlines)}s",
    )
    check(
        abs(deadlines[0] - deadlines[1]) < 0.01,
        f"the split should be even, got {deadlines}",
    )
    print(f"ok     40s budget splits into {deadlines}")
    clear_env()


def test_spent_budget_skips_the_rest() -> None:
    print("\n-- a spent budget skips the fallback rather than sending a doomed call --")
    clear_env()
    # 11s of budget and a first attempt that burns 1.5s leaves 9.5s, under the
    # floor, so the second model is not worth asking.
    os.environ["GEMINI_TIMEOUT_S"] = "11"
    session, client = make_model(
        {DEFAULT_MODEL_CHAIN[0]: RuntimeError("503 UNAVAILABLE")}, delay=1.5
    )
    try:
        session._generate(["hello"])
    except RuntimeError:
        pass
    else:
        fail("with the fallback skipped the call should raise")
    check(
        client.models.tried == [DEFAULT_MODEL_CHAIN[0]],
        f"the fallback should have been skipped, tried {client.models.tried}",
    )
    print("ok     skipped the fallback with too little budget left")
    clear_env()


def main() -> int:
    clear_env()
    test_resolution()
    test_first_model_answers()
    test_second_model_rescues()
    test_permanent_failure_stops()
    test_all_models_fail()
    test_deadlines_clear_the_api_floor()
    test_generous_budget_splits_evenly()
    test_spent_budget_skips_the_rest()

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all model chain checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
