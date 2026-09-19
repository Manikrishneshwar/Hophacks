#!/usr/bin/env python3
"""Extract comparable JSON descriptors from pad stroke points.

Incoming drawings can have any number of points. They are resampled to a
fixed count (default 100, allowed 100-500) so two sketches can be compared
in the same feature space.

Usage:
  python drawing_features.py describe strokes.json
  python drawing_features.py add house strokes.json --label house
  python drawing_features.py update house strokes.json
  python drawing_features.py compare strokes.json --top 5
  python drawing_features.py demo
"""

from __future__ import annotations

import argparse
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

Point = tuple[float, float]
Stroke = list[Point]

DEFAULT_POINT_COUNT = 100
MIN_POINT_COUNT = 100
MAX_POINT_COUNT = 500
DEFAULT_DB_PATH = Path("drawings_db.json")
GRID_SIZE = 8
DIRECTION_BINS = 8

SHAPE_WEIGHT = 0.50
DIRECTION_WEIGHT = 0.20
OCCUPANCY_WEIGHT = 0.20
STROKE_WEIGHT = 0.10


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clamp_point_count(n: int) -> int:
    if n < MIN_POINT_COUNT or n > MAX_POINT_COUNT:
        raise ValueError(
            f"point_count must be between {MIN_POINT_COUNT} and {MAX_POINT_COUNT}, got {n}"
        )
    return n


def _as_point(value: Any) -> Point:
    if isinstance(value, dict):
        if "x" not in value or "y" not in value:
            raise ValueError(f"Point dict needs x and y: {value!r}")
        return (float(value["x"]), float(value["y"]))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    raise ValueError(f"Unsupported point: {value!r}")


def _is_point_like(value: Any) -> bool:
    if isinstance(value, dict):
        return "x" in value and "y" in value
    return isinstance(value, (list, tuple)) and len(value) >= 2 and not isinstance(
        value[0], (list, tuple, dict)
    )


def parse_strokes(raw: Any) -> list[Stroke]:
    """Accept common pad / JSON stroke formats and return [[(x, y), ...], ...]."""
    if raw is None:
        raise ValueError("No stroke data provided")

    if isinstance(raw, dict):
        if "strokes" in raw:
            return parse_strokes(raw["strokes"])
        if "points" in raw:
            points = [_as_point(p) for p in raw["points"]]
            breaks = raw.get("stroke_breaks") or raw.get("breaks")
            if breaks:
                strokes: list[Stroke] = []
                starts = [0, *[int(i) for i in breaks], len(points)]
                for start, end in zip(starts, starts[1:]):
                    chunk = points[start:end]
                    if chunk:
                        strokes.append(chunk)
                return strokes
            stroke_ids = raw.get("stroke_ids")
            if stroke_ids:
                grouped: dict[Any, Stroke] = {}
                order: list[Any] = []
                for point, stroke_id in zip(points, stroke_ids, strict=True):
                    if stroke_id not in grouped:
                        grouped[stroke_id] = []
                        order.append(stroke_id)
                    grouped[stroke_id].append(point)
                return [grouped[key] for key in order]
            return [points]
        raise ValueError("Object must contain 'strokes' or 'points'")

    if not isinstance(raw, list) or not raw:
        raise ValueError("Strokes must be a non-empty list")

    if _is_point_like(raw[0]):
        if isinstance(raw[0], dict) and "stroke" in raw[0]:
            grouped = {}
            order = []
            for item in raw:
                stroke_id = item.get("stroke")
                if stroke_id not in grouped:
                    grouped[stroke_id] = []
                    order.append(stroke_id)
                grouped[stroke_id].append(_as_point(item))
            return [grouped[key] for key in order]
        return [[_as_point(p) for p in raw]]

    strokes = []
    for stroke in raw:
        if not stroke:
            continue
        if isinstance(stroke, dict):
            # How a capture's own `.json` stores a stroke: the points sit beside
            # metadata like tool and smoothing, and `raw` holds the unfiltered
            # samples. Iterating the dict itself would walk its keys.
            stroke = stroke.get("points") or []
            if not stroke:
                continue
        points = [_as_point(p) for p in stroke]
        if points:
            strokes.append(points)
    if not strokes:
        raise ValueError("All strokes were empty")
    return strokes


def polyline_length(points: Stroke) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def resample_polyline(points: Stroke, count: int) -> Stroke:
    if count <= 0:
        return []
    if len(points) == 1:
        return [points[0]] * count
    if count == 1:
        return [points[0]]

    distances = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distances.append(distances[-1] + math.hypot(x1 - x0, y1 - y0))
    total = distances[-1]
    if total <= 1e-12:
        return [points[0]] * count

    samples: Stroke = []
    target = 0.0
    step = total / (count - 1)
    index = 0
    for i in range(count):
        if i == count - 1:
            samples.append(points[-1])
            break
        while index < len(distances) - 2 and distances[index + 1] < target:
            index += 1
        span = distances[index + 1] - distances[index]
        t = 0.0 if span <= 1e-12 else (target - distances[index]) / span
        x0, y0 = points[index]
        x1, y1 = points[index + 1]
        samples.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
        target += step
    return samples


def allocate_counts(lengths: list[float], total_points: int) -> list[int]:
    if not lengths:
        return []
    if sum(lengths) <= 1e-12:
        base = total_points // len(lengths)
        counts = [base] * len(lengths)
        for i in range(total_points - sum(counts)):
            counts[i] += 1
        return [max(1, c) if total_points >= len(lengths) else c for c in counts]

    raw = [length / sum(lengths) * total_points for length in lengths]
    counts = [max(1, int(value)) for value in raw]
    while sum(counts) > total_points:
        idx = max(range(len(counts)), key=lambda i: (counts[i], raw[i]))
        if counts[idx] <= 1:
            break
        counts[idx] -= 1
    leftovers = total_points - sum(counts)
    remainders = sorted(
        range(len(raw)),
        key=lambda i: (raw[i] - int(raw[i]), raw[i]),
        reverse=True,
    )
    for i in range(max(0, leftovers)):
        counts[remainders[i % len(counts)]] += 1
    return counts


def resample_strokes(strokes: list[Stroke], point_count: int) -> list[Stroke]:
    lengths = [polyline_length(stroke) or float(len(stroke)) for stroke in strokes]
    counts = allocate_counts(lengths, point_count)
    return [resample_polyline(stroke, count) for stroke, count in zip(strokes, counts)]


def iter_points(strokes: Iterable[Stroke]) -> Iterable[Point]:
    for stroke in strokes:
        yield from stroke


def bounding_box(strokes: list[Stroke]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in iter_points(strokes)]
    ys = [y for _, y in iter_points(strokes)]
    return min(xs), min(ys), max(xs), max(ys)


def normalize_strokes(strokes: list[Stroke]) -> list[Stroke]:
    min_x, min_y, max_x, max_y = bounding_box(strokes)
    width = max_x - min_x
    height = max_y - min_y
    scale = max(width, height)
    if scale <= 1e-12:
        return [[(0.5, 0.5) for _ in stroke] for stroke in strokes]

    offset_x = (scale - width) / 2.0
    offset_y = (scale - height) / 2.0
    normalized = []
    for stroke in strokes:
        normalized.append(
            [
                ((x - min_x + offset_x) / scale, (y - min_y + offset_y) / scale)
                for x, y in stroke
            ]
        )
    return normalized


def flatten_points(strokes: list[Stroke]) -> list[Point]:
    return [point for stroke in strokes for point in stroke]


def direction_histogram(points: list[Point], bins: int = DIRECTION_BINS) -> list[float]:
    hist = [0.0] * bins
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        if math.hypot(dx, dy) <= 1e-12:
            continue
        angle = math.atan2(dy, dx)
        index = int(((angle + math.pi) / (2 * math.pi)) * bins) % bins
        hist[index] += 1.0
    total = sum(hist)
    if total <= 0:
        return hist
    return [value / total for value in hist]


def occupancy_grid(points: list[Point], size: int = GRID_SIZE) -> list[float]:
    grid = [0.0] * (size * size)
    for x, y in points:
        col = min(size - 1, max(0, int(x * size)))
        row = min(size - 1, max(0, int(y * size)))
        grid[row * size + col] += 1.0
    total = sum(grid)
    if total <= 0:
        return grid
    return [value / total for value in grid]


def mean_curvature(points: list[Point]) -> float:
    if len(points) < 3:
        return 0.0
    turns = []
    for i in range(1, len(points) - 1):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        v1 = (x1 - x0, y1 - y0)
        v2 = (x2 - x1, y2 - y1)
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 <= 1e-12 or n2 <= 1e-12:
            continue
        dot = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        turns.append(abs(math.acos(dot)))
    if not turns:
        return 0.0
    return sum(turns) / len(turns)


def histogram_intersection(a: list[float], b: list[float]) -> float:
    return sum(min(x, y) for x, y in zip(a, b))


def rmse(a: list[Point], b: list[Point]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    total = 0.0
    for (x0, y0), (x1, y1) in zip(a[:n], b[:n]):
        total += (x0 - x1) ** 2 + (y0 - y1) ** 2
    return math.sqrt(total / n)


def l2_distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def shape_similarity(a: list[Point], b: list[Point]) -> float:
    forward = rmse(a, b)
    backward = rmse(a, list(reversed(b)))
    return max(0.0, 1.0 - min(forward, backward) / math.sqrt(2.0))


def compare_descriptors(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    shape = shape_similarity(a["resampled_points"], b["resampled_points"])
    direction = histogram_intersection(a["direction_histogram"], b["direction_histogram"])
    occupancy = max(
        0.0,
        1.0 - l2_distance(a["occupancy_grid"], b["occupancy_grid"]) / math.sqrt(2.0),
    )
    n1 = max(1, int(a["stroke_count"]))
    n2 = max(1, int(b["stroke_count"]))
    strokes = 1.0 - abs(n1 - n2) / max(n1, n2)
    score = (
        SHAPE_WEIGHT * shape
        + DIRECTION_WEIGHT * direction
        + OCCUPANCY_WEIGHT * occupancy
        + STROKE_WEIGHT * strokes
    )
    return {
        "score": round(score, 6),
        "shape": round(shape, 6),
        "direction": round(direction, 6),
        "occupancy": round(occupancy, 6),
        "strokes": round(strokes, 6),
    }


def build_descriptors(
    strokes: Any,
    point_count: int = DEFAULT_POINT_COUNT,
) -> dict[str, Any]:
    """Turn raw pad strokes into a JSON-serializable descriptor object."""
    point_count = clamp_point_count(point_count)
    parsed = parse_strokes(strokes)
    normalized = normalize_strokes(parsed)
    resampled = resample_strokes(normalized, point_count)
    points = flatten_points(resampled)
    raw_count = sum(len(stroke) for stroke in parsed)
    min_x, min_y, max_x, max_y = bounding_box(parsed)
    width = max_x - min_x
    height = max_y - min_y

    return {
        "point_count": point_count,
        "raw_point_count": raw_count,
        "stroke_count": len(parsed),
        "stroke_lengths": [round(polyline_length(stroke), 6) for stroke in resampled],
        "aspect_ratio": round(width / height, 6) if height > 1e-12 else None,
        "path_length": round(polyline_length(points), 6),
        "mean_curvature": round(mean_curvature(points), 6),
        "start": [round(points[0][0], 6), round(points[0][1], 6)],
        "end": [round(points[-1][0], 6), round(points[-1][1], 6)],
        "direction_histogram": [round(v, 6) for v in direction_histogram(points)],
        "occupancy_grid": [round(v, 6) for v in occupancy_grid(points)],
        "resampled_points": [[round(x, 6), round(y, 6)] for x, y in points],
        "resampled_strokes": [
            [[round(x, 6), round(y, 6)] for x, y in stroke] for stroke in resampled
        ],
    }


class DrawingFeatureStore:
    """Persist drawing descriptors and compare a new sketch against them."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        point_count: int = DEFAULT_POINT_COUNT,
    ) -> None:
        self.db_path = Path(db_path)
        self.point_count = clamp_point_count(point_count)
        self._data = {"point_count": self.point_count, "drawings": {}}
        self._load()

    def _load(self) -> None:
        if not self.db_path.exists():
            return
        loaded = json.loads(self.db_path.read_text())
        self._data["drawings"] = loaded.get("drawings", {})
        stored_count = loaded.get("point_count", self.point_count)
        if stored_count != self.point_count and self._data["drawings"]:
            raise ValueError(
                f"Database was built with point_count={stored_count}, "
                f"but this store is using {self.point_count}"
            )
        self._data["point_count"] = self.point_count

    def save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(json.dumps(self._data, indent=2))

    def describe(self, strokes: Any) -> dict[str, Any]:
        return build_descriptors(strokes, self.point_count)

    def get(self, drawing_id: str) -> dict[str, Any]:
        try:
            return self._data["drawings"][drawing_id]
        except KeyError as exc:
            raise KeyError(f"Unknown drawing {drawing_id!r}") from exc

    def list_ids(self) -> list[str]:
        return list(self._data["drawings"])

    def add(
        self,
        strokes: Any,
        drawing_id: str | None = None,
        *,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a new drawing. Pass drawing_id or one is generated."""
        drawing_id = drawing_id or uuid.uuid4().hex[:12]
        if drawing_id in self._data["drawings"]:
            raise ValueError(f"Drawing {drawing_id!r} already exists; use update()")
        now = utc_now()
        record = {
            "id": drawing_id,
            "label": label,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
            "descriptors": self.describe(strokes),
        }
        self._data["drawings"][drawing_id] = record
        self.save()
        return record

    def update(
        self,
        drawing_id: str,
        strokes: Any | None = None,
        *,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace strokes and/or metadata on an existing drawing."""
        record = self.get(drawing_id)
        if strokes is not None:
            record["descriptors"] = self.describe(strokes)
        if label is not None:
            record["label"] = label
        if metadata is not None:
            record["metadata"] = metadata
        record["updated_at"] = utc_now()
        self._data["drawings"][drawing_id] = record
        self.save()
        return record

    def upsert(
        self,
        strokes: Any,
        drawing_id: str,
        *,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if drawing_id in self._data["drawings"]:
            return self.update(drawing_id, strokes, label=label, metadata=metadata)
        return self.add(strokes, drawing_id, label=label, metadata=metadata)

    def compare(
        self,
        strokes: Any,
        *,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Score a new drawing against every stored descriptor."""
        if not self._data["drawings"]:
            return []
        query = self.describe(strokes)
        matches = []
        for drawing_id, record in self._data["drawings"].items():
            parts = compare_descriptors(query, record["descriptors"])
            matches.append(
                {
                    "id": drawing_id,
                    "label": record.get("label"),
                    **parts,
                }
            )
        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[: max(1, top_k)]

    def score_all(self, strokes: Any) -> list[dict[str, Any]]:
        """Return feature scores for every stored drawing, best first."""
        if not self._data["drawings"]:
            return []
        return self.compare(strokes, top_k=len(self._data["drawings"]))


def load_strokes_file(path: Path) -> Any:
    return json.loads(path.read_text())


def dump(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _regular_polygon(sides: int, points_per_side: int, *, jitter: float = 0.0, offset: Point = (0.0, 0.0), scale: float = 1.0) -> list[Point]:
    vertices = [
        (
            math.cos(2 * math.pi * i / sides - math.pi / 2),
            math.sin(2 * math.pi * i / sides - math.pi / 2),
        )
        for i in range(sides)
    ]
    vertices.append(vertices[0])
    points: list[Point] = []
    seed = 0.17 + sides * 0.03 + jitter
    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        for i in range(points_per_side):
            t = i / points_per_side
            noise_x = jitter * math.sin(seed * 17 + i * 0.4)
            noise_y = jitter * math.cos(seed * 11 + i * 0.6)
            points.append(
                (
                    offset[0] + scale * (x0 + t * (x1 - x0) + noise_x),
                    offset[1] + scale * (y0 + t * (y1 - y0) + noise_y),
                )
            )
    return points


def run_demo(db_path: Path, point_count: int) -> None:
    if db_path.exists():
        db_path.unlink()
    store = DrawingFeatureStore(db_path, point_count=point_count)

    square = [_regular_polygon(4, 80, offset=(10, 20), scale=40)]
    messy_square = [_regular_polygon(4, 140, jitter=0.04, offset=(200, 80), scale=18)]
    triangle = [_regular_polygon(3, 90, offset=(3, 4), scale=25)]
    circle = [_regular_polygon(24, 20, offset=(0, 0), scale=30)]

    store.add(square, "square", label="square")
    store.add(triangle, "triangle", label="triangle")
    store.add(circle, "circle", label="circle")
    print(f"Stored {len(store.list_ids())} drawings with {point_count} resampled points each")

    matches = store.compare(messy_square, top_k=3)
    dump({"query": "noisy translated square", "matches": matches})

    updated_square = [_regular_polygon(4, 60, offset=(0, 0), scale=12)]
    store.update("square", updated_square, label="square-redrawn")
    print("Updated drawing 'square'")
    dump(store.compare(messy_square, top_k=1))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="JSON database path")
    common.add_argument(
        "--points",
        type=int,
        default=DEFAULT_POINT_COUNT,
        help=f"Resampled point count ({MIN_POINT_COUNT}-{MAX_POINT_COUNT})",
    )

    parser = argparse.ArgumentParser(description="Store and compare pad drawings")
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser(
        "describe", parents=[common], help="Print descriptors for a stroke file"
    )
    describe.add_argument("strokes", type=Path)

    add = sub.add_parser("add", parents=[common], help="Store a new drawing")
    add.add_argument("drawing_id")
    add.add_argument("strokes", type=Path)
    add.add_argument("--label")

    update = sub.add_parser(
        "update", parents=[common], help="Replace strokes on an existing drawing"
    )
    update.add_argument("drawing_id")
    update.add_argument("strokes", type=Path)
    update.add_argument("--label")

    compare = sub.add_parser(
        "compare", parents=[common], help="Compare a new drawing to the database"
    )
    compare.add_argument("strokes", type=Path)
    compare.add_argument("--top", type=int, default=5)

    sub.add_parser(
        "demo", parents=[common], help="Run a built-in square/triangle/circle comparison"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "demo":
        run_demo(args.db, args.points)
        return 0

    store = DrawingFeatureStore(args.db, point_count=args.points)
    strokes = load_strokes_file(args.strokes)
    if args.command == "describe":
        dump(store.describe(strokes))
    elif args.command == "add":
        dump(store.add(strokes, args.drawing_id, label=args.label))
    elif args.command == "update":
        dump(store.update(args.drawing_id, strokes, label=args.label))
    elif args.command == "compare":
        dump(store.compare(strokes, top_k=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
