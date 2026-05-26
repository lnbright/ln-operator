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

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

{
  echo
  echo "===== daily-check started $(ts) ====="
  "$CLAUDE" -p "$(cat "$PROMPT")" \
    --dangerously-skip-permissions \
    --model claude-opus-4-7 \
    2>&1
  rc=$?
  echo "===== daily-check finished $(ts) (exit=$rc) ====="
} >> "$LOG"
