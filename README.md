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
top 20 candidate peers from your local graph scored by centrality and diversity,
split by tier (hub / mid-tier / small) based on your existing portfolio. Offers to
generate a deposit address with QR code at the end.

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
├── engine.py            Fees, rebalancing, health monitoring, routing sync
├── advisor.py           Peer scoring, graph analysis, candidate discovery
├── agent.py             Claude API (optional) — web search for peer research
├── lnd_client.py        LND REST API client
├── db.py                SQLite schema, migrations, queries
├── telegram_bot.py      Telegram notifications
├── logging_config.py    Rotating log setup
├── requirements.txt     Python dependencies
├── dashboard/
│   └── app.py           Flask web dashboard (port 4000)
├── .env                 Secrets (not committed)
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
cat > .env << 'EOF'
LND_REST_URL=https://127.0.0.1:9000
LND_CERT=/home/lnd/tls.cert
LND_MACAROON=/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon

# Optional
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
chmod 600 .env
```

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
main.py recompute_signals                    # nightly job — recompute floor/cap/multiplier

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

# Nightly — recompute slow signals (rebalance-cost floor, adaptive cap, market mult)
15 3 * * * cd /path/to/ln-operator && venv/bin/python3 main.py recompute_signals >> logs/signals.log 2>&1
```

### Dashboard

```bash
venv/bin/python3 dashboard/app.py    # or install lnd-dashboard.service
```

Access at `http://YOUR_IP:4000`. No auth — use Tailscale or LAN only.

---

## Fee Formula

Per-channel outbound fee is computed in layers, in this order:

```
1. Pin set?                 → use pin (warns if below floor)
2. base    = sigmoid(local_ratio)                # liquidity state
3. mult    = market_multiplier  (slow, demand-derived)
4. floor   = rebalance_cost_floor                # what refilling costs
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

Each channel gets a fee budget based on its track record:

**Discovery** (< 15 balanced days) — 1000 ppm baseline. If we already have
rebalance data for the target (e.g. you've manually refilled it), the budget
rises to the per-channel **adaptive cap** so auto-rebalances can succeed at
true market price. Balanced time only counts when local ratio is 30-70%.

**Proven** (15+ balanced days, earns fees) — 50% of earned ppm.
Floor: 150 ppm. Ceiling: per-channel adaptive cap.

**Deadweight** (15+ balanced days, zero revenue) — 150 ppm.
Consider closing.

### Adaptive Cap

Replaces the old global `REBALANCE_HARD_CAP_PPM`. Computed nightly per channel:

```
adaptive_cap = clamp( median(successful_rebal_ppm last 30d, this target) × 1.5,
                      REBALANCE_CAP_MIN_PPM,   # 500
                      REBALANCE_CAP_MAX_PPM )  # 5000
```

If a channel has no rebalance history yet, the cap defaults to
`REBALANCE_CAP_DEFAULT_PPM` (1000). The cap rises as observed costs grow,
so channels with expensive refill paths (like LNBiG) stop hitting the old
1000 cap and failing every auto-rebalance attempt.

### Auto-Chunking

Full amount fails → halve and retry, down to 100k min. Partial success kept.

### Fallback Pairs

Source→target fails → try same source with different target. Triggered when
the source channel has failed on any pair during the run.

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

Candidates scored by **diversity** (50%) and **centrality** (50%).
Avg outbound fee shown for reference. Three tiers: hub (rank 1-50),
mid-tier (51-250), small (251-500). Portfolio strategy selects the tier.

---

## Data Flow

```
LND /v1/switch → sync_routing → forwarding_log (SQLite)
  ├─ Dashboard: routing events, daily revenue, per-channel revenue/net
  ├─ CLI: history
  └─ Rebalance budget: earned ppm

LND /v1/payments → sync_rebalances → rebalance_log (SQLite)
  ├─ Dashboard: rebalance history (auto + manual)
  ├─ Dashboard: per-channel rebal cost, net 30d, net lifetime
  └─ Rebalance-failing alert
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
| `FEE_HARD_CEILING_PPM` | 2000 | Absolute cap — even floor can't exceed this |

### Hysteresis (when to broadcast fee changes)
| Setting | Default | |
|---------|---------|---|
| `FEE_HYSTERESIS_TOLERANCE_PPM` | 10 | Min absolute change to broadcast |
| `FEE_HYSTERESIS_TOLERANCE_PCT` | 0.10 | Also need ≥10% relative change |
| `FEE_HYSTERESIS_COOLDOWN_SEC` | 21600 | Don't update same channel within 6h |
| `FEE_HYSTERESIS_SNAP_PPM` | 30 | Big jumps skip the cooldown |

### Rebalance floor & market multiplier
| Setting | Default | |
|---------|---------|---|
| `REBALANCE_FLOOR_WINDOW_DAYS` | 30 | History window for floor |
| `REBALANCE_FLOOR_MIN_SAMPLES` | 5 | Below this, fall back to manual data |
| `REBALANCE_FLOOR_MULTIPLIER` | 1.5 | floor = median × this |
| `REBALANCE_FLOOR_DEFAULT_PPM` | 0 | No data → no floor (sigmoid alone) |
| `MARKET_MULT_STEP` | 0.05 | Per-recompute nudge size |
| `MARKET_MULT_MIN` | -0.5 | Max downward adjustment |
| `MARKET_MULT_MAX` | 2.0 | Max upward adjustment (3× base) |

### Rebalancer
| Setting | Default | |
|---------|---------|---|
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Trigger below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Trigger above 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max per attempt |
| `REBALANCE_DISCOVERY_PPM` | 1000 | New channel baseline budget |
| `REBALANCE_DEADWEIGHT_PPM` | 150 | Zero-revenue budget |
| `REBALANCE_DISCOVERY_DAYS` | 15 | Days before judging |
| `REBALANCE_CAP_DEFAULT_PPM` | 1000 | Adaptive-cap fallback (no data) |
| `REBALANCE_CAP_MIN_PPM` | 500 | Adaptive cap lower bound |
| `REBALANCE_CAP_MAX_PPM` | 5000 | Adaptive cap upper bound |
| `REBALANCE_CAP_MULTIPLIER` | 1.5 | cap = observed median × this |

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
  recompute_signals    ← refreshes market_multiplier, rebalance_cost_floor,
                          adaptive_cap per channel
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
   Floor = `median(successful_rebal_ppm last 30d, this target) × FLOOR_MULTIPLIER`.
   Auto rebalances preferred; if `< MIN_SAMPLES`, falls back to manual data
   (since for some peers, manual rebalances ARE the market signal). No data
   at all → no floor (sigmoid alone decides).

4. **Hard ceiling** — `FEE_HARD_CEILING_PPM`. Last line of defense against a
   runaway floor (e.g. data poisoned by an urgent expensive manual refill).

### Hysteresis (`_should_broadcast`)

A computed target only becomes a broadcast `channel_update` if:
- Δ from current fee ≥ both `TOLERANCE_PPM` AND `TOLERANCE_PCT` of current, AND
- one of:
  - cooldown expired (`COOLDOWN_SEC` since last broadcast), OR
  - Δ ≥ `SNAP_PPM` (urgent — skip cooldown), OR
  - channel crossed an edge zone (`EDGE_LOW`/`EDGE_HIGH`) since last update.

This is what actually prevents gossip spam. The sigmoid shape is for *what*
fee, not *whether to broadcast*.

### Adaptive rebalance cap

Replaces the global `REBALANCE_HARD_CAP_PPM`. Per-channel cap derived from
observed market price (`median × 1.5`, clamped to `[MIN, MAX]`). Lets channels
with expensive refill paths (e.g. LNBiG) escape the death-spiral where
auto-rebalance fails every attempt at a too-low global cap.

The discovery tier uses `max(REBALANCE_DISCOVERY_PPM, adaptive_cap)` when data
exists — so a new channel whose refill cost we already know about gets a
realistic budget instead of the default 1000 ppm.

### Corner cases & how each is handled

| Case | Behavior |
|---|---|
| Manual urgency rebalance at very high cost | Floor prefers `triggered_by='auto'` rows; manual ignored when ≥5 auto samples exist |
| Channel with no auto successes (e.g. LNBiG) | Falls back to ALL successful rebalances (auto + manual) — manual *is* the price signal |
| Single bad outlier rebalance | Median (not p90) ignores it |
| New channel, < 5 rebalances | Floor falls back to `REBALANCE_FLOOR_DEFAULT_PPM` (0 = no floor) |
| Pin below floor | Pin wins (explicit intent), warning logged |
| Floor recompute noise → hysteresis fight | Floor is **cached nightly**, not recomputed every 2h |
| Channel at <20% local, market says "lower" | **Blocked** — in defense zone, multiplier can only raise |
| Channel at >80% local, market says "raise" | **Allowed** — earn more on outflow |
| Channel at >80% local, market says "lower" | Allowed, bounded by `SIGMOID_MIN_PPM` |
| Just paid expensive rebalance → big jump needed | `SNAP_PPM` escapes cooldown |
| Crossing 20%/80% boundary | Edge-zone crossing escapes cooldown |
| Tiny fee drift (1-2 ppm) | Caught by tolerance — no broadcast |
| Channel offline | Skipped — no policy update |
| Sender races a fee change mid-route | Cooldown + tolerance dampens flap, so sender mission control sees stable policy |

### When data is missing

The `channel_signals` row is created lazily on first read. Channels with no
rebalance history are perfectly fine — they just use sigmoid alone (floor = 0)
and the default rebalance cap (1000). The system "warms up" over the first
30 days of observed activity.

### Inspecting state

```bash
sqlite3 ln_operator.db "SELECT * FROM channel_signals;"
main.py recompute_signals       # manual trigger; prints per-channel table
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
