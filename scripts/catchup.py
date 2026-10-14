#!/usr/bin/env python3
"""
catchup.py — Session-start consolidation catch-up.

Replaces the nightly sleep cron. On session start, a detached instance of this
script checks which recent UTC days have not been consolidated yet (using the
presence of data/daily/<date>.json as the done-marker) and runs sleep.py for
each missing day, oldest first, sequentially with spacing.

Design constraints (learned the hard way):
  - NON-BLOCKING: launched detached by the SessionStart hook; the session never
    waits on it.
  - RE-ENTRANCY SAFE: sleep.py spawns `claude -p`, which may itself fire the
    SessionStart hook. The launcher sets GRADUAL_WISDOM_CATCHUP=1 and skips
    spawning catch-up when it is already set; this script also sets it for its
    own children. The sleep.py file-lock is a second guard.
  - BOUNDED: never processes more than MAX_LOOKBACK_DAYS at once (a laptop off
    for weeks must not trigger a rate-limit burst); older gaps are logged and
    skipped.
  - IDEMPOTENT: a day whose daily/<date>.json already exists is skipped.

Usage:
    python3 catchup.py              # run catch-up (used by the hook, detached)
    python3 catchup.py --dry-run    # print the days it WOULD process, do nothing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
PROBE_DIR = DATA_DIR / "probes"
CONFIG_PATH = SKILL_ROOT / "config.json"
SLEEP_SCRIPT = SKILL_ROOT / "scripts" / "sleep.py"
PROBE_SCRIPT = SKILL_ROOT / "scripts" / "probe.py"

# Never process more than this many days in a single catch-up run.
MAX_LOOKBACK_DAYS = 14
# Seconds between per-day sleep.py runs, to avoid rate-limit bursts.
INTER_DAY_PAUSE = 15
# Fallback probe cadence if config is unreadable.
DEFAULT_PROBE_INTERVAL_DAYS = 3


def _today_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stdout.write(f"[{stamp}] {msg}\n")
    sys.stdout.flush()


def missing_days(today: datetime) -> tuple[list[str], list[str]]:
    """Return (to_process, skipped_too_old).

    Considers the window [today - MAX_LOOKBACK_DAYS, yesterday]. A day is a
    catch-up target if its daily/<date>.json is absent. Days older than the
    window that are also missing are reported separately (skipped).
    """
    yesterday = today - timedelta(days=1)
    window_start = today - timedelta(days=MAX_LOOKBACK_DAYS)

    to_process: list[str] = []
    for offset in range(MAX_LOOKBACK_DAYS, 0, -1):  # oldest -> newest
        day = today - timedelta(days=offset)
        if day > yesterday:
            continue
        ds = day.strftime("%Y-%m-%d")
        if not (DAILY_DIR / f"{ds}.json").exists():
            to_process.append(ds)

    # Report (do not process) still-missing days just older than the window,
    # so a long outage is visible in the log rather than silently truncated.
    skipped_too_old: list[str] = []
    for offset in range(MAX_LOOKBACK_DAYS + 1, MAX_LOOKBACK_DAYS + 8):
        day = today - timedelta(days=offset)
        ds = day.strftime("%Y-%m-%d")
        if not (DAILY_DIR / f"{ds}.json").exists():
            skipped_too_old.append(ds)
    _ = window_start  # documented window bound; kept for clarity
    return to_process, skipped_too_old


def run_day(ds: str) -> int:
    proc = subprocess.run(
        [sys.executable, str(SLEEP_SCRIPT), "--date", ds],
        cwd=str(SKILL_ROOT),
    )
    return proc.returncode


def _probe_interval() -> int:
    try:
        import json
        with open(CONFIG_PATH) as f:
            return int(json.load(f).get("probe_interval_days", DEFAULT_PROBE_INTERVAL_DAYS))
    except Exception:
        return DEFAULT_PROBE_INTERVAL_DAYS


def probe_due(today: datetime) -> bool:
    """True if no probe has run within the configured interval."""
    if not PROBE_DIR.exists():
        return True
    dates = []
    for p in PROBE_DIR.glob("*.json"):
        try:
            dates.append(datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    if not dates:
        return True
    return (today - max(dates)).days >= _probe_interval()


def run_probe() -> int:
    proc = subprocess.run([sys.executable, str(PROBE_SCRIPT)], cwd=str(SKILL_ROOT))
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the days that would be processed; do nothing.")
    args = ap.parse_args()

    today = _today_utc()
    to_process, skipped_too_old = missing_days(today)

    if skipped_too_old:
        _log(f"WARNING: {len(skipped_too_old)} day(s) older than the "
             f"{MAX_LOOKBACK_DAYS}-day window are un-consolidated and will be "
             f"skipped: {', '.join(skipped_too_old)}")

    if args.dry_run:
        if to_process:
            _log(f"catch-up DRY-RUN: would process {len(to_process)} day(s): "
                 f"{', '.join(to_process)}")
        else:
            _log("catch-up DRY-RUN: no days to process.")
        _log(f"catch-up DRY-RUN: probe due = {probe_due(today)}")
        return 0

    failures: list[str] = []
    if to_process:
        _log(f"catch-up: {len(to_process)} day(s) to process: {', '.join(to_process)}")
        for i, ds in enumerate(to_process):
            if i:
                time.sleep(INTER_DAY_PAUSE)
            _log(f"catch-up: consolidating {ds} ...")
            rc = run_day(ds)
            _log(f"catch-up: {ds} finished rc={rc}")
            if rc != 0:
                failures.append(f"{ds}(rc={rc})")
        _log("catch-up: consolidation done"
             + (f" with failures: {', '.join(failures)}" if failures else "."))
    else:
        _log("catch-up: no days to consolidate (all recent days done).")

    # Probe cadence (replaces the twice-weekly probe cron).
    if probe_due(today):
        _log("catch-up: probe due, running probe suite ...")
        prc = run_probe()
        _log(f"catch-up: probe finished rc={prc}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
