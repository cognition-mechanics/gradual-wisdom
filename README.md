# gradual-wisdom

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A persistent memory layer for [Claude Code](https://claude.ai/code), built as a scientific instrument.

Each day a *sleep* job reads your Claude Code session transcripts, extracts structured observations about how you work, and merges them into a compact knowledge base. Each morning a *wake* hook injects that knowledge into every new session. A probe suite runs the same fixed questions against two conditions — augmented vs. baseline — so you can measure whether the memory actually changes behavior, not just whether it looks like it does.

This is **scaffolded cognition**, not weight learning. The memory lives in a JSON file you can read, edit, ablate, or delete. That makes it scientifically tractable.

---

## How it works

```
  sessions accumulate          sleep (session-start catch-up)
  in ~/.claude/projects/  ──►  scripts/sleep.py
                               └─ reads today's transcripts
                               └─ extracts structured signal per session
                               └─ merges into data/wisdom.json
                                  (confidence-weighted, with decay)
          ▲
          │
  SessionStart hook            wake (each new session)
  injects wisdom block    ◄──  scripts/wake.py
                               └─ reads wisdom.json
                               └─ emits compact context block
                               └─ hook injects it at session start

  session-start cadence        probe suite
                          ──►  scripts/probe.py
                               └─ runs each probe in two conditions:
                                  baseline  (no wisdom loaded)
                                  treatment (wisdom loaded)
                               └─ saves to data/probes/YYYY-MM-DD.json
```

---

## Install

```bash
bash scripts/install.sh
```

The installer checks your Python environment and prints the `settings.json` `SessionStart` hook (`scripts/session_start.sh`) you need to add. Consolidation runs on **session start** via `catchup.py` (detached), which fills any missed UTC days and runs probes when due — there is **no cron**. This removes every scheduled-job failure mode (env stripping, laptop asleep at 03:00, silent overnight rc=1).

> **macOS note:** Because consolidation now runs on session start (when you are actively at the machine), the old cron pitfalls no longer apply — the Keychain is unlocked and the `claude` CLI auth works. No `ANTHROPIC_API_KEY` in a cron env is required.

---

## What gets stored

Nine categories, each item carrying a confidence score in [0, 1]:

| Category | What it holds |
|---|---|
| `user_facts` | Stable facts about the user |
| `user_preferences` | Expressed or inferred preferences |
| `user_style` | Communication style signals |
| `claude_lessons` | Mistakes made and corrected |
| `claude_calibrations` | Adjustments to approach confirmed by the user |
| `claude_opinions` | Positions taken on technical questions |
| `shared_themes` | Projects and topics that recur across sessions |
| `relationship` | How collaboration works between user and model |

Plus a six-axis **personality vector** (`directness`, `technical_depth`, `humor`, `verbosity`, `hedging`, `formality`) that shifts at most 0.05/day.

### Confidence dynamics

- New item starts at `0.5`
- Reinforced: `+0.10`, capped at `0.98`
- Not reinforced today: `×0.99` (half-life ≈ 69 days)
- Below `0.20`: archived to `data/archive/`, not silently deleted

`data/wisdom.json` is a plain JSON file. Hand-editing it is supported and expected.

---

## The probe suite

The probe runner (`scripts/probe.py`) measures behavioral drift across five categories:

| Category | What it probes |
|---|---|
| `recall` | Does the model accurately recall project names, stacks, and facts? |
| `opinion` | Are expressed opinions stable or drifting? |
| `style` | Does output style match stated preferences? |
| `self_reflection` | Does the model's self-model match observed behavior? |
| `reasoning` | Does reasoning structure hold across sessions? |

Each probe runs twice on the same day: once with no wisdom loaded (baseline) and once with the current wisdom as the system prompt (treatment). Diff the pair to get the marginal effect of memory on that date. Track `treatment_response` across weeks to see behavioral evolution.

---

## File layout

```
gradual-wisdom/
├── SKILL.md                    — Claude-facing skill description
├── README.md                   — this file
├── config.json                 — tunables (model, decay rate, confidence thresholds)
├── prompts/
│   ├── consolidate.md          — per-session extraction prompt
│   ├── merge.md                — legacy merge prompt (merge is programmatic now)
│   ├── match.md                — semantic matching of new vs stored observations
│   ├── dedupe.md               — duplicate detection for dedupe.py
│   └── probes.json             — versioned probe suite (do not edit in place)
├── scripts/
│   ├── sleep.py                — consolidation of one day (run by catchup.py)
│   ├── dedupe.py               — one-off merge of paraphrased duplicates
│   ├── wake.py                 — SessionStart hook emitter
│   ├── probe.py                — probe suite runner
│   ├── metrics.py              — longitudinal analysis reporter
│   ├── health-check.sh         — sanity check for scheduled jobs
│   ├── catchup.py              — session-start catch-up (consolidate missed days + probe)
│   ├── session_start.sh        — SessionStart hook launcher (wake + detached catch-up)
│   └── install.sh              — setup helper
└── data/                       — excluded from git (your personal data)
    ├── wisdom.json             — cumulative state, loaded at wake
    ├── daily/YYYY-MM-DD.json   — per-day consolidation audit trail
    ├── probes/YYYY-MM-DD.json  — probe results (both conditions)
    ├── metrics.jsonl           — time-series for plotting
    └── archive/                — pruned items, kept for analysis
```

---

## Configuration

All tunables are in `config.json`:

```json
{
  "model": "sonnet",
  "runtime": "auto",
  "decay_rate": 0.99,
  "prune_threshold": 0.20,
  "probe_interval_days": 3,
  "wake_token_budget": 2000,
  "wake_max_items_per_category": 8
}
```

`runtime: "auto"` uses the Anthropic SDK if `ANTHROPIC_API_KEY` is set, otherwise falls back to the `claude` CLI. For scheduled jobs, always set `ANTHROPIC_API_KEY` — see install note.

---

## Cost

- **Sleep cycle:** ~1–2 API calls per substantive session. Plus one batched matching call per day. At 4–8 sessions/day with `sonnet`, expect ~$0.10–0.30/day.
- **Probe suite** (10 probes × 2 conditions, twice weekly): ~$0.30–0.50/week.

Switch `model` to `haiku` in `config.json` for a ~10× cost reduction with acceptable quality.

---

## Privacy

All data stays on your machine. The Anthropic API is called only for consolidation and probe runs. Session transcript content is processed but not stored externally.

---

## Reset

```bash
rm data/wisdom.json data/metrics.jsonl
```

Daily files in `data/daily/` are append-only audit logs. Deleting `wisdom.json` and replaying `sleep.py` for past days reconstructs state from scratch.

---

## Versioning

- **Probe set version** is recorded in every result file. Do not edit `prompts/probes.json` once you start collecting — fork to `v0.2` and keep `v0.1` results comparable.
- **wisdom.json** carries a `version` field. Schema changes should bump it and ship a migration script.

---

## Status

Active experiment. v0.1-draft. Probe suite and consolidation logic are functional; embedding-based semantic drift metrics are stubbed and planned.

If you run this and find something interesting, open an issue.

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming conventions, what to open an issue for first, and the PR checklist.

## License

[MIT](LICENSE) © 2026 Ali Pirouzi
