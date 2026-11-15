"""Deterministic memory structures and the code-level staleness risk score.

Three memory organizations share one observation stream (strict prompt
parity): the only thing that differs between the ablations is how the same
per-frame observations are stored and presented.

* ``AppendOnlyLedger``   -- append every observation, never invalidate.
* ``VersionedLedger``    -- one active record per entity/event; a conflict or
                            a disappearance marks the old record ``superseded``
                            and links the new evidence.

The staleness risk score is computed by Python from the versioned ledger, not
by the model.  It combines (a) the verification gap in frames, (b) the number
of recorded state conflicts, and (c) the model's own answer confidence.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Iterable

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "of", "to",
    "and", "or", "what", "which", "who", "when", "where", "how", "did", "does",
    "do", "has", "have", "had", "after", "before", "during", "while", "with",
    "that", "this", "it", "its", "he", "she", "they", "his", "her", "their",
    "from", "by", "for", "as", "into", "onto", "then", "than", "there",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _entity_key(name: str) -> str:
    return _norm(name)


# States that mean "the tracker could not see it in this frame".
ABSENT_STATES = {"absent", "not visible", "not_visible", "out of view",
                  "out_of_view", "unseen", "not in view"}


def _is_absent_state(state: Any) -> bool:
    return _norm(state) in ABSENT_STATES


def is_trackable_entity(key: str) -> bool:
    """True for real physical entities that can go stale.

    Transient action phrases (``event:*``), the per-frame activity record and
    purely printed labels are NOT trackable entities: an action phrase that
    appears only in frame 0 looks maximally stale but re-verifying it in a
    later frame always returns "not visible", which is exactly the failure
    mode the staleness gate must avoid.
    """
    normalized = _norm(key)
    if not normalized:
        return False
    if normalized.startswith("event:"):
        return False
    if normalized in ("activity", "current_activity"):
        return False
    if normalized.startswith("label:"):
        return False
    return True


def _tokens(text: str) -> list[str]:
    return [tok for tok in re.findall(r"[a-z0-9_]+", _norm(text)) if tok not in _STOPWORDS]


class AppendOnlyLedger:
    """A ledger that only ever appends; stale records are never invalidated.

    This is the deliberate failure mode under study: later observations are
    added next to older ones, so the model has to decide on its own which
    record is still true -- and the context keeps growing.
    """

    kind = "append_only"

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def ingest(self, observation: dict[str, Any]) -> None:
        frame = int(observation.get("frame", 0))
        summary = observation.get("summary") or observation.get("description") or ""
        if summary:
            self.entries.append({"frame": frame, "type": "summary", "text": summary})
        for ent in observation.get("entities", []) or []:
            self.entries.append({
                "frame": frame,
                "type": "entity",
                "entity": ent.get("entity"),
                "state": ent.get("state"),
                "location": ent.get("location"),
                "confidence": ent.get("confidence"),
                "evidence": ent.get("evidence") or summary,
            })
        for absent in observation.get("absent", []) or []:
            self.entries.append({
                "frame": frame,
                "type": "entity",
                "entity": absent.get("entity"),
                "state": "absent",
                "location": absent.get("expected_location"),
                "confidence": absent.get("confidence"),
                "evidence": summary,
            })
        for ev in observation.get("events", []) or []:
            self.entries.append({
                "frame": frame,
                "type": "event",
                "event": ev.get("event"),
                "status": ev.get("status", "observed"),
                "entities": ev.get("entities", []),
                "evidence": summary,
            })

    def snapshot(self, current_frame: int) -> dict[str, Any]:
        return {
            "policy": "append_only",
            "note": "Every record is retained forever; nothing is invalidated.",
            "records": deepcopy(self.entries),
            "record_count": len(self.entries),
        }

    def relevant_records(self, question: str) -> list[dict[str, Any]]:
        query_tokens = set(_tokens(question))
        hits = []
        for row in self.entries:
            name = row.get("entity") or row.get("event") or ""
            if set(_tokens(name)) & query_tokens:
                hits.append(row)
        return hits or self.entries

    def conflict_counts(self) -> dict[str, int]:
        return {}


class VersionedLedger:
    """World-state ledger with explicit supersession and evidence history.

    Record schema (per the experiment spec):
        {entity, state, location, last_verified_frame, status: active|superseded,
         confidence, history: [older records with evidence]}

    ``events`` are kept in the same record structure keyed by ``event:<name>``
    so progress-tracking questions get the same supersession semantics.
    """

    kind = "versioned"

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.conflicts: dict[str, int] = {}
        self.frame_log: list[int] = []
        self.frame_summaries: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ ingest
    def ingest(self, observation: dict[str, Any]) -> None:
        frame = int(observation.get("frame", 0))
        self.frame_log.append(frame)
        summary = observation.get("summary") or observation.get("description") or ""
        if summary:
            self.frame_summaries.append({"frame": frame, "summary": summary})
        for ent in observation.get("entities", []) or []:
            self._upsert_entity(ent, frame, summary)
        for absent in observation.get("absent", []) or []:
            name = absent.get("entity")
            if not name:
                continue
            self._apply(name, "absent", absent.get("expected_location"), frame,
                        float(absent.get("confidence") or 0.6),
                        evidence=summary, reason="entity disappeared from its expected location")
        for ev in observation.get("events", []) or []:
            name = ev.get("event")
            if not name:
                continue
            status = ev.get("status", "observed")
            self._apply(f"event:{name}", status, None, frame,
                        float(ev.get("confidence") or 0.7),
                        evidence=ev.get("evidence") or summary,
                        reason="event progress update",
                        extra={"entities": ev.get("entities", [])})

    def _upsert_entity(self, ent: dict[str, Any], frame: int, evidence: str) -> None:
        name = ent.get("entity")
        if not name:
            return
        self._apply(name, ent.get("state"), ent.get("location"), frame,
                    float(ent.get("confidence") or 0.7),
                    evidence=ent.get("evidence") or evidence,
                    reason="new observation")

    def _apply(self, name: str, state: Any, location: Any, frame: int,
               confidence: float, evidence: str, reason: str,
               extra: dict[str, Any] | None = None) -> None:
        key = _entity_key(name)
        current = self.records.get(key)
        new_state = {"state": state, "location": location}
        if current is None:
            record = {
                "entity": name,
                "state": state,
                "location": location,
                "last_verified_frame": frame,
                "status": "active",
                "confidence": round(confidence, 3),
                "history": [],
            }
            if extra:
                record.update(deepcopy(extra))
            self.records[key] = record
            return
        old_state = {"state": current["state"], "location": current["location"]}
        if (_is_absent_state(state) and not _is_absent_state(current["state"])):
            # The entity left the view (camera moved), it did not change state.
            # Keep the last known state and location, flag the visibility, and
            # do NOT count it as a conflict: nothing was contradicted.
            current["last_known_state"] = current["state"]
            current["last_known_location"] = current["location"]
            current["visibility"] = "out_of_view"
            current["last_verified_frame"] = max(int(current["last_verified_frame"]), frame)
            current["confidence"] = round(max(float(current.get("confidence") or 0), confidence), 3)
            current["history"].append({
                "frame": frame, "state": current["state"], "location": current["location"],
                "status": "out_of_view", "confidence": round(confidence, 3),
                "evidence": evidence,
            })
            return
        if old_state == new_state:
            # Re-observation confirms the active record: refresh its frame.
            current["last_verified_frame"] = max(int(current["last_verified_frame"]), frame)
            current["confidence"] = round(max(float(current.get("confidence") or 0), confidence), 3)
            current["visibility"] = "in_view"
            current["history"].append({
                "frame": frame, "state": state, "location": location,
                "status": "confirmed", "confidence": round(confidence, 3),
                "evidence": evidence,
            })
            return
        # Conflict or disappearance: retire the old record, install the new one.
        current["status"] = "superseded"
        current["superseded_at_frame"] = frame
        current["history"].append({
            "frame": frame,
            "last_verified_frame": current["last_verified_frame"],
            "superseded_at_frame": frame,
            "state": current["state"],
            "location": current["location"],
            "status": "superseded",
            "reason": reason,
            "replaced_by": {"state": state, "location": location},
            "evidence": evidence,
        })
        self.conflicts[key] = self.conflicts.get(key, 0) + 1
        record = {
            "entity": name,
            "state": state,
            "location": location,
            "last_verified_frame": frame,
            "status": "active",
            "confidence": round(confidence, 3),
            "history": current["history"],
            "previously": [
                {"state": current["state"], "location": current["location"],
                 "last_verified_frame": current["last_verified_frame"],
                 "superseded_at_frame": frame, "reason": reason},
            ],
        }
        if extra:
            record.update(extra)
        self.records[key] = record

    # ------------------------------------------------------------------ query
    def snapshot_flat(self, current_frame: int) -> dict[str, Any]:
        """Compact rendering of the SAME versioned semantics.

        Same information as `snapshot` (one active record per entity, the
        superseded older states with their frames, the per-frame summaries) but
        without the nested history/evidence objects.  Used as a control for the
        prompt-length asymmetry between the ablations.
        """
        current, superseded = [], []
        for key, rec in sorted(self.records.items()):
            current.append({"entity": rec["entity"], "state": rec["state"],
                            "location": rec["location"],
                            "last_verified_frame": rec["last_verified_frame"],
                            "status": rec["status"]})
            for hist in rec.get("history", []):
                if hist.get("status") == "superseded":
                    superseded.append({"entity": rec["entity"], "state": hist.get("state"),
                                       "location": hist.get("location"),
                                       "last_verified_frame": hist.get("last_verified_frame"),
                                       "superseded_at_frame": hist.get("superseded_at_frame")})
        return {
            "policy": "versioned",
            "note": "Only status=active records are current beliefs; records in "
                    "superseded were retired when a later observation contradicted them.",
            "current_frame": current_frame,
            "frame_summaries": [{"frame": f["frame"], "summary": f["summary"]}
                                for f in self.frame_summaries],
            "current_state": current,
            "superseded": superseded,
        }

    def active_records(self) -> list[dict[str, Any]]:
        return [rec for rec in self.records.values() if rec["status"] == "active"]

    def snapshot(self, current_frame: int) -> dict[str, Any]:
        active = {}
        for key, rec in sorted(self.records.items()):
            view = deepcopy(rec)
            view["staleness_frames"] = max(0, int(current_frame) - int(rec["last_verified_frame"]))
            active[key] = view
        return {
            "policy": "versioned",
            "note": "Only active records are current beliefs; history entries marked "
                    "superseded are NOT alternative current states.",
            "current_frame": current_frame,
            "frame_summaries": deepcopy(self.frame_summaries),
            "active_records": active,
            "superseded_transitions": dict(sorted(self.conflicts.items())),
        }

    def relevant_keys(self, question: str) -> list[str]:
        query_tokens = set(_tokens(question))
        hits = [key for key, rec in self.records.items()
                if set(_tokens(rec["entity"])) & query_tokens]
        if hits:
            return hits
        # No lexical match: every active record is potentially relevant.  This
        # is deliberately conservative and identical for every sample.
        return sorted(key for key, rec in self.records.items() if rec["status"] == "active")

    def conflict_count(self, keys: Iterable[str] | None = None) -> int:
        if keys is None:
            return sum(self.conflicts.values())
        return sum(self.conflicts.get(key, 0) for key in keys)

    def observation_count(self, key: str) -> int:
        """How many frames this entity was reported in."""
        record = self.records.get(key)
        if record is None:
            return 0
        return 1 + len(record.get("history", []))

    def risk_keys(self, question: str) -> list[str]:
        """Physical entities the staleness score and the gate may reason about.

        Order of preference:
        1. trackable entities whose name matches the question;
        2. trackable entities that actually suffered a state conflict;
        3. trackable entities seen in more than one frame;
        4. every trackable entity.
        Transient action phrases, the activity record and printed labels are
        never eligible.
        """
        trackable = [key for key in self.records if is_trackable_entity(key)]
        query_tokens = set(_tokens(question))
        matched = [key for key in trackable
                   if query_tokens & set(_tokens(self.records[key]["entity"]))]
        if matched:
            return sorted(matched)
        conflicted = [key for key in trackable if self.conflicts.get(key, 0) > 0]
        if conflicted:
            return sorted(conflicted)
        multi_frame = [key for key in trackable if self.observation_count(key) > 1]
        if multi_frame:
            return sorted(multi_frame)
        return sorted(trackable)

    def superseded_count(self) -> int:
        return sum(1 for rec in self.records.values() if rec["status"] == "superseded")


class NoLedger:
    kind = "direct"

    def snapshot(self, current_frame: int) -> None:
        return None


def build_ledger(kind: str):
    if kind == "append_only_ledger":
        return AppendOnlyLedger()
    if kind in ("versioned_state_ledger", "versioned_state_ledger_flat"):
        return VersionedLedger()
    return NoLedger()


# ---------------------------------------------------------------------- risk
WEIGHT_SPAN = 0.45
WEIGHT_CONFLICT = 0.30
WEIGHT_UNCERTAINTY = 0.25


def staleness_risk(ledger: VersionedLedger, question: str, current_frame: int,
                   model_confidence: float | None) -> dict[str, Any]:
    """Code-level Staleness Risk Score in [0, 1].

    span_norm    = mean staleness of the relevant active records / question horizon
    conflict_norm= conflicts affecting the relevant records / observations seen
    uncertainty  = 1 - model confidence (0.5 when the model gave none)

    The score is deterministic; the model never computes it.
    """
    keys = ledger.risk_keys(question)
    active = [ledger.records[key] for key in keys
              if ledger.records[key]["status"] == "active"]
    if not active:
        active = [rec for rec in ledger.active_records()
                  if is_trackable_entity(rec.get("entity", ""))]
    if not active:
        active = ledger.active_records()
    horizon = max(1, int(current_frame) + 1)
    spans = [max(0, int(current_frame) - int(rec["last_verified_frame"])) for rec in active]
    span_norm = min(1.0, (sum(spans) / len(spans)) / horizon) if spans else 1.0
    observations = max(1, len(ledger.frame_log))
    conflict_norm = min(1.0, ledger.conflict_count(keys) / observations)
    if model_confidence is None:
        uncertainty = 0.5
    else:
        uncertainty = max(0.0, min(1.0, 1.0 - float(model_confidence)))
    risk = (WEIGHT_SPAN * span_norm + WEIGHT_CONFLICT * conflict_norm
            + WEIGHT_UNCERTAINTY * uncertainty)
    risk = max(0.0, min(1.0, risk))
    return {
        "risk": round(risk, 6),
        "components": {
            "span_frames": round(sum(spans) / len(spans), 3) if spans else None,
            "span_norm": round(span_norm, 6),
            "horizon_frames": horizon,
            "conflicts": ledger.conflict_count(keys),
            "conflict_norm": round(conflict_norm, 6),
            "observations_seen": observations,
            "model_confidence": model_confidence,
            "uncertainty": round(uncertainty, 6),
        },
        "weights": {"span": WEIGHT_SPAN, "conflict": WEIGHT_CONFLICT,
                    "uncertainty": WEIGHT_UNCERTAINTY},
        "relevant_keys": keys,
    }


def verification_frame(ledger: VersionedLedger, question: str, current_frame: int) -> dict[str, Any]:
    """Deterministic choice of the one intermediate frame worth re-checking.

    Priority: the frame of the most recent supersession that touches a
    relevant record; otherwise the midpoint between the oldest relevant
    verification and the current frame.
    """
    keys = ledger.risk_keys(question)
    candidates = []
    for key in keys:
        if key not in ledger.records:
            continue
        rec = ledger.records[key]
        for hist in rec.get("history", []):
            if hist.get("status") == "superseded":
                candidates.append(int(hist["frame"]))
        if rec["status"] == "superseded" and rec.get("superseded_at_frame") is not None:
            candidates.append(int(rec["superseded_at_frame"]))
    if candidates:
        frame = max(candidates)
        reason = "latest supersession touching a question-relevant physical entity"
    else:
        last = [int(rec["last_verified_frame"]) for key in keys
                if key in ledger.records for rec in [ledger.records[key]]]
        base = min(last) if last else 0
        frame = int(math.floor((base + int(current_frame)) / 2))
        reason = "midpoint between the oldest verification and the current frame"
    return {"frame": frame, "reason": reason, "relevant_keys": keys}


REVERIFY_NOTE = "[re-verified ground-truth frame observation]"


def apply_verified_observation(ledger: VersionedLedger, observation: dict[str, Any]) -> VersionedLedger:
    """Return a copy of the ledger with one re-verified frame ingested.

    The copy keeps the FULL history of the episode: the per-frame summaries of
    every frame are carried over and only the verified frame's summary is
    amended in place.  (Dropping them would hand the model a ledger with a
    single frame of narration, which is not the state it answered from the
    first time.)

    The verified frame is an *intermediate* frame, so it must not overwrite
    evidence that came later:

    * entity last seen at or before the verified frame -> the verification is
      the freshest evidence, so it becomes the active state (confidence 1.0);
    * entity seen again after the verified frame -> the verification is older
      evidence and goes into the history chain (counting a conflict only if it
      contradicts what was believed at that frame).
    """
    clone = VersionedLedger()
    clone.records = deepcopy(ledger.records)
    clone.conflicts = deepcopy(ledger.conflicts)
    clone.frame_log = list(ledger.frame_log)
    clone.frame_summaries = deepcopy(ledger.frame_summaries)

    f_ver = int(observation.get("frame", 0))
    verified = deepcopy(observation)
    verified["summary"] = (str(observation.get("summary") or "")
                           + " " + REVERIFY_NOTE).strip()
    verified["entities"] = [dict(ent, confidence=1.0)
                            for ent in (observation.get("entities") or [])]
    verified["absent"] = [dict(ent, confidence=1.0)
                          for ent in (observation.get("absent") or [])]

    # 1) amend the summary of the verified frame in place, keep the others
    amended = False
    for entry in clone.frame_summaries:
        if int(entry.get("frame", -1)) == f_ver:
            entry["summary"] = f"{entry.get('summary', '')} {REVERIFY_NOTE}".strip()
            amended = True
            break
    if not amended:
        clone.frame_summaries.append({"frame": f_ver, "summary": verified["summary"]})

    # 2) the extra perception is one more observation
    if f_ver not in clone.frame_log:
        clone.frame_log.append(f_ver)

    # 3) update the verified entities without clobbering later evidence
    for ent in verified["entities"]:
        name = ent.get("entity")
        if not name:
            continue
        key = _entity_key(name)
        record = clone.records.get(key)
        state, location = ent.get("state"), ent.get("location")
        evidence = verified["summary"]
        if record is None:
            clone._apply(name, state, location, f_ver, 1.0, evidence=evidence,
                         reason="re-verified frame")
            continue
        if int(record.get("last_verified_frame", -1)) <= f_ver:
            # No newer observation: the verification is the current truth.
            previous = {"state": record["state"], "location": record["location"],
                        "last_verified_frame": record["last_verified_frame"]}
            record["history"].append({
                "frame": f_ver, "last_verified_frame": previous["last_verified_frame"],
                "superseded_at_frame": f_ver, "state": previous["state"],
                "location": previous["location"], "status": "superseded",
                "reason": "re-verified ground truth", "evidence": evidence,
                "replaced_by": {"state": state, "location": location},
            })
            if previous["state"] != state or previous["location"] != location:
                clone.conflicts[key] = clone.conflicts.get(key, 0) + 1
            record["state"] = state
            record["location"] = location
            record["last_verified_frame"] = f_ver
            record["confidence"] = 1.0
            record["status"] = "active"
            record["visibility"] = "in_view"
        else:
            # The entity was observed again after the verified frame: the
            # verification is older evidence and belongs in the history chain.
            believed = None
            for hist in record.get("history", []):
                if int(hist.get("frame", -1)) == f_ver:
                    believed = hist.get("state")
            if believed is not None and believed != state:
                clone.conflicts[key] = clone.conflicts.get(key, 0) + 1
            record["history"].append({
                "frame": f_ver, "state": state, "location": location,
                "status": "re-verified", "confidence": 1.0, "evidence": evidence,
            })
    return clone