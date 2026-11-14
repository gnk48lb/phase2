"""Prepare the Plan-Bench temporal subset for the Phase-2 experiments.

Steps
-----
1. make sure the Plan-Bench checkout exists (clone it if it does not);
2. parse ``questions.tsv`` and keep the Temporal Reasoning dimension;
3. probe the multimodal capability of the unlimited MiniMax route;
4. perception, two stages, both question-blind and both cached:
   a. one objective annotation per frame      -> cache/frame_captions.json
   b. one state track per episode (frames +   -> cache/state_tracks.json
      annotations, entity continuity and explicit absence)
5. turn the tracks into the canonical observation stream every ablation
   method shares, and write ``cache/tasks.json``.

The perception passes never see the question or the options, so the shared
observation stream cannot leak the answer into one of the ablations.

Usage (from phase2_planbench/):
    python prepare_data.py
    python prepare_data.py --limit 10            # smoke
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from common import (BASE_URL, API_KEY, CreditMeter, JsonCache, MagpieMultimodalClient,
                    UNLIMITED_MODEL, image_part, magpie_credits, now_iso, write_json)
from prompts import (PERCEPTION_SYSTEM, PERCEPTION_USER, TRACK_SYSTEM, TRACK_TERSE, TRACK_USER)

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "Plan-Bench"
TSV = REPO / "questions.tsv"
CACHE = ROOT / "cache"
REPO_URL = "https://github.com/MinneFeng/Plan-Bench"
PRINT_LOCK = threading.Lock()


# --------------------------------------------------------------------- clone
def ensure_repo() -> None:
    if (REPO / "questions.tsv").exists():
        print(f"Plan-Bench already present at {REPO}", flush=True)
        return
    REPO.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {REPO_URL} -> {REPO}", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(REPO)], check=True)


# ------------------------------------------------------------------- parsing
def parse_tasks(category: str = "temporal") -> list[dict[str, Any]]:
    if not TSV.exists():
        raise FileNotFoundError(f"{TSV} not found; run the clone step first")
    with TSV.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    tasks: list[dict[str, Any]] = []
    for row in rows:
        if row["category"] != category:
            continue
        images = [p.strip() for p in row["question_image"].split(",") if p.strip()]
        options = [row[key].strip() for key in ("A", "B", "C", "D") if row.get(key, "").strip()]
        if row["question_type"] == "single" and len(options) < 2:
            raise ValueError(f"row {row['index']} is single-choice but has no options")
        if row["question_type"] == "open":
            # Open items keep their option text out of the prompt; the gold is a
            # free-form string.
            options = []
        tasks.append({
            "task_id": f"pb{row['index']}",
            "index": row["index"],
            "subset": row["category_2"],
            "dimension": row["category"],
            "question_type": row["question_type"],
            "question": row["question"].strip(),
            "options": options,
            "gold": row["answer"].strip(),
            "answer_hint": row.get("answer_hint", "").strip(),
            "frame_images": images,
            "current_frame": max(0, len(images) - 1),
            "num_frames": len(images),
        })
    return tasks


# ---------------------------------------------------------------- capabilities
def probe_capabilities(client: MagpieMultimodalClient, image_path: Path) -> dict[str, Any]:
    """One minimal image request: does the route accept images natively?"""
    parts = [
        {"type": "text", "text": "Describe this image in five words."},
        image_part(image_path),
    ]
    started = time.monotonic()
    try:
        text, meta = client.complete_parts(UNLIMITED_MODEL, "You are a vision assistant.",
                                           parts, max_tokens=4000)
        supported = bool(text.strip())
        return {"minimax_multimodal": supported, "latency_s": round(time.monotonic() - started, 2),
                "sample": text[:200], "meta": meta, "image": str(image_path)}
    except Exception as exc:  # noqa: BLE001
        return {"minimax_multimodal": False, "latency_s": round(time.monotonic() - started, 2),
                "error": f"{type(exc).__name__}: {exc}", "image": str(image_path)}


# ------------------------------------------------------------------- parsing of model JSON
_JSON = re.compile(r"\{.*\}", re.S)


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip().removeprefix("```json").removesuffix("```").strip()
    candidates = [raw] + [m.group(0) for m in _JSON.finditer(text)]
    for candidate in candidates:
        candidate = candidate.strip()
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                value = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


# ----------------------------------------------------------------- stage 1: captions
def caption_one(client: MagpieMultimodalClient, cache: JsonCache, rel_path: str,
                index: int, total: int, model: str) -> dict[str, Any]:
    cached = cache.get(rel_path)
    if cached is not None and not cached.get("_error"):
        return cached
    absolute = REPO / rel_path
    text_hint = PERCEPTION_USER.format(index=index, total=total)
    caption: dict[str, Any] | None = None
    for attempt in range(2):
        parts = [{"type": "text", "text": text_hint}, image_part(absolute)]
        try:
            text, meta = client.complete_parts(model, PERCEPTION_SYSTEM, parts, max_tokens=4000)
        except Exception as exc:  # noqa: BLE001
            caption = {"summary": "", "scene": "", "objects": [], "actions": [], "numbers": [],
                       "_error": f"{type(exc).__name__}: {exc}"}
            break
        parsed = parse_json_object(text)
        if parsed is not None:
            caption = parsed
            caption["_meta"] = {"latency_s": meta.get("latency_ms", 0) / 1000,
                                "tokens": meta.get("total_tokens"),
                                "in": meta.get("input_tokens"), "out": meta.get("output_tokens")}
            break
        text_hint = (PERCEPTION_USER.format(index=index, total=total)
                     + " Your previous answer was not valid JSON. Output ONLY one JSON object.")
    if caption is None:
        caption = {"summary": "", "scene": "", "objects": [], "actions": [], "numbers": [],
                   "_error": "json_parse_failed"}
    cache.put(rel_path, caption)
    return caption


# ------------------------------------------------------------- stage 2: tracks
def track_one(client: MagpieMultimodalClient, cache: JsonCache, task: dict[str, Any],
              captions: list[dict[str, Any]], model: str) -> dict[str, Any]:
    key = task["task_id"]
    cached = cache.get(key)
    if cached is not None and not cached.get("_error"):
        return cached
    parts: list[dict[str, Any]] = [{"type": "text", "text": TRACK_USER.format(total=task["num_frames"])}]
    for index, (rel, caption) in enumerate(zip(task["frame_images"], captions)):
        annotation = json.dumps({k: v for k, v in caption.items() if not k.startswith("_")},
                                ensure_ascii=False, separators=(",", ":"))
        parts.append({"type": "text", "text": f"### frame {index} ({rel})\nannotation: {annotation}"})
        parts.append(image_part(REPO / rel))
    track: dict[str, Any] | None = None
    for attempt in range(2):
        attempt_parts = list(parts)
        if attempt:
            attempt_parts = attempt_parts + [{"type": "text", "text": TRACK_TERSE}]
        try:
            text, meta = client.complete_parts(model, TRACK_SYSTEM, attempt_parts, max_tokens=12000)
        except Exception as exc:  # noqa: BLE001
            track = {"frames": [], "tracks": [], "_error": f"{type(exc).__name__}: {exc}"}
            break
        parsed = parse_json_object(text)
        if parsed is not None and isinstance(parsed.get("frames"), list) and parsed["frames"]:
            track = parsed
            track["_meta"] = {"latency_s": meta.get("latency_ms", 0) / 1000,
                              "tokens": meta.get("total_tokens"),
                              "in": meta.get("input_tokens"), "out": meta.get("output_tokens"),
                              "finish_reason": meta.get("finish_reason"),
                              "attempt": attempt}
            break
    if track is None:
        track = {"frames": [], "tracks": [], "_error": "json_parse_failed"}
    cache.put(key, track)
    return track


# ------------------------------------------------- canonical observation stream
def observations_from_track(task: dict[str, Any], track: dict[str, Any],
                             captions: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    frames = track.get("frames") or []
    by_index: dict[int, dict[str, Any]] = {}
    for position, frame in enumerate(frames):
        index = frame.get("f", frame.get("frame", position))
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = position
        by_index[index] = frame
    observations = []
    for index in range(task["num_frames"]):
        frame = by_index.get(index)
        if frame is None:
            return None
        entities = []
        for ent in frame.get("e", frame.get("entities", [])) or []:
            if isinstance(ent, dict):
                name = str(ent.get("entity") or "").strip()
                state = ent.get("state")
                location = ent.get("location")
                confidence = ent.get("confidence", 0.6)
            else:
                parts = list(ent) if isinstance(ent, (list, tuple)) else []
                name = str(parts[0]).strip() if parts else ""
                state = parts[1] if len(parts) > 1 else None
                location = parts[2] if len(parts) > 2 else None
                confidence = 0.6
            if not name:
                continue
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.6
            entities.append({"entity": name, "state": state,
                             "location": location, "confidence": confidence})
        events = []
        for ev in frame.get("v", frame.get("events", [])) or []:
            if isinstance(ev, dict):
                events.append({"event": str(ev.get("event") or "").strip(),
                               "entities": ev.get("entities", []) or [],
                               "status": ev.get("status", "observed"), "confidence": 0.6})
            else:
                parts = list(ev) if isinstance(ev, (list, tuple)) else []
                events.append({"event": str(parts[0]).strip() if parts else "",
                               "entities": [], "status": parts[1] if len(parts) > 1 else "observed",
                               "confidence": 0.6})
        summary = str(frame.get("s", frame.get("summary", "")) or "")
        if not summary:
            summary = str(captions[index].get("summary") or "") if index < len(captions) else ""
        observations.append({"frame": index, "summary": summary,
                             "entities": entities, "events": events, "absent": []})
    return observations


def observations_from_captions(task: dict[str, Any],
                               captions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback: per-frame annotations only, no cross-frame identity claims."""
    observations = []
    for index, caption in enumerate(captions):
        entities = []
        scene = caption.get("scene") or None
        for obj in caption.get("objects", []) or []:
            instance = obj.get("instance") or obj.get("category")
            if not instance:
                continue
            entities.append({"entity": str(instance), "state": obj.get("position"),
                             "location": scene, "confidence": 0.6})
            label = str(obj.get("printed_label") or "").strip()
            if label:
                entities.append({"entity": f"label:{label}", "state": obj.get("position"),
                                 "location": scene, "confidence": 0.6})
        actions = caption.get("actions", []) or []
        activity = "; ".join(
            f"{a.get('actor', 'person')} {a.get('action', '')} {a.get('target', '')}".strip()
            for a in actions[:2]) or str(caption.get("summary") or "")[:120]
        entities.append({"entity": "activity", "state": activity or None,
                         "location": scene, "confidence": 0.6})
        events = [{"event": f"{a.get('action', '')} {a.get('target', '')}".strip(),
                   "entities": [a.get("target")] if a.get("target") else [],
                   "status": a.get("status", "observed"), "confidence": 0.6}
                  for a in actions[:4]]
        observations.append({"frame": index, "summary": str(caption.get("summary") or ""),
                             "scene": scene, "entities": entities, "events": events, "absent": []})
    return observations


# ----------------------------------------------------------------------- main
def run_pool(items: list[Any], worker, meter, concurrency: int, label: str,
             chunk: int = 40) -> list[Any]:
    """Run `worker` over items in chunks, honouring the credit guard.

    Returns the items that were NOT processed (the credit guard stopped the
    run); they stay uncached and are picked up by the next invocation.
    """
    for start in range(0, len(items), chunk):
        if meter.exhausted():
            left = items[start:]
            print(f"{label}: credit guard hit - {len(left)} item(s) left for a later run "
                  f"(spent ~{meter.spent:.1f} credits)", flush=True)
            return left
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, item) for item in items[start:start + chunk]]
            for future in as_completed(futures):
                future.result()
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", default="temporal",
                        help="Plan-Bench dimension to use (temporal | planning | observation)")
    parser.add_argument("--limit", type=int, default=0, help="only the first N tasks (smoke)")
    parser.add_argument("--perception-model", default=UNLIMITED_MODEL)
    parser.add_argument("--perception-concurrency", type=int, default=20)
    parser.add_argument("--skip-perception", action="store_true",
                        help="reuse the caches without new calls")
    parser.add_argument("--credit-budget", type=float, default=900.0,
                        help="stop the perception pass after this many estimated credits; "
                             "unfinished frames/episodes stay uncached for the next run")
    parser.add_argument("--credit-reserve", type=float, default=2000.0,
                        help="credits that must stay in the pool; the perception pass "
                             "stops before the live balance reaches this line")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    ensure_repo()
    CACHE.mkdir(parents=True, exist_ok=True)
    tasks = parse_tasks(args.category)
    if args.limit:
        tasks = tasks[:args.limit]
    print(f"{len(tasks)} {args.category} tasks; "
          f"{len({t['subset'] for t in tasks})} subtasks; "
          f"{sum(t['num_frames'] for t in tasks)} frames", flush=True)

    client = MagpieMultimodalClient(BASE_URL, API_KEY, args.timeout, 2, 4000)

    # 1) capability probe
    caps_path = CACHE / "capabilities.json"
    caps = json.loads(caps_path.read_text(encoding="utf-8")) if caps_path.exists() else {}
    probe = probe_capabilities(client, REPO / tasks[0]["frame_images"][0])
    caps.update(probe)
    caps["probed_at"] = now_iso()
    write_json(caps_path, caps)
    print(f"multimodal probe: {caps.get('minimax_multimodal')} ({caps.get('latency_s')}s) "
          f"-> {probe.get('sample', probe.get('error'))!r}", flush=True)

    # 2a) per-frame captions
    caption_cache = JsonCache(CACHE / "frame_captions.json", "caption:v1")
    unique_images: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        for rel in task["frame_images"]:
            if rel not in seen:
                seen.add(rel)
                unique_images.append(rel)
    todo = [rel for rel in unique_images
            if (caption_cache.get(rel) or {}).get("_error") or caption_cache.get(rel) is None]
    print(f"stage 1 (captions): {len(unique_images)} unique frames, {len(todo)} to caption; "
          f"MiniMax credits left: {magpie_credits().get('balance')}", flush=True)
    meter = CreditMeter(args.credit_budget, reserve=args.credit_reserve)
    if todo and not args.skip_perception:
        counter = [0]
        start = time.monotonic()

        def caption_worker(rel: str) -> None:
            task_for_frame = next(t for t in tasks if rel in t["frame_images"])
            index = task_for_frame["frame_images"].index(rel) + 1
            caption = caption_one(client, caption_cache, rel, index,
                                  task_for_frame["num_frames"], args.perception_model)
            meta = caption.get("_meta", {})
            meter.record({"input_tokens": meta.get("in"), "output_tokens": meta.get("out")})
            counter[0] += 1
            with PRINT_LOCK:
                if counter[0] % 100 == 0 or counter[0] == len(todo):
                    print(f"  captioned {counter[0]}/{len(todo)} "
                          f"({counter[0] / max(1e-9, time.monotonic() - start):.1f}/s, "
                          f"credits~{meter.spent:.0f})", flush=True)

        left = run_pool(todo, caption_worker, meter, args.perception_concurrency, "stage 1")

    # 2b) per-episode state tracks
    track_cache = JsonCache(CACHE / "state_tracks.json", "track:v1")
    captions_by_task = {task["task_id"]: [caption_cache.get(rel) or {} for rel in task["frame_images"]]
                        for task in tasks}
    pending_tracks = [task for task in tasks
                      if (track_cache.get(task["task_id"]) or {}).get("_error")
                      or track_cache.get(task["task_id"]) is None]
    print(f"stage 2 (tracks): {len(pending_tracks)} episodes to track; "
          f"credits left: {magpie_credits().get('balance')}", flush=True)
    if pending_tracks and not args.skip_perception:
        counter = [0]
        start = time.monotonic()

        def track_worker(task: dict[str, Any]) -> None:
            track = track_one(client, track_cache, task, captions_by_task[task["task_id"]],
                              args.perception_model)
            meta = track.get("_meta", {})
            meter.record({"input_tokens": meta.get("in"), "output_tokens": meta.get("out")})
            counter[0] += 1
            with PRINT_LOCK:
                if counter[0] % 25 == 0 or counter[0] == len(pending_tracks):
                    print(f"  tracked {counter[0]}/{len(pending_tracks)} "
                          f"({counter[0] / max(1e-9, time.monotonic() - start):.1f}/s, "
                          f"credits~{meter.spent:.0f})", flush=True)

        left = run_pool(pending_tracks, track_worker, meter, args.perception_concurrency, "stage 2")
        if left:
            write_json(CACHE / "pending_tracks.json",
                       {"remaining": [task["task_id"] for task in left], "time": now_iso()})

    # 3) canonical observation stream
    out_tasks = []
    tracked = 0
    for task in tasks:
        captions = captions_by_task[task["task_id"]]
        track = track_cache.get(task["task_id"]) or {}
        observations = observations_from_track(task, track, captions)
        source = "track"
        if observations is None:
            observations = observations_from_captions(task, captions)
            source = "captions"
        else:
            tracked += 1
        out_tasks.append({**task, "observations": observations, "observation_source": source})
    payload = {"generated_at": now_iso(), "category": args.category, "capabilities": caps,
               "tasks": out_tasks}
    write_json(CACHE / "tasks.json", payload)
    print(f"wrote {CACHE / 'tasks.json'}: {len(out_tasks)} tasks "
          f"({tracked} from state tracks, {len(out_tasks) - tracked} caption fallback)", flush=True)


if __name__ == "__main__":
    main()
