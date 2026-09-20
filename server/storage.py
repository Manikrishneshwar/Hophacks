"""Persistence for captured drawings.

Every capture produces three things:

    data/captures/<timestamp>.png    the flattened drawing
    data/captures/<timestamp>.json   the raw strokes that produced it
    data/index.jsonl                 one metadata record appended per capture

The PNG is what step 2 sends to an image API. The stroke file keeps pressure and
per-point timing so the same drawing can be re-rendered at any resolution later.
The index is the thing a training script reads to enumerate the dataset.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

# Windows forbids ':' in filenames, so the usual ISO-8601 separators become
# dashes. The result still sorts chronologically as plain text.
_FILENAME_TIME_FORMAT = "%Y-%m-%dT%H-%M-%S"


@dataclass
class CaptureRecord:
    """One row of the dataset index."""

    id: str
    created_at: str
    session_id: str
    trigger: str
    png: str
    strokes: str
    width: int
    height: int
    dpr: float
    stroke_count: int
    point_count: int
    duration_ms: int
    idle_timeout_ms: int
    png_bytes: int
    # Filled in after recognition: spoken text, tag, and whether it was confirmed.
    analysis: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaptureStore:
    """Writes captures to disk, and optionally mirrors metadata elsewhere.

    ``sinks`` is the seam for step 2. A sink is any object with
    ``write(record)``; adding a Postgres/TigerData sink there gives you SQL over
    the dataset without touching the capture path. Sink failures are swallowed
    on purpose: a database being down must never cost you a drawing.
    """

    def __init__(self, capture_dir: Path, index_path: Path, answers_path: Path) -> None:
        self.capture_dir = capture_dir
        self.index_path = index_path
        self.answers_path = answers_path
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.sinks: list[Any] = []

    def _reserve_id(self) -> str:
        """A filename-safe, sortable, collision-free id for a new capture.

        The server mints this rather than the tablet so that a tablet with a
        skewed clock cannot produce misordered or duplicate filenames.
        """
        now = datetime.now()
        base = f"{now.strftime(_FILENAME_TIME_FORMAT)}-{now.microsecond // 1000:03d}"
        candidate, suffix = base, 1
        while (self.capture_dir / f"{candidate}.png").exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        # Reserve the name so a concurrent capture picks a different one.
        (self.capture_dir / f"{candidate}.png").touch()
        return candidate

    def save(
        self,
        png: bytes,
        strokes: dict[str, Any],
        meta: dict[str, Any],
    ) -> CaptureRecord:
        with self._lock:
            capture_id = self._reserve_id()

        png_path = self.capture_dir / f"{capture_id}.png"
        strokes_path = self.capture_dir / f"{capture_id}.json"
        created_at = datetime.now().astimezone().isoformat(timespec="milliseconds")

        stroke_list = strokes.get("strokes", [])
        record = CaptureRecord(
            id=capture_id,
            created_at=created_at,
            session_id=str(meta.get("session_id", "unknown")),
            trigger=str(meta.get("trigger", "idle")),
            png=png_path.name,
            strokes=strokes_path.name,
            width=int(meta.get("width", 0)),
            height=int(meta.get("height", 0)),
            dpr=float(meta.get("dpr", 1)),
            stroke_count=len(stroke_list),
            point_count=sum(len(s.get("points", [])) for s in stroke_list),
            duration_ms=int(meta.get("duration_ms", 0)),
            idle_timeout_ms=int(meta.get("idle_timeout_ms", config.IDLE_TIMEOUT_MS)),
            png_bytes=len(png),
        )

        png_path.write_bytes(png)
        strokes_path.write_text(
            json.dumps({**record.as_dict(), **strokes}, separators=(",", ":")),
            encoding="utf-8",
        )
        with self._lock:
            with self.index_path.open("a", encoding="utf-8") as index:
                index.write(json.dumps(record.as_dict(), separators=(",", ":")) + "\n")

        for sink in self.sinks:
            try:
                sink.write(record)
            except Exception as exc:  # noqa: BLE001 - a sink must not break capture
                print(f"[storage] sink {type(sink).__name__} failed: {exc}")

        return record

    def update_analysis(self, capture_id: str, analysis: dict[str, Any]) -> None:
        """Write the spoken result onto the stroke file and the index row."""
        with self._lock:
            json_path = self.capture_dir / f"{capture_id}.json"
            if json_path.exists():
                data = json.loads(json_path.read_text(encoding="utf-8"))
                data["analysis"] = analysis
                json_path.write_text(
                    json.dumps(data, separators=(",", ":")),
                    encoding="utf-8",
                )
            if not self.index_path.exists():
                return
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
            rewritten: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    rewritten.append(line)
                    continue
                if row.get("id") == capture_id:
                    row["analysis"] = analysis
                rewritten.append(json.dumps(row, separators=(",", ":")))
            self.index_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    def save_answer(
        self,
        question_id: str,
        question: str,
        value: str,
        session_id: str = "unknown",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a tap answer to its own log.

        Kept separate from the capture index because answers are about a
        question, not a drawing, and there may be none or several per capture.
        """
        entry = {
            "id": question_id,
            "answered_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "question": question,
            "answer": value,
            "session_id": session_id,
            "context": context or {},
        }
        with self._lock:
            with self.answers_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return entry

    def recent_answers(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.answers_path.exists():
            return []
        with self._lock:
            lines = self.answers_path.read_text(encoding="utf-8").splitlines()
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent captures first, for the viewer's gallery."""
        if not self.index_path.exists():
            return []
        with self._lock:
            lines = self.index_path.read_text(encoding="utf-8").splitlines()

        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) >= limit:
                break
        return records

    def for_day(self, day: str | None = None) -> list[dict[str, Any]]:
        """Today's captures, newest first. Ids sort as ISO dates, so we stop
        as soon as the filename rolls to an earlier day."""
        stamp = day or datetime.now().strftime("%Y-%m-%d")
        if not self.index_path.exists():
            return []
        with self._lock:
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(row.get("id") or "").startswith(stamp):
                if records:
                    break
                continue
            records.append(row)
        return records


store = CaptureStore(config.CAPTURE_DIR, config.INDEX_PATH, config.ANSWERS_PATH)
