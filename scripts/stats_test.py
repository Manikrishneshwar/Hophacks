"""Check today's request counts for the caretaker Today tab.

    python scripts/stats_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import _daily_request_stats, _request_label  # noqa: E402
from server.storage import CaptureStore  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    check(_request_label({"confirmed": True, "tag": "water", "detail": None}) == "water",
          "water tag should count as water")
    check(_request_label({"confirmed": True, "tag": "food", "detail": "pizza"}) == "pizza",
          "specific detail should win over tag")
    check(_request_label({"confirmed": False, "tag": "water"}) is None,
          "unconfirmed requests must not count")
    check(_request_label({"confirmed": True, "tag": "play"}) is None,
          "shape-game tags should be skipped")
    print("labels           water / pizza / skip rules ok")

    data_dir = Path(tempfile.mkdtemp(prefix="ink-stats-"))
    store = CaptureStore(data_dir / "captures", data_dir / "index.jsonl", data_dir / "answers.jsonl")
    day = "2026-09-19"
    rows = [
        {"id": f"{day}T10-00-00-001", "analysis": {"confirmed": True, "tag": "water", "detail": None}},
        {"id": f"{day}T11-00-00-002", "analysis": {"confirmed": True, "tag": "food", "detail": "pizza"}},
        {"id": f"{day}T12-00-00-003", "analysis": {"confirmed": True, "tag": "water", "detail": "tea"}},
        {"id": f"{day}T13-00-00-004", "analysis": {"confirmed": True, "tag": "water"}},
        {"id": f"{day}T14-00-00-005", "analysis": {"confirmed": False, "tag": "help"}},
        {"id": "2026-09-18T10-00-00-006", "analysis": {"confirmed": True, "tag": "food", "detail": "soup"}},
    ]
    for row in rows:
        store.index_path.parent.mkdir(parents=True, exist_ok=True)
        with store.index_path.open("a", encoding="utf-8") as index:
            index.write(json.dumps(row) + "\n")

    # Point the module store at our temp index by using for_day on this store.
    today = store.for_day(day)
    check(len(today) == 5, f"expected 5 today rows, got {len(today)}")
    stats = _daily_request_stats(today)
    print(f"stats            {stats}")
    by_label = {item["label"]: item["count"] for item in stats["items"]}
    check(by_label.get("water") == 2, f"water count {by_label}")
    check(by_label.get("pizza") == 1, f"pizza count {by_label}")
    check(by_label.get("tea") == 1, f"tea count {by_label}")
    check("soup" not in by_label, "yesterday's soup leaked into today")
    check(stats["total"] == 4, f"total {stats['total']}")
    check(stats["type"] == "stats" and stats["date"] == day, "payload shape wrong")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
