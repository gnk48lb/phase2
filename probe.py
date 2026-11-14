"""Capability probes: which routes answer, how fast, and which accept images.

Run from phase2_planbench/:
    python probe.py --text-models all
    python probe.py --image-model minimax-code-global/MiniMax-M3.1-Flash
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import statistics
import time
from typing import Any

from common import BASE_URL, API_KEY, MagpieMultimodalClient, model_catalog

ROOT = Path(__file__).resolve().parent
PROBE_IMAGE = ROOT / "Plan-Bench" / "image" / "260_1.jpg"


def text_probe(client: MagpieMultimodalClient, model: str, prompt: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        text, meta = client.complete(model, "You are a helpful assistant.", prompt)
        return {"model": model, "ok": True, "latency_s": round(time.monotonic() - started, 2),
                "text": text[:200], "meta": meta}
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "ok": False, "latency_s": round(time.monotonic() - started, 2),
                "error": f"{type(exc).__name__}: {exc}"}


def image_probe(client: MagpieMultimodalClient, model: str, image_path: Path) -> dict[str, Any]:
    started = time.monotonic()
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    parts = [
        {"type": "text", "text": "Look at this image. In one short sentence, state how many "
                                 "distinct round fruits you can see, and name any text/number "
                                 "printed on the image."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
    ]
    try:
        text, meta = client.complete_parts(model, "You are a vision assistant.", parts)
        return {"model": model, "ok": True, "latency_s": round(time.monotonic() - started, 2),
                "text": text[:400], "meta": meta}
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "ok": False, "latency_s": round(time.monotonic() - started, 2),
                "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-models", default="")
    parser.add_argument("--image-model", default="")
    parser.add_argument("--image", type=Path, default=PROBE_IMAGE)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "cache" / "probe_results.json")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    client = MagpieMultimodalClient(BASE_URL, API_KEY, args.timeout, 1, 800)
    results: dict[str, Any] = {"text": [], "image": []}
    if args.text_models:
        for model in [m.strip() for m in args.text_models.split(",") if m.strip()]:
            for _ in range(args.repeats):
                row = text_probe(client, model, "Reply with exactly: OK")
                results["text"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.image_model:
        row = image_probe(client, args.image_model, args.image)
        results["image"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    ok = [r for r in results["text"] if r["ok"]]
    if ok:
        results["text_latency_summary"] = {
            "n": len(ok),
            "median_s": round(statistics.median(r["latency_s"] for r in ok), 2),
            "max_s": round(max(r["latency_s"] for r in ok), 2),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {args.output}")


if __name__ == "__main__":
    main()
