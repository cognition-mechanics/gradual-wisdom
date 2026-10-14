#!/bin/sh
# SessionStart launcher for gradual-wisdom.
#
# 1. Runs wake.py so its wisdom block is injected into the new session
#    (wake.py's stdout becomes the hook's stdout -> session context).
# 2. Launches catch-up consolidation DETACHED so the session never waits on it.
#
# Re-entrancy: sleep.py (invoked by catch-up) spawns `claude -p`, which can
# itself fire this SessionStart hook. GRADUAL_WISDOM_CATCHUP guards against
# recursively spawning catch-up in those child invocations.

DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

# (1) Inject wisdom — stdout flows to the session context.
python3 "$DIR/scripts/wake.py"

# (2) Spawn catch-up only in top-level (non-re-entrant) sessions.
if [ -z "${GRADUAL_WISDOM_CATCHUP:-}" ]; then
  GRADUAL_WISDOM_CATCHUP=1 nohup python3 "$DIR/scripts/catchup.py" \
    >> "$DIR/data/catchup.log" 2>&1 &
fi

exit 0
