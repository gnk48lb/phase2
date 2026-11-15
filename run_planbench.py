"""Run the three memory-organization ablations on Plan-Bench.

Strict prompt parity: identical task rules, question, options, answer schema,
decoding parameters and observation stream for every method.  Only the
``## History`` block differs -- how the same history is organized.

Quota policy (enforced in code, not just documented):
  * ``minimax-code-global/MiniMax-M3.1-Flash`` is unlimited -> concurrency 10,
    may run the full temporal subset and every ablation.
  * every other model -> concurrency 2, at most 50 stratified samples, and
    only for the cross-model main comparison.

Two conditions are supported and are run separately:
  --mode text       history is the cached, question-blind observation stream
  --mode multimodal history is the raw episode frames (images), end to end

Examples (from phase2_planbench/):
    python run_planbench.py --mode multimodal --models minimax --limit 10 \
        --output-dir results/smoke_mm
    python run_planbench.py --mode multimodal --models minimax --output-dir results/mm_minimax
    python run_planbench.py --mode text --models minimax --output-dir results/txt_minimax
    python run_planbench.py --mode text --models cross --output-dir results/txt_cross
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import random
import threading
import time
from typing import Any

from common import (API_KEY, BASE_URL, CreditMeter, JsonlWriter, MagpieMultimodalClient,
                    UNLIMITED_MODEL, available_cross_models, concurrency_for, image_part,
                    load_jsonl, magpie_credits, max_samples_for, model_catalog, now_iso,
                    require_model_ids, write_json)
from grader import grade_answer
from memory_ledger import build_ledger
from prompts import METHODS, build_multimodal_parts, build_user_prompt, parse_answer, system_prompt

ROOT = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()


def model_slug(model: str) -> str:
    return model.replace("/", "__").replace(":", "_")


def stratified_subset(tasks: list[dict[str, Any]], limit: int, seed: int = 20261114) -> list[dict[str, Any]]:
    """Stratified sample across subtasks, proportional to availability.

    Every subtask keeps at least one item when it has any; the rest is
    allocated proportionally.  Deterministic for a fixed seed, so every model
    is compared on exactly the same tasks.
    """
    if limit >= len(tasks):
        return list(tasks)
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        groups.setdefault(task["subset"], []).append(task)
    rng = random.Random(seed)
    for group in groups.values():
        rng.shuffle(group)
    total = len(tasks)
    allocation = {name: max(1, round(limit * len(group) / total)) for name, group in groups.items()}
    while sum(allocation.values()) > limit:
        name = max(allocation, key=lambda k: allocation[k])
        allocation[name] -= 1
    while sum(allocation.values()) < limit:
        name = max(allocation, key=lambda k: len(groups[k]) - allocation[k])
        allocation[name] += 1
    chosen: list[dict[str, Any]] = []
    for name, group in groups.items():
        chosen.extend(group[:allocation[name]])
    rng.shuffle(chosen)
    return chosen


def load_tasks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = payload["tasks"] if isinstance(payload, dict) else payload
    for task in tasks:
        if not task.get("options") and task.get("question_type") == "single":
            raise ValueError(f"task {task.get('task_id')} is single-choice but has no options")
        if task.get("gold") is None:
            raise ValueError(f"task {task.get('task_id')} has no gold answer")
    return tasks


def build_history(task: dict[str, Any], method: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return (observations for the direct baseline, ledger snapshot)."""
    observations = task.get("observations") or []
    if method == "direct_baseline":
        return observations, None
    ledger = build_ledger(method)
    for observation in observations:
        ledger.ingest(observation)
    if method == "versioned_state_ledger_flat":
        return observations, ledger.snapshot_flat(task["current_frame"])
    return observations, ledger.snapshot(task["current_frame"])


def run_one(task: dict[str, Any], method: str, model: str, mode: str,
            client: MagpieMultimodalClient, label: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    observations, snapshot = build_history(task, method)
    system = system_prompt(task["question_type"])
    kwargs = dict(episode_id=task["task_id"], question=task["question"],
                  options=task.get("options") or [], observations=observations,
                  ledger_snapshot=snapshot, current_frame=task["current_frame"],
                  question_type=task["question_type"])
    images: list[dict[str, Any]] = []
    if mode == "multimodal":
        images = [image_part(ROOT / "Plan-Bench" / rel) for rel in task["frame_images"]]
    error = None
    text = ""
    meta: dict[str, Any] = {}
    prompt_chars = 0
    # Thinking models sometimes spend the whole budget on reasoning and return
    # an empty body.  A terser instruction (and a bigger budget) recovers most
    # of those cells; the remaining ones stay recorded as transport failures.
    terse = ("\n\nOutput only the JSON object. Do not explain, do not reason aloud, "
             "do not add any other text.")
    for attempt in range(3):
        try:
            if images:
                parts = build_multimodal_parts(method, image_parts=images, **kwargs)
                if attempt:
                    parts = parts + [{"type": "text", "text": terse}]
                prompt_chars = sum(len(part.get("text", "")) for part in parts
                                   if isinstance(part, dict))
                text, meta = client.complete_parts(model, system, parts,
                                                   max_tokens=4000 * (attempt + 1))
            else:
                user = build_user_prompt(method, **kwargs) + (terse if attempt else "")
                prompt_chars = len(system) + len(user)
                text, meta = client.complete(model, system, user)
            if text.strip():
                break
            error = "empty model response"
        except Exception as exc:  # noqa: BLE001 - recorded as an infrastructure failure
            error = f"{type(exc).__name__}: {exc}"
            text = ""
    parsed = parse_answer(text, task.get("options") or []) if text else {
        "answer": None, "confidence": None, "evidence_frames": [], "parsed": False, "raw_answer": None}
    grade = grade_answer(parsed["answer"], task["gold"], task["question_type"])
    return {
        "task_id": task["task_id"], "subset": task["subset"], "question_type": task["question_type"],
        "model": label or model, "actual_model": model,
        "method": method, "mode": mode, "gold": task["gold"],
        "prediction": parsed["answer"], "correct": grade["correct"], "exact": grade["exact"],
        "graded_detail": grade["detail"], "confidence": parsed["confidence"],
        "evidence_frames": parsed["evidence_frames"], "parsed": parsed["parsed"],
        "raw_answer": parsed["raw_answer"], "raw_response": text[:6000],
        "request": meta, "error": error, "latency_s": round(time.monotonic() - started, 3),
        "time": now_iso(), "prompt_chars": prompt_chars, "images": len(images),
        "num_frames": task["num_frames"], "observation_source": task.get("observation_source"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "cache" / "tasks.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--models", default="minimax",
                        help="'minimax', 'cross' (all available limited models), "
                             "or comma-separated Magpie ids")
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--mode", choices=("text", "multimodal"), default="text")
    parser.add_argument("--limit", type=int, default=0, help="use only the first N tasks (smoke)")
    parser.add_argument("--subset-file", type=Path, default=None,
                        help="JSON file with the task_ids to run (the shared cross-check subset)")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--api-key", default=API_KEY)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--credit-budget", type=float, default=2000.0,
                        help="stop the run after this many estimated MiniMax credits "
                             "(0 = unlimited); every finished answer is already on disk")
    parser.add_argument("--rerun-methods", default="",
                        help="comma-separated methods whose existing answers must be "
                             "discarded and re-run (the old rows are backed up next to "
                             "the answers file); use when the shared memory structure "
                             "changed, e.g. the absent-state semantics fix")
    parser.add_argument("--model-alias", default="",
                        help="report every route under one model label (the actual route is "
                             "kept in the row as actual_model); use when a run has to switch "
                             "account pools mid-experiment")
    parser.add_argument("--credit-reserve", type=float, default=2000.0,
                        help="credits that must stay in the pool; the run stops before "
                             "the live balance reaches this line")
    args = parser.parse_args()

    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    if any(method not in METHODS for method in methods):
        parser.error(f"unknown method; choose from {METHODS}")

    catalog = model_catalog(args.base_url, args.api_key)
    if args.models == "cross":
        models = available_cross_models(catalog)
        if not models:
            parser.error("none of the cross-check candidates are in the live Magpie catalog")
    else:
        presets = {"minimax": UNLIMITED_MODEL}
        models = require_model_ids(
            [presets.get(name.strip(), name.strip()) for name in args.models.split(",") if name.strip()],
            catalog)

    # One label for the whole matrix when a run has to switch account pools
    # mid-experiment; the route that served each row is kept in the row itself.
    label_of = {route: (args.model_alias or route) for route in models}

    tasks = load_tasks(args.tasks)
    if args.subset_file:
        wanted = set(json.loads(args.subset_file.read_text(encoding="utf-8")))
        tasks = [task for task in tasks if task["task_id"] in wanted]
    if args.limit:
        tasks = tasks[:args.limit]

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    answers_path = output / "answers.jsonl"

    # Quota guard: a limited model can never be asked for more than its cap.
    per_model_tasks: dict[str, list[dict[str, Any]]] = {}
    for model in models:
        cap = max_samples_for(model)
        if cap is not None and len(tasks) > cap:
            chosen = stratified_subset(tasks, cap)
            print(f"Quota guard: {model} capped to {cap} stratified tasks "
                  f"({len(tasks)} requested)", flush=True)
            per_model_tasks[model] = chosen
        else:
            per_model_tasks[model] = tasks

    manifest = {
        "created_at": now_iso(), "mode": args.mode, "tasks_file": str(args.tasks),
        "subset_file": str(args.subset_file) if args.subset_file else None,
        "tasks_per_model": {label_of[model]: [task["task_id"] for task in per_model_tasks[model]]
                            for model in models},
        "routes": {label_of[model]: model for model in models},
        "methods": methods, "temperature": 0, "timeout_s": args.timeout,
        "prompt_parity_version": "phase2-planbench-v1",
        "quota": {model: {"concurrency": concurrency_for(model),
                          "max_samples": max_samples_for(model),
                          "tier": "unlimited" if max_samples_for(model) is None else "limited"}
                  for model in models},
    }
    write_json(output / f"run_manifest_{args.mode}.json", manifest)

    rerun = {m.strip() for m in args.rerun_methods.split(",") if m.strip()}
    if rerun:
        unknown = rerun - set(methods)
        if unknown:
            parser.error(f"--rerun-methods must be a subset of --methods: {sorted(unknown)}")
        original = load_jsonl(answers_path)
        backup = answers_path.with_name(answers_path.stem + "_prerun.jsonl")
        with backup.open("w", encoding="utf-8") as handle:
            for row in original:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        kept = [row for row in original if row.get("method") not in rerun]
        with answers_path.open("w", encoding="utf-8") as handle:
            for row in kept:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        print(f"Re-running {sorted(rerun)}: dropped {len(original) - len(kept)} old row(s), "
              f"backup at {backup.name}", flush=True)

    done = {(label_of.get(row["model"], row["model"]), row["method"], row["task_id"],
             row["mode"]) for row in load_jsonl(answers_path)}
    # Jobs are BUNDLES: one task x one model runs all three methods together, so
    # a run stopped by the credit guard still leaves complete method-triplets
    # for the tasks it finished.
    bundles = [(task, model) for model in models for task in per_model_tasks[model]
               if any((label_of[model], method, task["task_id"], args.mode) not in done
                      for method in methods)]
    random.Random(20261114).shuffle(bundles)
    total_calls = sum(len(per_model_tasks[m]) * len(methods) for m in models)
    print(f"mode={args.mode} models={ {m: len(per_model_tasks[m]) for m in models} } "
          f"{len(bundles)} task bundle(s) / {total_calls} request(s) to do; "
          f"MiniMax credits left: {magpie_credits().get('balance')}", flush=True)

    writers = {"answers": JsonlWriter(answers_path),
               "raw": JsonlWriter(output / "raw_responses.jsonl"),
               "failures": JsonlWriter(output / "failures.jsonl")}
    client = MagpieMultimodalClient(args.base_url, args.api_key, args.timeout,
                                    args.request_retries, 4000)
    meter = CreditMeter(args.credit_budget, reserve=args.credit_reserve)
    counts: Counter = Counter()
    start = time.monotonic()
    stopped_early = False

    def record(row: dict[str, Any]) -> None:
        writers["answers"].write(row)
        writers["raw"].write({key: row[key] for key in
                              ("model", "method", "task_id", "mode", "raw_response", "request")})
        if row["error"] or not row["parsed"]:
            writers["failures"].write({key: row[key] for key in
                                       ("model", "method", "task_id", "mode", "error", "parsed",
                                        "raw_answer", "raw_response")})
        counts[(row["model"], row["method"])] += 1
        with PRINT_LOCK:
            print(f"[{counts.total()}/{total_calls}] {row['model']} {row['method']} "
                  f"{row['task_id']}: correct={row['correct']} exact={row['exact']} "
                  f"conf={row['confidence']} {row['latency_s']}s "
                  f"credits~{meter.spent:.1f}/{args.credit_budget or 'inf'} "
                  f"elapsed={time.monotonic()-start:.0f}s", flush=True)

    def execute_bundle(bundle):
        task, model = bundle
        rows = []
        for method in methods:
            row = run_one(task, method, model, args.mode, client,
                          label=label_of[model])
            meter.record(row.get("request"), provider=model.split("/")[0])
            record(row)
            rows.append(row)
        return rows

    pools = {model: ThreadPoolExecutor(max_workers=concurrency_for(model),
                                       thread_name_prefix=model_slug(model))
             for model in models}
    futures = []
    for bundle in bundles:
        if meter.exhausted():
            stopped_early = True
            break
        futures.append(pools[bundle[1]].submit(execute_bundle, bundle))
    try:
        for future in as_completed(futures):
            future.result()
    except Exception:
        for pending in futures:
            pending.cancel()
        raise
    finally:
        for pool in pools.values():
            pool.shutdown(wait=True)

    # Resume state: what is done and what is left, so the run can continue
    # later without re-paying for anything.
    done_now = {(label_of.get(row["model"], row["model"]), row["method"], row["task_id"],
                 row["mode"]) for row in load_jsonl(answers_path)}
    remaining = [(task["task_id"], model) for model in models
                 for task in per_model_tasks[model]
                 if any((label_of[model], method, task["task_id"], args.mode) not in done_now
                        for method in methods)]
    state = {"mode": args.mode, "models": models, "methods": methods,
             "completed_calls": counts.total(), "remaining_bundles": len(remaining),
             "remaining_task_ids": sorted({task_id for task_id, _ in remaining}),
             "credits": meter.snapshot(), "stopped_by_credit_guard": stopped_early,
             "time": now_iso()}
    write_json(output / f"resume_state_{args.mode}.json", state)
    print(f"Done in {time.monotonic()-start:.1f}s -> {output}; credits~{meter.spent:.1f}; "
          f"remaining bundles: {len(remaining)}"
          f"{' (stopped by the credit guard)' if stopped_early else ''}", flush=True)


if __name__ == "__main__":
    main()
