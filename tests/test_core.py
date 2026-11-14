"""Unit tests for the Phase-2 core logic (no network needed).

Run from phase2_planbench/:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grader import grade_answer
from memory_ledger import (AppendOnlyLedger, VersionedLedger, build_ledger,
                            staleness_risk, verification_frame)
from prompts import parse_answer, render_history


def observation(frame: int, entity: str, state: str, location: str = "kitchen",
                summary: str = "s") -> dict:
    return {"frame": frame, "summary": summary, "scene": location,
            "entities": [{"entity": entity, "state": state, "location": location,
                          "confidence": 0.8}],
            "events": [], "absent": []}


class TestGrader(unittest.TestCase):
    def test_exact_and_case(self):
        self.assertTrue(grade_answer("Step 4", "Step 4", "open")["exact"])
        self.assertTrue(grade_answer("  step 4 ", "Step 4", "open")["graded"])

    def test_numeric_gold_rejects_longer_number(self):
        self.assertFalse(grade_answer("24", "4", "open")["correct"])
        self.assertFalse(grade_answer("1,4", "4", "open")["correct"])
        self.assertTrue(grade_answer("4", "4", "open")["correct"])

    def test_sequence_gold(self):
        self.assertTrue(grade_answer("6,7,5,8", "6,7,5,8", "open")["graded"])
        self.assertFalse(grade_answer("6,5,7,8", "6,7,5,8", "open")["correct"])

    def test_single_choice(self):
        self.assertTrue(grade_answer("D", "D", "single")["correct"])
        self.assertFalse(grade_answer("A", "D", "single")["correct"])

    def test_no_answer(self):
        self.assertFalse(grade_answer(None, "4", "open")["correct"])


class TestLedgers(unittest.TestCase):
    def test_versioned_supersedes_on_conflict(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        ledger.ingest(observation(3, "mug", "held"))
        record = ledger.records["mug"]
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["state"], "held")
        self.assertEqual(record["last_verified_frame"], 3)
        self.assertEqual(ledger.conflicts["mug"], 1)
        self.assertTrue(any(h.get("status") == "superseded" for h in record["history"]))

    def test_versioned_confirmation_refreshes_frame(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        ledger.ingest(observation(4, "mug", "on table"))
        self.assertEqual(ledger.records["mug"]["last_verified_frame"], 4)
        self.assertEqual(ledger.conflict_count(["mug"]), 0)

    def test_build_ledger_flat(self):
        self.assertIsInstance(build_ledger("versioned_state_ledger_flat"), VersionedLedger)

    def test_append_only_keeps_every_record(self):
        ledger = AppendOnlyLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        ledger.ingest(observation(3, "mug", "held"))
        states = [row.get("state") for row in ledger.entries if row.get("entity") == "mug"]
        self.assertEqual(states, ["on table", "held"])

    def test_risk_grows_with_staleness_and_conflicts(self):
        fresh = VersionedLedger()
        for index in range(4):
            fresh.ingest(observation(index, "mug", "on table"))
        stale = VersionedLedger()
        stale.ingest(observation(0, "mug", "on table"))
        stale.ingest(observation(3, "mug", "held"))
        stale.ingest(observation(3, "mug", "held"))
        risk_fresh = staleness_risk(fresh, "where is the mug", 3, 0.9)["risk"]
        risk_stale = staleness_risk(stale, "where is the mug", 3, 0.9)["risk"]
        self.assertGreater(risk_stale, risk_fresh)
        self.assertLessEqual(risk_stale, 1.0)

    def test_risk_uses_model_confidence(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        high = staleness_risk(ledger, "mug", 3, 0.95)["risk"]
        low = staleness_risk(ledger, "mug", 3, 0.10)["risk"]
        self.assertGreater(low, high)

    def test_verification_frame_prefers_supersession(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        ledger.ingest(observation(4, "mug", "held"))
        choice = verification_frame(ledger, "where is the mug", 6)
        self.assertEqual(choice["frame"], 4)

    def test_verification_frame_falls_back_to_midpoint(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        choice = verification_frame(ledger, "where is the mug", 6)
        self.assertEqual(choice["frame"], 3)


class TestPrompts(unittest.TestCase):
    def test_render_history_differs_per_method(self):
        ledger = VersionedLedger()
        ledger.ingest(observation(0, "mug", "on table"))
        snapshot = ledger.snapshot(0)
        direct = render_history("direct_baseline", [observation(0, "mug", "on table")], None)
        append = render_history("append_only_ledger", [], snapshot)
        versioned = render_history("versioned_state_ledger", [], snapshot)
        flat_snapshot = ledger.snapshot_flat(0)
        flat = render_history("versioned_state_ledger_flat", [], flat_snapshot)
        self.assertIn("raw chronological observations", direct)
        self.assertIn("APPEND-ONLY", append)
        self.assertIn("superseded", versioned)
        self.assertIn("superseded", flat)
        self.assertNotEqual(direct, append)
        self.assertNotEqual(versioned, flat)

    def test_parse_single_choice(self):
        parsed = parse_answer('{"answer": "B", "confidence": 0.7, "evidence_frames": [1]}',
                               ["x", "y", "z"])
        self.assertEqual(parsed["answer"], "B")
        self.assertEqual(parsed["confidence"], 0.7)

    def test_parse_open_answer(self):
        parsed = parse_answer('{"answer": "Step 3", "confidence": 0.5}', [])
        self.assertEqual(parsed["answer"], "Step 3")

    def test_parse_falls_back_to_fenced_json(self):
        parsed = parse_answer('```json\n{"answer": "4", "confidence": 0.9}\n```', [])
        self.assertEqual(parsed["answer"], "4")

    def test_parse_rejects_garbage(self):
        parsed = parse_answer("I cannot answer", [])
        self.assertIsNone(parsed["answer"])


if __name__ == "__main__":
    unittest.main()
