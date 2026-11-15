"""Code-level staleness-risk gating on top of the versioned state ledger.

The risk score is computed by deterministic Python from the ledger, never by
the model:

    risk = 0.45 * span_norm + 0.30 * conflict_norm + 0.25 * uncertainty

  span_norm     mean staleness (current_frame - last_verified_frame) of the
                question-relevant active records, divided by the question horizon
  conflict_norm supersession count affecting those records / observations seen
  uncertainty   1 - the model's own answer confidence

When ``risk > tau`` the policy spends ONE extra perception to re-verify the
true state at a deterministically chosen intermediate frame, updates the
ledger with that evidence, and re-answers.  The verification is a real VLM
call on that frame (one extra perception), and the re-verified answer is
cached, so scanning many thresholds never re-pays for it.

Outputs
    results/gate_simulation.json   per-threshold accuracy / cost
    results/gate_simulation.csv    same, as a table
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
from pathlib import Path
import re
import statistics
import threading
import time
from typing import Any

from analyze_planbench import dedup_rows, is_failure
from common import (BASE_URL, API_KEY, CreditMeter, JsonCache, MagpieMultimodalClient,
                    UNLIMITED_MODEL, image_part, load_jsonl, magpie_credits, now_iso,
                    write_json)
from grader import grade_answer
from memory_ledger import (VersionedLedger, apply_verified_observation, is_trackable_entity,
                            staleness_risk, verification_frame)
from prompts import build_user_prompt, parse_answer, system_prompt

ROOT = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()

VERIFY_SYSTEM = """You verify the state of one tracked entity in one frame of a video.
You are given the frame and the entity name. Report what is objectively true in THIS frame.
Return EXACTLY ONE JSON object and no prose:
{"present": true or false, "state": "<short phrase describing its position/status in this frame>", "evidence": "<what in the image shows this>"}
If the entity is not visible in this frame, set "present" to false and state to "not visible".
Output JSON only."""

VERIFY_USER = ("Entity: {entity}\nThis is frame {frame} of {total} of the episode. "
               "What is the state of this entity in this frame?")


def load_gate_rows(path: Path, method: str) -> list[dict[str, Any]]:
    """Unique samples for the gate: one row per task, failures dropped.

    ``answers.jsonl`` is append-only, so the same cell can appear several
    times (repairs, re-runs).  Without the dedup the sample set inflates and
    the task count no longer matches the benchmark.
    """
    return [row for row in dedup_rows(load_jsonl(path))
            if row.get("method") == method and not is_failure(row)]


def reusable_observation(record: dict[str, Any] | None, entity: str | None,
                         frame: int) -> dict[str, Any] | None:
    """A cached single-frame verification that still applies to this target.

    The VLM perception call is the expensive part of the gate, so it is reused
    whenever the SAME entity is verified in the SAME frame.  The two re-answers
    are never reused: they depend on the ledger, and the ledger semantics
    changed with the absent-state fix.
    """
    if not record or not entity:
        return None
    if record.get("entity") != entity or int(record.get("frame", -1)) != int(frame):
        return None
    observation = record.get("observation") or {}
    if observation.get("present") is None or observation.get("error"):
        return None
    return observation


def build_ledger_for(task: dict[str, Any]) -> VersionedLedger:
    ledger = VersionedLedger()
    for observation in task.get("observations") or []:
        ledger.ingest(observation)
    return ledger


def pick_target(ledger: VersionedLedger, question: str, current_frame: int) -> dict[str, Any] | None:
    """The physical entity most worth re-verifying.

    Only trackable entities are eligible: an ``event:*`` action phrase or the
    per-frame activity record looks maximally stale (it was written at frame 0
    and never repeats) but re-verifying it in a later frame always returns
    "not visible".  Among the physical entities, one that actually suffered a
    state conflict is preferred over one that merely happens to be old.
    """
    keys = ledger.risk_keys(question)
    best, best_score = None, None
    for key in keys:
        record = ledger.records.get(key)
        if not record:
            continue
        if not is_trackable_entity(key):
            continue
        staleness = max(0, int(current_frame) - int(record["last_verified_frame"]))
        conflicts = ledger.conflicts.get(key, 0)
        score = (conflicts > 0, conflicts, staleness)
        if best_score is None or score > best_score:
            best, best_score = record, score
    if best is None:
        return None
    return {"key": best["entity"], "state": best["state"], "staleness": best_score[2],
            "conflicts": best_score[1]}


def verify_frame(client: MagpieMultimodalClient, task: dict[str, Any], frame: int,
                 entity: str, total: int) -> dict[str, Any]:
    """One extra perception call: the true state of ``entity`` at ``frame``."""
    rel = task["frame_images"][frame]
    parts = [{"type": "text", "text": VERIFY_USER.format(entity=entity, frame=frame, total=total)},
             image_part(ROOT / "Plan-Bench" / rel)]
    terse = "\n\nBe terse: output the single JSON object and nothing else."
    text, meta = "", {}
    error = None
    for attempt in range(2):
        try:
            attempt_parts = parts + ([{"type": "text", "text": terse}] if attempt else [])
            text, meta = client.complete_parts(UNLIMITED_MODEL, VERIFY_SYSTEM, attempt_parts,
                                               max_tokens=3000 * (attempt + 1))
            if text.strip():
                error = None
                break
            error = "empty model response"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            text = ""
    if not text:
        return {"present": None, "state": None, "error": error}
    raw = text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and "present" in value:
            return {"present": bool(value.get("present")),
                    "state": str(value.get("state") or "")[:120],
                    "evidence": str(value.get("evidence") or "")[:200],
                    "latency_s": meta.get("latency_ms", 0) / 1000,
                    "meta": {"input_tokens": meta.get("input_tokens"),
                             "output_tokens": meta.get("output_tokens"),
                             "total_tokens": meta.get("total_tokens")}}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            value = json.loads(match.group(0))
            if isinstance(value, dict) and "present" in value:
                return {"present": bool(value.get("present")),
                        "state": str(value.get("state") or "")[:120],
                        "evidence": str(value.get("evidence") or "")[:200],
                        "meta": {"input_tokens": meta.get("input_tokens"),
                                 "output_tokens": meta.get("output_tokens"),
                                 "total_tokens": meta.get("total_tokens")}}
        except json.JSONDecodeError:
            pass
    return {"present": None, "state": None, "error": "unparsable verification"}


def reanswer(client: MagpieMultimodalClient, task: dict[str, Any], ledger: VersionedLedger,
             verified: dict[str, Any]) -> dict[str, Any]:
    """Answer again with the ledger updated by the verification."""
    snapshot = ledger.snapshot(task["current_frame"])
    user = build_user_prompt("versioned_state_ledger", episode_id=task["task_id"],
                              question=task["question"], options=task.get("options") or [],
                              observations=task.get("observations") or [],
                              ledger_snapshot=snapshot, current_frame=task["current_frame"],
                              question_type=task["question_type"])
    # A thinking model can spend the whole budget on reasoning and return an
    # empty body; a terser instruction and a bigger budget recover most of
    # those cells, so a transport failure is not mistaken for a policy error.
    terse = "\n\nBe terse: output the single JSON object and nothing else."
    text, meta = "", {}
    error = None
    for attempt in range(2):
        try:
            text, meta = client.complete(UNLIMITED_MODEL, system_prompt(task["question_type"]),
                                         user + (terse if attempt else ""))
            if text.strip():
                error = None
                break
            error = "empty model response"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            text = ""
    if not text:
        return {"prediction": None, "correct": False, "error": error}
    parsed = parse_answer(text, task.get("options") or [])
    grade = grade_answer(parsed["answer"], task["gold"], task["question_type"])
    return {"prediction": parsed["answer"], "correct": grade["correct"], "exact": grade["exact"],
            "confidence": parsed["confidence"], "detail": grade["detail"],
            "meta": {"input_tokens": meta.get("input_tokens"),
                     "output_tokens": meta.get("output_tokens"),
                     "total_tokens": meta.get("total_tokens")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "cache" / "tasks.json")
    parser.add_argument("--answers", type=Path, default=ROOT / "results" / "txt_minimax" / "answers.jsonl",
                        help="answers.jsonl of the versioned-ledger run that the gate refines")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--method", default="versioned_state_ledger")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-verification", action="store_true",
                        help="only recompute the risk scores from the cache")
    parser.add_argument("--credit-budget", type=float, default=800.0,
                        help="stop after this many estimated credits; verified samples stay "
                             "cached and the run resumes where it stopped")
    parser.add_argument("--credit-reserve", type=float, default=2000.0,
                        help="credits that must stay in the pool")
    args = parser.parse_args()

    tasks = {task["task_id"]: task for task in
             json.loads(args.tasks.read_text(encoding="utf-8"))["tasks"]}
    rows = load_gate_rows(args.answers, args.method)
    if not rows:
        raise SystemExit(f"no {args.method} rows in {args.answers}")

    client = MagpieMultimodalClient(BASE_URL, API_KEY, args.timeout, 2, 3000)
    meter = CreditMeter(args.credit_budget, reserve=args.credit_reserve)
    cache = JsonCache(ROOT / "cache" / "gate_verified.json", "gate:v1")

    samples: list[dict[str, Any]] = []
    for row in rows:
        task = tasks.get(row["task_id"])
        if task is None:
            continue
        ledger = build_ledger_for(task)
        risk = staleness_risk(ledger, task["question"], task["current_frame"],
                               row.get("confidence"))
        target = pick_target(ledger, task["question"], task["current_frame"])
        choice = verification_frame(ledger, task["question"], task["current_frame"])
        samples.append({"task_id": row["task_id"], "subset": row["subset"],
                        "gold": row["gold"], "prediction": row.get("prediction"),
                        "correct": bool(row.get("correct")), "exact": bool(row.get("exact")),
                        "confidence": row.get("confidence"), "risk": risk,
                        "target": target, "verification_frame": choice["frame"],
                        "verification_reason": choice["reason"]})

    pending = []
    for sample in samples:
        cached = cache.get(sample["task_id"]) or {}
        frame = int(sample["verification_frame"])
        entity = (sample.get("target") or {}).get("key")
        observation = reusable_observation(cached, entity, frame)
        # The answers always depend on the ledger, so they are recomputed when
        # the cached record has none; only the single-frame perception is
        # reused, and only when it still applies to the same target/frame.
        if cached.get("answer") and (observation is not None or not entity):
            continue
        pending.append(sample)
    if pending and not args.skip_verification:
        print(f"verifying {len(pending)} samples (1 extra perception + 1 re-answer each); "
              f"MiniMax credits left: {magpie_credits().get('balance')}", flush=True)
        start = time.monotonic()
        done = [0]

        def worker(sample: dict[str, Any]) -> None:
            task = tasks[sample["task_id"]]
            target = sample.get("target")
            frame = int(sample["verification_frame"])
            entity = (target or {}).get("key")
            cached = cache.get(sample["task_id"]) or {}
            record: dict[str, Any] = {"frame": frame, "entity": entity}
            # Control: re-answer with the UNCHANGED ledger.  Any difference
            # between this and the original answer is the "second attempt"
            # effect, not the value of the extra perception.
            original_ledger = build_ledger_for(task)
            control = reanswer(client, task, original_ledger, record)
            meter.record(control.get("meta"))
            record["control_answer"] = control
            observation = reusable_observation(cached, entity, frame)
            if observation is not None:
                record["observation"] = observation
                record["observation_reused"] = True
            elif entity:
                observation = verify_frame(client, task, frame, entity, task["num_frames"])
                record["observation"] = observation
                meter.record(observation.get("meta"))
            ledger = build_ledger_for(task)
            if record.get("observation") and record["observation"].get("present") is not None:
                verified_obs = {
                    "frame": frame,
                    "summary": record["observation"].get("evidence") or "",
                    "entities": [{"entity": entity,
                                  "state": (record["observation"].get("state") or "not visible")
                                  if record["observation"].get("present") else "absent",
                                  "location": None, "confidence": 1.0}],
                    "events": [], "absent": [],
                }
                ledger = apply_verified_observation(ledger, verified_obs)
                record["ledger_after"] = ledger.snapshot(task["current_frame"])
            answer = reanswer(client, task, ledger, record)
            meter.record(answer.get("meta"))
            record["answer"] = answer
            record["time"] = now_iso()
            cache.put(sample["task_id"], record)
            done[0] += 1
            with PRINT_LOCK:
                if done[0] % 20 == 0 or done[0] == len(pending):
                    print(f"  verified {done[0]}/{len(pending)} "
                          f"({time.monotonic()-start:.0f}s)", flush=True)

        for start in range(0, len(pending), 20):
            if meter.exhausted():
                print(f"  credit guard hit - {len(pending) - start} sample(s) left for a later "
                      f"run (spent ~{meter.spent:.1f} credits)", flush=True)
                break
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = [pool.submit(worker, sample)
                           for sample in pending[start:start + 20]]
                for future in as_completed(futures):
                    future.result()

    # attach cached verification results
    for sample in samples:
        record = cache.get(sample["task_id"]) or {}
        answer = record.get("answer") or {}
        control = record.get("control_answer") or {}
        sample["verified_prediction"] = answer.get("prediction")
        sample["verified_correct"] = bool(answer.get("correct"))
        sample["control_prediction"] = control.get("prediction")
        sample["control_correct"] = bool(control.get("correct"))
        sample["verification"] = {"frame": sample["verification_frame"],
                                  "entity": (sample.get("target") or {}).get("key"),
                                  "observation": (record.get("observation") or {}).get("state"),
                                  "present": (record.get("observation") or {}).get("present")}

    # threshold sweep
    thresholds = [round(x / 100, 2) for x in range(0, 101, 5)]
    sweep = []
    for tau in thresholds:
        gated = [s for s in samples if s["risk"]["risk"] > tau]
        accuracy = sum(s["verified_correct"] if s["risk"]["risk"] > tau else s["correct"]
                       for s in samples) / max(1, len(samples))
        exact = sum((s["verified_correct"] if s["risk"]["risk"] > tau else s["exact"])
                    for s in samples) / max(1, len(samples))
        control_accuracy = sum(s["control_correct"] if s["risk"]["risk"] > tau else s["correct"]
                               for s in samples) / max(1, len(samples))
        sweep.append({
            "tau": tau,
            "accuracy": round(accuracy, 6),
            "exact": round(exact, 6),
            "control_accuracy": round(control_accuracy, 6),
            "avg_extra_perception_cost": round(len(gated) / max(1, len(samples)), 6),
            "gated_samples": len(gated),
            "n": len(samples),
        })

    base_accuracy = sum(s["correct"] for s in samples) / max(1, len(samples))
    verified_accuracy = sum(s["verified_correct"] for s in samples) / max(1, len(samples))
    control_accuracy = sum(s["control_correct"] for s in samples) / max(1, len(samples))
    risks = [s["risk"]["risk"] for s in samples]
    correct_risks = [s["risk"]["risk"] for s in samples if s["correct"]]
    wrong_risks = [s["risk"]["risk"] for s in samples if not s["correct"]]
    payload = {
        "created_at": now_iso(),
        "method": args.method,
        "source_run": args.answers.parent.name,
        "n_samples": len(samples),
        "base_accuracy": round(base_accuracy, 6),
        "always_verify_accuracy": round(verified_accuracy, 6),
        "reask_only_accuracy": round(control_accuracy, 6),
        "verification_gain_pp": round((verified_accuracy - control_accuracy) * 100, 4),
        "risk_summary": {
            "mean": round(statistics.mean(risks), 6) if risks else None,
            "median": round(statistics.median(risks), 6) if risks else None,
            "mean_correct": round(statistics.mean(correct_risks), 6) if correct_risks else None,
            "mean_wrong": round(statistics.mean(wrong_risks), 6) if wrong_risks else None,
            "n_correct": len(correct_risks), "n_wrong": len(wrong_risks),
        },
        "sweep": sweep,
        "samples": samples,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "gate_simulation.json", payload)
    with (args.output_dir / "gate_simulation.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sweep[0].keys()))
        writer.writeheader()
        writer.writerows(sweep)
    print(f"base accuracy {base_accuracy:.3f}; re-ask only {control_accuracy:.3f}; "
          f"always-verify {verified_accuracy:.3f}; {len(samples)} samples -> "
          f"{args.output_dir/'gate_simulation.json'}", flush=True)


if __name__ == "__main__":
    main()
