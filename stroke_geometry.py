#!/usr/bin/env python3
"""Read pad strokes geometrically, with no model and no network.

Two decisions live here. `detect_digit` spots a handwritten 1 or 2, which is
what starts the drawing game, and `grade` says whether a drawing is the circle,
square or triangle the game just asked for. Both are deterministic and instant,
so the game works with a dead API key and a dead Wi-Fi network.

Measurements are in CSS pixels, the units the pad records. Screen y grows
downward, so "top" is the smaller y.

Tremor is the reason the obvious metrics are avoided. Arc length over
end-to-end distance calls a shaky straight line bent, because the shake adds
length without moving the line anywhere. Every measurement here is taken either
against a fitted axis or on a coarsely resampled copy of the stroke, both of
which look past a fast small wobble and keep the gesture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from drawing_features import (
    Point,
    Stroke,
    bounding_box,
    parse_strokes,
    polyline_length,
    resample_polyline,
)

# A stroke shorter than this fraction of the longest one is decoration: the
# flag on a 1, a serif, a dot left by a resting finger.
MINOR_STROKE_RATIO = 0.45

# Anything below this is a tap or a speck rather than a drawn character.
MIN_MAIN_LENGTH = 40.0

# Ends closer than this fraction of the bounding-box diagonal make a closed
# shape: a cup, a bowl, an apple. Never a 1 or a 2.
CLOSED_MAX = 0.18

# More substantial strokes than this is a picture, not a character.
MAX_SUBSTANTIAL_STROKES = 3

ONE_MIN_ELONGATION = 2.0
ONE_MIN_STRAIGHTNESS = 0.78
ONE_MAX_TILT = 30.0

# A 1 may be finished with a bar underneath. A cross also has two strokes, so
# the second one only counts as a base if it is clearly shorter than the stem
# and clearly below it rather than through the middle.
BASE_SERIF_MAX_RATIO = 0.7
BASE_SERIF_MIN_DEPTH = 0.75

TWO_MIN_CLOSURE = 0.32
TWO_MAX_STRAIGHTNESS = 0.72
TWO_START_TOP = 0.45
TWO_END_BOTTOM = 0.60
TWO_MAX_TAIL_TILT = 50.0
TWO_MIN_TURNING = 120.0
TWO_MAX_TURNING = 400.0
TWO_MIN_ASPECT = 0.5
TWO_MAX_ASPECT = 3.0

# Confidence at which geometry is believed on its own and the Gemini round trip
# is skipped, and the lower bar at which it may fill in a digit Gemini missed.
FAST_PATH_CONFIDENCE = 0.85
ASSIST_CONFIDENCE = 0.60

MIN_SHAPE_DIAGONAL = 40.0
SHAPE_MAX_GAP = 0.35
SHAPE_MIN_SCORE = 0.45
SHAPE_SAMPLES = 64

# Corner count and circularity of the perfect shape. Hand-drawn attempts land
# below the circularity and scatter around the corner count.
SHAPE_IDEALS = {
    "circle": (0, 1.0),
    "square": (4, 0.785),
    "triangle": (3, 0.605),
}


@dataclass
class DigitGuess:
    """What geometry thinks, and how much it wants to be believed."""

    digit: str = ""
    confidence: float = 0.0
    # True when the drawing is positively not a character, which is the only
    # case where geometry overrules a digit the model claims to have seen.
    veto: bool = False
    reason: str = ""

    @property
    def fast_path(self) -> bool:
        return bool(self.digit) and self.confidence >= FAST_PATH_CONFIDENCE

    @property
    def assists(self) -> bool:
        return bool(self.digit) and self.confidence >= ASSIST_CONFIDENCE


@dataclass
class ShapeGuess:
    shape: str = ""
    score: float = 0.0
    corners: int = 0
    circularity: float = 0.0
    reason: str = ""


@dataclass
class Axis:
    """A stroke described by the straight line that best fits it."""

    tilt: float  # degrees away from vertical, 0 to 90
    elongation: float  # length along the axis over spread across it
    straightness: float  # 1.0 is a ruler


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ramp(value: float, low: float, high: float) -> float:
    """0 at `low`, 1 at `high`, linear between."""
    if high <= low:
        return 0.0
    return _clamp((value - low) / (high - low))


def _window(value: float, low: float, ideal: float, high: float) -> float:
    """1 at `ideal`, falling to 0 at `low` and `high`."""
    if value <= ideal:
        return _ramp(value, low, ideal)
    return 1.0 - _ramp(value, ideal, high)


def _confidence(floor: float, *parts: float) -> float:
    """Passing every gate is worth `floor`; the ramps earn the rest."""
    if not parts:
        return floor
    return round(floor + (1.0 - floor) * (sum(parts) / len(parts)), 3)


def _fit_axis(points: Stroke) -> Axis:
    """Fit a principal axis and measure the stroke against it.

    Deviation is an RMS across the axis rather than a maximum, so a single
    tremor spike cannot make a straight line look bent.
    """
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count

    sxx = syy = sxy = 0.0
    for x, y in points:
        dx, dy = x - mean_x, y - mean_y
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    sxx /= count
    syy /= count
    sxy /= count

    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    along = [(x - mean_x) * cos_t + (y - mean_y) * sin_t for x, y in points]
    across = [-(x - mean_x) * sin_t + (y - mean_y) * cos_t for x, y in points]
    extent = max(along) - min(along)
    spread = math.sqrt(sum(a * a for a in across) / count)

    return Axis(
        tilt=math.degrees(math.atan2(abs(cos_t), abs(sin_t))),
        elongation=extent / max(max(across) - min(across), 1.0),
        straightness=1.0 - _clamp(2.0 * spread / max(extent, 1.0)),
    )


def _smooth(points: Stroke, window: int = 7) -> Stroke:
    """Moving average along the path.

    Tremor is fast and small, so a short window takes it out while leaving the
    gesture and its corners intact. Arc length is what needs this most: an
    untouched shaky outline measures far longer than the shape it draws, which
    wrecks anything computed from a perimeter.
    """
    if window < 3 or len(points) < window:
        return list(points)
    half = window // 2
    smoothed: Stroke = []
    for index in range(len(points)):
        low = max(0, index - half)
        high = min(len(points), index + half + 1)
        chunk = points[low:high]
        smoothed.append((
            sum(x for x, _ in chunk) / len(chunk),
            sum(y for _, y in chunk) / len(chunk),
        ))
    return smoothed


def _turning(points: Stroke, samples: int = 28) -> float:
    """Total absolute turning in degrees along a smoothed coarse resample.

    Resampling to a couple of dozen points is what makes this usable: each
    segment then spans far more than the tremor amplitude, so the headings
    follow the gesture instead of the shake.
    """
    eased = _smooth(points)
    coarse = resample_polyline(eased, min(samples, max(3, len(eased))))
    total = 0.0
    previous: float | None = None
    for (x0, y0), (x1, y1) in zip(coarse, coarse[1:]):
        if math.hypot(x1 - x0, y1 - y0) < 1e-9:
            continue
        heading = math.atan2(y1 - y0, x1 - x0)
        if previous is not None:
            delta = (heading - previous + math.pi) % (2.0 * math.pi) - math.pi
            total += abs(delta)
        previous = heading
    return math.degrees(total)


def _tail_vector(points: Stroke, fraction: float = 0.2) -> tuple[float, float]:
    """Where the last `fraction` of the stroke, by arc length, was heading."""
    total = polyline_length(points)
    if total <= 1e-9:
        return 0.0, 0.0
    budget = total * fraction
    walked = 0.0
    index = 0
    for i in range(len(points) - 1, 0, -1):
        walked += math.dist(points[i - 1], points[i])
        index = i - 1
        if walked >= budget:
            break
    return points[-1][0] - points[index][0], points[-1][1] - points[index][1]


def _is_base_serif(main: Stroke, other: Stroke) -> bool:
    """Is `other` a bar under a 1, rather than the arm of a cross?

    A cross fails on both counts: its arm is as long as the stem and it crosses
    the middle, not the foot.
    """
    if polyline_length(other) > polyline_length(main) * BASE_SERIF_MAX_RATIO:
        return False
    _, min_y, _, max_y = bounding_box([main])
    height = max_y - min_y
    if height <= 1e-9:
        return False
    middle_y = sum(y for _, y in other) / len(other)
    return (middle_y - min_y) / height >= BASE_SERIF_MIN_DEPTH


def _score_one(main: Stroke, axis: Axis, substantial: list[Stroke]) -> tuple[float, str]:
    """A 1 is one tall, upright, near-straight stroke."""
    if len(substantial) > 2:
        return 0.0, f"a 1 is one stroke, maybe two with a base, not {len(substantial)}"
    if len(substantial) == 2 and not _is_base_serif(main, substantial[1]):
        return 0.0, "the second stroke crosses the stem instead of sitting under it"
    if axis.elongation < ONE_MIN_ELONGATION:
        return 0.0, f"too squat for a 1 (elongation {axis.elongation:.1f})"
    if axis.straightness < ONE_MIN_STRAIGHTNESS:
        return 0.0, f"too bent for a 1 (straightness {axis.straightness:.2f})"
    if axis.tilt > ONE_MAX_TILT:
        return 0.0, f"too tilted for a 1 ({axis.tilt:.0f} deg off vertical)"

    score = _confidence(
        0.55,
        _ramp(axis.straightness, ONE_MIN_STRAIGHTNESS, 0.97),
        _ramp(axis.elongation, ONE_MIN_ELONGATION, 6.0),
        _ramp(ONE_MAX_TILT - axis.tilt, 0.0, ONE_MAX_TILT),
    )
    return score, (
        f"upright near-straight stroke, elongation {axis.elongation:.1f}, "
        f"{axis.tilt:.0f} deg off vertical"
    )


def _score_two(
    main: Stroke,
    axis: Axis,
    closure: float,
    substantial: list[Stroke],
) -> tuple[float, str]:
    """A 2 is one open curve from the top down to a left-to-right baseline."""
    if len(substantial) > 1:
        return 0.0, f"a 2 is a single stroke, not {len(substantial)}"
    if closure < TWO_MIN_CLOSURE:
        return 0.0, f"ends too close together for a 2 (closure {closure:.2f})"
    if axis.straightness > TWO_MAX_STRAIGHTNESS:
        return 0.0, f"too straight for a 2 (straightness {axis.straightness:.2f})"

    min_x, min_y, max_x, max_y = bounding_box([main])
    width = max_x - min_x
    height = max_y - min_y
    if width <= 1e-9 or height <= 1e-9:
        return 0.0, "the stroke has no area to judge"

    start_y = (main[0][1] - min_y) / height
    end_y = (main[-1][1] - min_y) / height
    if start_y > TWO_START_TOP:
        return 0.0, f"a 2 starts at the top, this starts {start_y:.0%} down"
    if end_y < TWO_END_BOTTOM:
        return 0.0, f"a 2 ends at the bottom, this ends {end_y:.0%} down"

    tail_dx, tail_dy = _tail_vector(main)
    if tail_dx <= 0.0:
        return 0.0, "a 2 finishes left to right"
    tail_tilt = math.degrees(math.atan2(abs(tail_dy), abs(tail_dx)))
    if tail_tilt > TWO_MAX_TAIL_TILT:
        return 0.0, f"no baseline at the bottom ({tail_tilt:.0f} deg off horizontal)"

    aspect = height / width
    if not TWO_MIN_ASPECT <= aspect <= TWO_MAX_ASPECT:
        return 0.0, f"aspect {aspect:.1f} is not a 2"

    turning = _turning(main)
    if not TWO_MIN_TURNING <= turning <= TWO_MAX_TURNING:
        return 0.0, f"turning {turning:.0f} deg is not a 2"

    score = _confidence(
        0.5,
        _ramp(TWO_MAX_TAIL_TILT - tail_tilt, 0.0, TWO_MAX_TAIL_TILT),
        _window(turning, TWO_MIN_TURNING, 230.0, TWO_MAX_TURNING),
        _window(aspect, TWO_MIN_ASPECT, 1.4, TWO_MAX_ASPECT),
    )
    return score, (
        f"open curve from the top to a left-to-right baseline, turning "
        f"{turning:.0f} deg, aspect {aspect:.1f}"
    )


def detect_digit(raw: Any) -> DigitGuess:
    """Decide whether these strokes are a handwritten 1 or 2.

    Accepts any stroke format `parse_strokes` understands, including a capture's
    `polylines` and its raw `points`.
    """
    try:
        strokes = [stroke for stroke in parse_strokes(raw) if len(stroke) >= 2]
    except (TypeError, ValueError):
        return DigitGuess(reason="unreadable stroke data")
    if not strokes:
        return DigitGuess(reason="no strokes")

    ordered = sorted(strokes, key=polyline_length, reverse=True)
    main = ordered[0]
    main_length = polyline_length(main)
    if main_length < MIN_MAIN_LENGTH:
        return DigitGuess(reason=f"longest stroke is only {main_length:.0f}px")

    substantial = [
        stroke
        for stroke in ordered
        if polyline_length(stroke) >= main_length * MINOR_STROKE_RATIO
    ]

    min_x, min_y, max_x, max_y = bounding_box([main])
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    closure = math.dist(main[0], main[-1]) / diagonal if diagonal > 1e-9 else 0.0

    if len(substantial) > MAX_SUBSTANTIAL_STROKES:
        return DigitGuess(
            veto=True,
            reason=f"{len(substantial)} strokes is a picture, not a digit",
        )
    if closure < CLOSED_MAX:
        return DigitGuess(veto=True, reason="the main stroke closes on itself")

    axis = _fit_axis(main)
    one_score, one_why = _score_one(main, axis, substantial)
    two_score, two_why = _score_two(main, axis, closure, substantial)

    if one_score > 0.0 and one_score >= two_score:
        return DigitGuess(digit="1", confidence=one_score, reason=one_why)
    if two_score > 0.0:
        return DigitGuess(digit="2", confidence=two_score, reason=two_why)
    return DigitGuess(reason=f"not a 1 ({one_why}); not a 2 ({two_why})")


def _shoelace(contour: list[Point]) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(contour, contour[1:] + contour[:1]):
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _circularity(contour: list[Point]) -> float:
    """4*pi*area / perimeter^2: 1 for a circle, 0.79 a square, 0.60 a triangle."""
    perimeter = polyline_length(contour) + math.dist(contour[-1], contour[0])
    if perimeter <= 1e-9:
        return 0.0
    return min(1.0, 4.0 * math.pi * abs(_shoelace(contour)) / (perimeter * perimeter))


def _corners(contour: list[Point], min_turn: float = 45.0) -> int:
    """Count direction changes around a closed contour, one per corner.

    Each run of high-turn samples counts once, because a hand-drawn corner is
    rounded across several of them.
    """
    count = len(contour)
    if count < 7:
        return 0
    span = 3

    turns = []
    for i in range(count):
        bx, by = contour[(i - span) % count]
        hx, hy = contour[i]
        ax, ay = contour[(i + span) % count]
        before = math.atan2(hy - by, hx - bx)
        after = math.atan2(ay - hy, ax - hx)
        delta = abs((after - before + math.pi) % (2.0 * math.pi) - math.pi)
        turns.append(math.degrees(delta))

    # Begin at the flattest sample, so a corner lying on the seam between the
    # first and last points is one run instead of being counted at both ends.
    pivot = min(range(count), key=lambda i: turns[i])
    turns = turns[pivot:] + turns[:pivot]

    corners = 0
    i = 0
    while i < count:
        if turns[i] >= min_turn:
            corners += 1
            i += 1
            while i < count and turns[i] >= min_turn * 0.6:
                i += 1
        else:
            i += 1
    return corners


def classify_shape(raw: Any) -> ShapeGuess:
    """Name the closed shape these strokes draw, if it is one of the game's."""
    try:
        strokes = [stroke for stroke in parse_strokes(raw) if len(stroke) >= 2]
    except (TypeError, ValueError):
        return ShapeGuess(reason="unreadable stroke data")
    if not strokes:
        return ShapeGuess(reason="no strokes")

    contour = [point for stroke in strokes for point in stroke]
    min_x, min_y, max_x, max_y = bounding_box(strokes)
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    if diagonal < MIN_SHAPE_DIAGONAL:
        return ShapeGuess(reason="too small to judge")

    gap = math.dist(contour[0], contour[-1]) / diagonal
    # Smooth before resampling: the perimeter of a shaky outline is far longer
    # than the shape it draws, and circularity is a ratio against it squared.
    eased = _smooth(contour)
    coarse = resample_polyline(eased, min(SHAPE_SAMPLES, max(8, len(eased))))
    circularity = _circularity(coarse)
    corners = _corners(coarse)
    if gap > SHAPE_MAX_GAP:
        return ShapeGuess(
            corners=corners,
            circularity=circularity,
            reason=f"the outline never closes (gap {gap:.0%})",
        )

    best = ShapeGuess(corners=corners, circularity=circularity, reason="no shape fits")
    for shape, (ideal_corners, ideal_circularity) in SHAPE_IDEALS.items():
        # Hand-drawn shapes always come in under the ideal circularity, so only
        # the shortfall is penalised; a rounder-than-ideal square is impossible.
        circle_fit = 1.0 - _clamp(abs(circularity - ideal_circularity) / 0.35)
        corner_fit = 1.0 - _clamp(abs(corners - ideal_corners) / 3.0)
        score = round(0.6 * circle_fit + 0.4 * corner_fit, 3)
        if score > best.score:
            best = ShapeGuess(
                shape=shape,
                score=score,
                corners=corners,
                circularity=circularity,
                reason=f"{corners} corners, circularity {circularity:.2f}",
            )
    return best


def grade(raw: Any, target: str) -> dict[str, Any]:
    """Stand in for Gemini's shape grading. Deliberately generous.

    Returns the same keys `pipeline.grade_shape` hands back, so it can be
    swapped in without the caller noticing.
    """
    guess = classify_shape(raw)
    if not guess.shape or guess.score < SHAPE_MIN_SCORE:
        return {"match": False, "seen": guess.reason, "spoken": ""}
    return {
        "match": guess.shape == target,
        "seen": f"{guess.shape} ({guess.reason})",
        "spoken": "",
    }
