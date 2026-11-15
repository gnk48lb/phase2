"""Clean the gate cache after the ledger/target-selection fixes.

Two kinds of cached work exist per sample:

* the single-frame **perception** (the expensive VLM call): reusable as long
  as the SAME entity is verified in the SAME frame;
* the two **re-answers**: they depend on the ledger, and the ledger semantics
  changed (absent-state fix, event:* pollution), so they must be recomputed.

This script keeps the reusable perceptions and drops everything else, so the
next gate run only pays for what it actually needs.

    python clean_gate_cache.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import JsonCache, now_iso
from gate_simulation import build_ledger_for, pick_target, reusable_observation

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "cache" / "tasks.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "cache" / "gate_verified.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = {task["task_id"]: task for task in
             json.loads(args.tasks.read_text(encoding="utf-8"))["tasks"]}
    cache = JsonCache(args.cache, "gate:v1")
    entries = {key: value for key, value in cache.data.items() if key.startswith("gate:v1:")}

    kept, dropped_observation, dropped_answers = 0, 0, 0
    new_data: dict[str, Any] = {}
    for key, record in entries.items():
        task_id = key.split(":", 2)[-1]
        task = tasks.get(task_id)
        if task is None:
            dropped_observation += 1
            continue
        ledger = build_ledger_for(task)
        target = pick_target(ledger, task["question"], task["current_frame"])
        entity = (target or {}).get("key")
        frame = int(record.get("frame", -1))
        observation = reusable_observation(record, entity, frame)
        if observation is None:
            dropped_observation += 1
            continue
        # Keep the paid perception, drop the answers that depend on the ledger.
        new_record = {"frame": frame, "entity": entity, "observation": observation,
                      "observation_reused": True, "time": now_iso(),
                      "note": "perception reused; answers recomputed after the "
                              "absent-state / event-pollution fixes"}
        new_data[key] = new_record
        kept += 1
        dropped_answers += 1

    if not args.dry_run:
        cache.data = new_data
        cache._flush()
    print(f"cache entries: {len(entries)} -> kept {kept} perception(s), "
          f"dropped {dropped_observation} (target changed / unusable), "
          f"{dropped_answers} stale answer(s) marked for recompute")


if __name__ == "__main__":
    main()
