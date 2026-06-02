#!/bin/bash
# Daily LN node health check — invoked from cron at 09:00 Europe/London.
# Runs claude headless against scripts/daily-check-prompt.md. The prompt
# tells claude to write its exec summary to /tmp/daily-check-summary.txt and
# print it to stdout; this wrapper parses the JSON result for cost/duration/
# tokens, appends a "Run:" line, and is the one that sends Telegram (so the
# message can include this run's cost — which the agent can't know mid-run).

set -u  # don't set -e: we want the log line even if claude exits non-zero

REPO=/home/pi/ln-operator
LOG=$REPO/logs/daily-check.log
PROMPT=$REPO/scripts/daily-check-prompt.md
CLAUDE=/usr/bin/claude
SUMMARY=/tmp/daily-check-summary.txt
JSON=/tmp/daily-check-result.json

# ─── Opt-in gate ────────────────────────────────────────────────────────────
# This launches an AUTONOMOUS Claude agent with --dangerously-skip-permissions
# that can edit code, `git commit`, and `git push origin main` unattended, using
# whatever LND macaroon is in the environment. It is OFF by default. Enable it
# only deliberately by setting LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 in the cron
# line or environment. See the Security section of the README first.
if [ "${LN_OPERATOR_ENABLE_AI_DAILY_CHECK:-0}" != "1" ]; then
  echo "daily-check: disabled. Set LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 to enable." >&2
  exit 0
fi

# Prefer a read-only macaroon for the unattended agent so it cannot move funds
# even though the prompt instructs it to stay read-only. config.py reads .env
# with override=False, so this exported value takes precedence over LND_MACAROON
# in .env. Bake one with: info:read offchain:read onchain:read peers:read
# invoices:read (see README Security section). Falls back to .env if unset.
if [ -n "${DAILY_CHECK_LND_MACAROON:-}" ]; then
  export LND_MACAROON="$DAILY_CHECK_LND_MACAROON"
fi

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

# Rotate log at 1MB — keep one previous file. Daily writes are ~5KB,
# so this caps history at roughly 400 days with one rollover.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

rm -f "$SUMMARY" "$JSON"

echo >> "$LOG"
echo "===== daily-check started $(ts) =====" >> "$LOG"

# --max-budget-usd 5 caps API spend per run (insurance against a runaway
# loop from a bad prompt change). Typical run is <$2.
# --output-format json gives us total_cost_usd / duration_ms / usage so we
# can log and report actual spend. JSON goes to $JSON; stderr to the log.
"$CLAUDE" -p "$(cat "$PROMPT")" \
  --dangerously-skip-permissions \
  --model claude-opus-4-7 \
  --max-budget-usd 5 \
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
