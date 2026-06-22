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
import json
import os
import re
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


def merge_into_wisdom(
    client: LLMClient,
    current_wisdom: dict[str, Any],
    daily_summary: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = MERGE_PROMPT_PATH.read_text()
    user_msg = (
        "current_wisdom:\n```json\n"
        + json.dumps(current_wisdom, indent=2)
        + "\n```\n\n"
        + "daily_summary:\n```json\n"
        + json.dumps(daily_summary, indent=2)
        + "\n```"
    )
    text = client.call(system=prompt, user_msg=user_msg)
    return parse_json_strict(text, label="merge")


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
