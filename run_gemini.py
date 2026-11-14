"""Run Plan-Bench on Antigravity's internal Gemini model via agy CLI.

This provides an independent control group without touching Magpie credits
or external rate limits.

    python run_gemini.py --subset-file cache/cross_subset_50.json
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import threading
import time
from typing import Any

from common import JsonlWriter, load_jsonl
from grader import grade_answer
from memory_ledger import build_ledger
from prompts import build_user_prompt, parse_answer, system_prompt

AGY_BIN = r"C:\Users\gnk48\.gemini\bin\agy.exe"
ROOT = Path(__file__).resolve().parent
METHODS = ("direct_baseline", "append_only_ledger", "versioned_state_ledger")
PRINT_LOCK = threading.Lock()

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "The final answer only"},
        "confidence": {"type": "number", "description": "Confidence from 0 to 1"},
        "evidence_frames": {"type": "array", "items": {"type": "integer"},
                            "description": "Evidence frame indices"}
    },
    "required": ["answer"]
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_history(task: dict[str, Any], method: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    observations = task.get("observations") or []
    if method == "direct_baseline":
        return observations, None
    ledger = build_ledger(method)
    for obs in observations:
        ledger.ingest(obs)
    return observations, ledger.snapshot(task["current_frame"])


def call_agy(prompt: str, model: str, timeout_s: float = 120.0) -> tuple[dict[str, Any], dict[str, Any]]:
    cmd = [
        AGY_BIN,
        "--print", prompt,
        "--model", model,
        "--disable-slash-commands",
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA)
    ]
    started = time.monotonic()
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    latency_ms = round((time.monotonic() - started) * 1000)
    if res.returncode != 0:
        raise RuntimeError(f"agy exit code {res.returncode}: {res.stderr[:400]}")
    payload = json.loads(res.stdout)
    usage = payload.get("usage") or {}
    meta = {
        "latency_ms": latency_ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "http_status": 200,
        "request_retries": 0,
        "conversation_id": payload.get("conversation_id"),
    }
    return payload, meta


def run_one(task: dict[str, Any], method: str, model_tag: str, agy_model: str,
            timeout_s: float = 120.0) -> dict[str, Any]:
    started = time.monotonic()
    observations, snapshot = build_history(task, method)
    system = system_prompt(task["question_type"])
    user = build_user_prompt(
        method,
        episode_id=task["task_id"],
        question=task["question"],
        options=task.get("options") or [],
        observations=observations,
        ledger_snapshot=snapshot,
        current_frame=task["current_frame"],
        question_type=task["question_type"],
    )
    full_prompt = f"{system}\n\n{user}\n\nOutput only the required JSON via the schema."
    prompt_chars = len(full_prompt)

    error = None
    parsed_answer = None
    confidence = None
    evidence_frames = []
    is_parsed = False
    raw_answer = None
    raw_response = ""
    meta: dict[str, Any] = {}

    for attempt in range(2):
        try:
            payload, meta = call_agy(full_prompt, agy_model, timeout_s)
            raw_response = payload.get("response") or ""
            struct = payload.get("structured_output")
            if isinstance(struct, dict) and struct.get("answer"):
                raw_answer = str(struct["answer"])
                confidence = struct.get("confidence")
                evidence_frames = struct.get("evidence_frames") or []
                parsed_answer = raw_answer
                is_parsed = True
            elif raw_response:
                parsed = parse_answer(raw_response, task.get("options") or [])
                raw_answer = parsed.get("raw_answer")
                parsed_answer = parsed.get("answer")
                confidence = parsed.get("confidence")
                evidence_frames = parsed.get("evidence_frames") or []
                is_parsed = parsed.get("parsed", False)

            if is_parsed and parsed_answer is not None:
                error = None
                break
            error = "failed to parse structured output"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    # Clean parsed_answer if options exist
    if is_parsed and task.get("options") and parsed_answer:
        # Match single choice letter A/B/C
        parsed_answer = str(parsed_answer).strip()
        if len(parsed_answer) > 1 and parsed_answer[0] in "ABCDEF":
            parsed_answer = parsed_answer[0]

    grade = grade_answer(parsed_answer, task["gold"], task["question_type"])
    return {
        "task_id": task["task_id"],
        "subset": task["subset"],
        "question_type": task["question_type"],
        "model": model_tag,
        "method": method,
        "mode": "text",
        "gold": task["gold"],
        "prediction": parsed_answer,
        "correct": grade["correct"],
        "exact": grade["exact"],
        "graded_detail": grade["detail"],
        "confidence": confidence,
        "evidence_frames": evidence_frames,
        "parsed": is_parsed,
        "raw_answer": raw_answer,
        "raw_response": str(raw_response)[:6000],
        "request": meta,
        "error": error,
        "latency_s": round(time.monotonic() - started, 3),
        "time": now_iso(),
        "prompt_chars": prompt_chars,
        "images": 0,
        "num_frames": task["num_frames"],
        "observation_source": task.get("observation_source"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "cache" / "tasks.json")
    parser.add_argument("--subset-file", type=Path, default=ROOT / "cache" / "cross_subset_50.json")
    parser.add_argument("--methods", default="direct_baseline,append_only_ledger,versioned_state_ledger")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results" / "txt_gemini")
    parser.add_argument("--agy-model", default="gemini-3.8-flash-low")
    parser.add_argument("--model-tag", default="antigravity/gemini-3.8-flash")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    answers_path = output / "answers.jsonl"

    payload = json.loads(args.tasks.read_text(encoding="utf-8"))
    tasks = payload["tasks"] if isinstance(payload, dict) else payload

    if args.subset_file:
        wanted = set(json.loads(args.subset_file.read_text(encoding="utf-8")))
        tasks = [t for t in tasks if t["task_id"] in wanted]
    if args.limit:
        tasks = tasks[:args.limit]

    done = {(row["model"], row["method"], row["task_id"], row["mode"])
            for row in load_jsonl(answers_path)}
    bundles = [task for task in tasks
               if any((args.model_tag, method, task["task_id"], "text") not in done
                      for method in methods)]
    random.Random(20261114).shuffle(bundles)
    total_calls = len(tasks) * len(methods)

    writer = JsonlWriter(answers_path)
    failures_writer = JsonlWriter(output / "failures.jsonl")
    counts = len(done)
    start = time.monotonic()

    print(f"Running Gemini control: {len(tasks)} tasks x {len(methods)} methods = {total_calls} calls; "
          f"{len(bundles)} task bundle(s) left; concurrency={args.concurrency}", flush=True)

    def execute_bundle(task: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal counts
        rows = []
        for method in methods:
            if (args.model_tag, method, task["task_id"], "text") in done:
                continue
            row = run_one(task, method, args.model_tag, args.agy_model, args.timeout)
            writer.write(row)
            if row["error"] or not row["parsed"]:
                failures_writer.write(row)
            with PRINT_LOCK:
                counts += 1
                print(f"[{counts}/{total_calls}] {row['model']} {row['method']} {row['task_id']}: "
                      f"correct={row['correct']} exact={row['exact']} conf={row['confidence']} "
                      f"{row['latency_s']}s elapsed={time.monotonic() - start:.0f}s", flush=True)
            rows.append(row)
        return rows

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(execute_bundle, task) for task in bundles]
        for f in as_completed(futures):
            f.result()

    print(f"Done in {time.monotonic() - start:.1f}s -> {answers_path}", flush=True)


if __name__ == "__main__":
    main()
