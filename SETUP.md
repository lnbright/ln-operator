# LN Operator — Setup Guide

Lightning Network channel management and investment advisor for your LND node.
Runs on the same host as LND.

---

## Architecture

```
LND host
├── LND              — REST API on localhost:9000
├── LND Dashboard    — port 4000 (bound to a private/tailnet IP)
└── LN Operator      — CLI tool (no port, runs on-demand)
    ├── 60% Python engine    — fees, rebalancing, peer scoring
    ├── 30% SQLite database  — historical tracking & learning
    └── 10% Claude API agent — plain-English summaries

Backup host (separate machine, reachable over the tailnet)
└── receives channel.backup over rsync/SSH — destination configured
    via BACKUP_* keys in .env
```

---

## Prerequisites

- LND running and synced on the same host
- Python 3.9+ (already installed if dashboards work)
- Anthropic API key (optional — tool works without it, just no AI summaries)
- Telegram bot token + chat ID (optional — for notifications)

---

## Installation

### 1. Create directory and copy files

```bash
mkdir -p /home/pi/ln-operator
cd /home/pi/ln-operator

# Copy all .py files here:
#   config.py, db.py, lnd_client.py, telegram_bot.py,
#   engine.py, advisor.py, agent.py, main.py
```

### 2. Set up Python environment

```bash
cd /home/pi/ln-operator
python3 -m venv venv
venv/bin/pip install requests
```

That's it — `requests` is the only external dependency. `sqlite3` and `json` are
in the Python standard library.

### 3. File permissions for LND

The tool needs to read LND's macaroon and TLS cert. If you already run the
LND dashboard as `pi`, these permissions are already set:

```bash
# Skip this if your LND dashboard already works as pi user
sudo usermod -aG lnd pi
sudo chmod g+r /home/lnd/tls.cert
sudo chmod g+r /home/lnd/data/chain/bitcoin/mainnet/admin.macaroon
sudo chmod g+x /home/lnd /home/lnd/data /home/lnd/data/chain \
    /home/lnd/data/chain/bitcoin /home/lnd/data/chain/bitcoin/mainnet
```

### 4. Environment variables

Create a `.env` file (or export these in your shell / crontab):

```bash
cat > /home/pi/ln-operator/.env << 'EOF'
# Required — LND connection (defaults should work if LND is local)
LND_REST_URL=https://127.0.0.1:9000
LND_CERT=/home/lnd/tls.cert
LND_MACAROON=/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon

# Optional — Claude API for the 10% agent layer
# Get a key at https://console.anthropic.com
ANTHROPIC_API_KEY=

# Optional — Telegram notifications
# Create a bot via @BotFather, get chat ID via @userinfobot
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF

chmod 600 /home/pi/ln-operator/.env
```

### 5. Initialise the database

```bash
cd /home/pi/ln-operator
source .env && export $(grep -v '^#' .env | xargs)
venv/bin/python3 db.py
# Output: Database initialised at /home/pi/ln-operator/ln_operator.db
```

### 6. Test it

```bash
cd /home/pi/ln-operator
source .env && export $(grep -v '^#' .env | xargs)
venv/bin/python3 main.py status
```

You should see your node info, channel list with balance bars, and on-chain balance.

---

## Usage

Always run from the project directory with the venv:

```bash
cd /home/pi/ln-operator
source .env && export $(grep -v '^#' .env | xargs)
```

### Quick node status
```bash
venv/bin/python3 main.py status
```

### Investment advisor
```bash
# "I have 5M sats to deploy — what should I do?"
venv/bin/python3 main.py invest 5000000
```

### Update channel fees (dynamic, based on balance ratios)
```bash
venv/bin/python3 main.py fees              # apply changes
venv/bin/python3 main.py fees --dry-run    # preview only
```

### Rebalance depleted/overfull channels
```bash
venv/bin/python3 main.py rebalance              # execute
venv/bin/python3 main.py rebalance --dry-run    # preview only
```

### Health monitor (check for alerts)
```bash
venv/bin/python3 main.py monitor
```

### Combined cron job (fees → rebalance → monitor)
```bash
venv/bin/python3 main.py cron              # execute all
venv/bin/python3 main.py cron --dry-run    # preview all
```

### View recent history from database
```bash
venv/bin/python3 main.py history        # last 30 days
venv/bin/python3 main.py history 7      # last 7 days
```

### Skip Telegram on any command
```bash
venv/bin/python3 main.py --no-telegram invest 5000000
venv/bin/python3 main.py --no-telegram cron
```

---

## Crontab Setup

To run automated fee updates and health monitoring:

```bash
crontab -e
```

Add these lines:

```cron
# LN Operator — load env vars and run
# Fees + rebalance + monitor every 30 minutes
*/30 * * * * cd /home/pi/ln-operator && source .env && export $(grep -v '^\#' .env | xargs) && venv/bin/python3 main.py cron >> /home/pi/ln-operator/cron.log 2>&1

# Rotate log weekly (keep it from growing forever)
0 0 * * 0 truncate -s 0 /home/pi/ln-operator/cron.log
```

---

## How It Works

### Fee Management (every cron run)
1. Reads each channel's local/remote balance ratio
2. Calculates optimal fee: low local → high fees (protect), high local → low fees (attract)
3. Formula: `ppm = 50 + (500 - 50) × (1 - local_ratio)`
4. Only updates if fee changed by >5 ppm (avoids gossip spam)
5. Logs every change to SQLite

### Rebalancing (every cron run)
1. Finds channels below 20% local (depleted) and above 80% local (overfull)
2. Pairs them: push sats from overfull → depleted via circular payment
3. Never pays more than 250 ppm to rebalance
4. Logs every attempt (success or failure) to SQLite

### Investment Advisor (on-demand)
1. Gathers: node state, channel balances, on-chain fees, historical performance
2. Calculates treasury reserve (max of 10% or 3 months rebalancing costs)
3. Analyses existing channels for issues (undersized, inactive, unprofitable)
4. Scores candidate peers from 1ML + local graph (capacity, channels, centrality)
5. Allocates budget: upsize undersized first, then open new channels
6. Sends compact JSON to Claude API for plain-English summary
7. Reports via Telegram

### Monitoring (every cron run)
1. Snapshots all channel states to SQLite
2. Syncs forwarding history (routing fees earned)
3. Alerts on: depleted channels, saturated channels, offline peers
4. Sends alerts to Telegram

---

## Configuration

All tuneable values are in `config.py`:

| Setting | Default | What it does |
|---------|---------|-------------|
| `FEE_MIN_PPM` | 50 | Floor fee when channel is full |
| `FEE_MAX_PPM` | 500 | Ceiling fee when channel is depleted |
| `FEE_BASE_MSAT` | 0 | Base fee (0 is modern best practice) |
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Rebalance when local drops below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Rebalance when local exceeds 80% |
| `REBALANCE_MAX_FEE_PPM` | 250 | Max fee to pay for rebalancing |
| `TREASURY_MIN_RATIO` | 0.10 | Keep at least 10% as reserve |
| `MIN_CHANNEL_SIZE_SATS` | 1,000,000 | Minimum channel size |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Target channel size |

---

## Known Limitations

### Rebalancing uses basic sendpayment
The current implementation uses LND's `/v1/channels/transactions` endpoint,
which doesn't support `outgoing_chan_id`. This means LND picks the route
freely rather than being forced through the intended source channel.

Full circular rebalancing requires the **Router RPC** (`SendPaymentV2` with
`outgoing_chan_id` parameter). This is a planned enhancement — the database
schema and logging are already built for it.

### Peer diversity scoring is a placeholder
The diversity score (how much a new peer improves your graph reach) currently
defaults to 0.5. Real implementation would traverse the graph to check for
shared peers between you and the candidate. The scoring framework is ready
for this enrichment.

### External API dependency
1ML's API can be flaky. If it fails, the tool falls back to local graph
analysis only. This is handled gracefully — you'll see a warning in the
terminal but the plan will still be generated.

---

## File Reference

| File | Purpose |
|------|---------|
| `config.py` | All settings in one place |
| `db.py` | SQLite schema + query helpers (30% layer) |
| `lnd_client.py` | LND REST API client |
| `telegram_bot.py` | Telegram message formatting + sending |
| `engine.py` | Fee management, rebalancing, monitoring (60% layer) |
| `advisor.py` | Investment advisor — peer scoring, budget allocation (60% layer) |
| `agent.py` | Claude API integration for summaries (10% layer) |
| `main.py` | CLI entry point — ties everything together |
| `ln_operator.db` | SQLite database (created on first run) |
