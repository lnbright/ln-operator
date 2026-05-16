# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee optimisation,
rebalancing, investment planning, and monitoring — with a web dashboard for visibility.

Built for home node operators running LND on a Raspberry Pi or similar.

---

## What It Does

### Automated Channel Management (cron)

Runs every 12 hours unattended:

- **Dynamic fees** — adjusts each channel's fee rate based on its local/remote balance ratio.
  Depleted channels get high fees (protect remaining liquidity), full channels get low fees
  (attract routing). Formula: `ppm = 50 + (500 - 50) × (1 - local_ratio)`.

- **Smart rebalancing** — moves sats from overfull channels to depleted ones via circular
  payments. Each channel gets a rebalance budget based on its track record:
  - *Discovery* (new channels) — 150 ppm budget for 30 days to prove themselves
  - *Proven* (channels that earn fees) — budget = 50% of what they earn
  - *Deadweight* (30+ balanced days, zero routing) — minimal 50 ppm budget

- **Health monitoring** — snapshots all channel states, syncs routing history from LND,
  and alerts on depleted channels, offline peers, or repeated failures.

### Investment Advisor (on-demand)

When you have sats to deploy, run `main.py invest <amount>` and get:

- Treasury reserve calculation (based on historical rebalancing costs)
- On-chain fee environment check via mempool.space
- Peer scoring using 1ML data + local graph analysis
- Allocation plan: which channels to upsize, which new peers to connect to
- Plain-English summary from Claude API (optional — works without it)
- Interactive follow-up Q&A ("why this peer?", "what if I split across two?")

### Web Dashboard

Flask app serving a single-page view of your node at a glance:

- Node status, sync state, balances
- Channel health table with 30d revenue, rebalancing costs, net profit, and tier badges
- Channel liquidity split (sendable vs receivable)
- Daily fee revenue chart (30 days)
- Rebalance history, fee update log, recent alerts
- Recent payments, invoices, and routing events

---

## Architecture

```
60% Deterministic Python
    Fee calculation, rebalancing logic, peer scoring, budget allocation.
    All decisions are formula-driven with configurable thresholds.

30% SQLite Database
    Historical tracking of channel snapshots, fee changes, rebalance attempts,
    routing fees earned, and investment plans. The tool gets smarter over time
    as it accumulates performance data per channel.

10% Claude API (optional)
    Plain-English summaries of investment plans. Spots risks the Python
    engine might miss. Falls back gracefully if no API key is configured.
```

---

## Project Structure

```
ln-operator/
├── main.py              CLI entry point — all commands
├── config.py            All tuneable settings in one place
├── engine.py            Fee management, rebalancing, monitoring logic
├── advisor.py           Investment advisor — peer scoring, budget allocation
├── agent.py             Claude API integration for investment summaries
├── lnd_client.py        LND REST API client
├── db.py                SQLite schema, migrations, query helpers
├── telegram_bot.py      Telegram notification formatting and sending
├── logging_config.py    Rotating log file + console output setup
├── requirements.txt     Python dependencies
├── dashboard/
│   └── app.py           Flask web dashboard
├── logs/
│   └── ln_operator.log  Rotating log file (created on first run)
├── .env                 Secrets — API keys, LND paths (not committed)
└── ln_operator.db       SQLite database (created on first run)
```

---

## Prerequisites

- **LND** running and synced (REST API enabled, default port 9000)
- **Python 3.9+**
- **Anthropic API key** (optional — for AI investment summaries)
- **Telegram bot** (optional — for cron notifications)

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

# Optional — Claude API for investment plan summaries
ANTHROPIC_API_KEY=

# Optional — Telegram notifications for cron runs
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Dashboard
LN_OPERATOR_DB=/path/to/ln-operator/ln_operator.db
DASHBOARD_IP=0.0.0.0
OPERATOR_DASHBOARD_PORT=4002
EOF
chmod 600 .env
```

### 4. Initialise the database

```bash
venv/bin/python3 db.py
```

### 5. Test

```bash
venv/bin/python3 main.py status
```

### 6. LND file permissions

The tool needs to read LND's macaroon and TLS cert. If running as a
different user than LND:

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
# ── Full pipeline (designed for crontab) ─────────────────
venv/bin/python3 main.py pipeline              # run all steps
venv/bin/python3 main.py pipeline --dry-run    # preview without executing

# ── Individual pipeline steps (for debugging/manual use) ─
venv/bin/python3 main.py adjust_fees              # adjust channel fee rates
venv/bin/python3 main.py adjust_fees --dry-run    # preview only
venv/bin/python3 main.py rebalance_channels              # rebalance channels
venv/bin/python3 main.py rebalance_channels --dry-run    # preview only
venv/bin/python3 main.py sync_routing             # sync routing events from LND
venv/bin/python3 main.py healthcheck              # snapshot channels + fire alerts

# ── Interactive ──────────────────────────────────────────
venv/bin/python3 main.py invest 5000000    # investment advisor with follow-up Q&A

# ── Read-only ────────────────────────────────────────────
venv/bin/python3 main.py status            # quick node overview with balance bars
venv/bin/python3 main.py history           # last 30 days of activity
venv/bin/python3 main.py history 7         # last 7 days

# ── Flags ────────────────────────────────────────────────
venv/bin/python3 main.py --no-telegram pipeline    # skip Telegram on any command
```

### Crontab Setup

```bash
crontab -e
```

Add:

```cron
0 8,20 * * * cd /path/to/ln-operator && venv/bin/python3 main.py pipeline 2>&1
```

Runs adjust_fees → rebalance_channels → sync_routing → healthcheck at 8am and 8pm daily.

### Dashboard

Run directly:

```bash
venv/bin/python3 dashboard/app.py
```

Or install as a systemd service — see `dashboard/lnd-dashboard.service`.

---

## How the Fee Formula Works

Each channel's fee rate is set dynamically based on how much local
balance it has:

```
ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) × (1 - local_ratio)
```

| Local balance | Fee rate | Purpose |
|---------------|----------|---------|
| 80-100% (full) | 50-140 ppm | Low fees attract routing, earn while naturally draining |
| 40-60% (balanced) | 225-275 ppm | Mid fees, healthy state |
| 0-20% (depleted) | 410-500 ppm | High fees protect remaining liquidity and reputation |

Fees only update if the change is >5 ppm to avoid gossip network spam.
Base fee is always 0 (modern best practice — most pathfinding penalises
base fees).

---

## How the Rebalance Budget Works

Instead of a flat fee cap for all channels, each channel gets its own
budget based on its track record:

### Discovery (new channels, < 30 balanced days)

Budget: **150 ppm**. The channel is rebalanced to 50% and given 30 days
in a balanced state to prove it can route payments. The clock only ticks
when the channel is between 30-70% local ratio.

### Proven (30+ balanced days, earns routing fees)

Budget: **50% of average earned ppm**. A channel earning 300 ppm gets a
150 ppm rebalance budget. You never spend more rebalancing than the
channel earns. Floor of 50 ppm, ceiling of 500 ppm.

### Deadweight (30+ balanced days, zero revenue)

Budget: **50 ppm**. The channel had a fair chance in a balanced state and
earned nothing. Minimal budget — the investment advisor will flag it as a
close candidate so you can redeploy those sats.

---

## Data Flow

All routing data reads from a single source — the `forwarding_log`
table in SQLite:

```
LND node (/v1/switch API)
  │
  └─ sync_forwarding_history() — called by cron or main.py monitor
       │                          uses offset-based pagination (no duplicates)
       └─ forwarding_log table (SQLite)
            │
            ├─ Dashboard: Recent Routing Events
            ├─ Dashboard: Daily Fee Revenue chart
            ├─ Dashboard: Revenue 30d / Net 30d per channel
            ├─ CLI: main.py history
            └─ Engine: per-channel rebalance budget calculation
```

The offset cursor is stored in `sync_state` table and persists between
cron runs and manual runs. Each run fetches only new events since the
last sync — no duplicates, no gaps.

---

## Configuration

All settings are in `config.py`. Key values:

| Setting | Default | What it does |
|---------|---------|--------------|
| `FEE_MIN_PPM` | 50 | Floor fee when channel is full |
| `FEE_MAX_PPM` | 500 | Ceiling fee when channel is depleted |
| `FEE_BASE_MSAT` | 0 | Base fee (0 is best practice) |
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Rebalance when local drops below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Rebalance when local exceeds 80% |
| `REBALANCE_DISCOVERY_PPM` | 150 | Budget for new/unproven channels |
| `REBALANCE_REVENUE_RATIO` | 0.50 | Proven channels: max fee = earned × this |
| `REBALANCE_DEADWEIGHT_PPM` | 50 | Budget for channels that don't earn |
| `REBALANCE_DISCOVERY_DAYS` | 30 | Days of balanced time before judging |
| `TREASURY_MIN_RATIO` | 0.10 | Keep at least 10% as reserve |
| `MIN_CHANNEL_SIZE_SATS` | 1,000,000 | Minimum channel size |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Target channel size |

---

## Known Limitations

- **Rebalancing uses Router SendPaymentV2** with `outgoing_chan_id` and
  `last_hop_pubkey` to force circular paths. This requires LND 0.15+.

- **Peer diversity scoring** is currently a 0.5 placeholder. Real
  implementation would traverse the graph to check for shared peers.

- **1ML API** can be unreliable. The advisor falls back to local graph
  analysis if 1ML is down.

- **Dashboard requires cron** to have run at least once for routing
  data, rebalance history, and fee update sections to populate.

---

## Logging

All log output goes to `logs/ln_operator.log` (rotating, 5MB × 5 backups).
Terminal shows warnings and errors only. Full debug detail in the log file.

```bash
# Watch logs in real time
tail -f logs/ln_operator.log
```

---

## License

MIT
