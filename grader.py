"""Deterministic grading for Plan-Bench answers.

Plan-Bench temporal items are 35 single-choice and 175 open-ended.  Open
answers are short strings ("Step 4", "4", "1", "6,7,5,8").  Two accuracies are
reported everywhere:

``exact``      normalized string equality (case/space/punctuation insensitive)
``graded``     exact, or numerically / sequence-wise equivalent ("4" == "Step 4")

The grader never consults the model.
"""
from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z]+")


def _norm_text(text: str) -> str:
    text = str(text or "").strip().lower()
    text = text.replace("step", " ").replace("no.", " ")
    text = re.sub(r"[^\w,.\- ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_numeric(text: str) -> bool:
    return bool(_NUM.fullmatch(str(text or "").strip()))


def _numbers(text: str) -> list[float]:
    return [float(m) for m in _NUM.findall(str(text or ""))]


def _sequence(text: str) -> list[float] | None:
    raw = str(text or "").strip()
    if "," not in raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def grade_answer(prediction: str | None, gold: str, question_type: str) -> dict[str, Any]:
    """Return {'correct': bool, 'exact': bool, 'graded': bool, 'detail': str}."""
    if prediction is None:
        return {"correct": False, "exact": False, "graded": False, "detail": "no_answer"}
    if question_type == "single":
        exact = str(prediction).strip().upper() == str(gold).strip().upper()
        return {"correct": exact, "exact": exact, "graded": exact, "detail": "letter"}
    pred_norm, gold_norm = _norm_text(prediction), _norm_text(gold)
    if pred_norm == gold_norm:
        return {"correct": True, "exact": True, "graded": True, "detail": "exact"}
    if _is_numeric(gold_norm):
        # Numeric gold: only a purely numeric prediction can match, so "1,4"
        # never counts as "4".
        pred_seq, gold_seq = _sequence(prediction), _sequence(gold)
        if pred_seq is not None and gold_seq is not None:
            if pred_seq == gold_seq:
                return {"correct": True, "exact": False, "graded": True, "detail": "sequence"}
            return {"correct": False, "exact": False, "graded": False, "detail": "sequence_mismatch"}
        pred_nums, gold_nums = _numbers(prediction), _numbers(gold)
        if len(pred_nums) == 1 and len(gold_nums) == 1 and abs(pred_nums[0] - gold_nums[0]) < 1e-9:
            return {"correct": True, "exact": False, "graded": True, "detail": "numeric"}
        return {"correct": False, "exact": False, "graded": False, "detail": "mismatch"}
    # Non-numeric gold (e.g. a comma list of labels): allow a containment match.
    if gold_norm and re.search(r"(?<!\w)" + re.escape(gold_norm) + r"(?!\w)", pred_norm):
        return {"correct": True, "exact": False, "graded": True, "detail": "contains"}
    return {"correct": False, "exact": False, "graded": False, "detail": "mismatch"}
