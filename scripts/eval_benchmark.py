"""Collect labeled pad drawings and score ranking on a frozen eval set.

    python scripts/eval_benchmark.py collect
    python scripts/eval_benchmark.py collect --label
    python scripts/eval_benchmark.py import-confirmed
    python scripts/eval_benchmark.py add <capture-id> --tag food --detail pizza
    python scripts/eval_benchmark.py list
    python scripts/eval_benchmark.py run
    python scripts/eval_benchmark.py run --offline

Live collect waits for the next tablet capture (run.py must be up), copies the
PNG and strokes into eval/drawings, and stores a gold label. `run` ranks each
item with the same IntentRecognizer the pad uses (memory writes off) and writes
eval/reports/latest.html for judges.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import config, pipeline  # noqa: E402

INTENT_TAGS = ("water", "food", "help", "rest")
ALL_TAGS = INTENT_TAGS + ("play",)
SKIP_IMPORT_TAGS = {"game", "play", "yes", "no"}

DEFAULT_CUES: list[dict[str, str]] = [
    {"tag": "water", "detail": "", "digit": "", "prompt": "Draw a cup or glass of water"},
    {"tag": "water", "detail": "", "digit": "", "prompt": "Draw water again (a cup is fine)"},
    {"tag": "food", "detail": "apple", "digit": "", "prompt": "Draw an apple"},
    {"tag": "food", "detail": "pizza", "digit": "", "prompt": "Draw a pizza / pizza slice"},
    {"tag": "food", "detail": "soup", "digit": "", "prompt": "Draw a bowl of soup"},
    {"tag": "help", "detail": "", "digit": "", "prompt": "Draw a help sign (cross, plus, or H)"},
    {"tag": "help", "detail": "", "digit": "", "prompt": "Draw help again"},
    {"tag": "rest", "detail": "", "digit": "", "prompt": "Draw rest / a bed / sleep"},
    {"tag": "play", "detail": "", "digit": "1", "prompt": "Draw the digit 1"},
    {"tag": "play", "detail": "", "digit": "2", "prompt": "Draw the digit 2"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def eval_dir(path: Path | None = None) -> Path:
    return Path(path) if path is not None else ROOT / "eval"


def catalog_path(base: Path) -> Path:
    return base / "catalog.json"


def drawings_dir(base: Path) -> Path:
    return base / "drawings"


def reports_dir(base: Path) -> Path:
    return base / "reports"


def load_catalog(base: Path) -> dict[str, Any]:
    path = catalog_path(base)
    if not path.exists():
        return {"updated_at": utc_now(), "items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_catalog(base: Path, data: dict[str, Any]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = utc_now()
    path = catalog_path(base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_index() -> list[dict[str, Any]]:
    if not config.INDEX_PATH.exists():
        return []
    records = []
    with config.INDEX_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def catalog_ids(data: dict[str, Any]) -> set[str]:
    return {str(item.get("id") or item.get("source_id")) for item in data.get("items") or []}


def load_eval_strokes(path: Path) -> Any:
    """Tablet capture JSON uses stroke dicts; ranking wants polylines."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("strokes"), list):
        strokes = raw["strokes"]
        if strokes and isinstance(strokes[0], dict) and "points" in strokes[0]:
            return pipeline.build_payload(strokes)["polylines"]
    return raw


def copy_capture(record: dict[str, Any], base: Path) -> tuple[str, str]:
    drawings = drawings_dir(base)
    drawings.mkdir(parents=True, exist_ok=True)
    png_src = config.CAPTURE_DIR / record["png"]
    json_src = config.CAPTURE_DIR / record["strokes"]
    if not png_src.exists() or not json_src.exists():
        raise FileNotFoundError(f"missing files for {record['id']}")
    png_name = f"{record['id']}.png"
    json_name = f"{record['id']}.json"
    shutil.copy2(png_src, drawings / png_name)
    shutil.copy2(json_src, drawings / json_name)
    return f"drawings/{png_name}", f"drawings/{json_name}"


def make_item(
    record: dict[str, Any],
    *,
    tag: str,
    detail: str = "",
    digit: str = "",
    note: str = "",
    png: str,
    strokes: str,
) -> dict[str, str]:
    return {
        "id": record["id"],
        "source_id": record["id"],
        "png": png,
        "strokes": strokes,
        "tag": tag,
        "detail": (detail or "").strip().lower(),
        "digit": str(digit or "").strip(),
        "note": note.strip(),
        "added_at": utc_now(),
    }


def upsert_item(data: dict[str, Any], item: dict[str, str]) -> str:
    items = data.setdefault("items", [])
    for index, existing in enumerate(items):
        if existing.get("id") == item["id"]:
            items[index] = item
            return "updated"
    items.append(item)
    return "added"


def parse_tag(value: str) -> str:
    token = (value or "").strip().lower()
    aliases = {
        "w": "water",
        "drink": "water",
        "f": "food",
        "eat": "food",
        "h": "help",
        "r": "rest",
        "sleep": "rest",
        "p": "play",
        "game": "play",
        "digit": "play",
    }
    tag = aliases.get(token, token)
    if tag not in ALL_TAGS:
        raise ValueError(f"tag must be one of {', '.join(ALL_TAGS)}")
    return tag


def parse_label_line(line: str) -> dict[str, str] | None:
    text = line.strip().lower()
    if not text or text in {"s", "skip"}:
        return None
    if text in {"q", "quit"}:
        raise SystemExit(0)
    parts = text.split()
    tag = parse_tag(parts[0])
    rest = parts[1] if len(parts) > 1 else ""
    digit = rest if rest in {"1", "2"} else ""
    detail = "" if digit else rest
    return {"tag": tag, "detail": detail, "digit": digit}


def open_png(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def wait_for_capture(seen: set[str], timeout_s: float | None = None) -> dict[str, Any]:
    started = time.time()
    while True:
        for record in load_index():
            if record["id"] not in seen:
                return record
        if timeout_s is not None and time.time() - started > timeout_s:
            raise TimeoutError("no new capture")
        time.sleep(0.4)


def cmd_list(base: Path) -> int:
    data = load_catalog(base)
    items = data.get("items") or []
    print(f"{base}\n{len(items)} labeled drawing(s)\n")
    if not items:
        print("Nothing yet. Draw on the tablet, then:")
        print("  .\\.venv\\Scripts\\python.exe scripts\\eval_benchmark.py collect")
        return 0
    header = f"{'id':<28} {'tag':<6} {'detail':<10} {'digit':<5} note"
    print(header)
    print("-" * len(header))
    counts: dict[str, int] = {}
    for item in items:
        tag = item.get("tag") or "-"
        counts[tag] = counts.get(tag, 0) + 1
        print(
            f"{item['id']:<28} {tag:<6} {(item.get('detail') or '-'):<10} "
            f"{(item.get('digit') or '-'):<5} {item.get('note') or ''}"
        )
    print("\n" + ", ".join(f"{tag}={counts[tag]}" for tag in sorted(counts)))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    records = {row["id"]: row for row in load_index()}
    record = records.get(args.capture)
    if record is None:
        print(f"No capture {args.capture!r} in {config.INDEX_PATH}")
        return 1
    tag = parse_tag(args.tag)
    data = load_catalog(args.dir)
    png, strokes = copy_capture(record, args.dir)
    item = make_item(
        record,
        tag=tag,
        detail=args.detail or "",
        digit=args.digit or "",
        note=args.note or "",
        png=png,
        strokes=strokes,
    )
    action = upsert_item(data, item)
    save_catalog(args.dir, data)
    print(f"{action} {item['id']}  {tag}"
          + (f"/{item['detail']}" if item["detail"] else "")
          + (f" digit={item['digit']}" if item["digit"] else ""))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    data = load_catalog(args.dir)
    before = len(data.get("items") or [])
    data["items"] = [item for item in data.get("items") or [] if item.get("id") != args.capture]
    if len(data["items"]) == before:
        print(f"Not in catalog: {args.capture}")
        return 1
    save_catalog(args.dir, data)
    print(f"removed {args.capture}")
    return 0


def cmd_import_confirmed(args: argparse.Namespace) -> int:
    data = load_catalog(args.dir)
    known = catalog_ids(data)
    added = 0
    skipped = 0
    for record in load_index():
        analysis = record.get("analysis") or {}
        if not analysis.get("confirmed"):
            continue
        tag = (analysis.get("tag") or "").strip().lower()
        if not tag:
            continue
        if tag in SKIP_IMPORT_TAGS and not args.include_game:
            skipped += 1
            continue
        if tag not in ALL_TAGS:
            skipped += 1
            continue
        if record["id"] in known and not args.force:
            skipped += 1
            continue
        try:
            png, strokes = copy_capture(record, args.dir)
        except FileNotFoundError as exc:
            print(f"skip {record['id']}: {exc}")
            skipped += 1
            continue
        detail = (analysis.get("detail") or "").strip().lower()
        digit = detail if tag == "play" and detail in {"1", "2"} else ""
        if digit:
            detail = ""
        item = make_item(
            record,
            tag=tag,
            detail=detail,
            digit=digit,
            note="imported from a confirmed pad tap; review if the drawing was the follow-up, not the intent",
            png=png,
            strokes=strokes,
        )
        upsert_item(data, item)
        known.add(item["id"])
        added += 1
        print(f"import {item['id']}  {tag}" + (f"/{detail}" if detail else ""))
    save_catalog(args.dir, data)
    print(f"\n{added} added, {skipped} skipped. Review with: collect --label")
    return 0


def cmd_collect_label(args: argparse.Namespace) -> int:
    data = load_catalog(args.dir)
    known = catalog_ids(data)
    pending = [row for row in load_index() if row["id"] not in known]
    if args.relabel:
        pending = load_index()
    if not pending:
        print("No unlabeled captures. Draw on the tablet, or use collect (live).")
        return 0
    print(f"{len(pending)} capture(s). Type: water | food pizza | play 1 | skip | quit\n")
    for index, record in enumerate(pending, start=1):
        analysis = record.get("analysis") or {}
        png_path = config.CAPTURE_DIR / record["png"]
        print(
            f"[{index}/{len(pending)}] {record['id']}\n"
            f"  file: {png_path}\n"
            f"  live guess: {analysis.get('tag') or '-'} / {analysis.get('detail') or '-'}  "
            f"confirmed={analysis.get('confirmed')}"
        )
        if args.open:
            open_png(png_path)
        try:
            line = input("  label> ").strip()
        except EOFError:
            print()
            break
        parsed = parse_label_line(line)
        if parsed is None:
            print("  skipped")
            continue
        png, strokes = copy_capture(record, args.dir)
        item = make_item(record, png=png, strokes=strokes, **parsed)
        action = upsert_item(data, item)
        save_catalog(args.dir, data)
        print(f"  {action} as {item['tag']}"
              + (f"/{item['detail']}" if item["detail"] else "")
              + (f" digit={item['digit']}" if item["digit"] else ""))
    return 0


def cmd_collect_live(args: argparse.Namespace) -> int:
    if not config.INDEX_PATH.exists():
        print(f"No captures yet at {config.INDEX_PATH}. Start the pad with:")
        print("  .\\.venv\\Scripts\\python.exe run.py")
        return 1
    cues = list(DEFAULT_CUES)
    if args.tag:
        tag = parse_tag(args.tag)
        cues = [cue for cue in cues if cue["tag"] == tag]
        if args.count:
            template = {
                "tag": tag,
                "detail": args.detail or "",
                "digit": args.digit or "",
                "prompt": f"Draw {tag}"
                + (f" ({args.detail})" if args.detail else "")
                + (f" digit {args.digit}" if args.digit else ""),
            }
            cues = [template] * args.count
    data = load_catalog(args.dir)
    seen = {row["id"] for row in load_index()}
    print("Live collect. Leave run.py running. Draw when prompted, wait for idle capture.")
    print("After each capture: Enter keeps it, n discards, q quits.\n")
    kept = 0
    for index, cue in enumerate(cues, start=1):
        print(f"[{index}/{len(cues)}] {cue['prompt']}")
        print(f"  gold label will be: {cue['tag']}"
              + (f"/{cue['detail']}" if cue["detail"] else "")
              + (f" digit={cue['digit']}" if cue["digit"] else ""))
        try:
            record = wait_for_capture(seen)
        except KeyboardInterrupt:
            print("\nstopped")
            break
        seen.add(record["id"])
        png_path = config.CAPTURE_DIR / record["png"]
        print(f"  captured {record['id']}  ({record.get('stroke_count')} strokes)")
        if args.open:
            open_png(png_path)
        try:
            reply = input("  keep? [Y/n/q] ").strip().lower()
        except EOFError:
            reply = ""
        if reply in {"q", "quit"}:
            break
        if reply in {"n", "no", "s", "skip"}:
            print("  discarded")
            continue
        png, strokes = copy_capture(record, args.dir)
        item = make_item(
            record,
            tag=cue["tag"],
            detail=cue["detail"],
            digit=cue["digit"],
            note=cue["prompt"],
            png=png,
            strokes=strokes,
        )
        action = upsert_item(data, item)
        save_catalog(args.dir, data)
        kept += 1
        print(f"  {action} ({kept} in this session, {len(data['items'])} total)\n")
    print(f"done. {kept} kept. Rank them with:")
    print("  .\\.venv\\Scripts\\python.exe scripts\\eval_benchmark.py run")
    return 0


def predicted_detail(result: Any) -> str:
    if result.candidates:
        return (result.candidates[0].detail or "").strip().lower()
    return ""


def score_item(item: dict[str, Any], result: Any) -> dict[str, Any]:
    expected_tag = (item.get("tag") or "").strip().lower()
    expected_detail = (item.get("detail") or "").strip().lower()
    expected_digit = str(item.get("digit") or "").strip()
    ranked = [candidate.tag_id for candidate in result.candidates]
    pred_tag = result.top_tag
    pred_detail = predicted_detail(result)
    spoken = (result.spoken or "").lower()
    row: dict[str, Any] = {
        "id": item["id"],
        "kind": "digit" if expected_digit else ("intent" if expected_tag in INTENT_TAGS else expected_tag),
        "expected_tag": expected_tag,
        "expected_detail": expected_detail,
        "expected_digit": expected_digit,
        "predicted_tag": pred_tag,
        "predicted_detail": pred_detail,
        "predicted_digit": result.digit or "",
        "spoken": result.spoken or "",
        "seen": getattr(result, "seen", "") or "",
        "fallback_used": bool(result.fallback_used),
        "top1": False,
        "top3": False,
        "detail_ok": None,
        "digit_ok": None,
        "ranks": [
            {
                "tag_id": candidate.tag_id,
                "detail": candidate.detail,
                "likelihood": candidate.likelihood,
                "source": candidate.source,
            }
            for candidate in result.candidates[:5]
        ],
    }
    if expected_digit:
        row["digit_ok"] = (result.digit or "") == expected_digit
        row["top1"] = row["digit_ok"]
        row["top3"] = row["digit_ok"]
    elif expected_tag in INTENT_TAGS:
        row["top1"] = pred_tag == expected_tag
        row["top3"] = expected_tag in ranked[:3]
        if expected_detail:
            row["detail_ok"] = expected_detail == pred_detail or expected_detail in spoken
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    intent = [row for row in rows if row["kind"] == "intent"]
    detail = [row for row in intent if row["detail_ok"] is not None]
    digits = [row for row in rows if row["kind"] == "digit"]
    confusion: dict[str, dict[str, int]] = {}
    for row in intent:
        expected = row["expected_tag"] or "?"
        predicted = row["predicted_tag"] or "?"
        confusion.setdefault(expected, {})
        confusion[expected][predicted] = confusion[expected].get(predicted, 0) + 1

    def rate(ok: int, total: int) -> float | None:
        return round(ok / total, 3) if total else None

    return {
        "n": len(rows),
        "intent_n": len(intent),
        "intent_top1": sum(1 for row in intent if row["top1"]),
        "intent_top3": sum(1 for row in intent if row["top3"]),
        "intent_top1_acc": rate(sum(1 for row in intent if row["top1"]), len(intent)),
        "intent_top3_acc": rate(sum(1 for row in intent if row["top3"]), len(intent)),
        "detail_n": len(detail),
        "detail_ok": sum(1 for row in detail if row["detail_ok"]),
        "detail_acc": rate(sum(1 for row in detail if row["detail_ok"]), len(detail)),
        "digit_n": len(digits),
        "digit_ok": sum(1 for row in digits if row["digit_ok"]),
        "digit_acc": rate(sum(1 for row in digits if row["digit_ok"]), len(digits)),
        "fallback_n": sum(1 for row in rows if row["fallback_used"]),
        "mean_latency_s": round(
            sum(row.get("latency_s") or 0.0 for row in rows) / len(rows), 2
        ) if rows else None,
        "confusion": confusion,
    }


def pct(ok: int | None, total: int | None) -> str:
    if not total:
        return "n/a"
    return f"{ok}/{total} ({100 * (ok or 0) / total:.0f}%)"


def render_html(
    *,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    items: dict[str, dict[str, Any]],
    base: Path,
    offline: bool,
    embed: bool,
) -> str:
    stamps = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "offline templates" if offline else "Gemini ranking"
    cards = []
    for row in rows:
        item = items[row["id"]]
        png_path = base / item["png"]
        if embed and png_path.exists():
            encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
            src = f"data:image/png;base64,{encoded}"
        else:
            src = html.escape(f"../{item['png']}")
        ok = row["top1"]
        expected = row["expected_tag"]
        if row["expected_detail"]:
            expected += f"/{row['expected_detail']}"
        if row["expected_digit"]:
            expected += f" digit {row['expected_digit']}"
        predicted = row["predicted_tag"] or "—"
        if row["predicted_detail"]:
            predicted += f"/{row['predicted_detail']}"
        if row["predicted_digit"]:
            predicted += f" digit {row['predicted_digit']}"
        mark = "pass" if ok else "miss"
        cards.append(
            "<article class='card {mark}'>"
            "<img src='{src}' alt='{id}'>"
            "<div>"
            "<h3>{id}</h3>"
            "<p><b>{mark}</b> · {latency:.1f}s{fallback}</p>"
            "<p>gold: {expected}<br>pred: {predicted}</p>"
            "<p class='spoken'>{spoken}</p>"
            "</div></article>".format(
                mark=mark,
                src=src,
                id=html.escape(row["id"]),
                latency=row.get("latency_s") or 0.0,
                fallback=" · template fallback" if row["fallback_used"] else "",
                expected=html.escape(expected),
                predicted=html.escape(str(predicted)),
                spoken=html.escape(row.get("spoken") or ""),
            )
        )
    tags = sorted({row["expected_tag"] for row in rows if row["kind"] == "intent"}
                  | {row["predicted_tag"] or "?" for row in rows if row["kind"] == "intent"})
    matrix_rows = []
    if tags:
        head = "<tr><th>gold \\ pred</th>" + "".join(f"<th>{html.escape(tag)}</th>" for tag in tags) + "</tr>"
        body = []
        for expected in tags:
            cells = [f"<th>{html.escape(expected)}</th>"]
            for predicted in tags:
                count = summary["confusion"].get(expected, {}).get(predicted, 0)
                cells.append(f"<td>{count or ''}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        matrix_rows = [head, *body]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ink ranking eval · {stamps}</title>
<style>
  :root {{ --bg:#0e0f13; --panel:#171a21; --line:#262b36; --text:#e7e9ee;
          --muted:#8b93a3; --good:#46c98b; --bad:#ef5f5f; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width:1100px; margin:0 auto; padding:32px 20px 80px; }}
  h1 {{ font-weight:600; margin:0 0 8px; }}
  .sub {{ color:var(--muted); margin-bottom:28px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
            gap:12px; margin-bottom:28px; }}
  .stat {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
           padding:16px 18px; }}
  .stat b {{ display:block; font-size:28px; margin-top:4px; }}
  table {{ border-collapse:collapse; margin:0 0 32px; }}
  th, td {{ border:1px solid var(--line); padding:8px 12px; text-align:center; }}
  th {{ color:var(--muted); font-weight:500; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }}
  .card {{ display:flex; gap:12px; background:var(--panel); border:1px solid var(--line);
           border-radius:12px; padding:12px; }}
  .card img {{ width:88px; height:140px; object-fit:cover; background:#fff;
               border-radius:8px; flex:none; }}
  .card h3 {{ margin:0 0 6px; font-size:12px; color:var(--muted); font-weight:500; }}
  .card p {{ margin:0 0 6px; }}
  .spoken {{ color:var(--muted); font-size:13px; }}
  .pass {{ box-shadow: inset 3px 0 0 var(--good); }}
  .miss {{ box-shadow: inset 3px 0 0 var(--bad); }}
</style>
</head>
<body>
<main>
  <h1>Ink ranking eval</h1>
  <p class="sub">{html.escape(mode)} · {stamps} · {summary['n']} drawings</p>
  <section class="stats">
    <div class="stat">Intent top-1<b>{pct(summary['intent_top1'], summary['intent_n'])}</b></div>
    <div class="stat">Intent top-3<b>{pct(summary['intent_top3'], summary['intent_n'])}</b></div>
    <div class="stat">Named object<b>{pct(summary['detail_ok'], summary['detail_n'])}</b></div>
    <div class="stat">Digit 1 / 2<b>{pct(summary['digit_ok'], summary['digit_n'])}</b></div>
    <div class="stat">Mean latency<b>{summary['mean_latency_s'] or '—'}s</b></div>
    <div class="stat">Template fallback<b>{summary['fallback_n']}/{summary['n']}</b></div>
  </section>
  {"<h2>Confusion</h2><table>" + "".join(matrix_rows) + "</table>" if matrix_rows else ""}
  <h2>Drawings</h2>
  <div class="grid">{"".join(cards)}</div>
</main>
</body>
</html>
"""


def cmd_run(args: argparse.Namespace) -> int:
    from recognize import IntentRecognizer

    data = load_catalog(args.dir)
    items = list(data.get("items") or [])
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("Catalog is empty. Collect drawings first.")
        return 1

    recognizer = IntentRecognizer(root=ROOT)
    rows = []
    print(f"Ranking {len(items)} drawing(s)" + (" offline" if args.offline else " with Gemini") + "\n")
    for index, item in enumerate(items, start=1):
        png = args.dir / item["png"]
        strokes_path = args.dir / item["strokes"]
        strokes = load_eval_strokes(strokes_path)
        started = time.time()
        result = recognizer.interpret(
            strokes,
            image=png if png.exists() else None,
            offline=args.offline,
            update_memory=False,
        )
        latency = time.time() - started
        row = score_item(item, result)
        row["latency_s"] = round(latency, 2)
        rows.append(row)
        mark = "ok  " if row["top1"] else "MISS"
        pred = row["predicted_tag"] or "-"
        if row["predicted_detail"]:
            pred += f"/{row['predicted_detail']}"
        gold = row["expected_tag"]
        if row["expected_detail"]:
            gold += f"/{row['expected_detail']}"
        print(f"{mark} {index:>2}/{len(items)}  {gold:<16} -> {pred:<16} {latency:5.1f}s  {row['spoken']}")

    summary = summarize(rows)
    print(
        f"\nintent top-1 {pct(summary['intent_top1'], summary['intent_n'])}"
        f"   top-3 {pct(summary['intent_top3'], summary['intent_n'])}"
        f"   object {pct(summary['detail_ok'], summary['detail_n'])}"
        f"   digit {pct(summary['digit_ok'], summary['digit_n'])}"
    )

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out = reports_dir(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": utc_now(),
        "offline": args.offline,
        "summary": summary,
        "items": rows,
    }
    json_path = out / f"{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    by_id = {item["id"]: item for item in items}
    html_text = render_html(
        summary=summary,
        rows=rows,
        items=by_id,
        base=args.dir,
        offline=args.offline,
        embed=args.embed,
    )
    html_path = out / f"{stamp}.html"
    latest = out / "latest.html"
    html_path.write_text(html_text, encoding="utf-8")
    latest.write_text(html_text, encoding="utf-8")
    shutil.copy2(json_path, out / "latest.json")
    print(f"\n{json_path}")
    print(latest)
    if args.open:
        open_png(latest)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """No Gemini. Checks catalog IO, stroke loading, scoring, and the HTML report."""
    import tempfile

    from recognize import RecognitionResult, Candidate

    tmp = Path(tempfile.mkdtemp(prefix="ink-eval-"))
    drawings = drawings_dir(tmp)
    drawings.mkdir(parents=True)
    png = drawings / "demo.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ))
    stroke_file = drawings / "demo.json"
    stroke_file.write_text(
        json.dumps({
            "id": "demo",
            "strokes": [{"tool": "touch", "points": [[10, 10, 0.5, 0], [40, 12, 0.5, 80]]}],
        }),
        encoding="utf-8",
    )
    polylines = load_eval_strokes(stroke_file)
    if polylines != [[[10, 10], [40, 12]]]:
        print(f"FAIL stroke loader: {polylines!r}")
        return 1

    data = {"items": []}
    item = {
        "id": "demo",
        "source_id": "demo",
        "png": "drawings/demo.png",
        "strokes": "drawings/demo.json",
        "tag": "food",
        "detail": "pizza",
        "digit": "",
        "note": "selftest",
        "added_at": utc_now(),
    }
    upsert_item(data, item)
    save_catalog(tmp, data)
    loaded = load_catalog(tmp)
    if loaded["items"][0]["tag"] != "food":
        print("FAIL catalog round-trip")
        return 1

    hit = RecognitionResult(
        top_tag="food",
        fallback_used=False,
        spoken="I would like a slice of pizza, please.",
        candidates=[
            Candidate(
                tag_id="food", label="food", rank=1, rank_weight=1, likelihood=0.9,
                feature_score=0.1, final_weight=0.9, detail="pizza",
            ),
            Candidate(
                tag_id="water", label="water", rank=2, rank_weight=1, likelihood=0.2,
                feature_score=0.1, final_weight=0.2,
            ),
        ],
    )
    miss = RecognitionResult(
        top_tag="water",
        fallback_used=True,
        spoken="I would like a glass of water, please.",
        candidates=[
            Candidate(
                tag_id="water", label="water", rank=1, rank_weight=1, likelihood=0.8,
                feature_score=0.1, final_weight=0.8,
            ),
        ],
    )
    hit_row = score_item(item, hit)
    miss_row = score_item(item, miss)
    if not hit_row["top1"] or not hit_row["detail_ok"] or not hit_row["top3"]:
        print(f"FAIL expected a pizza hit, got {hit_row}")
        return 1
    if miss_row["top1"] or miss_row["detail_ok"]:
        print(f"FAIL water should not match pizza gold, got {miss_row}")
        return 1
    hit_row["latency_s"] = 0.4
    miss_row["latency_s"] = 0.5
    summary = summarize([hit_row])
    html_text = render_html(
        summary=summary,
        rows=[hit_row],
        items={"demo": item},
        base=tmp,
        offline=True,
        embed=True,
    )
    if "Intent top-1" not in html_text or "data:image/png" not in html_text:
        print("FAIL html report")
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok     eval catalog, stroke loader, scoring, html report")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect labeled drawings and score ranking for a judge-facing eval.",
    )
    parser.add_argument("--dir", type=Path, default=ROOT / "eval", help="Eval set directory")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Label new tablet captures into the eval set")
    collect.add_argument("--label", action="store_true", help="Label existing captures instead of waiting")
    collect.add_argument("--relabel", action="store_true", help="Revisit captures already in the catalog")
    collect.add_argument("--tag", help="Only collect this tag (live)")
    collect.add_argument("--detail", default="", help="Gold detail when using --tag")
    collect.add_argument("--digit", default="", help="Gold digit when using --tag play")
    collect.add_argument("--count", type=int, help="How many to collect when --tag is set")
    collect.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    add = sub.add_parser("add", help="Copy one capture into the eval set")
    add.add_argument("capture")
    add.add_argument("--tag", required=True)
    add.add_argument("--detail", default="")
    add.add_argument("--digit", default="")
    add.add_argument("--note", default="")

    remove = sub.add_parser("remove", help="Drop an item from the catalog")
    remove.add_argument("capture")

    imported = sub.add_parser("import-confirmed", help="Seed from confirmed pad taps (review afterwards)")
    imported.add_argument("--include-game", action="store_true")
    imported.add_argument("--force", action="store_true")

    sub.add_parser("list", help="Show the labeled set")

    run = sub.add_parser("run", help="Rank every labeled drawing and write a report")
    run.add_argument("--offline", action="store_true", help="Skip Gemini; score local templates only")
    run.add_argument("--limit", type=int)
    run.add_argument("--embed", action=argparse.BooleanOptionalAction, default=True,
                     help="Embed PNGs in the HTML so it is one file for judges")
    run.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    sub.add_parser("selftest", help="No API: catalog, scoring, and HTML checks")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.dir = args.dir.resolve()
    if args.command == "list":
        return cmd_list(args.dir)
    if args.command == "add":
        return cmd_add(args)
    if args.command == "remove":
        return cmd_remove(args)
    if args.command == "import-confirmed":
        return cmd_import_confirmed(args)
    if args.command == "collect":
        if args.label or args.relabel:
            return cmd_collect_label(args)
        return cmd_collect_live(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "selftest":
        return cmd_selftest(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
