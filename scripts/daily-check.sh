#!/bin/bash
# Daily LN node health check — invoked from cron (e.g. once each morning).
#
# Runs every day and always sends a Telegram summary. It has two modes:
#
#   • AGENT OFF (default) — runs only the DETERMINISTIC checks: the data-integrity
#     reconciliation (reconcile.run_checks) + the unit suite (make test), and
#     Telegrams a pass/fail + issues summary. Fully read-only: no LLM, no spend.
#
#   • AGENT ON (LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1) — runs the autonomous Claude
#     agent against scripts/daily-check-prompt.md, which writes its exec summary to
#     /tmp/daily-check-summary.txt; this wrapper parses the JSON result for
#     cost/duration/tokens, appends a "Run:" line, and Telegrams it.
#
# Flip LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 in the cron line to enable the agent.

set -u  # don't set -e: we want the log line even if a step exits non-zero

# Repo root: override with LN_OPERATOR_REPO, else derive from this script's path.
REPO="${LN_OPERATOR_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG=$REPO/logs/daily-check.log
PROMPT=$REPO/scripts/daily-check-prompt.md
CLAUDE="${CLAUDE_BIN:-claude}"  # resolved on PATH; override with CLAUDE_BIN
# Model + per-run spend cap are env-overridable (see docs/daily-check.md).
MODEL="${DAILY_CHECK_MODEL:-claude-opus-4-8}"
MAX_BUDGET_USD="${DAILY_CHECK_MAX_BUDGET_USD:-5}"
SUMMARY=/tmp/daily-check-summary.txt
JSON=/tmp/daily-check-result.json

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

# Rotate log at 1MB — keep one previous file. Daily writes are ~5KB,
# so this caps history at roughly 400 days with one rollover.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

if [ "${LN_OPERATOR_ENABLE_AI_DAILY_CHECK:-0}" != "1" ]; then
# ─── DETERMINISTIC mode (agent disabled) ────────────────────────────────────
echo >> "$LOG"
echo "===== daily-check (deterministic) started $(ts) =====" >> "$LOG"

# Unit suite — capture output, derive a one-line summary + FAIL/ERROR headers.
TEST_OUT=$(make test 2>&1)
test_rc=$?
TEST_SUMMARY=$(printf '%s\n' "$TEST_OUT" | grep -E '^(Ran |OK($| )|FAILED)' | tr '\n' ' ' | sed 's/  */ /g; s/ *$//')
FAILED_TESTS=$(printf '%s\n' "$TEST_OUT" | grep -E '^(FAIL|ERROR): ')
printf '%s\n' "$TEST_OUT" | tail -8 >> "$LOG"

# Reconcile checks + Telegram delivery. Runs in $REPO so the imports resolve.
"$REPO/venv/bin/python3" - "$test_rc" "$TEST_SUMMARY" "$FAILED_TESTS" >>"$LOG" 2>&1 <<'PY'
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

echo "===== daily-check (deterministic) finished $(ts) (test_rc=$test_rc) =====" >> "$LOG"
exit 0
fi

# ─── AGENT mode (LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1) ────────────────────────
# This launches an AUTONOMOUS Claude agent with --dangerously-skip-permissions
# that can edit code, `git commit`, and `git push origin main` unattended, using
# whatever LND macaroon is in the environment. See the Security section of the
# README first.

# Prefer a read-only macaroon for the unattended agent so it cannot move funds
# even though the prompt instructs it to stay read-only. config.py reads .env
# with override=False, so this exported value takes precedence over LND_MACAROON
# in .env. Bake one with: info:read offchain:read onchain:read peers:read
# invoices:read (see README Security section). Falls back to .env if unset.
if [ -n "${DAILY_CHECK_LND_MACAROON:-}" ]; then
  export LND_MACAROON="$DAILY_CHECK_LND_MACAROON"
fi

rm -f "$SUMMARY" "$JSON"

echo >> "$LOG"
echo "===== daily-check started $(ts) =====" >> "$LOG"
echo "model=$MODEL max-budget-usd=$MAX_BUDGET_USD" >> "$LOG"

# --max-budget-usd caps API spend per run (insurance against a runaway loop
# from a bad prompt change). Typical run is <$2; default cap $5, override with
# DAILY_CHECK_MAX_BUDGET_USD. Model defaults to claude-opus-4-8, override with
# DAILY_CHECK_MODEL.
# --output-format json gives us total_cost_usd / duration_ms / usage so we
# can log and report actual spend. JSON goes to $JSON; stderr to the log.
"$CLAUDE" -p "$(cat "$PROMPT")" \
  --dangerously-skip-permissions \
  --model "$MODEL" \
  --max-budget-usd "$MAX_BUDGET_USD" \
  --output-format json \
  >"$JSON" 2>>"$LOG"
rc=$?

# Parse cost/duration/tokens, build the "Run:" footer, send Telegram, and
# echo the delivered message into the log. Runs in $REPO so telegram_bot imports.
"$REPO/venv/bin/python3" - "$JSON" "$SUMMARY" "$rc" >>"$LOG" 2>&1 <<'PY'
import json, os, sys

jsonf, summaryf, rc = sys.argv[1], sys.argv[2], sys.argv[3]

d = {}
try:
    with open(jsonf) as f:
        d = json.load(f)
except Exception as e:
    print(f"[wrapper] could not parse JSON result: {e}")

cost   = d.get("total_cost_usd")
dur_ms = d.get("duration_ms")
usage  = d.get("usage") or {}
in_tok   = usage.get("input_tokens", 0) or 0
out_tok  = usage.get("output_tokens", 0) or 0
cache_in = usage.get("cache_read_input_tokens", 0) or 0

# Prefer the summary file the agent wrote; fall back to the JSON result text.
summary = ""
if os.path.exists(summaryf):
    summary = open(summaryf).read().strip()
if not summary:
    summary = (d.get("result") or "").strip()

def fmt_dur(ms):
    if not ms:
        return "?"
    s = int(ms / 1000)
    return f"{s // 60}m {s % 60:02d}s"

bits = [f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?", fmt_dur(dur_ms)]
tok_total = in_tok + out_tok + cache_in
if tok_total:
    bits.append(f"{round(tok_total / 1000)}k tok")
if rc != "0":
    bits.append(f"exit={rc}")
footer = "💸 *Run:* " + " · ".join(bits)

full = (summary + "\n\n" + footer).strip() if summary else footer
print(full)

try:
    import telegram_bot
    ok = telegram_bot.send_message(full)
    print("[telegram] sent" if ok else "[telegram] FAILED")
except Exception as e:
    print(f"[telegram] FAILED: {e}")
PY

echo "===== daily-check finished $(ts) (exit=$rc) =====" >> "$LOG"
