"""Shared plumbing for the Phase-2 Plan-Bench external validation.

The Magpie client itself is imported from ``../gate_dynamic_house/agents.py``
so both phases talk to the same local gateway through exactly the same
transport code.  This module only adds what Phase 2 needs on top of it:

* the quota tier table (unlimited MiniMax vs limited cross-check models),
* an OpenAI-style multimodal request path (same deadline / retry semantics),
* a thread-safe JSON cache used by the one-time frame-caption pass,
* JSONL helpers shared by the runner and the analyzers.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parent
PHASE1 = ROOT.parent / "gate_dynamic_house"
if str(PHASE1) not in sys.path:
    # Reuse the Phase-1 client verbatim.  The Phase-1 directory goes at the END
    # of sys.path so this package's own modules (ledger.py, env.py, ...) always
    # win a name collision; Phase 1's agents.py still resolves its own imports
    # (env, ledger) because its directory is on the path.
    sys.path.append(str(PHASE1))

from agents import EmptyResponseError, MagpieClient, extract_text  # noqa: E402

BASE_URL = os.getenv("MAGPIE_BASE_URL", "http://127.0.0.1:3425")
API_KEY = os.getenv("MAGPIE_API_KEY", "local")

UNLIMITED_MODEL = "minimax-code-global/MiniMax-M3.1-Flash"
# Cross-check models are quota-limited: low concurrency, at most 50 stratified
# samples, never used for a full sweep.  Ordered by observed reliability and
# latency on a ledger-sized prompt (2026-11-14): deepseek ~3s, glm ~8s,
# qwen ~19s; codearts/trae were measured far slower (55-105s) and are left out.
# The opencode-zen-free routes are free but capped at concurrency 2 by the
# provider (measured 0.8-2.1s per request on 2026-11-15).
CROSS_CHECK_CANDIDATES = (
    "opencode-zen-free/deepseek-v4.1-flash-free",
    "opencode-zen-free/glm-5.3-flash-free",
    "opencode-zen-free/minimax-m3.1-flash-free",
    "workbuddy/deepseek-v4.1-flash",
    "autoclaw-cn/zai_glm-5.3-flash",
    "loomy/qwen-3.8-max",
)

# Quota tiers.  ``None`` means "no fixed sample cap" (still bounded by the
# benchmark subset); every other model is explicitly capped at 50 stratified
# samples and concurrency 2.
# Both MiniMax routes serve the SAME model from different upstream account
# pools, so both belong to the unlimited tier.  Which pool pays for a run is a
# budgeting decision, not an experimental one: the route is recorded on every
# row, and the two pools have separate credit balances.
QUOTA_TIERS: dict[str, dict[str, Any]] = {
    UNLIMITED_MODEL: {"concurrency": 10, "max_samples": None, "tier": "unlimited"},
    "minimax-code/MiniMax-M3.1-Flash": {"concurrency": 10, "max_samples": None,
                                         "tier": "unlimited"},
}
DEFAULT_TIER = {"concurrency": 2, "max_samples": 50, "tier": "limited"}


def tier_for(model: str) -> dict[str, Any]:
    return QUOTA_TIERS.get(model, DEFAULT_TIER)


def concurrency_for(model: str) -> int:
    return int(tier_for(model)["concurrency"])


def max_samples_for(model: str) -> int | None:
    cap = tier_for(model)["max_samples"]
    return None if cap is None else int(cap)


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class JsonlWriter:
    """Append-only JSONL writer safe for a thread pool."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict[str, Any]) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


class JsonCache:
    """Thread-safe JSON file cache with strict "one remote call per key" semantics.

    The frame-caption pass is the only paid perception step in the
    captioning route; a crashed or restarted run must never re-bill a frame
    that is already on disk.
    """

    def __init__(self, path: Path, namespace: str):
        self.path = path
        self.namespace = namespace
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}
        else:
            self.data = {}

    def get(self, key: str) -> Any | None:
        with self.lock:
            return self.data.get(f"{self.namespace}:{key}")

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            self.data[f"{self.namespace}:{key}"] = value
            self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self.data)


def image_part(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


class MagpieMultimodalClient(MagpieClient):
    """MagpieClient with an OpenAI-style content-parts request path.

    The parent class only accepts plain strings.  This subclass keeps its
    hard-deadline transport and retry/backoff behaviour, but sends
    ``messages`` whose user content may be a list of text/image parts.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        structured = threading.local()
        self._structured_error = structured

    def complete_parts(self, model: str, system: str,
                       parts: list[dict[str, Any]],
                       max_tokens: int | None = None) -> tuple[str, dict[str, Any]]:
        budget = max_tokens or self.max_tokens
        body: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_tokens": budget,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }
        import urllib.request
        last_error = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                self.url,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
            )
            try:
                payload = json.loads(self._post_with_deadline(request, self.timeout))
                text = extract_text(payload)
                if not text.strip():
                    raise EmptyResponseError("empty model text")
                usage = payload.get("usage", {})
                meta = {
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                    "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
                    "total_tokens": usage.get("total_tokens"),
                    "http_status": 200,
                    "request_retries": attempt,
                    "finish_reason": (payload.get("choices") or [{}])[0].get("finish_reason"),
                    "reasoning_chars": len(payload.get("choices", [{}])[0]
                                           .get("message", {}).get("reasoning_content") or ""),
                }
                return text, meta
            except EmptyResponseError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                body["max_tokens"] = min(int(body["max_tokens"] * 2), 16000)
                if attempt < self.max_retries:
                    time.sleep((2 ** attempt) * 0.5 + random.random() * 0.2)
            except Exception as exc:  # noqa: BLE001 - mirror parent retry semantics
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep((2 ** attempt) * 0.5 + random.random() * 0.2)
        raise RuntimeError(f"Magpie multimodal request failed after {self.max_retries + 1} attempts: {last_error}")

    def complete_robust(self, model: str, system: str, user: str,
                        max_tokens: int = 4000, attempts: int = 3) -> tuple[str, dict[str, Any]]:
        """Text completion with an escalating anti-rumination retry ladder.

        Reasoning-heavy routes (GLM-5.3-Flash, DeepSeek-V4-Flash) sometimes spend
        the whole token budget thinking and return an empty body -- measured at
        ~40% of calls on the Loomy routes.  The later attempts add an explicit
        "output only the JSON object" nudge instead of merely raising the
        budget, which is what actually recovers those cells.
        """
        last_error: str | None = None
        for attempt in range(attempts):
            suffix = "" if attempt == 0 else (
                "\n\nOutput only the JSON object. Do not explain, do not reason aloud, "
                "do not add any other text.")
            try:
                text, meta = self.complete(model, system, user + suffix)
                if text.strip():
                    return text, meta
                last_error = "empty model response"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"Magpie request failed after {attempts} attempts: {last_error}")


def model_catalog(base_url: str = BASE_URL, api_key: str = API_KEY) -> list[dict[str, Any]]:
    import urllib.request
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/models",
                                     headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))["data"]


def require_model_ids(models: list[str], catalog: list[dict[str, Any]] | None = None) -> list[str]:
    available = {entry["id"] for entry in (catalog or model_catalog())}
    missing = [model for model in models if model not in available]
    if missing:
        raise RuntimeError(f"Models not in live Magpie catalog: {missing}")
    return models


def available_cross_models(catalog: list[dict[str, Any]] | None = None) -> list[str]:
    available = {entry["id"] for entry in (catalog or model_catalog())}
    return [model for model in CROSS_CHECK_CANDIDATES if model in available]


# ------------------------------------------------------------------ credits
# Magpie keeps its per-provider quota state under ~/.config/magpie.  The
# MiniMax routes are NOT unlimited: they draw from a pool of credits that is
# shared with every other client of the local gateway (including this very
# session), so a run has to know when to stop.
MAGPIE_CONFIG = Path(os.getenv("MAGPIE_CONFIG_DIR", Path.home() / ".config" / "magpie"))
CREDITS_PER_MILLION_TOKENS = 322.0  # measured on 2026-11-14 from credits-daily vs usage.jsonl


def magpie_credits(provider_prefix: str = "minimax") -> dict[str, Any]:
    """Live credit balances for every account of the matching providers."""
    path = MAGPIE_CONFIG / "quotas.json"
    if not path.exists():
        return {"available": False, "balance": None, "accounts": {}}
    try:
        quotas = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"available": False, "balance": None, "accounts": {}}
    accounts: dict[str, float] = {}
    for key, value in quotas.items():
        provider = (value.get("quota") or {}).get("provider", "")
        if not provider.startswith(provider_prefix):
            continue
        balance = (value.get("quota") or {}).get("balance", "")
        digits = "".join(ch for ch in str(balance) if ch.isdigit() or ch == ".")
        try:
            accounts[key] = float(digits)
        except ValueError:
            continue
    return {"available": bool(accounts), "balance": sum(accounts.values()),
            "accounts": accounts, "read_at": now_iso()}


class CreditMeter:
    """Estimates credit spend during a run and stops it when the budget is gone.

    The estimate uses the measured rate (CREDITS_PER_MILLION_TOKENS on input
    + output tokens).  Every answer is written to disk as soon as it is
    produced, so an interrupted run resumes exactly where it stopped.
    """

    def __init__(self, budget: float = 0.0, provider_prefix: str = "minimax",
                 reserve: float = 0.0) -> None:
        self.budget = float(budget)
        self.provider_prefix = provider_prefix
        # Credits that must stay in the pool: the run stops before eating them.
        self.reserve = float(reserve)
        self.lock = threading.Lock()
        self.tokens_in = 0
        self.tokens_out = 0
        self.requests = 0
        self.start_balance = magpie_credits(provider_prefix).get("balance")

    def record(self, meta: dict[str, Any] | None, provider: str = "") -> None:
        """Account one request. Only requests served by this meter's provider
        count: the other providers have their own credit pools."""
        if not meta:
            return
        if provider and not provider.startswith(self.provider_prefix):
            return
        with self.lock:
            self.requests += 1
            self.tokens_in += int(meta.get("input_tokens") or 0)
            self.tokens_out += int(meta.get("output_tokens") or 0)

    @property
    def spent(self) -> float:
        with self.lock:
            tokens = self.tokens_in + self.tokens_out
        return tokens / 1e6 * CREDITS_PER_MILLION_TOKENS

    def remaining(self) -> float | None:
        if self.budget <= 0:
            return None
        return max(0.0, self.budget - self.spent)

    def exhausted(self) -> bool:
        """True when the estimated spend reached the budget or the pool hit the floor."""
        if self.budget > 0 and self.spent >= self.budget:
            return True
        live = magpie_credits(self.provider_prefix)
        if live.get("available") and live.get("balance") is not None:
            # Stop before the pool drops to the reserve line.
            if live["balance"] <= self.reserve + 1.0:
                return True
        return False

    def snapshot(self) -> dict[str, Any]:
        live = magpie_credits(self.provider_prefix)
        return {"requests": self.requests, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "estimated_credits_spent": round(self.spent, 2),
                "budget_credits": self.budget, "reserve_credits": self.reserve,
                "live_balance_before": self.start_balance,
                "live_balance_now": live.get("balance")}