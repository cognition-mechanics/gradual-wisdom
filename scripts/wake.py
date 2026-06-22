#!/usr/bin/env python3
"""
wake.py — Emit the wisdom block for SessionStart hook injection.

Reads ~/.claude/skills/gradual-wisdom/data/wisdom.json and produces a compact
context block that the SessionStart hook injects into the new session.

Used by the Claude Code hook system. The hook contract: stdout is captured
and made available as additional context. We emit JSON with an
`additionalContext` string for hook compatibility, OR plain text if invoked
with --plain.

Usage:
    python3 wake.py             # emit hook-format JSON
    python3 wake.py --plain     # emit human-readable text
    python3 wake.py --raw       # emit raw wisdom.json (debugging)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
WISDOM_PATH = SKILL_ROOT / "data" / "wisdom.json"
CONFIG_PATH = SKILL_ROOT / "config.json"


def load_wisdom() -> dict[str, Any] | None:
    if not WISDOM_PATH.exists():
        return None
    try:
        with open(WISDOM_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def render_item_content(category: str, it: dict[str, Any]) -> str:
    """Categories use different field names per the consolidate/merge schema.
    Normalize them to a single display string here.
    """
    if category == "claude_lessons":
        what = (it.get("what") or "").strip()
        lesson = (it.get("lesson") or "").strip()
        if what and lesson:
            return f"{what} → {lesson}"
        return what or lesson or it.get("content", "")
    if category == "claude_opinions":
        topic = (it.get("topic") or "").strip()
        view = (it.get("view") or "").strip()
        if topic and view:
            return f"on {topic}: {view}"
        return view or topic or it.get("content", "")
    if category == "shared_themes":
        topic = (it.get("topic") or "").strip()
        depth = (it.get("depth") or "").strip()
        if topic and depth:
            return f"{topic} ({depth})"
        return topic or it.get("content", "")
    if category == "relationship":
        obs = (it.get("observation") or "").strip()
        ev = (it.get("evidence") or "").strip()
        if obs and ev:
            return f"{obs}  [evidence: {ev}]"
        return obs or ev or it.get("content", "")
    # default: content-bearing categories (user_*, claude_calibrations)
    return it.get("content", "")


def render_wisdom_block(wisdom: dict[str, Any], token_budget: int) -> str:
    """Produce a compact, structured block for context injection.

    Items are sorted by confidence within each category and trimmed to fit
    a soft token budget (estimated as ~4 chars per token).
    """
    char_budget = token_budget * 4

    items = wisdom.get("items", {})
    pv = wisdom.get("personality_vector", {})
    reminders = wisdom.get("reminders", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta = {
        "first_seen": wisdom.get("first_seen"),
        "last_consolidation": wisdom.get("last_consolidation"),
        "consolidation_count": wisdom.get("consolidation_count", 0),
    }

    # Order: facts and preferences first (highest signal-to-noise),
    # then style, then claude self-state, then themes.
    category_order = [
        "user_facts",
        "user_preferences",
        "user_style",
        "user_frustrations",
        "claude_calibrations",
        "claude_lessons",
        "claude_opinions",
        "relationship",
        "shared_themes",
    ]

    lines: list[str] = []
    lines.append("=== GRADUAL WISDOM (loaded at session start) ===")
    lines.append(
        f"Days observed: {meta['consolidation_count']} | "
        f"first seen: {meta['first_seen']} | "
        f"last consolidation: {meta['last_consolidation']}"
    )
    if pv:
        pv_str = ", ".join(f"{k}={v:.2f}" for k, v in pv.items())
        lines.append(f"Personality vector: {pv_str}")

    # Surface due reminders prominently — these are scheduled commitments.
    due = [r for r in reminders if r.get("due", "9999-99-99") <= today]
    if due:
        lines.append("")
        lines.append("## DUE REMINDERS")
        for r in due:
            lines.append(f"- [{r.get('due')}] {r.get('action','(no action)')}")
    lines.append("")

    for category in category_order:
        cat_items = items.get(category, [])
        if not cat_items:
            continue
        # sort by confidence desc, take all that fit
        sorted_items = sorted(
            cat_items,
            key=lambda x: x.get("confidence", 0),
            reverse=True,
        )
        lines.append(f"## {category}")
        for it in sorted_items:
            confidence = it.get("confidence", 0)
            content = render_item_content(category, it)
            count = it.get("reinforcement_count", 1)
            lines.append(f"- [{confidence:.2f}, x{count}] {content}")
        lines.append("")

    lines.append("=== END WISDOM ===")
    block = "\n".join(lines)

    # If over budget, trim by dropping lowest-confidence items category by category
    while len(block) > char_budget:
        # find the line with the smallest [conf, xN] pattern in items
        # cheap approach: drop the very last item-line in any category
        item_line_indices = [
            i for i, ln in enumerate(lines) if ln.startswith("- [")
        ]
        if not item_line_indices:
            break
        # drop the lowest-confidence item across all
        def conf_of(idx: int) -> float:
            try:
                return float(lines[idx][3:7])
            except (ValueError, IndexError):
                return 1.0
        worst = min(item_line_indices, key=conf_of)
        del lines[worst]
        block = "\n".join(lines)

    return block


def render_human(wisdom: dict[str, Any] | None) -> str:
    if wisdom is None:
        return "(no wisdom yet — first sleep cycle hasn't run)"
    config = load_config()
    return render_wisdom_block(wisdom, config["wake_token_budget"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plain", action="store_true", help="Emit plain text, not hook JSON")
    ap.add_argument("--raw", action="store_true", help="Emit raw wisdom.json")
    args = ap.parse_args()

    wisdom = load_wisdom()

    if args.raw:
        if wisdom is None:
            print("{}")
        else:
            print(json.dumps(wisdom, indent=2))
        return 0

    if args.plain:
        print(render_human(wisdom))
        return 0

    # SessionStart hook contract: emit a JSON object with hookSpecificOutput
    # containing additionalContext string. Empty additionalContext is fine.
    block = render_human(wisdom)
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": block,
        }
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
