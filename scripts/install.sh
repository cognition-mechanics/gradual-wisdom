#!/usr/bin/env bash
# install.sh — One-shot setup for gradual-wisdom.
#
# What this does:
#   1. chmod +x all scripts
#   2. Verify python3 + anthropic package availability
#   3. Print the cron line and SessionStart hook config you need to add manually
#
# We deliberately do NOT modify your crontab or settings.json automatically.
# This is a brain implant; you should consent to each wire individually.

set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$SKILL_ROOT/scripts"

echo ">> Skill root: $SKILL_ROOT"

echo ">> Making scripts executable..."
chmod +x "$SCRIPTS"/*.py "$SCRIPTS"/*.sh 2>/dev/null || true

echo ">> Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found"
  exit 1
fi
python3 --version

echo ">> Checking anthropic package..."
if ! python3 -c "import anthropic" 2>/dev/null; then
  echo "WARN: anthropic package not installed."
  echo "      Install it with:  pip3 install anthropic"
fi

echo ">> Checking ANTHROPIC_API_KEY..."
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARN: ANTHROPIC_API_KEY is not set in your environment."
  echo "      The sleep cycle and probe runner need it. Set it in your shell rc:"
  echo "        export ANTHROPIC_API_KEY=sk-ant-..."
fi

cat <<EOF

==============================================================================
SETUP STEPS (run these manually — install.sh will not modify your config)
==============================================================================

1) Add the cron line to run sleep at 03:00 local time every day:

   Run:   crontab -e
   Add:

   0 3 * * * /usr/bin/env -i HOME="\$HOME" \\
             PATH="/Users/ali/.local/bin:/usr/local/bin:/usr/bin:/bin" \\
             ANTHROPIC_API_KEY="\$ANTHROPIC_API_KEY" \\
             python3 $SCRIPTS/sleep.py >> $SKILL_ROOT/data/sleep.log 2>&1

   PATH must include the directory containing 'claude' if you're using CLI runtime
   (which 'auto' picks when ANTHROPIC_API_KEY is unset). Confirm with: which claude

   ANTHROPIC_API_KEY is harmless if unset — the runtime falls back to CLI.

   IMPORTANT macOS gotchas:
   - Grant /usr/sbin/cron Full Disk Access (System Settings > Privacy & Security)
     or it cannot read ~/.claude/projects/.
   - ANTHROPIC_API_KEY must be set explicitly in the cron env (the env -i line
     above already handles this). Do NOT rely on the claude CLI + Keychain fallback
     for overnight jobs: macOS locks the Keychain when the screen sleeps, so any
     cron that runs while you are away will fail silently with rc=1 and empty stderr.
     ANTHROPIC_API_KEY in the cron line is the only reliable path.
   - Schedule jobs during working hours (e.g. 12:00) rather than overnight if
     you prefer not to keep an API key in your crontab.

2) Add the SessionStart hook to ~/.claude/settings.json so each new Claude Code
   session loads the wisdom block:

   {
     "hooks": {
       "SessionStart": [{
         "matcher": "*",
         "hooks": [{
           "type": "command",
           "command": "python3 $SCRIPTS/wake.py"
         }]
       }]
     }
   }

   (Merge with any existing "hooks" object — don't overwrite it.)

3) (Optional) Add the twice-weekly probe runs to cron, Monday and Thursday at noon:

   0 12 * * 1,4 /usr/bin/env -i HOME="\$HOME" \\
                PATH="\$PATH" \\
                ANTHROPIC_API_KEY="\$ANTHROPIC_API_KEY" \\
                python3 $SCRIPTS/probe.py >> $SKILL_ROOT/data/probe.log 2>&1

   Running at noon (not overnight) avoids the macOS Keychain lock issue.
   See the IMPORTANT note in step 1.

4) Test it before you trust it:

   python3 $SCRIPTS/sleep.py --dry-run     # extract today, no wisdom write
   python3 $SCRIPTS/wake.py --plain        # see what the hook will inject
   python3 $SCRIPTS/probe.py --baseline-only --probe-id self.working-style
   python3 $SCRIPTS/metrics.py             # report so far

==============================================================================
EOF
