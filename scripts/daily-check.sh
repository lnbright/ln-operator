#!/bin/bash
# Daily LN node health check (deterministic) — invoked from cron each morning.
# Runs the data-integrity reconciliation checks (reconcile.run_checks) and the
# unit test suite (make test), then sends a concise pass/fail + issues summary to
# Telegram. The autonomous Claude agent that used to run here has been REMOVED:
# this is now fully read-only and deterministic — no LLM, no code edits, no
# commits, no `--dangerously-skip-permissions`. scripts/daily-check-prompt.md
# (the old agent prompt) is kept for reference only and is no longer invoked.

set -u  # don't set -e: we want the log line even if a step exits non-zero

# Repo root: override with LN_OPERATOR_REPO, else derive from this script's path.
REPO="${LN_OPERATOR_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG=$REPO/logs/daily-check.log
PY="$REPO/venv/bin/python3"

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

# Rotate log at 1MB — keep one previous file.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

echo >> "$LOG"
echo "===== daily-check started $(ts) =====" >> "$LOG"

# ─── Unit suite ─────────────────────────────────────────────────────────────
# Capture full output; derive a one-line summary + the FAIL/ERROR headers.
TEST_OUT=$(make test 2>&1)
test_rc=$?
TEST_SUMMARY=$(printf '%s\n' "$TEST_OUT" | grep -E '^(Ran |OK($| )|FAILED)' | tr '\n' ' ' | sed 's/  */ /g; s/ *$//')
FAILED_TESTS=$(printf '%s\n' "$TEST_OUT" | grep -E '^(FAIL|ERROR): ')
printf '%s\n' "$TEST_OUT" | tail -8 >> "$LOG"

# ─── Reconcile checks + Telegram delivery ───────────────────────────────────
# Runs in $REPO so reconcile / telegram_bot import. Everything printed here is
# logged; the script is otherwise silent on stdout so cron stays quiet.
"$PY" - "$test_rc" "$TEST_SUMMARY" "$FAILED_TESTS" >>"$LOG" 2>&1 <<'PY'
import datetime, sys

test_rc      = sys.argv[1]
test_summary = sys.argv[2].strip() or "no test summary captured"
failed_tests = sys.argv[3]

today = datetime.date.today().isoformat()
lines = [f"⚡ *Daily Check — {today}*", ""]

# Tests
if test_rc == "0":
    lines.append(f"🧪 *Tests:* ✅ {test_summary}")
else:
    lines.append(f"🧪 *Tests:* ❌ {test_summary}")
    for t in failed_tests.splitlines():
        t = t.strip()
        if t:
            lines.append(f"  • {t}")

# Data-integrity reconciliation (the deterministic consistency checks)
try:
    from reconcile import run_checks
    issues = run_checks(window_days=1)
    if not issues:
        lines.append("🔎 *Data integrity:* ✅ clean")
    else:
        n_fail = sum(1 for i in issues if i["severity"] == "fail")
        n_warn = len(issues) - n_fail
        lines.append(f"🔎 *Data integrity:* ⚠️ {len(issues)} issue(s) ({n_fail} fail, {n_warn} warn)")
        for i in issues:
            lines.append(f"  • [{i['severity']}] {i['check']}: {i['message']}")
except Exception as e:
    lines.append(f"🔎 *Data integrity:* ⚠️ checks failed to run: {e}")

full = "\n".join(lines)
print(full)

try:
    import telegram_bot
    ok = telegram_bot.send_message(full)
    print("[telegram] sent" if ok else "[telegram] FAILED")
except Exception as e:
    print(f"[telegram] FAILED: {e}")
PY

echo "===== daily-check finished $(ts) (test_rc=$test_rc) =====" >> "$LOG"
