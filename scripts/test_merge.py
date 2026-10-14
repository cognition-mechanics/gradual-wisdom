"""
Tests for the programmatic merge_into_wisdom implementation.
Written FIRST (red), frozen, then implementation written to green.

Run: cd <repo-root> && python3 -m pytest scripts/test_merge.py -v
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

BASE_CONFIG = {
    "decay_rate": 0.99,
    "prune_threshold": 0.2,
    "max_items_per_category": 200,
    "initial_confidence_new_item": 0.5,
    "reinforcement_boost": 0.1,
    "max_confidence": 0.98,
}

TODAY = "2026-07-01"
YESTERDAY = "2026-06-30"

PERSONALITY_AXES = ["directness", "technical_depth", "humor", "verbosity", "hedging", "formality"]

DEFAULT_PERSONALITY = {ax: 0.5 for ax in PERSONALITY_AXES}


def make_wisdom(items: dict | None = None, personality: dict | None = None, **kwargs) -> dict[str, Any]:
    w = {
        "version": 1,
        "last_consolidation": YESTERDAY,
        "consolidation_count": 1,
        "first_seen": "2026-06-01",
        "items": {
            "user_facts": [],
            "user_preferences": [],
            "user_frustrations": [],
            "user_style": [],
            "claude_lessons": [],
            "claude_calibrations": [],
            "claude_opinions": [],
            "shared_themes": [],
            "relationship": [],
        },
        "personality_vector": copy.deepcopy(DEFAULT_PERSONALITY),
        "pruned": [],
    }
    if items:
        for cat, cat_items in items.items():
            w["items"][cat] = copy.deepcopy(cat_items)
    if personality:
        w["personality_vector"] = copy.deepcopy(personality)
    w.update(kwargs)
    return w


def make_daily(sessions: list[dict] | None = None) -> dict[str, Any]:
    return {
        "date": TODAY,
        "session_count": 1,
        "skipped_sessions": [],
        "sessions": sessions or [],
    }


def make_session(**kwargs) -> dict[str, Any]:
    """Build a minimal per-session extract; caller populates only relevant keys."""
    base: dict[str, Any] = {}
    base.update(kwargs)
    return base


class FakeClientFixed:
    """Returns a canned 6-axis JSON for personality_target calls."""

    def __init__(self, personality_json: str):
        self._json = personality_json

    def call(self, system: str, user_msg: str) -> str:
        return self._json


class FakeClientRaises:
    """Raises RuntimeError on any .call()."""

    def call(self, system: str, user_msg: str) -> str:
        raise RuntimeError("Simulated LLM failure")


def run_merge(
    current_wisdom: dict,
    sessions: list[dict],
    config: dict | None = None,
    today: str = TODAY,
    client=None,
) -> dict[str, Any]:
    """Convenience wrapper around merge_into_wisdom."""
    from sleep import merge_into_wisdom  # imported here so RED phase can fail on missing symbol

    cfg = {**BASE_CONFIG, **(config or {})}
    if client is None:
        client = FakeClientFixed('{"directness": 0.5, "technical_depth": 0.5, "humor": 0.5, "verbosity": 0.5, "hedging": 0.5, "formality": 0.5}')
    daily = make_daily(sessions)
    return merge_into_wisdom(
        client=client,
        current_wisdom=current_wisdom,
        daily_summary=daily,
        config=cfg,
        today=today,
    )


# ---------------------------------------------------------------------------
# T1: New item gets correct id, prefix, confidence, dates, count
# ---------------------------------------------------------------------------

def test_new_item_gets_correct_fields():
    wisdom = make_wisdom()
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})]
    result = run_merge(wisdom, sessions)

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    item = facts[0]
    assert item["id"] == "f001"
    assert item["content"] == "Sam uses macOS"
    assert item["confidence"] == pytest.approx(BASE_CONFIG["initial_confidence_new_item"])
    assert item["first_seen"] == TODAY
    assert item["last_reinforced"] == TODAY
    assert item["reinforcement_count"] == 1


def test_new_item_uses_prefix_from_highest_existing_id():
    existing_fact = {
        "id": "uf003",
        "content": "Some existing frustration",
        "confidence": 0.6,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_frustrations": [existing_fact]})
    sessions = [make_session(user_signals={"frustrations": [{"content": "New frustration item"}]})]
    result = run_merge(wisdom, sessions)

    frustrations = result["items"]["user_frustrations"]
    new_items = [it for it in frustrations if it["id"] != "uf003"]
    assert len(new_items) == 1
    assert new_items[0]["id"] == "uf004"


def test_empty_category_uses_fallback_prefix():
    wisdom = make_wisdom()
    sessions = [make_session(user_signals={"preferences": [{"content": "Likes concise code"}]})]
    result = run_merge(wisdom, sessions)

    prefs = result["items"]["user_preferences"]
    assert len(prefs) == 1
    assert prefs[0]["id"].startswith("pr")


# ---------------------------------------------------------------------------
# T2: Exact-duplicate daily item reinforces
# ---------------------------------------------------------------------------

def test_exact_duplicate_reinforces():
    existing = {
        "id": "f001",
        "content": "Sam uses macOS",
        "confidence": 0.7,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 2,
    }
    wisdom = make_wisdom(items={"user_facts": [existing]})
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})]
    result = run_merge(wisdom, sessions)

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    item = facts[0]
    assert item["id"] == "f001"
    assert item["reinforcement_count"] == 3
    expected_confidence = min(0.7 + BASE_CONFIG["reinforcement_boost"], BASE_CONFIG["max_confidence"])
    assert item["confidence"] == pytest.approx(expected_confidence)
    assert item["last_reinforced"] == TODAY


def test_reinforcement_capped_at_max_confidence():
    existing = {
        "id": "f001",
        "content": "Sam uses macOS",
        "confidence": 0.95,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 5,
    }
    wisdom = make_wisdom(items={"user_facts": [existing]})
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})]
    result = run_merge(wisdom, sessions)

    item = result["items"]["user_facts"][0]
    assert item["confidence"] <= BASE_CONFIG["max_confidence"]
    assert item["confidence"] == pytest.approx(BASE_CONFIG["max_confidence"])


# ---------------------------------------------------------------------------
# T3: Near-duplicate (>=0.85) reinforces; dissimilar (<0.85) becomes new
# ---------------------------------------------------------------------------

def test_near_duplicate_reinforces():
    existing = {
        "id": "f001",
        "content": "Sam uses macOS for development",
        "confidence": 0.7,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_facts": [existing]})
    # Slightly rephrased — should be >=0.85 similar
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macos for development"}]})]
    result = run_merge(wisdom, sessions)

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    assert facts[0]["id"] == "f001"
    assert facts[0]["reinforcement_count"] == 2


def test_dissimilar_item_becomes_new():
    existing = {
        "id": "f001",
        "content": "Sam uses macOS",
        "confidence": 0.7,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_facts": [existing]})
    sessions = [make_session(user_signals={"facts": [{"content": "Sam works at Acme in sales"}]})]
    result = run_merge(wisdom, sessions)

    facts = result["items"]["user_facts"]
    assert len(facts) == 2
    ids = {it["id"] for it in facts}
    assert "f001" in ids
    assert "f002" in ids


# ---------------------------------------------------------------------------
# T4: Two same-day sessions with same claim → one reinforcement, not two new items
# ---------------------------------------------------------------------------

def test_same_claim_two_sessions_one_reinforcement():
    existing = {
        "id": "f001",
        "content": "Sam uses macOS",
        "confidence": 0.7,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 2,
    }
    wisdom = make_wisdom(items={"user_facts": [existing]})
    session1 = make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})
    session2 = make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})
    result = run_merge(wisdom, [session1, session2])

    facts = result["items"]["user_facts"]
    # Must not grow: existing item absorbs the duplicate
    assert len(facts) == 1
    assert facts[0]["reinforcement_count"] == 3
    assert facts[0]["id"] == "f001"


def test_two_distinct_sessions_same_claim_no_new_item():
    """Same as above but no pre-existing item: first session creates it, second reinforces it."""
    wisdom = make_wisdom()
    session1 = make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})
    session2 = make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})
    result = run_merge(wisdom, [session1, session2])

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    assert facts[0]["reinforcement_count"] == 1  # created by first, second absorbed


# ---------------------------------------------------------------------------
# T5: Decay applied to non-reinforced items; reinforced items not decayed
# ---------------------------------------------------------------------------

def test_decay_non_reinforced_items():
    stale = {
        "id": "f001",
        "content": "Some old fact",
        "confidence": 0.8,
        "first_seen": "2026-01-01",
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    reinforced = {
        "id": "f002",
        "content": "Sam uses macOS",
        "confidence": 0.6,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_facts": [stale, reinforced]})
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})]
    result = run_merge(wisdom, sessions)

    facts = {it["id"]: it for it in result["items"]["user_facts"]}
    assert facts["f001"]["confidence"] == pytest.approx(0.8 * BASE_CONFIG["decay_rate"])
    # Reinforced item should have boosted confidence, NOT decayed
    expected_reinforced = min(0.6 + BASE_CONFIG["reinforcement_boost"], BASE_CONFIG["max_confidence"])
    assert facts["f002"]["confidence"] == pytest.approx(expected_reinforced)


def test_new_items_not_decayed():
    wisdom = make_wisdom()
    sessions = [make_session(user_signals={"facts": [{"content": "Brand new fact"}]})]
    result = run_merge(wisdom, sessions)

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    assert facts[0]["confidence"] == pytest.approx(BASE_CONFIG["initial_confidence_new_item"])


# ---------------------------------------------------------------------------
# T6: Item decayed below prune_threshold moves to pruned
# ---------------------------------------------------------------------------

def test_pruned_items_below_threshold():
    near_threshold = {
        "id": "f001",
        "content": "Barely surviving fact",
        "confidence": 0.201,
        "first_seen": "2026-01-01",
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_facts": [near_threshold]})
    sessions = [make_session()]  # no reinforcement
    result = run_merge(wisdom, sessions)

    # After decay: 0.201 * 0.99 = 0.19899 < 0.2, should be pruned
    assert len(result["items"]["user_facts"]) == 0
    assert len(result["pruned"]) == 1
    pruned = result["pruned"][0]
    assert pruned["id"] == "f001"
    assert pruned["category"] == "user_facts"
    assert "final_confidence" in pruned
    assert pruned["final_confidence"] == pytest.approx(0.201 * BASE_CONFIG["decay_rate"])
    assert "first_seen" in pruned
    assert "last_reinforced" in pruned


def test_item_exactly_at_threshold_not_pruned():
    """An item decayed to exactly the threshold should NOT be pruned (strictly below)."""
    # confidence that after decay equals exactly threshold
    threshold = BASE_CONFIG["prune_threshold"]
    decay = BASE_CONFIG["decay_rate"]
    # We need after_decay == threshold, so before = threshold / decay
    confidence_before = threshold / decay
    item = {
        "id": "f001",
        "content": "Borderline fact",
        "confidence": confidence_before,
        "first_seen": "2026-01-01",
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"user_facts": [item]})
    sessions = [make_session()]
    result = run_merge(wisdom, sessions)

    after_decay = confidence_before * decay
    if after_decay < threshold:
        assert len(result["pruned"]) == 1
    else:
        assert len(result["pruned"]) == 0


# ---------------------------------------------------------------------------
# T7: Hard cap moves lowest-confidence excess to pruned
# ---------------------------------------------------------------------------

def test_hard_cap_prunes_lowest_confidence():
    config = {**BASE_CONFIG, "max_items_per_category": 2}
    items = [
        {"id": "f001", "content": "High confidence fact", "confidence": 0.9,
         "first_seen": YESTERDAY, "last_reinforced": YESTERDAY, "reinforcement_count": 1},
        {"id": "f002", "content": "Mid confidence fact", "confidence": 0.6,
         "first_seen": YESTERDAY, "last_reinforced": YESTERDAY, "reinforcement_count": 1},
    ]
    wisdom = make_wisdom(items={"user_facts": items})
    sessions = [make_session(user_signals={"facts": [{"content": "Brand new third fact"}]})]
    result = run_merge(wisdom, sessions, config=config)

    facts = result["items"]["user_facts"]
    assert len(facts) == 2
    # The lowest-confidence item after operations should be pruned
    pruned = result["pruned"]
    assert len(pruned) == 1
    # After decay, f002 goes from 0.6 → 0.594; f001 goes from 0.9 → 0.891; new = 0.5
    # Lowest is new fact at 0.5 → pruned
    assert pruned[0]["category"] == "user_facts"


def test_hard_cap_zero_tolerance_above_max():
    config = {**BASE_CONFIG, "max_items_per_category": 1}
    items = [
        {"id": "f001", "content": "Existing fact", "confidence": 0.9,
         "first_seen": YESTERDAY, "last_reinforced": YESTERDAY, "reinforcement_count": 1},
    ]
    wisdom = make_wisdom(items={"user_facts": items})
    sessions = [make_session(user_signals={"facts": [{"content": "Completely different new fact about something else entirely"}]})]
    result = run_merge(wisdom, sessions, config=config)

    facts = result["items"]["user_facts"]
    assert len(facts) == 1
    pruned = result["pruned"]
    assert len(pruned) == 1


# ---------------------------------------------------------------------------
# T8: Reminders carried verbatim
# ---------------------------------------------------------------------------

def test_reminders_carried_verbatim():
    reminders = [
        {"due": "2026-07-15", "action": "Review probe results", "created": "2026-06-30"},
        {"due": "2026-08-01", "action": "Check wisdom pruning rates", "created": "2026-06-30"},
    ]
    wisdom = make_wisdom()
    wisdom["reminders"] = copy.deepcopy(reminders)
    sessions = [make_session(user_signals={"facts": [{"content": "Some new fact"}]})]
    result = run_merge(wisdom, sessions)

    assert "reminders" in result
    assert result["reminders"] == reminders


def test_no_reminders_key_absent_when_not_in_current():
    wisdom = make_wisdom()
    assert "reminders" not in wisdom
    sessions = [make_session()]
    result = run_merge(wisdom, sessions)

    assert "reminders" not in result


# ---------------------------------------------------------------------------
# T9: Personality nudge clamped to ±0.05/axis and [0,1]
# ---------------------------------------------------------------------------

def test_personality_nudge_clamped_per_axis():
    current_pv = {ax: 0.5 for ax in PERSONALITY_AXES}
    wisdom = make_wisdom(personality=current_pv)
    # Client returns extreme targets — delta must be clamped to ±0.05
    client = FakeClientFixed(
        '{"directness": 1.0, "technical_depth": 0.0, "humor": 0.9, '
        '"verbosity": 0.1, "hedging": 0.8, "formality": 0.2}'
    )
    sessions = [make_session()]
    result = run_merge(wisdom, sessions, client=client)

    pv = result["personality_vector"]
    # directness: target=1.0, cur=0.5, delta=0.5 → clamped to +0.05 → 0.55
    assert pv["directness"] == pytest.approx(0.55)
    # technical_depth: target=0.0, cur=0.5, delta=-0.5 → clamped to -0.05 → 0.45
    assert pv["technical_depth"] == pytest.approx(0.45)
    for ax in PERSONALITY_AXES:
        assert 0.0 <= pv[ax] <= 1.0


def test_personality_clamped_to_zero_one_bounds():
    current_pv = {"directness": 0.02, "technical_depth": 0.98,
                  "humor": 0.5, "verbosity": 0.5, "hedging": 0.5, "formality": 0.5}
    wisdom = make_wisdom(personality=current_pv)
    # Target would push below 0 and above 1
    client = FakeClientFixed(
        '{"directness": 0.0, "technical_depth": 1.0, "humor": 0.5, '
        '"verbosity": 0.5, "hedging": 0.5, "formality": 0.5}'
    )
    sessions = [make_session()]
    result = run_merge(wisdom, sessions, client=client)

    pv = result["personality_vector"]
    # directness: 0.02 - 0.02 = 0.0, clamped to 0.0 (delta=-0.05 gives -0.03, still >=0)
    assert pv["directness"] >= 0.0
    assert pv["technical_depth"] <= 1.0


def test_personality_seeded_from_half_when_absent():
    wisdom = make_wisdom()
    del wisdom["personality_vector"]
    sessions = [make_session()]
    client = FakeClientFixed(
        '{"directness": 0.6, "technical_depth": 0.6, "humor": 0.6, '
        '"verbosity": 0.6, "hedging": 0.6, "formality": 0.6}'
    )
    result = run_merge(wisdom, sessions, client=client)

    pv = result["personality_vector"]
    # Seeded at 0.5, nudged toward 0.6 by max 0.05 → 0.55
    for ax in PERSONALITY_AXES:
        assert pv[ax] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# T10: Personality call raising → merge succeeds, vector unchanged
# ---------------------------------------------------------------------------

def test_personality_failure_does_not_block_merge():
    current_pv = {"directness": 0.7, "technical_depth": 0.3,
                  "humor": 0.4, "verbosity": 0.6, "hedging": 0.5, "formality": 0.8}
    wisdom = make_wisdom(personality=current_pv)
    sessions = [make_session(user_signals={"facts": [{"content": "Sam uses macOS"}]})]
    result = run_merge(wisdom, sessions, client=FakeClientRaises())

    # Merge must succeed despite the LLM failure
    assert result is not None
    assert "items" in result
    assert "personality_vector" in result

    # Vector must be unchanged (held steady)
    for ax in PERSONALITY_AXES:
        assert result["personality_vector"][ax] == pytest.approx(current_pv[ax])


# ---------------------------------------------------------------------------
# T11: Caller-managed fields NOT set by merge; required keys present
# ---------------------------------------------------------------------------

def test_merge_does_not_set_caller_fields():
    wisdom = make_wisdom()
    sessions = [make_session()]
    result = run_merge(wisdom, sessions)

    # These are set by main() after merge returns — merge must NOT touch them
    assert "last_consolidation" not in result
    assert "consolidation_count" not in result
    assert "version" not in result


def test_merge_returns_required_keys():
    wisdom = make_wisdom()
    sessions = [make_session()]
    result = run_merge(wisdom, sessions)

    assert "items" in result
    assert "personality_vector" in result
    assert "pruned" in result


def test_merge_carries_first_seen_from_current():
    wisdom = make_wisdom()
    wisdom["first_seen"] = "2026-04-01"
    sessions = [make_session()]
    result = run_merge(wisdom, sessions)

    assert result["first_seen"] == "2026-04-01"


# ---------------------------------------------------------------------------
# T12: End-to-end with realistic wisdom.json fixture
# ---------------------------------------------------------------------------

REALISTIC_WISDOM_FIXTURE = {
    "version": 1,
    "last_consolidation": YESTERDAY,
    "consolidation_count": 4,
    "first_seen": "2026-04-29",
    "items": {
        "user_facts": [
            {"id": "f001", "content": "User's name is Sam", "confidence": 0.8,
             "first_seen": "2026-04-29", "last_reinforced": "2026-04-29", "reinforcement_count": 1},
            {"id": "f002", "content": "Primary codebase is C#/.NET Aspire solution",
             "confidence": 0.88, "first_seen": "2026-04-29", "last_reinforced": YESTERDAY,
             "reinforcement_count": 3},
        ],
        "user_preferences": [
            {"id": "pr001", "content": "Concise responses, no boilerplate",
             "confidence": 0.76, "first_seen": "2026-04-29", "last_reinforced": "2026-04-29",
             "reinforcement_count": 1},
        ],
        "user_frustrations": [
            {"id": "uf001", "content": "Claude running tools after token limit hit",
             "confidence": 0.49, "first_seen": "2026-06-02", "last_reinforced": "2026-06-02",
             "reinforcement_count": 1},
        ],
        "user_style": [
            {"id": "s001", "content": "Direct in disagreement, challenges directly",
             "confidence": 0.86, "first_seen": "2026-04-29", "last_reinforced": "2026-04-30",
             "reinforcement_count": 2},
        ],
        "claude_lessons": [
            {"id": "l001", "what": "Declared file-reading impossible without checking MCP",
             "lesson": "Search available tools before refusing",
             "confidence": 0.66, "first_seen": "2026-04-29", "last_reinforced": "2026-04-29",
             "reinforcement_count": 1},
        ],
        "claude_calibrations": [
            {"id": "c001", "observation": "Stay objective and critical, never engage in pleasing behavior",
             "adjustment": "Disagree on substance when warranted",
             "confidence": 0.81, "first_seen": "2026-04-29", "last_reinforced": "2026-04-29",
             "reinforcement_count": 1},
        ],
        "claude_opinions": [
            {"id": "o001", "topic": "TDD workflow",
             "view": "Red-green discipline produces better outcomes than tests-after",
             "confidence": 0.6, "first_seen": "2026-04-29", "last_reinforced": "2026-04-29",
             "reinforcement_count": 1},
        ],
        "shared_themes": [
            {"id": "t001", "topic": "Evidence-based trust",
             "depth": "Core principle repeated across many sessions",
             "confidence": 0.85, "first_seen": "2026-04-29", "last_reinforced": YESTERDAY,
             "reinforcement_count": 3},
        ],
        "relationship": [
            {"id": "r001", "observation": "Sam asks for code reviews before merging",
             "confidence": 0.71, "first_seen": "2026-04-29", "last_reinforced": "2026-04-29",
             "reinforcement_count": 1},
        ],
    },
    "personality_vector": {
        "directness": 0.7,
        "technical_depth": 0.8,
        "humor": 0.3,
        "verbosity": 0.4,
        "hedging": 0.2,
        "formality": 0.6,
    },
    "pruned": [],
}


def test_end_to_end_realistic_fixture():
    wisdom = copy.deepcopy(REALISTIC_WISDOM_FIXTURE)
    sessions = [
        make_session(
            user_signals={
                "facts": [{"content": "User's name is Sam"}],  # matches f001 → reinforce
                "preferences": [{"content": "Prefers no fluff in answers"}],  # new
            },
            claude_signals={
                "lessons": [{"what": "Declared file-reading impossible without checking MCP",
                             "lesson": "Search available tools before refusing"}],  # matches l001 → reinforce
            },
            shared_themes=[{"topic": "Evidence-based trust", "depth": "Repeated again"}],  # matches t001 → reinforce
        )
    ]
    result = run_merge(wisdom, sessions)

    # Structure correctness
    assert "items" in result
    assert "personality_vector" in result
    assert "pruned" in result
    for cat in ["user_facts", "user_preferences", "user_frustrations", "user_style",
                "claude_lessons", "claude_calibrations", "claude_opinions",
                "shared_themes", "relationship"]:
        assert cat in result["items"]

    # f001 reinforced
    f001 = next(it for it in result["items"]["user_facts"] if it["id"] == "f001")
    assert f001["reinforcement_count"] == 2
    assert f001["last_reinforced"] == TODAY

    # l001 reinforced
    l001 = next(it for it in result["items"]["claude_lessons"] if it["id"] == "l001")
    assert l001["reinforcement_count"] == 2

    # t001 reinforced
    t001 = next(it for it in result["items"]["shared_themes"] if it["id"] == "t001")
    assert t001["reinforcement_count"] == 4

    # New preference item created
    prefs = result["items"]["user_preferences"]
    new_pref = next((it for it in prefs if "fluff" in it.get("content", "")), None)
    assert new_pref is not None

    # caller fields NOT set
    assert "last_consolidation" not in result
    assert "consolidation_count" not in result
    assert "version" not in result


# ---------------------------------------------------------------------------
# T13: Multi-category field mapping — various signal types routed correctly
# ---------------------------------------------------------------------------

def test_signal_routing_all_categories():
    wisdom = make_wisdom()
    sessions = [
        make_session(
            user_signals={
                "facts": [{"content": "Sam lives near the coast"}],
                "preferences": [{"content": "Prefers DDD"}],
                "frustrations": [{"content": "Claude ignores context"}],
                "style": [{"content": "Terse command style"}],
            },
            claude_signals={
                "lessons": [{"what": "Missed MCP tool", "lesson": "Check tools first"}],
                "calibrations": [{"observation": "Be direct", "adjustment": "Skip hedges"}],
                "opinions": [{"topic": "TDD", "view": "Always write tests first"}],
            },
            shared_themes=[{"topic": "Engineering rigor", "depth": "Recurring theme"}],
            relationship=[{"observation": "Sam prefers async communication"}],
        )
    ]
    result = run_merge(wisdom, sessions)

    assert len(result["items"]["user_facts"]) == 1
    assert len(result["items"]["user_preferences"]) == 1
    assert len(result["items"]["user_frustrations"]) == 1
    assert len(result["items"]["user_style"]) == 1
    assert len(result["items"]["claude_lessons"]) == 1
    assert len(result["items"]["claude_calibrations"]) == 1
    assert len(result["items"]["claude_opinions"]) == 1
    assert len(result["items"]["shared_themes"]) == 1
    assert len(result["items"]["relationship"]) == 1


def test_evidence_field_not_stored_in_wisdom_items():
    wisdom = make_wisdom()
    sessions = [make_session(
        user_signals={"facts": [{"content": "Sam uses macOS", "evidence": "from /Users/example path"}]}
    )]
    result = run_merge(wisdom, sessions)

    item = result["items"]["user_facts"][0]
    assert "evidence" not in item


# ---------------------------------------------------------------------------
# T14: Dedup uses correct primary-text field per category
# ---------------------------------------------------------------------------

def test_dedup_primary_field_lessons():
    existing = {
        "id": "l001",
        "what": "Missed MCP tool",
        "lesson": "Check tools first before refusing",
        "confidence": 0.6,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"claude_lessons": [existing]})
    sessions = [make_session(claude_signals={"lessons": [
        {"what": "Missed MCP tool", "lesson": "Search available tools before refusing"}
    ]})]
    result = run_merge(wisdom, sessions)

    lessons = result["items"]["claude_lessons"]
    assert len(lessons) == 1
    assert lessons[0]["reinforcement_count"] == 2


def test_dedup_primary_field_opinions():
    existing = {
        "id": "o001",
        "topic": "TDD workflow",
        "view": "Red-green is the best discipline",
        "confidence": 0.6,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"claude_opinions": [existing]})
    sessions = [make_session(claude_signals={"opinions": [
        {"topic": "TDD workflow", "view": "Always write tests first"}
    ]})]
    result = run_merge(wisdom, sessions)

    opinions = result["items"]["claude_opinions"]
    assert len(opinions) == 1
    assert opinions[0]["reinforcement_count"] == 2


def test_dedup_primary_field_calibrations():
    existing = {
        "id": "c001",
        "observation": "User wants direct responses",
        "adjustment": "Skip all hedging language",
        "confidence": 0.7,
        "first_seen": YESTERDAY,
        "last_reinforced": YESTERDAY,
        "reinforcement_count": 1,
    }
    wisdom = make_wisdom(items={"claude_calibrations": [existing]})
    sessions = [make_session(claude_signals={"calibrations": [
        {"observation": "User wants direct responses", "adjustment": "Be terse"}
    ]})]
    result = run_merge(wisdom, sessions)

    calibrations = result["items"]["claude_calibrations"]
    assert len(calibrations) == 1
    assert calibrations[0]["reinforcement_count"] == 2
