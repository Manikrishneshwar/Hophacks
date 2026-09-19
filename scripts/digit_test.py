"""Check the geometric 1 / 2 detector and the offline shape grader.

    python scripts/digit_test.py

Never calls Gemini. Synthetic strokes stand in for the pad: digits that should
be read, intent drawings that must not be mistaken for digits, and a tremor
overlay on every one of them, because a shaky 1 is the case that matters.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stroke_geometry  # noqa: E402


def stub_gemini_sdk() -> None:
    """Make `recognize` importable without the Gemini client installed.

    `_settle_digit` is pure logic over a result and a geometry guess, but
    reaching it imports gemini_session and therefore the SDK. Only the import is
    satisfied here; none of these stubs is ever called.
    """
    import types as module_types

    dotenv = module_types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.modules.setdefault("dotenv", dotenv)

    google = sys.modules.setdefault("google", module_types.ModuleType("google"))
    genai = module_types.ModuleType("google.genai")
    genai.Client = object
    errors = module_types.ModuleType("google.genai.errors")
    errors.ClientError = type("ClientError", (Exception,), {})
    errors.ServerError = type("ServerError", (Exception,), {})
    types_module = module_types.ModuleType("google.genai.types")
    types_module.GenerateContentConfig = object
    types_module.Part = object
    genai.errors = errors
    genai.types = types_module
    google.genai = genai
    sys.modules.setdefault("google.genai", genai)
    sys.modules.setdefault("google.genai.errors", errors)
    sys.modules.setdefault("google.genai.types", types_module)


STUBBED_SDK = False
try:
    from recognize import IntentRecognizer, RecognitionResult  # noqa: E402
except ImportError:
    stub_gemini_sdk()
    from recognize import IntentRecognizer, RecognitionResult  # noqa: E402

    STUBBED_SDK = True

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"FAIL   {message}")


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def tremor(stroke: list[list[float]], amplitude: float = 3.0, seed: int = 7) -> list[list[float]]:
    """Add a fast small wobble, the thing every metric here has to survive."""
    rng = random.Random(seed)
    out = []
    for index, (x, y) in enumerate(stroke):
        phase = index * 1.9
        out.append([
            x + amplitude * math.sin(phase) + rng.uniform(-0.6, 0.6),
            y + amplitude * math.cos(phase * 1.3) + rng.uniform(-0.6, 0.6),
        ])
    return out


def line(x0: float, y0: float, x1: float, y1: float, n: int = 60) -> list[list[float]]:
    return [
        [x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1)]
        for i in range(n)
    ]


def digit_one(tilt: float = 0.0) -> list[list[list[float]]]:
    """A plain vertical stroke, optionally leaning."""
    lean = math.tan(math.radians(tilt)) * 160.0
    return [line(200, 60, 200 + lean, 220, 80)]


def digit_one_with_flag() -> list[list[list[float]]]:
    """The common handwritten 1: a short flag into a long downstroke."""
    return [line(180, 78, 200, 60, 12) + line(200, 60, 200, 220, 70)]


def digit_one_with_base() -> list[list[list[float]]]:
    """A 1 finished with a separate base serif."""
    return [line(200, 60, 200, 220, 70), line(175, 220, 225, 220, 20)]


def digit_two() -> list[list[list[float]]]:
    """Arc over the top, diagonal down to the left, baseline to the right.

    Each leg starts where the last one ended. A gap between them would read as
    a sharp reversal and inflate the measured turning.
    """
    arc = [
        [200 + 45 * math.cos(math.radians(a)), 105 + 45 * math.sin(math.radians(a))]
        for a in range(190, 371, 6)
    ]
    diagonal = line(arc[-1][0], arc[-1][1], 160, 205, 30)
    baseline = line(diagonal[-1][0], diagonal[-1][1], 252, 207, 25)
    return [arc + diagonal[1:] + baseline[1:]]


def circle(radius: float = 60.0) -> list[list[list[float]]]:
    return [[
        [200 + radius * math.cos(math.radians(a)), 200 + radius * math.sin(math.radians(a))]
        for a in range(0, 366, 5)
    ]]


def square(size: float = 110.0) -> list[list[list[float]]]:
    x, y = 140.0, 140.0
    return [
        line(x, y, x + size, y, 25)
        + line(x + size, y, x + size, y + size, 25)
        + line(x + size, y + size, x, y + size, 25)
        + line(x, y + size, x, y, 25)
    ]


def triangle(size: float = 130.0) -> list[list[list[float]]]:
    top = [200.0, 130.0]
    left = [200 - size / 2, 130 + size]
    right = [200 + size / 2, 130 + size]
    return [
        line(*top, *right, 25) + line(*right, *left, 25) + line(*left, *top, 25)
    ]


def cross() -> list[list[list[float]]]:
    return [line(200, 130, 200, 270, 40), line(130, 200, 270, 200, 40)]


def cup() -> list[list[list[float]]]:
    """An open-topped cup: two walls and a base, three strokes."""
    return [
        line(165, 140, 175, 250, 30),
        line(175, 250, 235, 250, 20),
        line(235, 250, 245, 140, 30),
    ]


def expect_digit(name: str, strokes, want: str, *, min_confidence: float = 0.0) -> None:
    guess = stroke_geometry.detect_digit(strokes)
    got = guess.digit or "-"
    ok = guess.digit == want and guess.confidence >= min_confidence
    if not ok:
        fail(
            f"{name}: wanted {want or '-'}"
            + (f" at >= {min_confidence:.2f}" if min_confidence else "")
            + f", got {got} at {guess.confidence:.2f} ({guess.reason})"
        )
    else:
        print(f"ok     {name:<28} {got} {guess.confidence:.2f}  {guess.reason}")


def test_digits() -> None:
    print("\n-- digits, clean then with tremor --")
    for label, strokes in (
        ("upright 1", digit_one()),
        ("1 leaning 12 deg", digit_one(12.0)),
        ("1 with a flag", digit_one_with_flag()),
        ("1 with a base serif", digit_one_with_base()),
    ):
        expect_digit(label, strokes, "1", min_confidence=stroke_geometry.ASSIST_CONFIDENCE)
        shaky = [tremor(stroke) for stroke in strokes]
        expect_digit(f"{label} + tremor", shaky, "1",
                     min_confidence=stroke_geometry.ASSIST_CONFIDENCE)

    expect_digit("handwritten 2", digit_two(), "2",
                 min_confidence=stroke_geometry.ASSIST_CONFIDENCE)
    expect_digit("handwritten 2 + tremor", [tremor(s) for s in digit_two()], "2",
                 min_confidence=stroke_geometry.ASSIST_CONFIDENCE)


def test_fast_path() -> None:
    print("\n-- a clean 1 should skip Gemini entirely --")
    guess = stroke_geometry.detect_digit(digit_one())
    check(guess.fast_path, f"a clean upright 1 should reach the fast path, got {guess.confidence:.2f}")
    shaky = stroke_geometry.detect_digit([tremor(s) for s in digit_one()])
    check(shaky.assists, f"a shaky 1 should at least assist, got {shaky.confidence:.2f}")
    print(f"ok     clean 1 confidence {guess.confidence:.2f}, shaky {shaky.confidence:.2f}")


def test_not_digits() -> None:
    print("\n-- intent drawings must not become digits --")
    for label, strokes in (
        ("circle", circle()),
        ("square", square()),
        ("triangle", triangle()),
        ("cross", cross()),
        ("cup", cup()),
    ):
        expect_digit(label, strokes, "")
        expect_digit(f"{label} + tremor", [tremor(s) for s in strokes], "")


def test_veto() -> None:
    print("\n-- veto only fires on things a character cannot be --")
    for label, strokes, want in (
        ("circle", circle(), True),
        ("cup", cup(), False),
        ("upright 1", digit_one(), False),
        ("handwritten 2", digit_two(), False),
    ):
        guess = stroke_geometry.detect_digit(strokes)
        if guess.veto is not want:
            fail(f"{label}: veto should be {want}, got {guess.veto} ({guess.reason})")
        else:
            print(f"ok     {label:<28} veto={guess.veto}")


def test_settle_digit() -> None:
    print("\n-- reconciling geometry with Gemini --")
    if STUBBED_SDK:
        print("note   the Gemini client is not installed; its import was stubbed")
    recognizer = IntentRecognizer.__new__(IntentRecognizer)  # no API key needed

    # Gemini saw nothing; geometry fills it in.
    result = RecognitionResult(top_tag="water", fallback_used=False)
    recognizer._settle_digit(result, stroke_geometry.detect_digit(digit_one()))
    check(result.digit == "1", f"geometry should fill an empty digit, got {result.digit!r}")
    check(result.digit_source == "geometry", f"source was {result.digit_source!r}")

    # Gemini claims a digit on a closed loop; geometry vetoes it.
    result = RecognitionResult(top_tag="water", fallback_used=False, digit="1")
    recognizer._settle_digit(result, stroke_geometry.detect_digit(circle()))
    check(result.digit == "", f"a circle should veto Gemini's digit, got {result.digit!r}")

    # Gemini claims a digit and geometry has no objection; Gemini wins.
    result = RecognitionResult(top_tag="play", fallback_used=False, digit="2")
    recognizer._settle_digit(result, stroke_geometry.detect_digit(cup()))
    check(result.digit == "2", f"Gemini's digit should stand, got {result.digit!r}")
    check(result.digit_source == "gemini", f"source was {result.digit_source!r}")
    print("ok     fill, veto and pass-through all behave")


def test_shape_grading() -> None:
    print("\n-- offline shape grading for the game --")
    for target, strokes in (("circle", circle()), ("square", square()), ("triangle", triangle())):
        for suffix, sample in (("", strokes), (" + tremor", [tremor(s) for s in strokes])):
            graded = stroke_geometry.grade(sample, target)
            if not graded["match"]:
                fail(f"{target}{suffix} should grade as a match, got {graded['seen']}")
            else:
                print(f"ok     {target + suffix:<28} {graded['seen']}")

    # A circle is not a square, or the game would be unloseable.
    wrong = stroke_geometry.grade(circle(), "square")
    check(not wrong["match"], f"a circle should not pass as a square: {wrong['seen']}")
    open_line = stroke_geometry.grade(digit_one(), "circle")
    check(not open_line["match"], f"a straight line should not pass as a circle: {open_line['seen']}")
    print("ok     wrong shapes are still rejected")


def main() -> int:
    test_digits()
    test_fast_path()
    test_not_digits()
    test_veto()
    test_settle_digit()
    test_shape_grading()

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all digit and shape checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
