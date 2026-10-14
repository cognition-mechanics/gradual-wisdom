#!/usr/bin/env python3
"""
dedupe.py — One-time (or occasional) cleanup that merges paraphrased duplicates
already stored in wisdom.json.

Before semantic matching existed, every rephrasing of the same observation became
a new item, so most items were never reinforced. This script finds duplicates
with the same two-stage approach the nightly merge now uses (cheap lexical
candidates, then batched LLM adjudication) and merges each cluster into one item
that carries the combined evidence.

Usage:
    python3 dedupe.py             # dry run: report what would merge, write nothing
    python3 dedupe.py --apply --from data/archive/dedupe-proposal-<date>.json   # apply the reviewed proposal
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sleep  # noqa: E402

DEDUPE_PROMPT_PATH = sleep.PROMPTS_DIR / "dedupe.md"
BATCH_SIZE = 40


def cluster(ids: list[str], pairs: list[tuple[str, str]]) -> list[set[str]]:
    """Union-find over duplicate pairs; ids not in `ids` are ignored."""
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for i in ids:
        groups.setdefault(find(i), set()).add(i)
    return list(groups.values())


def merge_cluster(items: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicates into the highest-confidence item, combining their evidence."""
    if len(items) == 1:
        return items[0]
    ranked = sorted(
        items,
        key=lambda it: (it.get("confidence", 0), it.get("reinforcement_count", 1), it.get("last_reinforced", "")),
        reverse=True,
    )
    rep = dict(ranked[0])
    extra = len(items) - 1
    rep["confidence"] = min(rep.get("confidence", 0) + config["reinforcement_boost"] * extra, config["max_confidence"])
    rep["reinforcement_count"] = sum(it.get("reinforcement_count", 1) for it in items)
    rep["first_seen"] = min(it.get("first_seen", "9999") for it in items)
    rep["last_reinforced"] = max(it.get("last_reinforced", "") for it in items)
    rep["merged_ids"] = sorted(it["id"] for it in ranked[1:])
    return rep


def dedupe_category(
    items: list[dict[str, Any]],
    pairs: list[tuple[str, str]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {it["id"]: it for it in items}
    return [merge_cluster([by_id[i] for i in group], config) for group in cluster(list(by_id), pairs)]


def find_duplicate_pairs(client: Any, category: str, items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Lexical candidates per item, then batched adjudication. Bad output degrades to no pairs."""
    requests: list[dict[str, Any]] = []
    for it in items:
        others = [o for o in items if o["id"] != it["id"]]
        cands = sleep._find_candidates(sleep._match_text(it, category), others, category)
        if cands:
            requests.append({
                "key": it["id"],
                "category": category,
                "observation": sleep._match_text(it, category),
                "candidates": [{"id": c["id"], "text": sleep._match_text(c, category)} for c in cands],
            })
    if not requests:
        return []
    system = DEDUPE_PROMPT_PATH.read_text()
    pairs: set[tuple[str, str]] = set()
    for start in range(0, len(requests), BATCH_SIZE):
        batch = requests[start:start + BATCH_SIZE]
        allowed = {r["key"]: {c["id"] for c in r["candidates"]} for r in batch}
        try:
            text = client.call(system=system, user_msg=json.dumps(batch, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"WARN: dedupe batch failed for {category} ({e}); skipping batch\n")
            continue
        parsed = sleep.parse_json_strict(text, label=f"dedupe-{category}") if isinstance(text, str) else None
        raw = parsed.get("duplicates") if isinstance(parsed, dict) else None
        if not isinstance(raw, dict):
            continue
        for key, dup_ids in raw.items():
            if not isinstance(dup_ids, list):
                continue
            for d in dup_ids:
                if isinstance(d, str) and d in allowed.get(key, set()):
                    pairs.add(tuple(sorted((key, d))))
    return sorted(pairs)


def save_proposal(path: Path, pairs_by_category: dict[str, list[tuple[str, str]]]) -> None:
    path.write_text(json.dumps({c: [list(p) for p in ps] for c, ps in pairs_by_category.items()}, indent=2))


def load_proposal(path: Path) -> dict[str, list[tuple[str, str]]]:
    return {c: [tuple(p) for p in ps] for c, ps in json.loads(path.read_text()).items()}


def describe_clusters(category: str, items: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> list[str]:
    """Human-readable listing of each multi-item cluster, for review before --apply."""
    by_id = {it["id"]: it for it in items}
    lines: list[str] = []
    for group in cluster(list(by_id), pairs):
        if len(group) < 2:
            continue
        lines.append(f"[{category}]")
        for i in sorted(group):
            lines.append(f"   {i}: {sleep._match_text(by_id[i], category)[:160]}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the result (default is a dry run)")
    ap.add_argument("--category", help="limit to one category")
    ap.add_argument("--from", dest="from_file", help="apply a saved proposal instead of re-adjudicating")
    args = ap.parse_args()

    config = sleep.load_config()
    wisdom_path = sleep.DATA_DIR / "wisdom.json"
    wisdom = json.loads(wisdom_path.read_text())
    proposal = load_proposal(Path(args.from_file)) if args.from_file else None
    client = None if proposal is not None else sleep.get_client(config)
    found: dict[str, list[tuple[str, str]]] = {}
    review: list[str] = []

    report: dict[str, Any] = {}
    new_items: dict[str, list[dict[str, Any]]] = {}
    for category, items in wisdom.get("items", {}).items():
        if args.category and category != args.category:
            new_items[category] = items
            continue
        pairs = proposal.get(category, []) if proposal is not None else find_duplicate_pairs(client, category, items)
        found[category] = pairs
        review.extend(describe_clusters(category, items, pairs))
        merged = dedupe_category(items, pairs, config)
        new_items[category] = merged
        clusters = [m for m in merged if m.get("merged_ids")]
        report[category] = {"before": len(items), "after": len(merged), "clusters": len(clusters)}
        print(f"{category:20} {len(items):4} -> {len(merged):4}  ({len(clusters)} clusters)")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive = sleep.DATA_DIR / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    if not args.apply:
        prop_path = archive / f"dedupe-proposal-{today}.json"
        save_proposal(prop_path, found)
        (archive / f"dedupe-proposal-{today}.txt").write_text("\n".join(review) + "\n")
        print(f"\nDry run: nothing written to wisdom.json. Review {prop_path.with_suffix('.txt')},")
        print(f"then apply exactly that proposal with: python3 scripts/dedupe.py --apply --from {prop_path}")
        return 0

    backup = archive / f"wisdom.before-dedupe-{today}.json"
    backup.write_text(wisdom_path.read_text())
    wisdom["items"] = new_items
    tmp = wisdom_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wisdom, indent=2, ensure_ascii=False))
    tmp.replace(wisdom_path)
    (archive / f"dedupe-report-{today}.json").write_text(json.dumps(report, indent=2))
    print(f"\nWrote {wisdom_path} (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
