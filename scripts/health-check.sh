#!/usr/bin/env bash
# Verify cron-driven gradual-wisdom pipeline is healthy.
# Run weekly via cron. Output overwritten to data/health.txt each run.
set -u

DATA="/Users/ali/.claude/skills/gradual-wisdom/data"
NOW=$(date "+%Y-%m-%d %H:%M:%S %Z")
PASS=0
FAIL=0

check() {
  local name="$1" ok="$2" detail="$3"
  if [ "$ok" = "1" ]; then
    echo "PASS  $name  $detail"
    PASS=$((PASS+1))
  else
    echo "FAIL  $name  $detail"
    FAIL=$((FAIL+1))
  fi
}

echo "=== gradual-wisdom health check @ $NOW ==="
echo

# 1. sleep.log exists, recent activity, no recent tracebacks
if [ -f "$DATA/sleep.log" ]; then
  AGE_HOURS=$(( ( $(date +%s) - $(stat -f %m "$DATA/sleep.log") ) / 3600 ))
  TAIL=$(tail -50 "$DATA/sleep.log")
  TB_COUNT=$(echo "$TAIL" | grep -c "Traceback" || true)
  UPD_COUNT=$(echo "$TAIL" | grep -c "Updated /Users/ali/.claude/skills/gradual-wisdom/data/wisdom.json" || true)
  if [ "$AGE_HOURS" -le 36 ] && [ "$TB_COUNT" -eq 0 ] && [ "$UPD_COUNT" -ge 1 ]; then
    check "sleep.log healthy" 1 "age=${AGE_HOURS}h, updates=$UPD_COUNT, tracebacks=$TB_COUNT"
  else
    check "sleep.log healthy" 0 "age=${AGE_HOURS}h, updates=$UPD_COUNT, tracebacks=$TB_COUNT"
  fi
else
  check "sleep.log exists" 0 "missing — cron sleep job never ran"
fi

# 2. Recent probe file (Mon or Thu)
LATEST_PROBE=$(ls -t "$DATA/probes/"*.json 2>/dev/null | head -1)
if [ -n "$LATEST_PROBE" ]; then
  PROBE_AGE_DAYS=$(( ( $(date +%s) - $(stat -f %m "$LATEST_PROBE") ) / 86400 ))
  PROBE_SIZE=$(stat -f %z "$LATEST_PROBE")
  if [ "$PROBE_AGE_DAYS" -le 4 ] && [ "$PROBE_SIZE" -gt 5000 ]; then
    check "probe recent + sized" 1 "$(basename "$LATEST_PROBE") age=${PROBE_AGE_DAYS}d size=${PROBE_SIZE}B"
  else
    check "probe recent + sized" 0 "$(basename "$LATEST_PROBE") age=${PROBE_AGE_DAYS}d size=${PROBE_SIZE}B (need <=4d, >5KB)"
  fi
else
  check "probe file exists" 0 "no probe files"
fi

# 3. wisdom.json updated recently
if [ -f "$DATA/wisdom.json" ]; then
  W_AGE_HOURS=$(( ( $(date +%s) - $(stat -f %m "$DATA/wisdom.json") ) / 3600 ))
  if [ "$W_AGE_HOURS" -le 36 ]; then
    check "wisdom.json fresh" 1 "age=${W_AGE_HOURS}h"
  else
    check "wisdom.json fresh" 0 "age=${W_AGE_HOURS}h (need <=36h)"
  fi
else
  check "wisdom.json exists" 0 "missing"
fi

# 4. consolidation_count progressed
if [ -f "$DATA/wisdom.json" ]; then
  COUNT=$(python3 -c "import json; print(json.load(open('$DATA/wisdom.json')).get('consolidation_count', 0))" 2>/dev/null || echo "ERR")
  if [ "$COUNT" != "ERR" ] && [ "$COUNT" -ge 5 ]; then
    check "consolidation_count >=5" 1 "count=$COUNT"
  else
    check "consolidation_count >=5" 0 "count=$COUNT"
  fi
fi

# 5. Stale lock detection
if [ -f "$DATA/.sleep.lock" ]; then
  LOCK_AGE_HOURS=$(( ( $(date +%s) - $(stat -f %m "$DATA/.sleep.lock") ) / 3600 ))
  if [ "$LOCK_AGE_HOURS" -gt 2 ]; then
    check "no stale lock" 0 "lock held ${LOCK_AGE_HOURS}h — sleep.py crashed mid-run, delete $DATA/.sleep.lock"
  fi
fi

echo
echo "=== summary: PASS=$PASS FAIL=$FAIL ==="
if [ "$FAIL" -gt 0 ]; then
  echo
  echo "DIAGNOSIS:"
  echo "- If sleep.log missing: laptop asleep at 3am OR cron not loaded. Run 'crontab -l' to verify."
  echo "- If probe stale: claude CLI auth failed in cron. Run a manual probe and inspect probe.log tail."
  echo "- If wisdom.json stale: sleep.py crashed during merge. Inspect sleep.log for last error."
  echo "- If consolidation_count low: sleep ran but skipped all sessions (check daily/*.json sizes)."
  echo "- DO NOT auto-fix. Surface to user next session."
fi
