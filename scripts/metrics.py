#!/usr/bin/env python3
"""
metrics.py — Longitudinal metrics from probes and daily files.

Computes time-series of:
  - Wisdom growth (item counts per category)
  - Personality vector trajectory
  - Probe response style metrics: length, hedge frequency, list ratio
  - Memory accuracy (probe response vs wisdom ground truth)
  - Treatment-vs-baseline divergence per probe

This is intentionally analysis-without-API: we compute on local data only.
Embedding-based metrics (opinion stability, semantic drift) require an
embeddings provider — those are stubs marked TODO for a v0.2 follow-up.

Usage:
    python3 metrics.py              # markdown report
    python3 metrics.py --json       # JSON for downstream analysis
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
PROBES_DIR = DATA_DIR / "probes"
WISDOM_PATH = DATA_DIR / "wisdom.json"
METRICS_PATH = DATA_DIR / "metrics.jsonl"

HEDGE_WORDS = {
    "perhaps", "maybe", "might", "could", "somewhat", "fairly",
    "relatively", "generally", "usually", "often", "sometimes",
    "i think", "i believe", "in my view", "arguably", "potentially",
}


# ---------------------------------------------------------------------------
# Style metric extraction
# ---------------------------------------------------------------------------

def text_metrics(text: str) -> dict[str, float]:
    if not text:
        return {"chars": 0, "words": 0, "sentences": 0, "avg_sentence_len": 0,
                "hedge_rate": 0, "list_ratio": 0, "unique_word_ratio": 0}
    words = re.findall(r"\b\w+\b", text.lower())
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    list_lines = sum(1 for ln in text.split("\n") if re.match(r"^\s*([-*+]|\d+[.)])\s+", ln))
    total_lines = len([ln for ln in text.split("\n") if ln.strip()])
    hedges = sum(1 for w in words if w in HEDGE_WORDS) + sum(
        text.lower().count(p) for p in HEDGE_WORDS if " " in p
    )
    return {
        "chars": len(text),
        "words": len(words),
        "sentences": len(sentences),
        "avg_sentence_len": len(words) / len(sentences) if sentences else 0,
        "hedge_rate": hedges / len(words) if words else 0,
        "list_ratio": list_lines / total_lines if total_lines else 0,
        "unique_word_ratio": len(set(words)) / len(words) if words else 0,
    }


# ---------------------------------------------------------------------------
# Memory accuracy — coarse keyword overlap
# ---------------------------------------------------------------------------

def keyword_overlap(response: str, ground_truth_phrases: list[str]) -> float:
    """Fraction of ground-truth phrases that appear (case-insensitive substring) in the response."""
    if not ground_truth_phrases:
        return 0.0
    response_lower = response.lower()
    hits = sum(1 for p in ground_truth_phrases if p.lower() in response_lower)
    return hits / len(ground_truth_phrases)


def extract_ground_truth(wisdom: dict[str, Any], category_keys: list[str]) -> list[str]:
    items = wisdom.get("items", {})
    phrases: list[str] = []
    for key in category_keys:
        for it in items.get(key, []):
            content = it.get("content", "")
            # use noun phrases / proper nouns as the matchable unit (best-effort)
            phrases.append(content.strip())
    return phrases


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def load_all_probes() -> list[dict[str, Any]]:
    if not PROBES_DIR.exists():
        return []
    files = sorted(PROBES_DIR.glob("*.json"))
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            with open(f) as fh:
                out.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_metrics_jsonl() -> list[dict[str, Any]]:
    if not METRICS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(METRICS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_wisdom_snapshot() -> dict[str, Any] | None:
    if not WISDOM_PATH.exists():
        return None
    with open(WISDOM_PATH) as f:
        return json.load(f)


def compute_report() -> dict[str, Any]:
    wisdom = load_wisdom_snapshot()
    metrics_rows = load_metrics_jsonl()
    probe_files = load_all_probes()

    report: dict[str, Any] = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "wisdom_snapshot": None,
        "growth_trajectory": [],
        "personality_trajectory": [],
        "probe_trajectory": defaultdict(lambda: {"baseline": [], "treatment": []}),
        "memory_accuracy": [],
    }

    if wisdom:
        report["wisdom_snapshot"] = {
            "first_seen": wisdom.get("first_seen"),
            "last_consolidation": wisdom.get("last_consolidation"),
            "consolidation_count": wisdom.get("consolidation_count"),
            "item_counts": {k: len(v) for k, v in wisdom.get("items", {}).items()},
            "personality_vector": wisdom.get("personality_vector", {}),
        }

    # Growth + personality trajectory from metrics.jsonl
    for row in metrics_rows:
        if row.get("event") != "sleep":
            continue
        report["growth_trajectory"].append({
            "date": row.get("date"),
            "consolidation_count": row.get("consolidation_count"),
            "item_counts": row.get("wisdom_item_counts", {}),
        })
        if "personality_vector" in row:
            report["personality_trajectory"].append({
                "date": row.get("date"),
                **row["personality_vector"],
            })

    # Probe-level style metrics + treatment/baseline divergence
    recall_categories = {
        "recall.user-projects": ["user_facts"],
        "recall.user-style": ["user_preferences", "user_style"],
    }

    for pf in probe_files:
        date = pf["date"]
        for r in pf.get("results", []):
            pid = r["probe_id"]
            for cond in ("baseline_response", "treatment_response"):
                if cond not in r:
                    continue
                m = text_metrics(r[cond])
                m["date"] = date
                report["probe_trajectory"][pid][cond.replace("_response", "")].append(m)

            # Memory accuracy: only meaningful when we have wisdom
            if pid in recall_categories and wisdom and "treatment_response" in r:
                gt = extract_ground_truth(wisdom, recall_categories[pid])
                acc = keyword_overlap(r["treatment_response"], gt)
                baseline_acc = (
                    keyword_overlap(r["baseline_response"], gt)
                    if "baseline_response" in r else None
                )
                report["memory_accuracy"].append({
                    "date": date,
                    "probe_id": pid,
                    "treatment_accuracy": acc,
                    "baseline_accuracy": baseline_acc,
                    "ground_truth_size": len(gt),
                })

    # convert defaultdict
    report["probe_trajectory"] = dict(report["probe_trajectory"])
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Gradual Wisdom — Longitudinal Report", ""]
    snap = report.get("wisdom_snapshot")
    if snap:
        lines += [
            "## Snapshot",
            f"- First seen: `{snap['first_seen']}`",
            f"- Last consolidation: `{snap['last_consolidation']}`",
            f"- Consolidations: **{snap['consolidation_count']}**",
            "",
            "### Item counts",
        ]
        for k, v in snap["item_counts"].items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")
        lines.append("### Personality vector")
        for k, v in snap["personality_vector"].items():
            lines.append(f"- `{k}`: {v:.2f}")
        lines.append("")
    else:
        lines += ["## Snapshot", "_(no wisdom yet — first sleep cycle hasn't run)_", ""]

    growth = report["growth_trajectory"]
    if growth:
        lines += ["## Growth trajectory (last 10 days)", ""]
        for row in growth[-10:]:
            counts = row.get("item_counts", {})
            total = sum(counts.values())
            lines.append(f"- `{row['date']}`: {total} total items (consolidation #{row['consolidation_count']})")
        lines.append("")

    pt = report["personality_trajectory"]
    if len(pt) >= 2:
        lines += ["## Personality drift", ""]
        first, last = pt[0], pt[-1]
        for axis in ("directness", "technical_depth", "humor", "verbosity", "hedging", "formality"):
            if axis in first and axis in last:
                d = last[axis] - first[axis]
                arrow = "->" if abs(d) > 0.01 else "  "
                lines.append(f"- `{axis}`: {first[axis]:.2f} {arrow} {last[axis]:.2f}  (Δ {d:+.2f})")
        lines.append("")

    pt_probes = report["probe_trajectory"]
    if pt_probes:
        lines += ["## Probe style metrics (latest sample, treatment condition)", ""]
        for pid, conds in sorted(pt_probes.items()):
            t = conds.get("treatment", [])
            if not t:
                continue
            latest = t[-1]
            lines.append(
                f"- `{pid}`: {latest['words']}w, "
                f"avg_sent={latest['avg_sentence_len']:.1f}, "
                f"hedge={latest['hedge_rate']:.3f}, "
                f"list={latest['list_ratio']:.2f}"
            )
        lines.append("")

    ma = report["memory_accuracy"]
    if ma:
        lines += ["## Memory accuracy (recall probes)", ""]
        # average per probe over time
        by_probe: dict[str, list[float]] = defaultdict(list)
        for row in ma:
            if row.get("treatment_accuracy") is not None:
                by_probe[row["probe_id"]].append(row["treatment_accuracy"])
        for pid, vals in by_probe.items():
            if vals:
                lines.append(f"- `{pid}`: latest={vals[-1]:.2f}, mean={mean(vals):.2f}, n={len(vals)}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = ap.parse_args()

    report = compute_report()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
