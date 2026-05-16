# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee optimisation,
rebalancing, channel planning, and monitoring — with a web dashboard for visibility.

Built for home node operators running LND on a Raspberry Pi or similar.

---

## What It Does

### Automated Channel Management (pipeline)

Runs on a cron schedule unattended. Four steps in order:

**1. adjust_fees** — Sets each channel's fee rate based on its current local/remote balance
ratio. Depleted channels get high fees to protect remaining liquidity and reputation.
Full channels get low fees to attract routing traffic.

**2. rebalance_channels** — Moves sats from overfull channels (>80% local) to depleted
ones (<20% local) via circular payments using Router SendPaymentV2. Each channel has its
own rebalance budget based on its earnings history (see *Rebalance Budget* below).

**3. sync_routing** — Syncs new forwarding events from LND into SQLite using offset-based
pagination. Never fetches the same event twice. Single source of truth for all routing
revenue data shown in the dashboard and CLI.

**4. healthcheck** — Snapshots all channel states, updates the channel maturity tracker,
fires alerts for depleted channels, offline peers, or repeated failures.

### Channel Plan (on-demand)

Run `main.py plan` when you want to open new channels. The tool:

1. Reads your on-chain wallet balance directly from LND
2. Deducts existing anchor reserve (already locked by LND)
3. Calculates treasury reserve (configurable %, default 2.5%)
4. Deducts new anchor reserve for the channels being opened (10,000 sats each, max 100,000)
5. Deducts on-chain channel open fees (real fee rate from LND × 250 vBytes per channel)
6. Determines how many channels fit at the minimum channel size, maximising deployment
7. Shows the top 10 candidate peers from your local LND graph, scored by topology

All data comes from your own LND node — no external API dependencies.
You then research the candidates yourself (Amboss, 1ML) and open manually.

### Web Dashboard

Single-page Flask app combining live LND data with historical SQLite data:
- Node status, sync state, balances
- Channel health table with balance bars, 30d revenue, rebalancing costs, net profit, tier
- Channel liquidity split (sendable vs receivable)
- Daily fee revenue chart (30 days)
- Rebalance history, fee update log, recent alerts
- Recent payments and invoices

---

## Architecture

```
60% Deterministic Python
    Fee calculation (linear formula by balance ratio).
    Rebalance budget per channel (based on earnings history).
    Peer scoring from local LND graph (channel count, diversity, centrality).
    Portfolio-aware candidate selection (hub vs mid-tier strategy).
    All decisions are formula-driven with configurable thresholds in config.py.

30% SQLite Database
    Single source of truth for all routing fee data.
    Historical tracking: forwarding events, rebalances, fee changes, snapshots.
    Channel maturity tracking (how long each channel has been balanced).
    Sync state (offset cursor for LND forwarding history — no duplicates).
    Powers rebalance budget tiers and treasury reserve calculation.

10% External data (optional)
    mempool.space: fallback fee rate if LND estimate unavailable.
    1ML: alias enrichment for candidate display (best-effort, fails silently).
```

---

## Project Structure

```
ln-operator/
├── main.py              CLI entry point — all commands
├── config.py            All tuneable settings in one place
├── engine.py            Fee management, rebalancing, monitoring, forwarding sync
├── advisor.py           Peer scoring, graph analysis, treasury calculation
├── lnd_client.py        LND REST API client (all LND communication)
├── db.py                SQLite schema, sync state, query helpers
├── telegram_bot.py      Telegram notification formatting and sending
├── logging_config.py    Rotating log file + console output setup
├── requirements.txt     Python dependencies
├── dashboard/
│   └── app.py           Flask web dashboard (port 4000)
├── logs/
│   └── ln_operator.log  Rotating log file (created on first run)
├── .env                 Secrets — API keys, LND paths (not committed)
└── ln_operator.db       SQLite database (created on first run)
```

---

## Prerequisites

- **LND** running and synced (REST API enabled, default port 9000)
- **Python 3.9+**
- **Telegram bot** (optional — for pipeline notifications)

---

## Installation

### 1. Clone the repo

```bash
git clone git@github.com:YOUR_USERNAME/ln-operator.git
cd ln-operator
```

### 2. Create Python virtual environment

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cat > .env << 'EOF'
# Required — LND connection
LND_REST_URL=https://127.0.0.1:9000
LND_CERT=/home/lnd/tls.cert
LND_MACAROON=/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon

# Optional — Telegram notifications for pipeline runs and alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Database path (defaults to ln_operator.db in the project folder)
LN_OPERATOR_DB=/home/pi/ln-operator/ln_operator.db
EOF
chmod 600 .env
```

### 4. Initialise the database

```bash
venv/bin/python3 db.py
```

### 5. Do a first sync to populate routing history

```bash
venv/bin/python3 main.py sync_routing
```

Fetches all historical forwarding events from LND into the database.
Subsequent runs only fetch new events using the offset cursor.

### 6. Test

```bash
venv/bin/python3 main.py status
```

### 7. LND file permissions

```bash
sudo usermod -aG lnd YOUR_USER
sudo chmod g+r /home/lnd/tls.cert
sudo chmod g+r /home/lnd/data/chain/bitcoin/mainnet/admin.macaroon
sudo chmod g+x /home/lnd /home/lnd/data /home/lnd/data/chain \
    /home/lnd/data/chain/bitcoin /home/lnd/data/chain/bitcoin/mainnet
```

---

## Usage

### CLI Commands

```bash
# ── AUTOMATED — full pipeline, designed for crontab ────────
venv/bin/python3 main.py pipeline              # run all steps in sequence
venv/bin/python3 main.py pipeline --dry-run    # preview without executing

# ── FEATURES — interactive tools ───────────────────────────
venv/bin/python3 main.py plan                              # channel plan (reads wallet from LND)
venv/bin/python3 main.py plan --min-channel 3000000        # override min channel size
venv/bin/python3 main.py plan --treasury 0.01              # override treasury ratio (1%)
venv/bin/python3 main.py plan --min-channel 3000000 --treasury 0.01  # both overrides

venv/bin/python3 main.py status                # quick node overview with balance bars
venv/bin/python3 main.py history               # last 30 days of activity
venv/bin/python3 main.py history 7             # last 7 days

# ── DEBUG — run individual pipeline steps ──────────────────
venv/bin/python3 main.py adjust_fees              # adjust fee rates
venv/bin/python3 main.py adjust_fees --dry-run    # preview only
venv/bin/python3 main.py rebalance_channels              # rebalance
venv/bin/python3 main.py rebalance_channels --dry-run    # preview only
venv/bin/python3 main.py sync_routing             # sync forwarding history from LND
venv/bin/python3 main.py healthcheck              # snapshot + alerts

# ── FLAGS ──────────────────────────────────────────────────
venv/bin/python3 main.py --no-telegram pipeline   # skip Telegram on any command
```

### Crontab Setup

```bash
crontab -e
```

Add:

```cron
0 */2 * * * cd /path/to/ln-operator && venv/bin/python3 main.py pipeline 2>&1
```

Runs every 2 hours: adjust_fees → rebalance_channels → sync_routing → healthcheck.

### Channel Plan

```bash
venv/bin/python3 main.py plan --min-channel 3000000 --treasury 0.01
```

Example output:

```
⚡ LN Operator — Channel Plan
═══════════════════════════════════════════

  Wallet balance:           3,451,948 sats
  Existing anchor reserve:    -20,000 sats  (already locked by LND)

  ────────────────────────────────────────
  Treasury (1.0%):              34,519 sats
  New anchor reserve:           10,000 sats  (1 × 10,000)
  Channel open fees:               500 sats  (1 × 2 sat/vB × 250 vB)
  ────────────────────────────────────────
  Deployable:               3,386,929 sats

  → 1 channel(s) at 3,386,929 sats each

  ────────────────────────────────────────
  Top 10 candidates from LND graph:

   1. Kraken                         | score 0.787 | rank   3 | 1866 ch | hub
   2. WalletOfSatoshi.com            | score 0.774 | rank   1 | 2452 ch | hub
   3. LNBiG [Hub-2]                  | score 0.722 | rank  51 |  350 ch | mid-tier
   ...
```

Then research the candidates yourself on Amboss or 1ML and open channels manually:

```bash
lncli --lnddir=/home/lnd connect PUBKEY@IP:PORT
lncli --lnddir=/home/lnd openchannel --node_key PUBKEY --local_amt 3386929
```

### Dashboard

Install as systemd service (see `dashboard/lnd-dashboard.service`), or run directly:

```bash
venv/bin/python3 dashboard/app.py
```

---

## How the Fee Formula Works

```
ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) × (1 - local_ratio)
```

| Local balance | Fee rate | Purpose |
|---------------|----------|---------|
| 80-100% (full) | 50-140 ppm | Low fees attract routing |
| 40-60% (balanced) | 225-275 ppm | Mid fees, healthy state |
| 0-20% (depleted) | 410-500 ppm | High fees protect liquidity and reputation |

Fees only update if change >5 ppm (avoids gossip spam).
Base fee is always 0 (modern best practice).

When you open a new channel, use LND's defaults. The next pipeline run sets fees automatically.

---

## How the Rebalance Budget Works

Each channel gets its own fee budget based on its track record:

### Discovery (< 30 balanced days)
Budget: **150 ppm**. New channel gets 30 days balanced to prove it routes.
Balanced time only counts when local ratio is 30-70%.

### Proven (30+ balanced days, earns fees)
Budget: **50% of average earned ppm**. Never spend more rebalancing than you earn.
Floor: 50 ppm. Ceiling: 500 ppm.

### Deadweight (30+ balanced days, zero revenue)
Budget: **50 ppm**. Had its chance. Flag as close candidate.

---

## How the Channel Plan Works

```
1. Read wallet balance from LND (/v1/balance/blockchain)
2. Read existing anchor reserve (reserved_balance_anchor_chan)
3. Get current fee rate from LND (/v2/wallet/estimatefee/2)
   → fallback to mempool.space if LND estimate unavailable
4. For N = 1, 2, 3... channels:
     deployable = total - existing_anchor - treasury% - new_anchor(N) - open_fees(N)
     channel_size = deployable / N
     if channel_size >= min_channel_size → this N works, keep trying higher
     else → stop, use previous N
5. Show top 10 candidates from local LND graph scored by:
   - Channel count (topology reach)
   - Diversity (% of their peers new to you)
   - Centrality (proxy from channel count)
   - Capacity (local graph — improves accuracy as you add channels)
```

### Portfolio Strategy for Candidates

The candidate list applies a portfolio-aware strategy based on your existing channels:
- **0 hub connections** → show top 10 hubs (need routing backbone first)
- **1 hub connection** → show 2 hubs + 8 mid-tier
- **2+ hub connections** → show top 10 mid-tier (diversify away from hubs)

Hub = node with 500+ channels. Mid-tier = 20-499 channels.

### On Anchor Reserve

LND reserves 10,000 sats per anchor channel for emergency force-close fee bumping,
capped at 100,000 sats total regardless of channel count. This is a hard LND requirement
and is separate from the treasury percentage.

The plan automatically reads your current anchor reserve from LND and only
calculates the additional reserve needed for new channels.

---

## Data Flow

All routing fee data flows through a single source — `forwarding_log` in SQLite:

```
LND node (/v1/switch)
  │
  └─ sync_routing (pipeline step 3)
       │  offset-based pagination — only fetches new events
       │  offset cursor stored in sync_state table
       └─ forwarding_log (SQLite)
            │
            ├─ Dashboard: Recent Routing Events
            ├─ Dashboard: Daily Fee Revenue chart
            ├─ Dashboard: Revenue 30d / Net 30d per channel
            ├─ CLI: main.py history
            └─ Rebalance budget: get_channel_earned_ppm()
```

---

## Configuration

| Setting | Default | What it does |
|---------|---------|--------------|
| `FEE_MIN_PPM` | 50 | Floor fee when channel is full |
| `FEE_MAX_PPM` | 500 | Ceiling fee when channel is depleted |
| `FEE_BASE_MSAT` | 0 | Base fee (0 is best practice) |
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Rebalance when local < 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Rebalance when local > 80% |
| `REBALANCE_DISCOVERY_PPM` | 150 | Budget for new channels |
| `REBALANCE_REVENUE_RATIO` | 0.50 | Proven: budget = earned_ppm × this |
| `REBALANCE_DEADWEIGHT_PPM` | 50 | Budget for zero-revenue channels |
| `REBALANCE_DISCOVERY_DAYS` | 30 | Days balanced before judging |
| `TREASURY_MIN_RATIO` | 0.025 | Default treasury reserve (2.5%) |
| `ANCHOR_RESERVE_PER_CHANNEL` | 10,000 | Sats reserved per new anchor channel |
| `ANCHOR_RESERVE_MAX` | 100,000 | LND's hard cap on anchor reserve |
| `MIN_CHANNEL_SIZE_SATS` | 1,000,000 | Absolute minimum channel size |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Default minimum (overridable via --min-channel) |

---

## Known Limitations

**Local graph capacity** — your LND node's graph view depends on gossip propagation.
With few channels you may see incomplete capacity data for distant nodes. This improves
significantly as you open more channels. Channel count and topology data are reliable.

**Rebalancing requires LND 0.15+** — uses Router SendPaymentV2 with `outgoing_chan_id`.

**Dashboard requires sync_routing** to have run at least once for routing data sections.

---

## Logging

Logs go to `logs/ln_operator.log` (rotating, 5MB × 5 backups).
Terminal shows warnings and errors only. Full detail in the file.

```bash
tail -f logs/ln_operator.log
```

Typical pipeline run:
```
INFO  [main] pipeline starting
INFO  [engine] fees: ACINQ ↑ 120→480 ppm (local 2%)
INFO  [engine] rebalance: 1 depleted, 0 overfull — no overfull channel to pair
INFO  [engine] sync_routing: 3 new event(s) (offset 50)
INFO  [engine] healthcheck: 1 active, 0 inactive — overall local 2%
WARNING [engine] alert [channel_depleted]: ACINQ at 2% local
INFO  [main] pipeline complete in 2.3s — fees:1 rebalances:0 events:3 alerts:1
WARNING [main] alert [channel_depleted]: ACINQ at 2% local
```

---

## Roadmap

- Dashboard investment approval flow — approve/reject channel open recommendations
- Backup automation — auto-backup `channel.backup` on channel changes
- On-chain fee watcher — alert when fees drop below threshold
- Tests — pytest suite for core logic

---

## License

MIT
