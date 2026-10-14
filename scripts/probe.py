#!/usr/bin/env python3
"""
probe.py — Run the probe suite for longitudinal measurement.

For each probe, runs in two conditions:
  - baseline:  no wisdom loaded (fresh model)
  - treatment: wisdom loaded as system context

Saves all responses to data/probes/YYYY-MM-DD.json.

The diff between conditions on the same day is the marginal effect of
accumulated wisdom. The trajectory of treatment responses across days is
the behavioral evolution of the augmented system.

Usage:
    python3 probe.py                      # run full suite, both conditions
    python3 probe.py --baseline-only      # skip treatment
    python3 probe.py --treatment-only     # skip baseline
    python3 probe.py --probe-id <id>      # run a single probe by id
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import LLMClient  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = SKILL_ROOT / "prompts"
PROBES_PATH = PROMPTS_DIR / "probes.json"
WISDOM_PATH = SKILL_ROOT / "data" / "wisdom.json"
PROBES_DIR = SKILL_ROOT / "data" / "probes"
CONFIG_PATH = SKILL_ROOT / "config.json"

# Reuse wake.py's renderer so the treatment condition matches the actual hook injection
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from wake import render_wisdom_block, load_wisdom  # noqa: E402


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_probes() -> dict[str, Any]:
    with open(PROBES_PATH) as f:
        return json.load(f)


def get_client(config: dict[str, Any]) -> LLMClient:
    return LLMClient(
        model=config["model"],
        max_tokens=config["max_tokens_per_call"],
        runtime=config.get("runtime", "auto"),
        fallbacks=config.get("model_fallbacks"),
    )


def run_probe(
    client: LLMClient,
    probe: dict[str, Any],
    system: str | None,
) -> str:
    # Empty system prompt is safe for both backends
    return client.call(system=system or "", user_msg=probe["prompt"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--treatment-only", action="store_true")
    ap.add_argument("--probe-id", help="Run a single probe by id")
    args = ap.parse_args()

    if args.baseline_only and args.treatment_only:
        sys.stderr.write("ERROR: --baseline-only and --treatment-only are mutually exclusive\n")
        return 2

    config = load_config()
    probes_data = load_probes()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    selected = probes_data["probes"]
    if args.probe_id:
        selected = [p for p in selected if p["id"] == args.probe_id]
        if not selected:
            sys.stderr.write(f"No probe with id={args.probe_id}\n")
            return 2

    # Build treatment system prompt from current wisdom
    wisdom = load_wisdom()
    if wisdom is None:
        treatment_system = None
        sys.stderr.write("WARN: no wisdom.json yet — treatment condition has no augmentation.\n")
    else:
        treatment_system = render_wisdom_block(wisdom, config["wake_token_budget"])

    client = get_client(config)
    results: list[dict[str, Any]] = []

    for probe in selected:
        record: dict[str, Any] = {
            "probe_id": probe["id"],
            "category": probe["category"],
            "prompt": probe["prompt"],
            "model": config["model"],
            "probe_set_version": probes_data["version"],
            "wisdom_consolidation_count": (wisdom or {}).get("consolidation_count", 0),
        }

        if not args.treatment_only:
            print(f"[baseline]  {probe['id']}", file=sys.stderr)
            record["baseline_response"] = run_probe(
                client=client,
                probe=probe,
                system=None,
            )
            time.sleep(1)

        if not args.baseline_only and treatment_system is not None:
            print(f"[treatment] {probe['id']}", file=sys.stderr)
            record["treatment_response"] = run_probe(
                client=client,
                probe=probe,
                system=treatment_system,
            )
            time.sleep(1)

        results.append(record)

    PROBES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROBES_DIR / f"{today}.json"
    payload = {
        "date": today,
        "probe_set_version": probes_data["version"],
        "wisdom_consolidation_count": (wisdom or {}).get("consolidation_count", 0),
        "results": results,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
