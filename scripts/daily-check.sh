#!/bin/bash
# Daily LN node health check — invoked from cron at 09:00 Europe/London.
# Runs claude headless against scripts/daily-check-prompt.md. The prompt
# tells claude to deliver an exec summary via Telegram and print to stdout;
# this wrapper just routes stdout to a rotating log file.

set -u  # don't set -e: we want the log line even if claude exits non-zero

REPO=/home/pi/ln-operator
LOG=$REPO/logs/daily-check.log
PROMPT=$REPO/scripts/daily-check-prompt.md
CLAUDE=/usr/bin/claude

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

# Rotate log at 1MB — keep one previous file. Daily writes are ~5KB,
# so this caps history at roughly 400 days with one rollover.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

{
  echo
  echo "===== daily-check started $(ts) ====="
  # --max-budget-usd 5 caps API spend per run (insurance against a
  # runaway loop from a bad prompt change). Typical run is <$2.
  "$CLAUDE" -p "$(cat "$PROMPT")" \
    --dangerously-skip-permissions \
    --model claude-opus-4-7 \
    --max-budget-usd 5 \
    2>&1
  rc=$?
  echo "===== daily-check finished $(ts) (exit=$rc) ====="
} >> "$LOG"
