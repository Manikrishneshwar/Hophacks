"""Hold-one-out check that labelled priors match themselves.

    python scripts/priors_test.py

Does not call any API. Each drawing in data/priors is scored the way recognize
does: the best remaining template per tag. Digits are stored as templates but
are not spoken intents; they are checked 1 vs 2 among themselves.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from drawing_features import DrawingFeatureStore, build_descriptors, compare_descriptors  # noqa: E402
from recognize import FEATURE_BLEND_MIN, FEATURE_BLEND_WEIGHT, IntentRecognizer  # noqa: E402
from seed_drawings import from_priors  # noqa: E402

# Nearest-single-drawing matching is noisier than this; 0.80 is still well
# above chance for four spoken tags, and leaves room for a wobbly apple.
MIN_TAG_ACCURACY = 0.60


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    raise SystemExit(1)


def best_against(
    query: dict,
    group: list[dict],
    descriptors: dict[str, dict],
    skip_id: str,
) -> tuple[float, str | None]:
    best = 0.0
    best_id = None
    for row in group:
        if row["drawing_id"] == skip_id:
            continue
        score = compare_descriptors(query, descriptors[row["drawing_id"]])["score"]
        if score > best:
            best = score
            best_id = row["drawing_id"]
    return best, best_id


def main() -> int:
    priors_dir = ROOT / "data" / "priors"
    rows = from_priors(priors_dir)
    if not rows:
        fail(f"no labelled priors under {priors_dir}")

    descriptors = {
        row["drawing_id"]: build_descriptors(row["strokes"], 100) for row in rows
    }
    by_tag: dict[str, list[dict]] = defaultdict(list)
    by_digit: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_tag[row["tag_id"]].append(row)
        if row["tag_id"] == "_digit" and row["detail"] in {"1", "2"}:
            by_digit[row["detail"]].append(row)

    spoken = [row for row in rows if row["tag_id"] != "_digit"]
    spoken_tags = sorted({row["tag_id"] for row in spoken})
    hits = 0
    for held in spoken:
        query = descriptors[held["drawing_id"]]
        scores = {
            tag_id: best_against(query, by_tag[tag_id], descriptors, held["drawing_id"])
            for tag_id in spoken_tags
        }
        winner = max(scores, key=lambda tag_id: scores[tag_id][0])
        score, matched = scores[winner]
        want = held["tag_id"]
        mark = "ok" if winner == want else "MISS"
        if winner == want:
            hits += 1
        print(
            f"{mark:<4}  {held['drawing_id']:<10} -> {matched or '-':<10} "
            f"tag={winner:<7} want={want:<7} score={score:.2f}"
        )

    accuracy = hits / len(spoken) if spoken else 0.0
    print(f"tag    {hits}/{len(spoken)} held-out drawings voted for their own tag ({accuracy:.0%})")
    if accuracy < MIN_TAG_ACCURACY:
        fail(f"tag hold-one-out {accuracy:.0%} is under {MIN_TAG_ACCURACY:.0%}")
    print("ok     spoken priors hold-one-out by tag")

    digit_hits = 0
    digit_rows = [row for row in rows if row["tag_id"] == "_digit"]
    for held in digit_rows:
        query = descriptors[held["drawing_id"]]
        scores = {
            digit: best_against(query, by_digit[digit], descriptors, held["drawing_id"])
            for digit in ("1", "2")
        }
        winner = max(scores, key=lambda digit: scores[digit][0])
        score, matched = scores[winner]
        want = held["detail"]
        mark = "ok" if winner == want else "MISS"
        if winner == want:
            digit_hits += 1
        print(
            f"{mark:<4}  {held['drawing_id']:<10} -> {matched or '-':<10} "
            f"digit={winner} want={want} score={score:.2f}"
        )
    if digit_rows and digit_hits != len(digit_rows):
        fail(f"digit hold-one-out {digit_hits}/{len(digit_rows)} missed a 1 vs 2")
    print(f"ok     {digit_hits}/{len(digit_rows)} digit priors hold-one-out 1 vs 2")

    tmp = Path(tempfile.mkdtemp(prefix="ink-digit-"))
    try:
        shutil.copy(ROOT / "drawing_tags.json", tmp / "drawing_tags.json")
        store = DrawingFeatureStore(tmp / "drawings_db.json")
        ones = by_digit.get("1") or []
        if not ones:
            fail("no digit-1 priors to check")
        store.add(
            ones[0]["strokes"],
            ones[0]["drawing_id"],
            label=ones[0]["detail"],
            metadata=ones[0]["metadata"],
        )
        recognizer = IntentRecognizer(root=tmp, model=object())  # type: ignore[arg-type]
        result = recognizer.interpret(ones[0]["strokes"], offline=True, update_memory=False)
        if result.top_tag is not None:
            fail(f"a digit prior must not become intent {result.top_tag!r}")
        if result.digit != "1":
            fail(f"a clean 1 prior should still be a 1, got {result.digit!r}")
        if result.digit_source != "geometry":
            fail(f"digit source should be geometry, got {result.digit_source!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ok     digit priors are not spoken tags")

    water_blended = (1.0 - FEATURE_BLEND_WEIGHT) * 0.40 + FEATURE_BLEND_WEIGHT * 1.0
    if water_blended >= 0.95:
        fail(f"blend weight {FEATURE_BLEND_WEIGHT} would overturn a 0.95 food rank")
    if FEATURE_BLEND_MIN > 0.7:
        fail("FEATURE_BLEND_MIN is so high the priors will never mix in")
    print("ok     blend constants cannot overturn a clear Gemini rank")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
