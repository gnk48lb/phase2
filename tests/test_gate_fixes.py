"""Regression tests for the audited gate/ledger bugs.

Covers:
  1.1  apply_verified_observation must not lose the episode's frame summaries,
       and must not overwrite evidence that came after the verified frame;
  1.2  transient action phrases (``event:*``) and the activity record must
       never be picked as a staleness target or enter the risk score;
  1.3  the gate must deduplicate the append-only answers log by task;
  2.1  an entity that left the view keeps its last known state.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gate_simulation import load_gate_rows, pick_target
from memory_ledger import (VersionedLedger, apply_verified_observation,
                            is_trackable_entity, staleness_risk)


def episode():
    """A short episode with a transient action phrase and one physical object."""
    return [
        {"frame": 0, "summary": "a hand holds a tomato",
         "entities": [{"entity": "tomato", "state": "on the counter",
                       "location": "counter", "confidence": 0.8}],
         "events": [{"event": "holding tomato beside sliced pieces",
                     "status": "in progress", "entities": []}],
         "absent": []},
        {"frame": 2, "summary": "the counter is empty",
         "entities": [{"entity": "tomato", "state": "absent",
                       "location": "not visible", "confidence": 0.6}],
         "events": [{"event": "washing a bowl", "status": "in progress",
                     "entities": []}],
         "absent": []},
        {"frame": 4, "summary": "the tomato is on a plate",
         "entities": [{"entity": "tomato", "state": "on a plate",
                       "location": "plate", "confidence": 0.8}],
         "events": [{"event": "placing a plate", "status": "finished",
                     "entities": []}],
         "absent": []},
    ]


class TestVerifiedObservation(unittest.TestCase):
    """Bug 1.1"""

    def test_frame_summaries_survive_the_verification(self):
        ledger = VersionedLedger()
        for observation in episode():
            ledger.ingest(observation)
        self.assertEqual(len(ledger.frame_summaries), 3)
        clone = apply_verified_observation(ledger, {
            "frame": 2, "summary": "ground truth at frame 2",
            "entities": [{"entity": "tomato", "state": "on the counter",
                          "location": "counter"}], "events": [], "absent": []})
        self.assertEqual(len(clone.frame_summaries), 3)
        self.assertEqual([f["frame"] for f in clone.frame_summaries], [0, 2, 4])
        self.assertIn("re-verified", clone.frame_summaries[1]["summary"])
        self.assertNotIn("re-verified", clone.frame_summaries[0]["summary"])
        self.assertNotIn("re-verified", clone.frame_summaries[2]["summary"])

    def test_verification_does_not_clobber_later_evidence(self):
        ledger = VersionedLedger()
        for observation in episode():
            ledger.ingest(observation)
        clone = apply_verified_observation(ledger, {
            "frame": 2, "summary": "ground truth at frame 2",
            "entities": [{"entity": "tomato", "state": "on the counter",
                          "location": "counter"}], "events": [], "absent": []})
        record = clone.records["tomato"]
        # The entity was seen again after frame 2 (frame 4): the older
        # verification belongs in the evidence chain, not in the active state.
        self.assertEqual(record["state"], "on a plate")
        self.assertEqual(int(record["last_verified_frame"]), 4)
        self.assertTrue(any(h.get("status") == "re-verified"
                            for h in record["history"]))

    def test_verification_updates_an_entity_without_newer_observation(self):
        ledger = VersionedLedger()
        ledger.ingest(episode()[0])
        clone = apply_verified_observation(ledger, {
            "frame": 0, "summary": "ground truth at frame 0",
            "entities": [{"entity": "tomato", "state": "held in hand",
                          "location": "hand"}], "events": [], "absent": []})
        record = clone.records["tomato"]
        self.assertEqual(record["state"], "held in hand")
        self.assertEqual(record["confidence"], 1.0)


class TestTransientEntities(unittest.TestCase):
    """Bug 1.2"""

    def test_is_trackable_entity(self):
        self.assertTrue(is_trackable_entity("tomato"))
        self.assertFalse(is_trackable_entity("event:holding tomato"))
        self.assertFalse(is_trackable_entity("activity"))
        self.assertFalse(is_trackable_entity("current_activity"))
        self.assertFalse(is_trackable_entity("label:3"))

    def test_pick_target_never_selects_event_or_activity(self):
        ledger = VersionedLedger()
        for observation in episode():
            ledger.ingest(observation)
        target = pick_target(ledger, "where is the tomato", 4)
        self.assertIsNotNone(target)
        self.assertFalse(str(target["key"]).startswith("event:"))
        self.assertNotEqual(target["key"], "activity")

    def test_staleness_risk_only_uses_trackable_entities(self):
        ledger = VersionedLedger()
        for observation in episode():
            ledger.ingest(observation)
        risk = staleness_risk(ledger, "where is the tomato", 4, 0.5)
        for key in risk["relevant_keys"]:
            self.assertTrue(is_trackable_entity(key), key)

    def test_conflicted_entity_beats_a_stale_background_object(self):
        ledger = VersionedLedger()
        ledger.ingest({"frame": 0, "summary": "s0",
                       "entities": [{"entity": "wall clock", "state": "on the wall",
                                     "location": "wall", "confidence": 0.8}],
                       "events": [], "absent": []})
        ledger.ingest({"frame": 3, "summary": "s3",
                       "entities": [{"entity": "mug", "state": "on the table",
                                     "location": "table", "confidence": 0.8}],
                       "events": [], "absent": []})
        ledger.ingest({"frame": 4, "summary": "s4",
                       "entities": [{"entity": "mug", "state": "in the sink",
                                     "location": "sink", "confidence": 0.8}],
                       "events": [], "absent": []})
        target = pick_target(ledger, "anything", 4)
        self.assertEqual(target["key"], "mug")


class TestAbsentSemantics(unittest.TestCase):
    """Bug 2.1"""

    def test_out_of_view_keeps_last_known_state(self):
        ledger = VersionedLedger()
        ledger.ingest(episode()[0])
        ledger.ingest(episode()[1])
        record = ledger.records["tomato"]
        self.assertEqual(record["state"], "on the counter")
        self.assertEqual(record["last_known_state"], "on the counter")
        self.assertEqual(record["last_known_location"], "counter")
        self.assertEqual(record["visibility"], "out_of_view")
        self.assertNotIn("tomato", ledger.conflicts)


class TestGateDeduplication(unittest.TestCase):
    """Bug 1.3"""

    def test_gate_rows_are_deduplicated_by_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answers.jsonl"
            rows = [
                {"model": "m", "method": "versioned_state_ledger", "task_id": "t1",
                 "mode": "text", "prediction": "Step 4", "error": None},
                {"model": "m", "method": "versioned_state_ledger", "task_id": "t1",
                 "mode": "text", "prediction": None, "error": "boom"},
                {"model": "m", "method": "versioned_state_ledger", "task_id": "t2",
                 "mode": "text", "prediction": "Step 2", "error": None},
            ]
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            loaded = load_gate_rows(path, "versioned_state_ledger")
            self.assertEqual(len(loaded), 2)
            self.assertEqual({row["task_id"] for row in loaded}, {"t1", "t2"})
            # the successful row of t1 wins over the later failed retry
            t1 = next(row for row in loaded if row["task_id"] == "t1")
            self.assertEqual(t1["prediction"], "Step 4")


if __name__ == "__main__":
    unittest.main()
