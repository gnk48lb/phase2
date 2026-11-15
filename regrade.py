"""Re-parse and re-grade every stored answer, offline (no API calls).

The raw model responses are kept in ``answers.jsonl``, so a fix in the parser
or the grader can be applied to the whole corpus without paying for a single
extra request.  The stored prediction/correct/exact fields are replaced by
the freshly parsed ones.

    python regrade.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from grader import grade_answer
from prompts import parse_answer

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--tasks", type=Path, default=ROOT / "cache" / "tasks.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = {task["task_id"]: task for task in
             json.loads(args.tasks.read_text(encoding="utf-8"))["tasks"]}

    for path in sorted(args.results.glob("*/answers.jsonl")):
        if path.parent.name.startswith("_"):
            continue
        rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
        changed = 0
        for row in rows:
            raw = row.get("raw_response")
            if not raw:
                continue
            options = (tasks.get(row["task_id"]) or {}).get("options") or []
            parsed = parse_answer(raw, options)
            grade = grade_answer(parsed["answer"], row["gold"], row["question_type"])
            before = (row.get("prediction"), row.get("correct"), row.get("exact"))
            after = (parsed["answer"], grade["correct"], grade["exact"])
            row["prediction"], row["correct"], row["exact"] = after
            row["graded_detail"] = grade["detail"]
            row["confidence"] = parsed["confidence"]
            row["parsed"] = parsed["parsed"]
            if before != after:
                changed += 1
        if not args.dry_run:
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
        print(f"{path.parent.name}: {len(rows)} rows, {changed} re-graded")


if __name__ == "__main__":
    main()
