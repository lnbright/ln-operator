# Daily Check (AI agent)

An **optional**, **off-by-default** daily job that runs an autonomous Claude
agent (`scripts/daily-check.sh` → `scripts/daily-check-prompt.md`) to review the
last 24 hours of node activity and send a concise summary to Telegram.

> ⚠️ This agent runs with `--dangerously-skip-permissions` and the prompt
> authorizes it to **edit code, `git commit`, and `git push origin main`**
> unattended. Read this page and `scripts/daily-check-prompt.md` before enabling
> it, and prefer a read-only LND macaroon (below).

## What it does

Each run inspects the SQLite DB and live LND state and produces an exec summary:

- **Flows / rebalances / fees (24h)** — sats forwarded and earned, rebalance
  success/failure and cost, fee broadcasts by reason, sat-flow anomalies.
- **Reconciliation** — cross-checks `rebalance_log` / `fee_updates` against LND
  payments and the engine's own math to catch silent drift.
- **Diagnosis** — depleted/overfull channels, repeated rebalance failures,
  inactive-channel timelines from LND's journal, fee asymmetries.
- **Suggestions** — config tuning and peer ideas (text only — never auto-applied).
- **Self-fix (code only)** — if it finds a genuine code bug it may edit, run
  `make test`, and commit/push; it reverts instead of pushing if tests fail.

Delivery is owned by the cron wrapper, which appends the run's cost/duration and
sends the Telegram message. A typical run is ~5–7 min and ~$1.50–2.00, capped at
`--max-budget-usd 5`.

## Requirements

- The `claude` CLI at `/usr/bin/claude` (the wrapper hardcodes this path).
- The wrapper pins a model (`--model …`) — change it in `scripts/daily-check.sh`
  if needed.
- Telegram configured (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) for delivery.

## Enabling it

It exits immediately unless explicitly opted in:

```bash
LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1
```

Give it a **read-only** macaroon so it cannot move funds even though the prompt
instructs it to stay read-only. Bake one and point the wrapper at it:

```bash
lncli bakemacaroon \
  info:read offchain:read onchain:read peers:read invoices:read \
  --save_to ~/.lnd-macaroons/ln-operator-readonly.macaroon
```

```cron
# Daily at 09:00 — opt-in flag + read-only macaroon
0 9 * * * LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 \
  DAILY_CHECK_LND_MACAROON=/home/youruser/.lnd-macaroons/ln-operator-readonly.macaroon \
  /path/to/ln-operator/scripts/daily-check.sh
```

`DAILY_CHECK_LND_MACAROON` is exported before the agent runs and overrides
`LND_MACAROON` for that process only (`config.py` loads `.env` with
`override=False`, so the export wins).

## Disabling it

Remove `LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1` from the cron line (or the whole
line). With the flag unset the script logs `disabled` and exits 0.

## Logs

`logs/daily-check.log` (rotated at 1 MB, one previous file). The agent also
writes its summary to `/tmp/daily-check-summary.txt` each run.
