"""
Tests for wake.py rendering. Written first (red), then wake.py fixed (green).

Run: cd <repo-root> && python3 -m pytest scripts/test_wake.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wake  # noqa: E402


def make_item(conf: float, count: int = 1, **fields) -> dict:
    return {"id": "x001", "confidence": conf, "reinforcement_count": count, **fields}


def wisdom_with(items: dict) -> dict:
    return {
        "items": items,
        "personality_vector": {},
        "reminders": [],
        "first_seen": "2026-01-01",
        "last_consolidation": "2026-01-02",
        "consolidation_count": 2,
    }


# --- calibrations: observation/adjustment schema must render, not blank -----

def test_calibration_with_observation_and_adjustment_renders_both():
    it = make_item(0.5, observation="Answered too long", adjustment="Lead with the verdict")
    assert wake.render_item_content("claude_calibrations", it) == "Answered too long → Lead with the verdict"


def test_calibration_with_only_observation_renders_observation():
    it = make_item(0.5, observation="Answered too long")
    assert wake.render_item_content("claude_calibrations", it) == "Answered too long"


def test_legacy_calibration_with_content_still_renders():
    it = make_item(0.5, content="Be terser")
    assert wake.render_item_content("claude_calibrations", it) == "Be terser"


def test_no_blank_item_lines_are_emitted():
    w = wisdom_with({"claude_calibrations": [
        make_item(0.5, observation="A", adjustment="B"),
        make_item(0.4, content="C"),
    ]})
    block = wake.render_wisdom_block(w, token_budget=2000)
    item_lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
    assert item_lines, "expected rendered items"
    assert all(not ln.rstrip().endswith("]") for ln in item_lines), block


# --- trimming must not leave empty category headings ------------------------

def test_category_heading_dropped_when_all_its_items_are_trimmed():
    w = wisdom_with({
        "user_facts": [make_item(0.9, content="F" * 300)],
        "user_frustrations": [make_item(0.1, content="tiny frustration")],
    })
    # budget fits the header + the fact, but not the low-confidence frustration
    block = wake.render_wisdom_block(w, token_budget=130)
    assert "tiny frustration" not in block
    assert "## user_frustrations" not in block, block


def test_empty_category_has_no_heading():
    w = wisdom_with({"user_facts": [make_item(0.5, content="fact")], "user_frustrations": []})
    block = wake.render_wisdom_block(w, token_budget=2000)
    assert "## user_frustrations" not in block


# --- per-category cap so one category cannot crowd out the rest -------------

def test_per_category_cap_keeps_highest_confidence_items():
    facts = [make_item(round(0.30 + i * 0.01, 2), content=f"fact {i}") for i in range(30)]
    w = wisdom_with({"user_facts": facts, "claude_lessons": [make_item(0.5, what="w", lesson="l")]})
    block = wake.render_wisdom_block(w, token_budget=4000, max_items_per_category=5)
    fact_lines = [ln for ln in block.splitlines() if ln.startswith("- [") and "fact " in ln]
    assert len(fact_lines) == 5
    assert "fact 29" in block and "fact 0" not in block
    assert "w → l" in block


def test_reinforced_items_outrank_equal_confidence_singletons():
    facts = [
        make_item(0.5, count=1, content="seen once"),
        make_item(0.5, count=4, content="seen four times"),
    ]
    w = wisdom_with({"user_facts": facts})
    block = wake.render_wisdom_block(w, token_budget=2000, max_items_per_category=1)
    assert "seen four times" in block
    assert "seen once" not in block
