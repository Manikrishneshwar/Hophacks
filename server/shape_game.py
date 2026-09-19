"""A short shape-drawing game started by a handwritten 1 or 2.

State lives in this process, keyed by tablet session. Gemini grades each
attempt; this module only decides what to ask next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHAPES = ("circle", "square", "triangle")
MAX_SUCCESSES = 3
MAX_ATTEMPTS = 3

PROMPTS = {
    "circle": "Draw a circle.",
    "square": "Draw a square.",
    "triangle": "Draw a triangle.",
}

NUDGES = {
    "circle": "Not quite a circle. Draw a round closed loop.",
    "square": "Not quite a square. Draw four sides and four corners.",
    "triangle": "Not quite a triangle. Draw three sides that meet.",
}

_games: dict[str, "ShapeGame"] = {}


@dataclass
class ShapeGame:
    session_id: str
    target: str
    queue: list[str] = field(default_factory=list)
    successes: int = 0
    attempts: int = 0

    def prompt(self) -> str:
        return PROMPTS.get(self.target, f"Draw a {self.target}.")

    def nudge(self, spoken: str = "") -> str:
        line = " ".join((spoken or "").split())
        if line and not line.endswith((".", "!", "?")):
            line += "."
        return line or NUDGES.get(self.target, "Not quite. Try that shape again.")


def get(session_id: str) -> ShapeGame | None:
    return _games.get(session_id or "")


def start(session_id: str, *, digit: str = "1") -> ShapeGame:
    order = list(SHAPES)
    if str(digit).strip() == "2":
        order = ["square", "triangle", "circle"]
    game = ShapeGame(
        session_id=session_id,
        target=order[0],
        queue=order[1:],
    )
    _games[session_id] = game
    return game


def end(session_id: str) -> None:
    _games.pop(session_id, None)


def succeed(session_id: str) -> tuple[str, bool]:
    """Advance after a match. Returns (spoken, done)."""
    game = _games.get(session_id)
    if game is None:
        return "Nice work. That's enough for now.", True
    done_shape = game.target
    game.successes += 1
    game.attempts = 0
    praise = f"That's a {done_shape}."
    if game.successes >= MAX_SUCCESSES or not game.queue:
        end(session_id)
        return f"{praise} Nice work. That's enough for now.", True
    game.target = game.queue.pop(0)
    return f"{praise} Now {game.prompt().lower()}", False


def fail(session_id: str, spoken: str = "") -> tuple[str, bool]:
    """Nudge a retry, or skip the shape after too many misses."""
    game = _games.get(session_id)
    if game is None:
        return "Let's stop the drawing game.", True
    game.attempts += 1
    nudge = game.nudge(spoken)
    if game.attempts < MAX_ATTEMPTS:
        return nudge, False
    if not game.queue:
        end(session_id)
        return f"{nudge} We'll stop there.", True
    game.target = game.queue.pop(0)
    game.attempts = 0
    return f"{nudge} Let's try a {game.target} instead.", False
