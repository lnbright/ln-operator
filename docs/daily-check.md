# Daily Check

A daily cron job (`scripts/daily-check.sh`, 09:00) that **always runs and sends a
concise summary to Telegram**. It has two modes, switched by one flag:

- **Deterministic (default)** — read-only, no LLM, no spend. Runs the data-integrity
  reconciliation (`reconcile.run_checks`) and the unit suite (`make test`) and
  Telegrams a pass/fail summary with any issues inline.
- **AI agent (opt-in, `LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1`)** — an autonomous Claude
  agent (`scripts/daily-check.sh` → `scripts/daily-check-prompt.md`) reviews the last
  24 hours of node activity, can self-fix code bugs, and sends a richer exec summary.

> ⚠️ The AI agent runs with `--dangerously-skip-permissions` and the prompt
> authorizes it to **edit code, `git commit`, and `git push origin main`**
> unattended. Read this page and `scripts/daily-check-prompt.md` before enabling
> it, and prefer a read-only LND macaroon (below).

## Deterministic mode (default)

No LLM, no setup. Each run executes `make test` and
`reconcile.run_checks(window_days=1)`, then Telegrams:

- 🧪 **Tests** — pass/fail, with the failing test names listed when red.
- 🔎 **Data integrity** — `clean`, or each reconciliation issue inline
  (`[fail]`/`[warn]` + message).

Fully read-only: no code edits, no commits, no API spend.

## AI agent mode (opt-in)

Each run inspects the SQLite DB and live LND state and produces an exec summary:

- **Flows / rebalances / fees (24h)** — sats forwarded and earned, rebalance
  success/failure and cost, fee broadcasts by reason, sat-flow anomalies.
- **Reconciliation** — the deterministic data-integrity checks run in Python
  (`reconcile.run_checks`), not by hand: missing payment_hash, fee over the max
  budget, fee over the row's recorded budget (×1.1), duplicate payment_hash,
  chunk-fee spikes. These are the **runtime-only** failure modes (LND ignoring a
  fee_limit, double-logged payments, routing spikes) a unit test can't reproduce.
  The agent reports the issues (an LLM gets SQLite arithmetic subtly wrong) and does
  deeper analysis ONLY when a check fails — clean run, one line. Pure-logic invariants
  (budget ≤ max, a pinned channel broadcasting its pin) are NOT checked here — they're
  covered by engine unit tests, so a runtime re-assertion adds nothing. The
  fee-hysteresis rule stays unverified (engine state the tables don't hold), and two
  LND-dependent checks (self-payment↔log matching, live `/v1/fees`) were dropped as
  redundant with `sync_rebalances` and the 2h pipeline self-heal. See `reconcile.py`.
- **Diagnosis** — depleted/overfull channels, repeated rebalance failures,
  inactive-channel timelines from LND's journal, fee asymmetries.
- **Suggestions** — config tuning and peer ideas (text only — never auto-applied).
  Capital suggestions name concrete peers via the peer-finder
  (`suggest_peers_for`, see [graph-cache.md](graph-cache.md)) instead of
  hand-waving "add a source"; an empty result is read as resize/close.
- **Self-fix (code only)** — if it finds a genuine code bug it may edit, run
  `make test`, and commit/push; it reverts instead of pushing if tests fail.

The report is **de-duplicated across days** by a deterministic store
(`db.reconcile_findings`, `daily_findings` table): a finding is reported once, then
re-surfaces only when its state *materially* changes, and is noted once when it
resolves — so the agent stops repeating the same stranded/concentration lines every
morning. Requires a fresh graph cache for the peer suggestions — the nightly
`refresh_graph` cron (see the project README) runs ahead of this job.

Delivery is owned by the cron wrapper, which appends the run's cost/duration and
sends the Telegram message. A typical run is ~5–7 min and ~$1.50–2.00, capped by
`--max-budget-usd` (default $5; see Customization).

## Requirements

- Telegram configured (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) for delivery —
  needed in both modes.
- **AI agent mode only:** the `claude` CLI on `PATH` (override the binary with
  `CLAUDE_BIN`). The deterministic mode needs no LLM.

## Customization

Everything is set by environment variable — no need to edit the script. The
wrapper reads each with a default, so set only what you want to change (in the
cron line or environment):

| Env var | Default | What it controls |
|---|---|---|
| `LN_OPERATOR_ENABLE_AI_DAILY_CHECK` | `0` (off) | Mode switch. `1` runs the AI agent; anything else runs the deterministic checks. The daily run + Telegram happen either way. |
| `DAILY_CHECK_MODEL` | `claude-opus-4-8` | Which model runs the agent. Set to any current model id (e.g. a newer Opus) to trade cost/speed for capability. |
| `DAILY_CHECK_MAX_BUDGET_USD` | `5` | Hard cap on API spend **per run**, in USD. Insurance against a runaway loop; a normal run is <$2. Lower it to be thrifty, raise it if you expand the prompt. |
| `DAILY_CHECK_LND_MACAROON` | (unset → falls back to `.env` `LND_MACAROON`) | Path to the macaroon the agent uses. **Point this at a read-only macaroon** so it can't move funds (see below). |
| `CLAUDE_BIN` | `claude` (resolved on `PATH`) | Path/name of the Claude CLI binary. |
| `LN_OPERATOR_REPO` | derived from the script's location | Repo root (cwd for the run). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (from `.env`) | Telegram delivery target. Unset → no message sent (file + stdout still written). |

The agent's **behaviour** (what it inspects, reconciles, fixes, and how the
summary is formatted) is the prompt itself — edit
`scripts/daily-check-prompt.md` to change scope. The model/budget actually used
are echoed into `logs/daily-check.log` at the top of each run.

> The wrapper still passes `--dangerously-skip-permissions`, so the agent can
> edit code and `git push` unattended. The macaroon governs **fund** access, not
> code access — keep it read-only and review the prompt before enabling.

## Enabling the AI agent

The deterministic check runs daily with no setup. To switch the daily run to the
**AI agent** instead, opt in:

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
# Daily at 09:00 — AI agent enabled (flag=1) + read-only macaroon
0 9 * * * LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 \
  DAILY_CHECK_LND_MACAROON=/home/youruser/.lnd-macaroons/ln-operator-readonly.macaroon \
  /path/to/ln-operator/scripts/daily-check.sh
```

Same line with a cheaper $3 cap and a specific model (any knob from the table
above can be prepended the same way):

```cron
0 9 * * * LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 \
  DAILY_CHECK_LND_MACAROON=/home/youruser/.lnd-macaroons/ln-operator-readonly.macaroon \
  DAILY_CHECK_MAX_BUDGET_USD=3 DAILY_CHECK_MODEL=claude-opus-4-7 \
  /path/to/ln-operator/scripts/daily-check.sh
```

`DAILY_CHECK_LND_MACAROON` is exported before the agent runs and overrides
`LND_MACAROON` for that process only (`config.py` loads `.env` with
`override=False`, so the export wins).

## Disabling the AI agent

Set `LN_OPERATOR_ENABLE_AI_DAILY_CHECK=0` (or remove it). The daily run falls back
to the **deterministic** checks — it still runs and still Telegrams, just without the
LLM. To stop the daily check entirely, remove the whole cron line.

## Logs

`logs/daily-check.log` (rotated at 1 MB, one previous file). The agent also
writes its summary to `/tmp/daily-check-summary.txt` each run.
