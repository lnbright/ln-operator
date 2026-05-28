# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee
optimisation, liquidity rebalancing, channel planning, health monitoring,
and alerting — with a web dashboard for real-time visibility.

Built for home node operators running LND on a Raspberry Pi or similar hardware.

---

## What It Does

### Pipeline — Automated Channel Management

Runs unattended on a cron schedule. Four steps execute in sequence:

**1. Fee adjustment** — Sets each channel's fee rate based on its local/remote
balance ratio. Full channels get low fees to attract routing. Depleted channels
get high fees to protect remaining liquidity. Base fee is always zero.

**2. Rebalancing** — Moves sats from overfull channels (>80% local) to depleted
ones (<20% local) via circular self-payments through LND's Router SendPaymentV2.
Each channel has its own fee budget based on earnings history. If the full amount
can't route, it halves and retries down to 100k sats. If one source→target pair
has no route, it tries alternative pairs before giving up.

**3. Routing sync** — Pulls new forwarding events from LND into SQLite using
offset-based pagination. Also detects manual rebalances done via `lncli` and
imports them so the dashboard tracks all rebalancing activity.

**4. Health check** — Snapshots channel states, updates maturity tracking, fires
alerts for depleted channels, offline peers, and repeated rebalance failures.

### Plan — Channel Investment Planner

Reads your on-chain wallet balance from LND and calculates how many channels you
can afford after deducting anchor reserves, treasury, and on-chain fees. Shows the
top 10 candidate peers per tier (hub / mid-tier / small) using a two-stage rank:
centrality (channels + capacity) prefilters within each tier, then a live LND
graph call computes diversity (% of their peers you don't already share) and
reranks. Runs only on demand — the per-candidate graph calls are too slow for
the pipeline. Offers to generate a deposit address with QR code at the end.

No external API dependencies — everything from your own LND node.

### Dashboard — Web Interface

Single-page Flask app showing live LND data and historical SQLite data:
node status, channel health with balance bars and profit/loss, daily revenue
chart, rebalance history (auto + manual), fee updates, alerts, payments,
and invoices.

### Status — CLI Overview

Node summary with per-channel balance bars and fee rates (your fees, their fees,
their inbound fees).

---

## Project Structure

```
ln-operator/
├── main.py              CLI entry — commands, display, orchestration
├── config.py            All tuneable settings
├── engine/              Channel-management engine (package)
│   ├── fees.py             Sigmoid + hysteresis + market-mult recompute
│   ├── rebalance_planner.py  Budget, candidate selection, plan generation
│   ├── rebalance_executor.py Per-plan execution with chunked retry
│   ├── sync.py             Forwarding + manual-rebalance pull from LND
│   └── monitor.py          Channel health report + alerts
├── advisor.py           Peer ranking (tier-segmented, centrality → diversity)
├── agent.py             Claude API (optional) — web search for peer research
├── lnd_client.py        LND REST API client
├── db.py                SQLite schema, migrations, queries
├── telegram_bot.py      Telegram notifications (alerts + daily summary)
├── logging_config.py    Rotating log setup
├── backup.py            Off-site channel.backup rsync
├── requirements.txt     Python dependencies
├── dashboard/
│   └── app.py           Flask web dashboard (port 4000)
├── services/            systemd unit files (dashboard + channel-backup)
├── scripts/             Operator helpers (daily-check, etc.)
├── tests/               Unit tests (pytest / unittest)
├── .env.example         Template — copy to .env, fill in
└── ln_operator.db       SQLite database (created on first run)
```

---

## Installation

### 1. Clone

```bash
git clone https://github.com/jr21M/ln-operator.git
cd ln-operator
```

### 2. Virtual environment

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Environment file

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env       # fill in the keys you need
```

`.env.example` is the source of truth for every env key the codebase reads
(LND, Claude API, Telegram, backup destination, DB path, dashboard bind).
Only `LND_*` is strictly required — everything else is optional and falls
back to a documented default.

### 4. LND file access

```bash
sudo usermod -aG lnd YOUR_USER
sudo chmod g+r /home/lnd/tls.cert
sudo chmod g+r /home/lnd/data/chain/bitcoin/mainnet/admin.macaroon
sudo chmod g+x /home/lnd /home/lnd/data /home/lnd/data/chain \
    /home/lnd/data/chain/bitcoin /home/lnd/data/chain/bitcoin/mainnet
```

### 5. Initialise

```bash
venv/bin/python3 db.py
venv/bin/python3 main.py sync_routing
venv/bin/python3 main.py status
```

### 6. Optional: QR codes in plan command

```bash
sudo apt install qrencode
```

---

## Usage

```bash
# ── AUTOMATED ──────────────────────────────────────────────
main.py pipeline                             # fees → rebalance → sync → health
main.py pipeline --dry-run                   # preview only

# ── PLANNING ───────────────────────────────────────────────
main.py plan                                 # reads wallet, shows candidates
main.py plan --min-channel 3000000           # override min channel size
main.py plan --treasury 0.01                 # override treasury ratio
main.py status                               # balance bars + fee rates
main.py history [days]                       # activity from database

# ── REBALANCING ────────────────────────────────────────────
main.py rebalance_channels                   # auto (20/80 thresholds)
main.py rebalance_channels --dry-run         # channel status + scenarios
main.py rebalance_channels --force           # target 50% on all
main.py rebalance_channels --force 0.4       # target 40% on all

# ── INDIVIDUAL STEPS ──────────────────────────────────────
main.py adjust_fees [--dry-run]
main.py sync_routing
main.py healthcheck
main.py recompute_signals                    # nightly job — refresh slow market signals

# ── MANUAL FEE PINS (override auto-fees on specific channels) ──
main.py set_fee <alias-or-chan_id> <ppm> [--note "..."]   # pin
main.py clear_fee <alias-or-chan_id>                      # remove pin
# Pins are shown by `main.py status` and in the dashboard's
# Recent Fee Updates card (📌 pin badge on the Source column).

# ── OFF-SITE BACKUP (run by systemd, not invoked manually) ──
main.py backup [--trigger path|timer|manual]   # rsync channel.backup to BACKUP_SSH_HOST
```

### Crontab

```
# Fast loop — fees, rebalances, sync, healthcheck
0 */2 * * * cd /path/to/ln-operator && venv/bin/python3 main.py pipeline 2>&1

# Nightly — refresh slow market signals (market multiplier per channel)
15 3 * * * cd /path/to/ln-operator && venv/bin/python3 main.py recompute_signals >> logs/signals.log 2>&1
```

### Dashboard

```bash
venv/bin/python3 dashboard/app.py    # or install services/lnd-dashboard.service
```

Access at `http://YOUR_IP:4000`. No auth — use Tailscale or LAN only.

---

## Fee Formula

Per-channel outbound fee is computed in layers, in this order:

```
1. Pin set?                 → use pin (warns if below floor)
2. base    = sigmoid(local_ratio)                # liquidity state
3. mult    = market_multiplier  (slow, demand-derived)
4. floor   = last_refill_ppm × REBALANCE_FEE_MARGIN  # 0 if never refilled
5. target  = clamp(max(base × (1+mult), floor), 0, FEE_HARD_CEILING_PPM)
6. Broadcast only if hysteresis permits          # no gossip spam
```

The sigmoid replaces the old linear curve. It has clean plateaus near 0% and
100% local — small drift inside the healthy middle doesn't snap fees around.
Sample shape with defaults (`SIGMOID_MIN=25`, `SIGMOID_MAX=250`, `K=8`):

| Local | Fee |
|-------|-----|
| 5% | 244 ppm |
| 20% | 231 ppm |
| 50% | 138 ppm |
| 80% | 44 ppm |
| 95% | 31 ppm |

Base fee is always 0. See **Advanced — Fee Engine Internals** below for the
full pipeline, hysteresis, signals, and corner cases.

### Manual Fee Pins

The auto-fee formula can be overridden per-channel with `set_fee`. A pinned
channel keeps its fixed ppm across every pipeline run until you clear the pin
with `clear_fee`. Pins are stored in the `fee_overrides` table and are
honored by both the `pipeline` and `adjust_fees` commands. If a pin is set
*below* the rebalance-cost floor, `adjust_fees` logs a warning so you know
you're selling outbound below what refilling costs.

```bash
main.py set_fee LNBiG 3000 --note "experimenting with high outbound"
main.py status              # 📌 next to the pinned channel + details block
main.py clear_fee LNBiG     # auto resumes on next pipeline run
```

The dashboard's *Recent Fee Updates* card tags each row as `auto` or `📌 pin`
in a Source column so you can tell at a glance which changes came from the
formula vs. a manual pin.

---

## Rebalance Budget

Single-signal model. No tiers, no maturity gates. The most recent successful
refill ppm for a channel drives both:

- **The budget** (max fee we'll pay to refill it again)
- **The outbound fee floor** (what we charge to recoup that cost + margin)

```
budget   = (last_refill_ppm OR DEFAULT_BUDGET)
            × (1 + ESCALATION_STEP × failures_since_last_success)
            capped at REBALANCE_MAX_BUDGET_PPM

fee_floor = last_refill_ppm × REBALANCE_FEE_MARGIN
            (0 if no successful refill yet — sigmoid alone)
```

### Bootstrap & drift recovery — failure escalation

A channel with no successful refill yet starts at `REBALANCE_DEFAULT_BUDGET_PPM`
(500). Each consecutive *whole-attempt* failure since the last success raises
the budget by `REBALANCE_BUDGET_ESCALATION_STEP` (20%) per cron cycle, capped
at `REBALANCE_MAX_BUDGET_PPM` (5000). One success resets the counter and the
budget anchors to the actual paid ppm.

Example: a channel where real market price is ~2300 ppm bootstraps as
`500 → 600 → 720 → 864 → 1037 → 1244 → 1493 → 1791 → 2150 → 2580` and
succeeds on the 9th attempt (≈18h at the 2h cron).

The same mechanism handles upward market drift after bootstrap — when the
last-known price stops succeeding, failures re-escalate until a new price is
discovered, then `last_refill_ppm` and the fee floor track the new market.

### Outbound fee impact

After the first successful refill at `R` ppm:
- The fee floor becomes `R × 1.1` (e.g. 2300 → 2530).
- `update_all_fees` posts that target on the next 2h pipeline run, subject
  to hysteresis (`SNAP_PPM` usually lets it through without waiting).
- No 5-sample warmup, no median smoothing — one refill = one fee update.

### Auto-Chunking

Full amount fails → halve and retry, down to 100k min. Each successful chunk
is logged as its own success row in `rebalance_log` at the chunk's actual ppm,
so `last_refill_ppm` reflects what we actually paid (very small chunks can
appear inflated because LND's base fee dominates at low amounts).

### Fallback Pairs

For every depleted target, the planner emits one or more **primary** pairs
(sources whose surplus sums to the target's deficit) and then **fallback**
pairs (every other overfull source paired with the same target).

At execution time the run keeps two ledgers:

- `target_deficits` — sats each target still needs.
- `source_remaining` — sats each source can still send.

Each plan is capped at `min(plan amount, target deficit, source remaining)`
before being attempted, and both ledgers decrement on success. A fallback
fires only when its target's deficit is still ≥ 50k *and* its source still
has ≥ 50k to send — both conditions emerge naturally from the ledgers, no
separate gating. This means:

- A primary that partially fills its target leaves the deficit open, and
  the next plan (often a fallback against the same target) picks up where
  it left off without overshooting.
- A source already drained by an earlier successful plan auto-skips the
  rest of its plans, avoiding wasted insufficient-balance attempts.
- A target fully filled removes itself from contention; remaining
  fallbacks for it are skipped.

### Force Mode

`--force 0.4` ignores thresholds, targets 40% on all channels. `--dry-run`
shows per-channel end states at different force levels so you can pick.

---

## Plan Command

```
Wallet balance         3,528,518 sats
Existing anchor        -30,000 sats  (already locked by LND)
Treasury (2.5%)        -88,212 sats
New anchor             -10,000 sats  (1 × 10,000)
Open fees                 -250 sats  (1 × 1 sat/vB × 250 vB)
────────────────────────────────────
Deployable             3,400,056 sats → 1 channel at 3,400,056 sats
```

Two-stage tier-segmented ranking. Tiers are absolute channel-count
buckets — **hub** (≥100 channels), **mid-tier** (30-99), **small** (10-29);
anything below 10 is dropped as noise. Within each tier:

1. **Centrality** (log-normalised mean of channel count + total capacity)
   prefilters the top 30 candidates — cheap, derived from the local graph.
2. **Diversity** (fraction of the candidate's peers that aren't already in
   your graph) is computed via a live `get_node_info` call per prefiltered
   candidate, then used to rerank. Top 10 per tier are surfaced.

Why tiered: a small node's peers are obscure leaves (high diversity by
default) and a hub's peers overlap heavily with yours (low diversity). A
single global ranking would just surface backwater nodes. Per-tier ranking
asks the right question — "the most diversifying hub", "the most
diversifying mid-tier", "the most diversifying small" — independently.

Avg outbound fee is shown for reference but not scored — local graph fee
data is unreliable.

---

## Data Flow

```
LND /v1/switch → sync_routing → forwarding_log (SQLite)
  ├─ Dashboard: routing events, daily revenue, per-channel revenue/net
  ├─ CLI: history
  └─ market_multiplier nudges (nightly recompute_signals)

LND /v1/payments → sync_rebalances → rebalance_log (SQLite)
  ├─ Dashboard: rebalance history (auto + manual)
  ├─ Dashboard: per-channel rebal cost, net 30d, net lifetime
  ├─ Rebalance-failing alert
  ├─ Rebalance budget: last_refill_ppm + failure-escalation counter
  └─ Outbound fee floor: last_refill_ppm × REBALANCE_FEE_MARGIN
```

Offset-based sync — no duplicates. Manual rebalances detected by matching
circular self-payments. Channel open time used as floor to prevent
misattribution to new channels with the same peer.

---

## Configuration

Key settings in `config.py`:

### Fee curve
| Setting | Default | |
|---------|---------|---|
| `SIGMOID_MIN_PPM` | 25 | Lower asymptote (channel full → drain) |
| `SIGMOID_MAX_PPM` | 250 | Upper asymptote (channel depleted → defend) |
| `SIGMOID_K` | 8.0 | Steepness; higher = sharper midpoint transition |
| `SIGMOID_MIDPOINT` | 0.5 | local_ratio at curve midpoint |
| `FEE_HARD_CEILING_PPM` | 5000 | Absolute cap — even floor can't exceed this |

### Hysteresis (when to broadcast fee changes)
| Setting | Default | |
|---------|---------|---|
| `FEE_HYSTERESIS_TOLERANCE_PPM` | 10 | Min absolute change to broadcast |
| `FEE_HYSTERESIS_TOLERANCE_PCT` | 0.10 | Also need ≥10% relative change |
| `FEE_HYSTERESIS_COOLDOWN_SEC` | 21600 | Don't update same channel within 6h |
| `FEE_HYSTERESIS_SNAP_PPM` | 30 | Big jumps skip the cooldown |

### Market multiplier (slow demand signal)
| Setting | Default | |
|---------|---------|---|
| `MARKET_MULT_STEP` | 0.15 | Per-recompute nudge size (~14 nights to full ramp-up) |
| `MARKET_MULT_MIN` | -0.5 | Max downward adjustment |
| `MARKET_MULT_MAX` | 2.0 | Max upward adjustment (3× base) |
| `MARKET_MULT_BUSY_HOURS` | 24 | Forwards within → nudge up |
| `MARKET_MULT_SILENT_DAYS` | 3 | No forwards for → nudge down |

### Rebalancer (single-signal budget + fee coupling)
| Setting | Default | |
|---------|---------|---|
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Trigger below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Trigger above 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max per attempt |
| `REBALANCE_DEFAULT_BUDGET_PPM` | 500 | Bootstrap budget when no refill history |
| `REBALANCE_MAX_BUDGET_PPM` | 5000 | Hard ceiling on rebalance fee |
| `REBALANCE_BUDGET_ESCALATION_STEP` | 0.20 | +20% per consecutive failure since last success |
| `REBALANCE_FEE_MARGIN` | 1.1 | Outbound fee floor = last_refill × this |

### Planner
| Setting | Default | |
|---------|---------|---|
| `TREASURY_MIN_RATIO` | 0.025 | Wallet reserve |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Min channel size |

---

## Alerts

| Alert | Trigger |
|-------|---------|
| `channel_depleted` | Local < 20% |
| `channel_saturated` | Local > 80% |
| `peer_offline` | Channel inactive |
| `rebalance_failing` | 3+ consecutive failures |

---

## Logging

```bash
tail -f logs/ln_operator.log    # rotating, 5MB × 5 backups
```

---

## Channel Backup

Optional: watch `channel.backup` and push to a remote host on every change.

```bash
sudo apt install inotify-tools
# See SETUP.md for full systemd service configuration
```

---

## Advanced — Fee Engine Internals

The 2h pipeline reads cached signals; a nightly job recomputes them. This
split keeps the fast loop cheap and the slow signals stable.

### Cadence

```
Every 2h (cron):
  1. adjust_fees       ← reads channel_signals, decides target,
                          gated broadcast (hysteresis)
  2. rebalance_channels
  3. sync_routing
  4. healthcheck

Nightly (cron, separate line):
  recompute_signals    ← refreshes per-channel market_multiplier and logs
                          last_refill_ppm / failure counts for visibility
```

Suggested cron line for the nightly job:

```
15 3 * * * cd /path/to/ln-operator && venv/bin/python3 main.py recompute_signals >> logs/signals.log 2>&1
```

### The four layers

1. **Sigmoid base** — `sigmoid_fee_ppm(local_ratio)`. Liquidity-driven base fee
   with clean plateaus near 0% and 100% local. No clamps needed at the edges —
   the curve naturally asymptotes to `SIGMOID_MIN/MAX_PPM`.

2. **Market multiplier** — slow per-channel scalar in `channel_signals`. Each
   nightly run nudges `+MARKET_MULT_STEP` if the channel forwarded in the last
   24h, `-MARKET_MULT_STEP` if silent ≥ `MARKET_MULT_SILENT_DAYS`. Bounded by
   `[MARKET_MULT_MIN, MARKET_MULT_MAX]`. Modulates the sigmoid: `adjusted =
   base × (1 + mult)`.

3. **Rebalance-cost floor** — "don't sell outbound below what refilling costs."
   Floor = `last_refill_ppm × REBALANCE_FEE_MARGIN`, read live from
   `rebalance_log`. Activates on the first successful refill — no warmup, no
   median smoothing. No refill history → no floor (sigmoid alone decides).

4. **Hard ceiling** — `FEE_HARD_CEILING_PPM` (5000). Last line of defense
   against runaway data. Matched to `REBALANCE_MAX_BUDGET_PPM` so a channel
   can always charge enough outbound to recoup what we'd pay to refill it.

### Hysteresis (`_should_broadcast`)

A computed target only becomes a broadcast `channel_update` if:
- Δ from current fee ≥ both `TOLERANCE_PPM` AND `TOLERANCE_PCT` of current, AND
- one of:
  - cooldown expired (`COOLDOWN_SEC` since last broadcast), OR
  - Δ ≥ `SNAP_PPM` (urgent — skip cooldown), OR
  - channel crossed an edge zone (`EDGE_LOW`/`EDGE_HIGH`) since last update.

This is what actually prevents gossip spam. The sigmoid shape is for *what*
fee, not *whether to broadcast*.

### Rebalance budget & failure escalation

`get_channel_rebalance_budget` reads `last_refill_ppm` and
`failures_since_last_success` live from `rebalance_log` and returns:

```
budget = (last_refill OR DEFAULT_BUDGET) × (1 + STEP × failures)
         capped at REBALANCE_MAX_BUDGET_PPM
```

This single formula handles bootstrap, drift, and re-bootstrap after a long
idle period. There are no tiers, no maturity windows, no separate adaptive
cap or revenue-ratio gate — the budget tracks the actual paid price, and
failures walk it back up if the market has moved.

### Corner cases & how each is handled

| Case | Behavior |
|---|---|
| Brand-new channel, no refill yet | Budget = `DEFAULT_BUDGET` (500), no fee floor (sigmoid alone). Failures escalate budget at 20% per cron cycle |
| Manual urgency refill at high cost | Stored as success row with actual ppm → next budget = that ppm, next fee floor = ppm × REBALANCE_FEE_MARGIN. No filtering of manual rows |
| Single chunk succeeded at small amount | Logged as success at chunk ppm. May be inflated vs full-amount price — accepted as the cost of having any signal at all |
| Market drifted upward, refills fail | Failure counter ticks, budget escalates 20%/cycle until new price is discovered |
| Channel idle 30+ days, then drains | `last_refill_ppm` still anchors — budget starts at last known price + escalation if it has drifted |
| Pin below floor | Pin wins (explicit intent), warning logged |
| Channel at <20% local, market says "lower" | **Blocked** — in defense zone, multiplier can only raise |
| Channel at >80% local, market says "raise" | **Allowed** — earn more on outflow |
| Just paid expensive refill → big fee jump | `SNAP_PPM` escapes cooldown so the floor jump goes live in the next cron cycle |
| Crossing 20%/80% boundary | Edge-zone crossing escapes cooldown |
| Tiny fee drift (1-2 ppm) | Caught by tolerance — no broadcast |
| Channel offline | Skipped — no policy update |

### When data is missing

Channels with no refill history start at `DEFAULT_BUDGET` (500) and use the
sigmoid alone for outbound fees (no floor). Failure escalation discovers the
real market price within ~9 cron cycles (18h at 2h cron) for a 2300-ppm peer.

### Inspecting state

```bash
# Most recent successful refill ppm per channel (drives budget + fee floor)
sqlite3 ln_operator.db "SELECT target_chan_id, fee_ppm, ts FROM rebalance_log \
  WHERE success=1 ORDER BY target_chan_id, ts DESC;"

main.py recompute_signals       # manual trigger; prints per-channel signal table
main.py adjust_fees --dry-run   # see what would change without broadcasting
```

---

## Known Limitations

- **Local graph capacity** is unreliable for distant nodes. Channel count is reliable.
- **Inbound fees** not visible in local graph — check Amboss before opening channels.
- **Dashboard has no auth** — Tailscale or LAN only.
- **Channel opens are manual** — plan recommends, you execute via `lncli`.

---

## License

MIT
