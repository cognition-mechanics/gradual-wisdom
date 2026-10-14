#!/usr/bin/env python3
"""
sleep.py — End-of-day consolidation.

Pulls the day's Claude Code sessions, asks Claude (no tools) to extract
structured signal from each, merges into a daily summary, then folds that
into cumulative wisdom.

The "no input during sleep" constraint is enforced by calling the Anthropic
API without any tools. Read-only with respect to past sessions, write-only
with respect to future self.

Usage:
    python3 sleep.py                       # consolidate today (UTC)
    python3 sleep.py --date 2026-04-29     # consolidate a specific day
    python3 sleep.py --dry-run             # extract but don't update wisdom
    python3 sleep.py --force               # re-run even if already done today
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import string
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import LLMClient  # noqa: E402


# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
ARCHIVE_DIR = DATA_DIR / "archive"
WISDOM_PATH = DATA_DIR / "wisdom.json"
LOCK_PATH = DATA_DIR / ".sleep.lock"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
CONFIG_PATH = SKILL_ROOT / "config.json"

PROMPTS_DIR = SKILL_ROOT / "prompts"
CONSOLIDATE_PROMPT_PATH = PROMPTS_DIR / "consolidate.md"
MERGE_PROMPT_PATH = PROMPTS_DIR / "merge.md"

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Project-dir name fragment that identifies our own internal claude -p calls.
# These sessions are routed through .runtime-cwd inside the skill folder, so
# their encoded project dir name will contain this substring.
INTERNAL_PROJECT_MARKER = "gradual-wisdom"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Locking — single instance per run
# ---------------------------------------------------------------------------

def acquire_lock(timeout_seconds: int) -> None:
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < timeout_seconds:
            sys.stderr.write(
                f"ERROR: another sleep cycle is in progress (lock age {int(age)}s). "
                f"If this is stale, delete {LOCK_PATH}\n"
            )
            sys.exit(3)
        # stale lock, take it
        LOCK_PATH.unlink()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))


def release_lock() -> None:
    if LOCK_PATH.exists():
        LOCK_PATH.unlink()


# ---------------------------------------------------------------------------
# Session discovery and parsing
# ---------------------------------------------------------------------------

def day_window_utc(date_str: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def find_sessions_for_day(date_str: str) -> list[Path]:
    """Find session JSONL files that contain at least one message in the day's window."""
    if not PROJECTS_DIR.exists():
        return []

    start, end = day_window_utc(date_str)
    candidates: list[Path] = []
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        # First filter: skip our own internal sessions by project dir name.
        # Robust to prompt changes; doesn't depend on transcript content.
        if INTERNAL_PROJECT_MARKER in jsonl.parent.name:
            continue
        try:
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        # quick prefilter: file mtime must be at-or-after the day start
        if mtime < start:
            continue
        candidates.append(jsonl)
    return candidates


def extract_messages_in_window(jsonl_path: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Extract message events from a session JSONL whose timestamp falls in [start, end)."""
    messages: list[dict[str, Any]] = []
    try:
        with open(jsonl_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = obj.get("timestamp") or obj.get("ts")
                if not ts_raw:
                    continue
                ts = parse_ts(ts_raw)
                if ts is None or not (start <= ts < end):
                    continue
                messages.append(obj)
    except OSError:
        return []
    return messages


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        # ms or s epoch
        if value > 1e12:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


SKIP_ROLES = {"queue-operation", "system-init", "tool-permission-grant", "summary"}

# Sessions whose transcripts contain any of these markers are our own automation
# calls (sleep.py / probe.py invoking claude -p). Filter them out so we don't
# consolidate consolidations.
SELF_REF_MARKERS = (
    "=== SYSTEM INSTRUCTIONS ===",
    "Consolidation Prompt \u2014 The Dream",
    "Merge Prompt \u2014 Updating Cumulative Wisdom",
)


def extract_message_content(m: dict[str, Any]) -> tuple[str | None, str]:
    """Pull (role, content_text) from a Claude Code JSONL event.

    Claude Code wraps messages in event envelopes like
    `{"type": "user-message", "message": {...}}`. Unwrap before reading.
    Returns (None, "") for events that should be skipped (metadata, internal).
    """
    # Unwrap one level of envelope if present
    if "message" in m and isinstance(m["message"], dict):
        outer_type = m.get("type", "")
        m = m["message"]
        role = m.get("role") or outer_type
    else:
        role = m.get("role") or m.get("type") or ""

    if role in SKIP_ROLES:
        return None, ""

    content = m.get("content")
    if isinstance(content, str):
        return role, content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                # Encrypted thinking signatures are huge and useless for consolidation
                continue
            elif btype == "tool_use":
                tool_name = block.get("name", "?")
                tool_input = block.get("input", {})
                if not isinstance(tool_input, str):
                    tool_input = json.dumps(tool_input)
                parts.append(f"[tool_use:{tool_name} {tool_input[:200]}]")
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = " ".join(
                        b.get("text", "") for b in result
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if not isinstance(result, str):
                    result = json.dumps(result)
                parts.append(f"[tool_result: {result[:300]}]")
        return role, "\n".join(p for p in parts if p)
    return role, ""


def is_self_referential(transcript: str) -> bool:
    return any(marker in transcript for marker in SELF_REF_MARKERS)


def session_to_transcript(messages: list[dict[str, Any]], max_chars: int = 80000) -> str:
    """Render a session's messages into a transcript string for the consolidation prompt."""
    lines: list[str] = []
    for m in messages:
        role, content = extract_message_content(m)
        if role is None or not content.strip():
            continue
        # trim very long single messages
        if len(content) > 4000:
            content = content[:4000] + " ...[truncated]"
        lines.append(f"=== {role} ===\n{content}\n")
    transcript = "\n".join(lines)
    if len(transcript) > max_chars:
        # keep beginning and end (most informative usually)
        head = transcript[: max_chars // 2]
        tail = transcript[-max_chars // 2 :]
        transcript = head + "\n\n[...middle truncated...]\n\n" + tail
    return transcript


# ---------------------------------------------------------------------------
# API calls — strictly no tools
# ---------------------------------------------------------------------------

def get_client(config: dict[str, Any]) -> LLMClient:
    return LLMClient(
        model=config["model"],
        max_tokens=config["max_tokens_per_call"],
        runtime=config.get("runtime", "auto"),
        fallbacks=config.get("model_fallbacks"),
    )


def consolidate_session(
    client: LLMClient,
    session_id: str,
    transcript: str,
) -> dict[str, Any] | None:
    prompt = CONSOLIDATE_PROMPT_PATH.read_text()
    user_msg = f"session_id: {session_id}\n\nTRANSCRIPT:\n\n{transcript}"
    text = client.call(system=prompt, user_msg=user_msg)
    return parse_json_strict(text, label=f"consolidate({session_id})")


# ---------------------------------------------------------------------------
# Programmatic merge helpers
# ---------------------------------------------------------------------------

_CATEGORY_PRIMARY_FIELD: dict[str, str] = {
    "user_facts": "content",
    "user_preferences": "content",
    "user_frustrations": "content",
    "user_style": "content",
    "claude_lessons": "what",
    "claude_calibrations": "observation",
    "claude_opinions": "topic",
    "shared_themes": "topic",
    "relationship": "observation",
}

_CATEGORY_FALLBACK_PREFIX: dict[str, str] = {
    "user_facts": "f",
    "user_preferences": "pr",
    "user_frustrations": "fu",
    "user_style": "st",
    "claude_lessons": "l",
    "claude_calibrations": "c",
    "claude_opinions": "o",
    "shared_themes": "t",
    "relationship": "r",
}

_DAILY_SIGNAL_MAP: dict[str, tuple[str, str]] = {
    # (session_path_key, sub_list_key) → category
    # resolved via _extract_daily_items
}

_PERSONALITY_AXES = ["directness", "technical_depth", "humor", "verbosity", "hedging", "formality"]


def _normalize_text(text: str) -> str:
    lower = text.lower()
    collapsed = re.sub(r"\s+", " ", lower).strip()
    return collapsed.translate(str.maketrans("", "", string.punctuation))


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _primary_text(item: dict[str, Any], category: str) -> str:
    field = _CATEGORY_PRIMARY_FIELD.get(category, "content")
    return item.get(field, "")


def _next_id(existing_items: list[dict[str, Any]], category: str) -> str:
    """Derive the next sequential id for the category.

    For non-empty categories: scan existing ids, extract the alpha prefix and
    max numeric suffix, increment by 1 with zero-padding to 3 digits.
    For empty categories: use the fallback prefix map with 001.
    """
    if not existing_items:
        prefix = _CATEGORY_FALLBACK_PREFIX.get(category, "x")
        return f"{prefix}001"

    best_prefix = ""
    best_num = 0
    for item in existing_items:
        item_id = item.get("id", "")
        match = re.match(r"^([a-zA-Z]+)(\d+)$", item_id)
        if not match:
            continue
        alpha = match.group(1)
        num = int(match.group(2))
        if num > best_num:
            best_num = num
            best_prefix = alpha

    if not best_prefix:
        best_prefix = _CATEGORY_FALLBACK_PREFIX.get(category, "x")
        best_num = 0

    return f"{best_prefix}{best_num + 1:03d}"


def _extract_daily_items(daily_summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Flatten all sessions into per-category lists of daily items.

    Evidence fields are dropped; only the mapped text fields are kept.
    """
    category_items: dict[str, list[dict[str, Any]]] = {cat: [] for cat in _CATEGORY_PRIMARY_FIELD}

    for session in daily_summary.get("sessions", []):
        user_signals = session.get("user_signals", {})

        for raw_item in user_signals.get("facts", []):
            category_items["user_facts"].append({"content": raw_item.get("content", "")})

        for raw_item in user_signals.get("preferences", []):
            category_items["user_preferences"].append({"content": raw_item.get("content", "")})

        for raw_item in user_signals.get("frustrations", []):
            category_items["user_frustrations"].append({"content": raw_item.get("content", "")})

        for raw_item in user_signals.get("style", []):
            category_items["user_style"].append({"content": raw_item.get("content", "")})

        claude_signals = session.get("claude_signals", {})

        for raw_item in claude_signals.get("lessons", []):
            category_items["claude_lessons"].append({
                "what": raw_item.get("what", ""),
                "lesson": raw_item.get("lesson", ""),
            })

        for raw_item in claude_signals.get("calibrations", []):
            category_items["claude_calibrations"].append({
                "observation": raw_item.get("observation", ""),
                "adjustment": raw_item.get("adjustment", ""),
            })

        for raw_item in claude_signals.get("opinions", []):
            category_items["claude_opinions"].append({
                "topic": raw_item.get("topic", ""),
                "view": raw_item.get("view", ""),
            })

        for raw_item in session.get("shared_themes", []):
            category_items["shared_themes"].append({
                "topic": raw_item.get("topic", ""),
                "depth": raw_item.get("depth", ""),
            })

        for raw_item in session.get("relationship", []):
            category_items["relationship"].append({
                "observation": raw_item.get("observation", ""),
            })

    return category_items


def _merge_category(
    category: str,
    existing_items: list[dict[str, Any]],
    daily_items: list[dict[str, Any]],
    today: str,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge daily items into existing items for one category.

    Returns (merged_items, pruned_items). Pruning by threshold happens here;
    hard-cap pruning happens in the caller after all categories are merged.

    Algorithm:
    - Working set = deep copy of existing items + items created this run.
    - For each daily item:
        1. Find best match (ratio>=0.85) among untouched working items → reinforce, mark touched.
        2. Else if match among already-touched items → absorb (no-op).
        3. Else → create new item, mark touched.
    - Decay all untouched items.
    - Prune items below threshold.
    """
    boost = config["reinforcement_boost"]
    max_conf = config["max_confidence"]
    decay_rate = config["decay_rate"]
    prune_threshold = config["prune_threshold"]
    initial_conf = config["initial_confidence_new_item"]

    working: list[dict[str, Any]] = [copy.deepcopy(it) for it in existing_items]
    touched: set[int] = set()  # indices into working

    for daily_item in daily_items:
        daily_primary = _normalize_text(_primary_text(daily_item, category))
        if not daily_primary:
            continue

        best_ratio = 0.0
        best_idx = -1
        best_ratio_touched = 0.0
        best_idx_touched = -1

        for idx, w_item in enumerate(working):
            w_primary = _normalize_text(_primary_text(w_item, category))
            ratio = _similarity(daily_primary, w_primary)
            if idx not in touched:
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = idx
            else:
                if ratio > best_ratio_touched:
                    best_ratio_touched = ratio
                    best_idx_touched = idx

        if best_ratio >= 0.85 and best_idx >= 0:
            # Reinforce the best untouched match
            w_item = working[best_idx]
            w_item["confidence"] = min(w_item["confidence"] + boost, max_conf)
            w_item["last_reinforced"] = today
            w_item["reinforcement_count"] = w_item.get("reinforcement_count", 0) + 1
            touched.add(best_idx)
        elif best_ratio_touched >= 0.85 and best_idx_touched >= 0:
            # Absorb: already-touched match found, no new item
            pass
        else:
            # Create new item
            new_id = _next_id(working, category)
            new_item: dict[str, Any] = {
                "id": new_id,
                "confidence": initial_conf,
                "first_seen": today,
                "last_reinforced": today,
                "reinforcement_count": 1,
            }
            new_item.update(daily_item)
            working.append(new_item)
            touched.add(len(working) - 1)

    # Decay untouched items
    for idx, w_item in enumerate(working):
        if idx not in touched:
            w_item["confidence"] *= decay_rate

    # Prune by threshold
    kept: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    for w_item in working:
        if w_item["confidence"] < prune_threshold:
            pruned_entry: dict[str, Any] = {
                "id": w_item.get("id", ""),
                "category": category,
                "final_confidence": w_item["confidence"],
                "first_seen": w_item.get("first_seen", ""),
                "last_reinforced": w_item.get("last_reinforced", ""),
            }
            pruned_entry.update({k: v for k, v in w_item.items()
                                  if k not in pruned_entry})
            pruned.append(pruned_entry)
        else:
            kept.append(w_item)

    return kept, pruned


def _apply_hard_cap(
    category: str,
    items: list[dict[str, Any]],
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """If items exceed max_items, move the lowest-confidence excess to pruned."""
    if len(items) <= max_items:
        return items, []

    sorted_items = sorted(items, key=lambda it: it["confidence"])
    excess_count = len(items) - max_items
    excess = sorted_items[:excess_count]
    kept = sorted_items[excess_count:]

    pruned: list[dict[str, Any]] = []
    for it in excess:
        pruned_entry: dict[str, Any] = {
            "id": it.get("id", ""),
            "category": category,
            "final_confidence": it["confidence"],
            "first_seen": it.get("first_seen", ""),
            "last_reinforced": it.get("last_reinforced", ""),
        }
        pruned_entry.update({k: v for k, v in it.items() if k not in pruned_entry})
        pruned.append(pruned_entry)

    return kept, pruned


def personality_target(client: Any, daily_summary: dict[str, Any]) -> dict[str, float] | None:
    """Ask the model to read today's claude_signals + relationship items and return
    a 6-axis personality vector. Returns None if the call fails or returns unparseable data.
    Never raises.
    """
    prompt = (
        "You are reading today's session signals for a gradual-wisdom system. "
        "Based ONLY on the claude_signals (lessons, calibrations, opinions) and "
        "relationship items in the daily summary below, return a JSON object with "
        "exactly these six float keys in [0,1]: directness, technical_depth, humor, "
        "verbosity, hedging, formality. "
        "If there is insufficient signal to form a view, return the JSON string null. "
        "Output ONLY the JSON — no commentary."
    )
    user_msg = json.dumps(daily_summary, indent=2)

    try:
        text = client.call(system=prompt, user_msg=user_msg)
    except Exception:
        return None

    try:
        cleaned = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
        if fence:
            cleaned = fence.group(1).strip()
        if cleaned.lower() in ("null", ""):
            return None
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            return None
        result: dict[str, float] = {}
        for axis in _PERSONALITY_AXES:
            if axis not in parsed:
                return None
            result[axis] = float(parsed[axis])
        return result
    except Exception:
        return None


def _nudge_personality(
    current: dict[str, float],
    target: dict[str, float] | None,
) -> dict[str, float]:
    """Nudge each axis from current toward target by at most 0.05, clamped to [0,1].

    If target is None, hold current steady.
    Seeds missing axes from 0.5.
    """
    base = {ax: current.get(ax, 0.5) for ax in _PERSONALITY_AXES}
    if target is None:
        return base

    result: dict[str, float] = {}
    for ax in _PERSONALITY_AXES:
        cur = base[ax]
        tgt = target.get(ax, cur)
        raw_delta = tgt - cur
        clamped_delta = max(-0.05, min(0.05, raw_delta))
        result[ax] = max(0.0, min(1.0, cur + clamped_delta))
    return result


def merge_into_wisdom(
    client: Any,
    current_wisdom: dict[str, Any],
    daily_summary: dict[str, Any],
    config: dict[str, Any],
    today: str,
) -> dict[str, Any]:
    """Programmatic merge of daily_summary into current_wisdom.

    Returns a new wisdom dict with keys: items, personality_vector, pruned,
    first_seen (if present in current), and reminders (verbatim if present).
    Does NOT set version, last_consolidation, or consolidation_count — the
    caller (main) sets those after merge returns.
    """
    daily_items_by_category = _extract_daily_items(daily_summary)

    new_items: dict[str, list[dict[str, Any]]] = {}
    all_pruned: list[dict[str, Any]] = []

    existing_items_map = current_wisdom.get("items", {})
    max_items = config["max_items_per_category"]

    for category in _CATEGORY_PRIMARY_FIELD:
        existing = existing_items_map.get(category, [])
        daily = daily_items_by_category.get(category, [])

        kept, pruned = _merge_category(
            category=category,
            existing_items=existing,
            daily_items=daily,
            today=today,
            config=config,
        )

        kept, cap_pruned = _apply_hard_cap(category, kept, max_items)
        all_pruned.extend(pruned)
        all_pruned.extend(cap_pruned)
        new_items[category] = kept

    pv_target = personality_target(client, daily_summary)
    current_pv = current_wisdom.get("personality_vector", {})
    new_pv = _nudge_personality(current_pv, pv_target)

    result: dict[str, Any] = {
        "items": new_items,
        "personality_vector": new_pv,
        "pruned": all_pruned,
    }

    if "first_seen" in current_wisdom:
        result["first_seen"] = current_wisdom["first_seen"]

    if "reminders" in current_wisdom:
        result["reminders"] = current_wisdom["reminders"]

    return result


def parse_json_strict(text: str, label: str) -> dict[str, Any] | None:
    """Strip optional markdown fence and parse JSON; return None on failure."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"WARN: {label} returned non-JSON ({e}). First 300 chars:\n{cleaned[:300]}\n")
        # Persist full raw response for post-mortem
        debug_dir = DATA_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"raw-{label.replace('/', '_').replace('(', '').replace(')', '')}-{int(time.time())}.txt"
        debug_path.write_text(cleaned)
        sys.stderr.write(f"      Full response: {debug_path}\n")
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def empty_wisdom(today: str) -> dict[str, Any]:
    return {
        "version": 1,
        "last_consolidation": None,
        "consolidation_count": 0,
        "first_seen": today,
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
        "personality_vector": {
            "directness": 0.5,
            "technical_depth": 0.5,
            "humor": 0.5,
            "verbosity": 0.5,
            "hedging": 0.5,
            "formality": 0.5,
        },
        "pruned": [],
    }


def archive_pruned(wisdom: dict[str, Any], today: str) -> None:
    pruned = wisdom.get("pruned") or []
    if not pruned:
        return
    archive_path = ARCHIVE_DIR / f"{today}-pruned.json"
    with open(archive_path, "w") as f:
        json.dump(pruned, f, indent=2)
    # clear from active wisdom — the merge result already removed them from items
    wisdom["pruned"] = []


def append_metrics(record: dict[str, Any]) -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(METRICS_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--dry-run", action="store_true", help="Extract but don't update wisdom.json")
    ap.add_argument("--force", action="store_true", help="Re-run even if already done for this date")
    args = ap.parse_args()

    config = load_config()
    today = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_path = DAILY_DIR / f"{today}.json"
    if daily_path.exists() and not args.force and not args.dry_run:
        sys.stderr.write(f"Already consolidated for {today}. Use --force to re-run.\n")
        return 0

    acquire_lock(config["consolidation_lock_seconds"])
    try:
        client = get_client(config)
        start, end = day_window_utc(today)

        # 1. Discover sessions
        session_files = find_sessions_for_day(today)
        if not session_files:
            sys.stderr.write(f"No session files found for {today}.\n")
            return 0

        # 2. Per-session extraction
        per_session_results: list[dict[str, Any]] = []
        skipped: list[str] = []
        for jsonl in session_files:
            session_id = jsonl.stem
            messages = extract_messages_in_window(jsonl, start, end)
            if len(messages) < config["session_min_messages"]:
                skipped.append(session_id)
                continue
            transcript = session_to_transcript(messages)
            if is_self_referential(transcript):
                print(f"Skipping {session_id}: self-referential automation session", file=sys.stderr)
                skipped.append(session_id)
                continue
            print(f"Consolidating {session_id} ({len(messages)} msgs, {len(transcript)} chars)...", file=sys.stderr)
            if args.dry_run:
                # save transcript for inspection
                debug_dir = DATA_DIR / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"transcript-{session_id}.txt").write_text(transcript)
            result = consolidate_session(
                client=client,
                session_id=session_id,
                transcript=transcript,
            )
            if result is None:
                # parse failure already logged + persisted by parse_json_strict
                skipped.append(session_id)
                continue
            if result.get("skip"):
                # log why so we can see if model is being too conservative
                summary = result.get("session_summary", "(no summary given)")
                print(f"  -> skipped by model: {summary}", file=sys.stderr)
                skipped.append(session_id)
                continue
            per_session_results.append(result)
            time.sleep(1)  # gentle rate-limit padding

        # 3. Daily summary file (audit trail)
        daily_summary = {
            "date": today,
            "session_count": len(per_session_results),
            "skipped_sessions": skipped,
            "sessions": per_session_results,
        }
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        with open(daily_path, "w") as f:
            json.dump(daily_summary, f, indent=2)
        print(f"Wrote {daily_path}", file=sys.stderr)

        if args.dry_run:
            print("Dry run: skipping wisdom merge.", file=sys.stderr)
            return 0

        if not per_session_results:
            print("No substantive sessions to consolidate. Wisdom not updated.", file=sys.stderr)
            return 0

        # 4. Merge into cumulative wisdom
        current_wisdom: dict[str, Any]
        if WISDOM_PATH.exists():
            with open(WISDOM_PATH) as f:
                current_wisdom = json.load(f)
        else:
            current_wisdom = empty_wisdom(today)

        new_wisdom = merge_into_wisdom(
            client=client,
            current_wisdom=current_wisdom,
            daily_summary=daily_summary,
            config=config,
            today=today,
        )

        if new_wisdom is None:
            sys.stderr.write("Merge step failed; preserving previous wisdom.\n")
            return 4

        # safety: preserve metadata if model dropped fields
        new_wisdom.setdefault("version", 1)
        new_wisdom["last_consolidation"] = today
        new_wisdom["consolidation_count"] = current_wisdom.get("consolidation_count", 0) + 1
        new_wisdom.setdefault("first_seen", current_wisdom.get("first_seen", today))

        # 5. Archive pruned items
        archive_pruned(new_wisdom, today)

        # 6. Persist
        with open(WISDOM_PATH, "w") as f:
            json.dump(new_wisdom, f, indent=2)
        print(f"Updated {WISDOM_PATH}", file=sys.stderr)

        # 7. Metrics row
        append_metrics({
            "date": today,
            "event": "sleep",
            "sessions_consolidated": len(per_session_results),
            "sessions_skipped": len(skipped),
            "wisdom_item_counts": {
                k: len(v) for k, v in new_wisdom.get("items", {}).items()
            },
            "personality_vector": new_wisdom.get("personality_vector", {}),
            "consolidation_count": new_wisdom["consolidation_count"],
        })

        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
