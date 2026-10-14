---
name: gradual-wisdom
description: Diurnal consolidation cycle for Claude Code. Each night a "sleep" process condenses the day's sessions into structured wisdom; each morning a "wake" hook injects that wisdom into the new session's context. Includes a scientific probe suite for measuring behavioral drift over time. The intent is to grow Claude's persistent context (scaffolded cognition) and to make that growth measurable.
origin: user
version: 0.1.0
---

# Gradual Wisdom

A diurnal cycle of consolidation and recall, layered on top of `continuous-learning-v2`.

## When to Activate

- User asks about "the dream cycle", "wisdom file", "what Claude remembers across sessions"
- User wants to inspect, prune, edit, or reset accumulated wisdom
- User wants to run probes or view longitudinal metrics
- User wants to manually trigger a sleep cycle (e.g. `/dream`) or skip a day's consolidation
- Setting up the `SessionStart` hook (session-start catch-up model; no cron)
- Discussing the scientific framework — probe results, opinion drift, memory accuracy

## How It Works

```
        [waking]                                      [sleep]
+-----------------------+                  +-------------------------+
| Sessions accumulate    |                 | SessionStart catch-up   |
| in ~/.claude/projects/ |  -- end of --> | scripts/sleep.py        |
| (existing Claude Code  |     day         | (read-only, no tools)   |
|  behavior)             |                 |                         |
+-----------------------+                  | 1. Pull today's sessions|
            ^                              | 2. Per-session extract  |
            |                              | 3. Merge into daily/    |
            |                              | 4. Update wisdom.json   |
            |                              | 5. Append metrics.jsonl |
            |                              +-------------------------+
            |                                          |
            |                                          v
+-----------------------+                  +-------------------------+
| SessionStart hook      |  <-- morning -- | data/wisdom.json        |
| runs scripts/wake.py   |                 | (compact, machine-       |
| -> injects wisdom into |                 |  oriented JSON)          |
| new session context    |                 +-------------------------+
+-----------------------+
```

The "no input during sleep" constraint is enforced by calling the Anthropic API
**without any tools attached**. The dream cycle has read access to past sessions
and write access to its future self — nothing else.

## Components

| Path | Purpose |
|------|---------|
| `scripts/sleep.py` | End-of-day consolidation. Idempotent per UTC day. |
| `scripts/wake.py` | Emits compact context for `SessionStart` hook to inject. |
| `scripts/catchup.py` | Session-start catch-up: consolidates any missed UTC days (daily-file = done-marker), bounded to a 14-day lookback, and runs the probe suite when due. Detached; never blocks the session. |
| `scripts/session_start.sh` | The `SessionStart` hook: runs `wake.py` (injects wisdom) then launches `catchup.py` detached. Re-entrancy-guarded via `GRADUAL_WISDOM_CATCHUP`. |
| `scripts/probe.py` | Runs the probe suite (baseline + treatment), saves responses. |
| `scripts/metrics.py` | Computes longitudinal metrics from probes & daily files. |
| `scripts/install.sh` | One-shot setup: makes scripts executable, prints the `SessionStart` hook line. Consolidation runs on session start (catch-up); no cron. |
| `prompts/consolidate.md` | The "dream" prompt — strict JSON output, no tools. |
| `prompts/probes.json` | Versioned probe suite. v0.1 is a draft for co-design. |
| `config.json` | Tunables: API model, decay rate, pruning threshold, probe interval. |
| `data/daily/YYYY-MM-DD.json` | Append-only audit trail. Each day's raw consolidation. |
| `data/wisdom.json` | Cumulative state. This is what gets loaded at wake. |
| `data/probes/YYYY-MM-DD.json` | Probe responses, both conditions. |
| `data/metrics.jsonl` | Time-series metrics for plotting. |
| `data/archive/` | Pruned items below confidence threshold. Kept for science. |

## Honest Framing

This system does **not** modify model weights. It is **scaffolded cognition** —
behavioral change driven by accumulated structured context. The "memory" is fully
inspectable, ablatable (run a probe with `--no-wisdom`), and editable. That
property is what makes it scientifically tractable: you can measure the marginal
effect of accumulated wisdom on any given behavior.

## Commands

| Command | Description |
|---------|-------------|
| `python3 scripts/sleep.py` | Manually trigger consolidation for the current UTC day. |
| `python3 scripts/sleep.py --date YYYY-MM-DD` | Reconsolidate a past day. |
| `python3 scripts/wake.py` | Print the wisdom block (what `SessionStart` hook injects). |
| `python3 scripts/probe.py` | Run probe suite for today (both baseline and treatment). |
| `python3 scripts/probe.py --baseline-only` | Skip the wisdom-loaded condition. |
| `python3 scripts/metrics.py` | Compute and print the longitudinal report. |
| `python3 scripts/metrics.py --json` | Same, as JSON for downstream analysis. |

## Configuration

See `config.json`. Key knobs:

- `model` — which Claude model performs consolidation (default `claude-sonnet-4-5`).
- `decay_rate` — daily multiplier applied to confidence of un-reinforced items (default `0.99`).
- `prune_threshold` — items with confidence below this get archived (default `0.2`).
- `max_items_per_category` — hard cap per category in wisdom.json (default `200`).
- `probe_interval_days` — how often probes run automatically (default `7`).
- `wake_token_budget` — soft cap for the wake-injection block (default `2000` tokens).

## Privacy

Everything stays local. The Anthropic API is called for consolidation/probes,
but nothing is shared with anyone else. Daily files are full audit logs — you
can `grep` them, edit them, delete them. The wisdom file is a derived artifact;
deleting it triggers a "fresh wake" with no accumulated context.

## Related

- `continuous-learning-v2` — atomic per-session instinct extraction. This skill
  consumes the same source data but at a coarser temporal grain.
- `strategic-compact` — short-form session compression. Different goal: token
  saving within a session, not cross-session memory.
