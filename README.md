# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee optimisation,
rebalancing, investment planning, and monitoring — with a web dashboard for visibility.

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
own rebalance budget based on its earnings history — new channels get a discovery budget,
proven channels get a budget proportional to what they earn, deadweight channels get minimal
spend. See *Rebalance Budget* section below.

**3. sync_routing** — Syncs new forwarding events from LND into the local SQLite database
using offset-based pagination. Never fetches the same event twice. The database is the
single source of truth for all routing revenue data shown in the dashboard and CLI.

**4. healthcheck** — Snapshots all channel states, updates the channel maturity tracker,
fires alerts for depleted channels, offline peers, or repeated failures.

### Investment Advisor (on-demand)

When you have sats to deploy, run `main.py invest <amount>` and get a
research-backed recommendation:

**Python engine (60%):**
- Calculates treasury reserve from historical rebalancing costs
- Checks on-chain fee environment via mempool.space
- Pulls the full network graph from your LND node
- Classifies existing channels as hubs (500+ channels) or mid-tier
- Applies portfolio strategy: no hubs → recommend hub first; 1 hub → mix;
  2+ hubs → mid-tier only for diversification
- Scores candidates by channel count, diversity, and centrality
- Shortlists top 10 candidates for the agent to evaluate

**Claude agent (10%):**
- Searches Amboss and 1ML for each of the 10 shortlisted candidates
- Finds real total capacity and average channel size (local graph data is
  unreliable for capacity — your graph view improves as you add more channels)
- Triangulates between sources — if Amboss and 1ML agree, use it; if they
  differ, use the conservative figure
- Disqualifies candidates that don't accept external channel opens (e.g. Binance)
- Recommends top 3 with reasoning, suggests final allocation respecting min channel size
- Interactive follow-up Q&A: "find me alternatives", "why this peer?", etc.

**SQLite database (30%):**
- Stores all historical data: forwarding events, rebalances, fee changes, snapshots
- Powers the rebalance budget tiers (proven vs discovery vs deadweight)
- Powers the treasury reserve calculation (avg monthly rebalancing costs × 3)
- Powers the dashboard charts and per-channel performance metrics

### Web Dashboard

Single-page Flask app combining live LND data with historical SQLite data:

- Node status, sync state, block height, uptime
- Channel health table with balance bars, 30d revenue, rebalancing costs,
  net profit, and tier badges (proven / discovery / deadweight)
- Channel liquidity split — sendable vs receivable as a two-tone bar
- Daily fee revenue chart (30 days, from forwarding_log)
- Rebalance history with fees paid and success/failure
- Recent fee updates with direction (↑/↓) and local ratio at time of change
- Recent alerts (depleted channels, offline peers, failures)
- Recent payments and invoices

---

## Architecture

```
60% Deterministic Python
    Fee calculation using linear formula based on balance ratio.
    Rebalance budget per channel based on earnings history.
    Peer scoring using local LND graph (channel count, diversity, centrality).
    Portfolio-aware allocation (hub vs mid-tier strategy).
    All decisions are formula-driven with configurable thresholds in config.py.

30% SQLite Database
    Single source of truth for all routing fee data.
    Historical tracking: forwarding events, rebalances, fee changes, snapshots.
    Channel maturity tracking (how long each channel has been balanced).
    Sync state (offset cursor for LND forwarding history — no duplicates).
    Powers rebalance budget tiers and treasury reserve calculation.

10% Claude API (optional)
    Researches shortlisted peers via web search (Amboss, 1ML).
    Finds real capacity and average channel size that local graph can't reliably provide.
    Disqualifies peers that don't accept external channels.
    Suggests final allocation respecting minimum channel size.
    Falls back gracefully to a local summary if no API key is configured.
```

---

## Project Structure

```
ln-operator/
├── main.py              CLI entry point — all commands, argument parsing
├── config.py            All tuneable settings in one place
├── engine.py            Fee management, rebalancing, monitoring, forwarding sync
├── advisor.py           Investment advisor — graph analysis, peer scoring, allocation
├── agent.py             Claude API — web search for peer research, allocation suggestion
├── lnd_client.py        LND REST API client (all LND communication goes here)
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
- **Anthropic API key** (optional — for AI investment research)
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

# Optional — Claude API for investment advisor research
# Get a key at https://console.anthropic.com
ANTHROPIC_API_KEY=

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

This fetches all historical forwarding events from LND into the database.
Run it once after install — subsequent runs only fetch new events.

### 6. Test

```bash
venv/bin/python3 main.py status
```

### 7. LND file permissions

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
# ── AUTOMATED — full pipeline, designed for crontab ────────
venv/bin/python3 main.py pipeline              # run all steps in sequence
venv/bin/python3 main.py pipeline --dry-run    # preview without executing

# ── FEATURES — interactive tools ───────────────────────────
venv/bin/python3 main.py invest 5000000              # investment advisor
venv/bin/python3 main.py invest 5000000 --min-channel 2000000  # override min size
venv/bin/python3 main.py status                      # quick node overview
venv/bin/python3 main.py history                     # last 30 days of activity
venv/bin/python3 main.py history 7                   # last 7 days

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

### Dashboard

Install as a systemd service (see `dashboard/lnd-dashboard.service`), or run directly:

```bash
venv/bin/python3 dashboard/app.py
```

Access at `http://YOUR_TAILSCALE_IP:4000`.

---

## How the Fee Formula Works

Each channel's fee rate is set dynamically based on local balance ratio:

```
ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) × (1 - local_ratio)
```

| Local balance | Fee rate | Purpose |
|---------------|----------|---------|
| 80-100% (full) | 50-140 ppm | Low fees attract routing, earn while naturally draining |
| 40-60% (balanced) | 225-275 ppm | Mid fees, healthy state |
| 0-20% (depleted) | 410-500 ppm | High fees protect remaining liquidity and reputation |

Fees only update if the change is >5 ppm (avoids gossip network spam).
Base fee is always 0 — modern best practice, most pathfinding penalises non-zero base fees.

When you open a new channel, just use LND's default fees. The next pipeline run
will set fees automatically based on the channel's balance ratio.

---

## How the Rebalance Budget Works

Each channel gets its own rebalance fee budget based on its track record.
The budget system prevents the most common home-node failure mode: paying more
to rebalance a channel than the channel ever earns in routing fees.

### Discovery (new channels, < 30 balanced days)

Budget: **150 ppm**. The channel starts at 100% local (you funded it), gets
rebalanced to 50%, and has 30 days in a balanced state to prove it can route.
The clock only ticks when the local ratio is between 30-70% — time spent
depleted doesn't count.

### Proven (30+ balanced days, earns routing fees)

Budget: **50% of average earned ppm**. A channel earning 300 ppm gets a
150 ppm rebalance budget. You never spend more rebalancing than you earn.
Floor: 50 ppm. Ceiling: 500 ppm (hard cap).

### Deadweight (30+ balanced days, zero revenue)

Budget: **50 ppm**. The channel had a fair chance while balanced and
earned nothing. The investment advisor will flag it as a close candidate.

---

## How the Investment Advisor Works

### Step 1 — Portfolio classification

The advisor reads your existing channels and calls LND's graph API to get
the channel count for each current peer. Nodes with 500+ channels are classified
as hubs; others as mid-tier.

### Step 2 — Graph traversal

Calls `describe_graph()` on your local LND node to get the full network graph.
Builds a candidate list of up to 250 nodes you're not already connected to,
ranked by channel count. Assigns tiers: rank 1-50 = hub, rank 51-250 = mid-tier.

Note: local graph capacity numbers can be unreliable for distant nodes since
your gossip view depends on your channel connections. As you add more channels
your graph becomes more complete. Channel count and topology data are reliable.

### Step 3 — Portfolio strategy

Based on how many hubs you already have:
- **0 hubs** → shortlist top 10 hubs. You need a routing backbone first.
- **1 hub** → shortlist 2 top hubs + 8 mid-tier. Mix of reliability and diversity.
- **2+ hubs** → shortlist top 10 mid-tier. Diversify away from hub competition.

### Step 4 — Graph enrichment

For the shortlisted 10 candidates, calls `get_node_info()` to get their peer
list and fee policies. Computes diversity score (what fraction of their peers
you're not already connected to) and average fee rate.

### Step 5 — Agent research (Claude API)

Sends the shortlist to Claude with web search enabled. The agent:
1. Searches Amboss and 1ML for each candidate to find real capacity and avg channel size
   (the quality metric the local graph can't reliably provide)
2. Cross-checks both sources — uses the more conservative figure if they differ
3. Disqualifies nodes that don't accept external channel connections (e.g. some exchanges)
4. Recommends top 3 by combining engine score (topology) with real avg channel size (quality)
5. Suggests final allocation respecting the minimum channel size setting

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
            ├─ Dashboard: Recent Routing Events table
            ├─ Dashboard: Daily Fee Revenue chart (30d)
            ├─ Dashboard: Revenue 30d / Net 30d per channel
            ├─ CLI: main.py history
            ├─ Rebalance budget: get_channel_earned_ppm()
            └─ Treasury reserve: get_avg_monthly_fee_revenue()
```

The sync uses `last_offset_index` from LND's API as a pagination cursor.
Running `sync_routing` manually or via cron always picks up from where it left off.
No duplicates possible — LND's offset guarantees each event is fetched at most once.

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
| `REBALANCE_REVENUE_RATIO` | 0.50 | Proven channels: budget = earned_ppm × this |
| `REBALANCE_DEADWEIGHT_PPM` | 50 | Budget for channels that had their chance |
| `REBALANCE_DISCOVERY_DAYS` | 30 | Days balanced before judging a channel |
| `TREASURY_MIN_RATIO` | 0.10 | Keep at least 10% of investment as reserve |
| `MIN_CHANNEL_SIZE_SATS` | 1,000,000 | Absolute minimum channel size |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Default minimum (overridable via CLI) |

---

## Known Limitations

**Local graph capacity is unreliable for distant nodes.** Your LND node's graph view
depends on gossip propagation from your channel peers. With few channels, you may see
a fraction of a large node's real capacity. This improves significantly as you open
more channels. The investment advisor accounts for this by having the Claude agent
look up real capacity from Amboss/1ML rather than trusting local numbers.

**Rebalancing requires Router SendPaymentV2** with `outgoing_chan_id` to force the
circular route. This requires LND 0.15+.

**Dashboard shows empty routing sections** until `sync_routing` has run at least once.
Run `venv/bin/python3 main.py sync_routing` after install.

---

## Logging

All log output goes to `logs/ln_operator.log` (rotating, 5MB × 5 backups).
Terminal shows only warnings and errors. Full INFO/DEBUG detail in the log file.

```bash
# Watch logs in real time
tail -f logs/ln_operator.log
```

A typical pipeline run produces log entries like:

```
INFO  [main] pipeline starting
INFO  [engine] fees: ACINQ ↑ 120→480 ppm (local 2%)
INFO  [engine] rebalance: 1 depleted, 0 overfull — no overfull channel to pair with
INFO  [engine] sync_routing: 3 new event(s) saved (offset 50)
INFO  [engine] healthcheck: 1 active, 0 inactive — overall local 2%
WARNING [engine] healthcheck alert [channel_depleted]: ACINQ at 2% local
INFO  [main] pipeline complete in 2.3s — fees:1 rebalances:0 events:3 alerts:1
```

---

## Roadmap

- Flask dashboard investment approval flow — Telegram link to approve/reject channel open recommendations
- Backup automation — auto-backup `channel.backup` on every channel change
- On-chain fee watcher — alert when fees drop below threshold (good time to open)
- Tests — pytest suite for core logic functions

---

## License

MIT
