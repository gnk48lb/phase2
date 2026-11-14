"""Drop infrastructure-failed rows from a run so the next run repairs them.

A failed request (empty body, timeout, unparsable answer) is a transport
fault, not a policy outcome.  Removing those rows and re-running the same
command re-does exactly those cells and nothing else.

    python repair_run.py results/txt_minimax/answers.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import load_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answers", type=Path)
    parser.add_argument("--keep-unparsed", action="store_true",
                        help="keep rows whose answer could not be parsed (only drop "
                             "transport errors)")
    args = parser.parse_args()

    rows = load_jsonl(args.answers)
    keep, dropped = [], []
    for row in rows:
        # A cell is a transport failure only when there is no answer at all;
        # a parsed fallback (bare option letter) is a real answer and stays.
        bad = bool(row.get("error")) or row.get("prediction") is None
        if not bad and args.keep_unparsed and not row.get("parsed"):
            bad = True
        (dropped if bad else keep).append(row)
    with args.answers.open("w", encoding="utf-8") as handle:
        for row in keep:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{args.answers}: kept {len(keep)}, dropped {len(dropped)} failed cell(s)")
    for row in dropped[:20]:
        print(f"  - {row['model']} {row['method']} {row['task_id']}: "
              f"{str(row.get('error'))[:80]}")


if __name__ == "__main__":
    main()
