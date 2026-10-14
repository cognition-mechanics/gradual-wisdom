"""
Tests for semantic matching in the merge (candidate retrieval + LLM adjudication).
Written first (red), then sleep.py extended (green).

Run: cd <repo-root> && python3 -m pytest scripts/test_matching.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sleep  # noqa: E402

CONFIG = {
    "decay_rate": 0.99,
    "prune_threshold": 0.2,
    "max_items_per_category": 200,
    "initial_confidence_new_item": 0.5,
    "reinforcement_boost": 0.1,
    "max_confidence": 0.98,
}
PV = {"directness": 0.5, "technical_depth": 0.5, "humor": 0.5,
      "verbosity": 0.5, "hedging": 0.5, "formality": 0.5}


class RouterClient:
    """Answers the match prompt with a scripted response and anything else with a personality vector."""

    def __init__(self, match_response: str | Exception):
        self.match_response = match_response
        self.match_calls: list[str] = []

    def call(self, system: str, user_msg: str) -> str:
        if "MATCH_ADJUDICATION" in system:
            self.match_calls.append(user_msg)
            if isinstance(self.match_response, Exception):
                raise self.match_response
            return self.match_response
        return json.dumps(PV)


def fact(id_: str, content: str, conf: float = 0.5, count: int = 1) -> dict[str, Any]:
    return {"id": id_, "content": content, "confidence": conf, "first_seen": "2026-01-01",
            "last_reinforced": "2026-01-01", "reinforcement_count": count}


def wisdom(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": {"user_facts": facts}, "personality_vector": dict(PV)}


def daily(*facts_text: str) -> dict[str, Any]:
    return {"sessions": [{"user_signals": {"facts": [{"content": t} for t in facts_text]}}]}


def run(w, d, client):
    return sleep.merge_into_wisdom(client, w, d, CONFIG, "2026-02-01")


# --- candidate retrieval -----------------------------------------------------

def test_candidates_include_paraphrase_and_exclude_unrelated():
    existing = [
        fact("f001", "Prefers short, terse answers without padding"),
        fact("f002", "Works on Azure DevOps pipelines"),
    ]
    cands = sleep._find_candidates("Wants terse answers with no padding", existing, "user_facts", k=5)
    ids = [c["id"] for c in cands]
    assert ids and ids[0] == "f001"
    assert "f002" not in ids


def test_candidates_capped_at_k():
    existing = [fact(f"f{i:03d}", f"uses macOS laptop number {i}") for i in range(20)]
    cands = sleep._find_candidates("uses a macOS laptop", existing, "user_facts", k=5)
    assert len(cands) == 5


# --- adjudicated reinforcement -------------------------------------------------

def test_adjudicated_paraphrase_reinforces_existing_item():
    w = wisdom([fact("f001", "Prefers short, terse answers without padding")])
    client = RouterClient(json.dumps({"matches": {"user_facts:0": "f001"}}))
    result = run(w, daily("Wants terse answers with no padding"), client)
    items = result["items"]["user_facts"]
    assert len(items) == 1
    assert items[0]["reinforcement_count"] == 2
    assert abs(items[0]["confidence"] - 0.6) < 1e-9
    assert items[0]["last_reinforced"] == "2026-02-01"
    assert len(client.match_calls) == 1


def test_adjudicator_null_creates_new_item():
    w = wisdom([fact("f001", "Prefers short, terse answers without padding")])
    client = RouterClient(json.dumps({"matches": {"user_facts:0": None}}))
    result = run(w, daily("Wants terse answers with no padding"), client)
    assert len(result["items"]["user_facts"]) == 2


def test_adjudicator_id_outside_candidates_is_ignored():
    w = wisdom([
        fact("f001", "Prefers short, terse answers without padding"),
        fact("f002", "Works on Azure DevOps pipelines"),
    ])
    # f002 is not a retrieval candidate for this text, so the claim must be rejected
    client = RouterClient(json.dumps({"matches": {"user_facts:0": "f002"}}))
    result = run(w, daily("Wants terse answers with no padding"), client)
    by_id = {i["id"]: i for i in result["items"]["user_facts"]}
    assert by_id["f002"]["reinforcement_count"] == 1
    assert len(result["items"]["user_facts"]) == 3


def test_two_daily_items_matched_to_same_id_reinforce_once():
    w = wisdom([fact("f001", "Prefers short, terse answers without padding")])
    client = RouterClient(json.dumps({"matches": {"user_facts:0": "f001", "user_facts:1": "f001"}}))
    result = run(w, daily("Wants terse answers with no padding", "Likes terse answers, no padding"), client)
    items = result["items"]["user_facts"]
    assert len(items) == 1
    assert items[0]["reinforcement_count"] == 2


# --- graceful degradation ------------------------------------------------------

def test_adjudicator_failure_falls_back_to_lexical_rule():
    w = wisdom([fact("f001", "Uses macOS")])
    client = RouterClient(RuntimeError("rate limited"))
    result = run(w, daily("uses macOS", "Wants terse answers with no padding"), client)
    items = {i["content"]: i for i in result["items"]["user_facts"]}
    assert items["Uses macOS"]["reinforcement_count"] == 2   # lexical match still works
    assert "Wants terse answers with no padding" in items     # unmatched → new


def test_adjudicator_garbage_falls_back_to_lexical_rule():
    w = wisdom([fact("f001", "Prefers short, terse answers without padding")])
    client = RouterClient("not json at all")
    result = run(w, daily("Wants terse answers with no padding"), client)
    assert len(result["items"]["user_facts"]) == 2


def test_no_adjudication_call_when_nothing_has_candidates():
    w = wisdom([])
    client = RouterClient(json.dumps({"matches": {}}))
    run(w, daily("Brand new fact"), client)
    assert client.match_calls == []
