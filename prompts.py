"""Prompt templates with strict parity across the three ablations.

Every method receives the *same* episode, question, answer schema, decoding
parameters and task rules.  The only difference is the ``## History`` block:
how the identical observation stream is organized.

* direct_baseline        raw chronological observations
* append_only_ledger     an append-only state log (stale rows retained)
* versioned_state_ledger a versioned ledger (superseded rows retired)

The perception pass (one objective annotation per frame) is deliberately
question-blind: it never sees the question or the options, so it cannot leak
the answer into the observation stream that all three methods share.
"""
from __future__ import annotations

import json
import re
from typing import Any

METHODS = ("direct_baseline", "append_only_ledger", "versioned_state_ledger",
           "versioned_state_ledger_flat")

# ---------------------------------------------------------------- perception
PERCEPTION_SYSTEM = """You are an objective visual annotator.
Describe ONLY what is visible in this single image. Never guess what happens next, never infer intent, never mention any question.
Return EXACTLY ONE JSON object and no prose, no Markdown:
{
 "summary": "<=25 words: the scene and what the person is doing right now>",
 "scene": "<the place: room / kitchen counter / street / etc>",
 "objects": [{"instance": "<distinctive identifier: category + distinguishing attribute, e.g. 'red apple with sticker'>", "category": "<object class>", "position": "<left|centre|right> <near|far>", "attributes": "<colour / size / shape / state>", "printed_label": "<the number or letter printed on it, or empty>"}],
 "actions": [{"actor": "<person|hand|...>", "action": "<short verb phrase>", "target": "<object it acts on>", "status": "in progress|just started|finished"}],
 "numbers": [{"text": "<number or letter visible in the image>", "position": "<where it appears>"}]
}
Rules: at most 8 objects and 4 actions; keep every string short; include EVERY number or letter printed anywhere in the image; make each object's instance identifier distinctive enough that the same physical object could be recognised in another frame; use an empty string when a field does not apply; output JSON only."""

PERCEPTION_USER = "Frame {index} of {total}. Annotate this frame."

# --------------------------------------------------------------- state tracking
TRACK_SYSTEM = """You are an objective state tracker for a sequence of frames of ONE episode.
You are given the frames and their independent annotations. Your only job is to state, for every frame, which entities exist in that frame and what state each is in.
Rules:
- Use the SAME entity name across frames for the same physical object (e.g. "tortilla", "red apple", "target 3"). Name it by category plus a distinguishing attribute.
- If an entity was visible in an earlier frame but is NOT visible in a later frame, you MUST still list it in that later frame with state "absent". Never silently drop an entity that appeared earlier.
- The person's current activity is an entity named "activity"; its state is what the person is doing in that frame.
- Every printed number or letter is an entity named "label:<text>"; its state is where it appears.
- At most 6 entities per frame; each state is at most 6 words; the summary is at most 12 words.
- Do not answer any question, do not speculate about the future, do not describe anything you cannot see.

Return EXACTLY ONE JSON object and no prose, no Markdown, using this COMPACT schema:
{"frames": [{"f": <frame index>, "s": "<summary, <=12 words>", "e": [["<entity>", "<state, <=6 words>", "<location>"], ...], "v": [["<event, <=5 words>", "<status>"], ...]}],
 "tracks": [["<entity>", <first frame>, <last frame>]]}
Output JSON only."""

TRACK_USER = ("These are the {total} frames of one episode, in chronological order, each followed by "
              "its independent annotation. Track every entity across the sequence.")

TRACK_TERSE = ("Your previous answer was truncated or invalid. Be extremely terse: at most 4 "
               "entities per frame, states of at most 4 words, and output ONLY the JSON object.")

# ------------------------------------------------------------------- answering
ANSWER_RULES_SINGLE = """You answer a question about one long-horizon episode.
You receive an observation history and one multiple-choice question.
Use ONLY the supplied history. Do not invent observations, objects, actions or frames.
If the history supports more than one option, choose the most likely one and lower your confidence; never refuse to answer.

Return EXACTLY ONE JSON object and no prose, Markdown or code fences:
{"answer": "<one option letter>", "confidence": <number between 0 and 1>, "evidence_frames": [<frame indices you relied on>]}
The "answer" field must be exactly one of the option letters listed in the question."""

ANSWER_RULES_OPEN = """You answer a question about one long-horizon episode.
You receive an observation history and one open-ended question.
Use ONLY the supplied history. Do not invent observations, objects, actions or frames.
If the history does not settle the question, give your single best answer anyway and lower your confidence; never refuse to answer.

Return EXACTLY ONE JSON object and no prose, Markdown or code fences:
{"answer": "<the final answer only>", "confidence": <number between 0 and 1>, "evidence_frames": [<frame indices you relied on>]}
The "answer" field must contain the final answer only: follow the format the question asks for (for example "Step 4", "4", or "6,7,5,8"), with no explanation and no units."""

HISTORY_HEADERS = {
    "direct_baseline": "## History: raw chronological observations",
    "append_only_ledger": "## History: append-only state log",
    "versioned_state_ledger": "## History: versioned world-state ledger",
    "versioned_state_ledger_flat": "## History: versioned world-state ledger",
}

HISTORY_NOTES = {
    "direct_baseline": (
        "Every observation of the episode is listed in chronological order. "
        "Each entry describes what was visible in that frame."
    ),
    "append_only_ledger": (
        "The log is APPEND-ONLY: every record ever written is retained. "
        "Later observations do NOT delete or invalidate earlier records, so the same "
        "entity may appear several times with different states. Resolve conflicts yourself."
    ),
    "versioned_state_ledger": (
        "The ledger keeps ONE active record per entity or event. When a later observation "
        "conflicts with an older one, or an entity disappears, the old record is explicitly "
        "marked status=\"superseded\" and kept only as history/evidence. "
        "Only status=\"active\" records describe the current state; superseded records are NOT "
        "alternative current states. staleness_frames = current_frame - last_verified_frame."
    ),
    "versioned_state_ledger_flat": (
        "The ledger keeps ONE active record per entity. When a later observation conflicts with "
        "an older one, the old record is retired into the superseded list and the active record "
        "is updated with the new evidence. Only current_state entries describe the current state; "
        "entries in superseded are history, not alternative current states."
    ),
}


def answer_rules(question_type: str) -> str:
    return ANSWER_RULES_SINGLE if question_type == "single" else ANSWER_RULES_OPEN


def system_prompt(question_type: str) -> str:
    return answer_rules(question_type)


def _compact(rows: Any) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def render_history(method: str, observations: list[dict[str, Any]],
                   ledger_snapshot: dict[str, Any] | None) -> str:
    header = HISTORY_HEADERS[method]
    note = HISTORY_NOTES[method]
    if method == "direct_baseline":
        body = _compact(observations)
    elif method == "append_only_ledger":
        body = _compact((ledger_snapshot or {}).get("records", []))
    elif method == "versioned_state_ledger":
        body = _compact(ledger_snapshot or {})
    elif method == "versioned_state_ledger_flat":
        body = _compact(ledger_snapshot or {})
    else:
        raise ValueError(f"unknown method: {method}")
    return f"{header}\n{note}\n{body}"


def build_user_prompt(method: str, *, episode_id: str, question: str,
                      options: list[str], observations: list[dict[str, Any]],
                      ledger_snapshot: dict[str, Any] | None,
                      current_frame: int, question_type: str = "open",
                      history_override: str | None = None,
                      extra_context: str = "") -> str:
    blocks = [
        f"## Episode\n{episode_id}",
        f"## Current frame index\n{current_frame}",
        history_override if history_override is not None
        else render_history(method, observations, ledger_snapshot),
        f"## Question\n{question}",
    ]
    if options:
        blocks.append("## Options\n" + "\n".join(
            f"{chr(65 + i)}. {opt}" for i, opt in enumerate(options)))
    if extra_context:
        blocks.append(f"## Additional episode context\n{extra_context}")
    blocks.append("## Answer format\nReturn one JSON object with fields answer, confidence, evidence_frames.")
    return "\n".join(blocks)


def build_multimodal_parts(method: str, *, episode_id: str, question: str,
                           options: list[str], observations: list[dict[str, Any]],
                           ledger_snapshot: dict[str, Any] | None,
                           current_frame: int, question_type: str = "open",
                           image_parts: list[dict[str, Any]],
                           frame_labels: list[str] | None = None,
                           extra_context: str = "") -> list[dict[str, Any]]:
    """Same prompt, with the episode frames attached before the question.

    The image stream is identical for every method, so the images themselves
    are never the variable.  The text history block still differs by method.
    """
    labels = frame_labels or [f"frame {i + 1}" for i in range(len(image_parts))]
    intro = ("## Episode\n" + episode_id + "\n"
             f"## Current frame index\n{current_frame}\n"
             f"## Sampled frames\nThe {len(image_parts)} images below are the episode's frames "
             "in chronological order.\n")
    parts: list[dict[str, Any]] = [{"type": "text", "text": intro}]
    for label, image in zip(labels, image_parts):
        parts.append({"type": "text", "text": f"### {label}"})
        parts.append(image)
    if method == "direct_baseline":
        # The frames themselves are the observation history: no state summary.
        history_override = ("## History: raw chronological observations\n"
                            "The frames above are the complete observation history of the "
                            "episode, in chronological order. There is no state summary.")
    else:
        history_override = None
    body = build_user_prompt(method, episode_id=episode_id, question=question,
                             options=options, observations=observations,
                             ledger_snapshot=ledger_snapshot,
                             current_frame=current_frame, question_type=question_type,
                             history_override=history_override, extra_context=extra_context)
    parts.append({"type": "text", "text": body})
    return parts


_JSON_OBJECT = re.compile(r"\{.*?\}", re.S)


def _unwrap_answer(text: str) -> str:
    """Peel nested JSON off an answer string.

    Some providers (Claude via Antigravity, for one) put the whole schema
    object into the ``answer`` FIELD as a string, so the raw text looks like
    ``{"answer": "{\\"answer\\": \\"Step 4\\", ...}"}``.  Unwrapping keeps such
    answers gradable instead of silently counting them wrong.
    """
    current = str(text or "").strip()
    for _ in range(3):
        if not current.startswith("{"):
            return current
        try:
            value = json.loads(current)
        except json.JSONDecodeError:
            return current
        if isinstance(value, dict) and "answer" in value:
            inner = value.get("answer")
            if isinstance(inner, str) and inner.strip().startswith("{"):
                current = inner.strip()
                continue
            return str(inner).strip()
        if isinstance(value, str):
            current = value.strip()
            continue
        return current
    return current


def parse_answer(text: str, options: list[str]) -> dict[str, Any]:
    """Parse the strict JSON answer, with a conservative last-resort fallback."""
    candidates = [text.strip()]
    candidates += [m.group(0) for m in _JSON_OBJECT.finditer(text)]
    for raw in candidates:
        raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            # double-encoded: the whole object arrived as a JSON string
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, dict) or "answer" not in value:
            continue
        answer = _unwrap_answer(value.get("answer"))
        if options:
            letter = _normalize_answer(answer, options)
            if letter is None:
                continue
            answer = letter
        else:
            answer = answer.strip().strip(".").strip()
            if not answer:
                continue
        confidence = value.get("confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None
        frames = value.get("evidence_frames")
        if not isinstance(frames, list):
            frames = []
        return {"answer": answer, "confidence": confidence,
                "evidence_frames": frames, "parsed": True, "raw_answer": answer}
    if options:
        bare = re.search(r"\b([A-Z])\b", text)
        if bare and 0 <= ord(bare.group(1)) - 65 < len(options):
            return {"answer": bare.group(1), "confidence": None, "evidence_frames": [],
                    "parsed": False, "raw_answer": bare.group(1)}
    return {"answer": None, "confidence": None, "evidence_frames": [],
            "parsed": False, "raw_answer": text[:200]}


def _normalize_answer(answer: str, options: list[str]) -> str | None:
    if len(answer) == 1 and answer.upper() in [chr(65 + i) for i in range(len(options))]:
        return answer.upper()
    lowered = answer.strip().lower()
    for i, option in enumerate(options):
        if lowered == str(option).strip().lower():
            return chr(65 + i)
    match = re.match(r"^(?:option\s*)?([A-Z])\b", answer.strip(), re.I)
    if match and ord(match.group(1).upper()) - 65 < len(options):
        return match.group(1).upper()
    return None
