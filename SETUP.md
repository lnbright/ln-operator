# LN Operator — Setup Guide

Lightning Network channel management and investment advisor for your LND node.
Runs on the same host as LND.

---

## Architecture

```
LND host
├── LND              — REST API on localhost:9000
├── LN Operator      — CLI + cron-driven pipeline
│   ├── Python engine     — fees, rebalancing, peer scoring, routing sync
│   ├── SQLite database   — historical tracking, channel maturity, fee pins
│   └── Flask dashboard   — port 4000, bound to a private/tailnet IP
└── Telegram (optional)   — pipeline notifications and alerts

Backup host (separate machine, reachable over the tailnet)
└── receives channel.backup over rsync/SSH — destination configured
    via BACKUP_* keys in .env
```

---

## Prerequisites

- LND running and synced on the same host
- Python 3.9+
- Telegram bot token + chat ID (optional — for pipeline notifications and alerts)
- `qrencode` (optional — for terminal QR codes in the `plan` deposit-address step)

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
venv/bin/pip install -r requirements.txt
```

Dependencies: `requests`, `python-dotenv`, `flask` (dashboard), `psutil`.

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

# Optional — Telegram notifications
# Create a bot via @BotFather, get chat ID via @userinfobot
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional — off-site channel.backup over SSH
# BACKUP_SSH_HOST=backup-host
# BACKUP_SSH_USER=backup-user
# BACKUP_SSH_PORT=22
# BACKUP_DEST_DIR=/path/on/remote/

# Optional — bind address for the dashboard (defaults to 0.0.0.0)
# DASHBOARD_HOST=100.64.0.1
EOF

chmod 600 /home/pi/ln-operator/.env
```

`config.py` auto-loads `.env` via `python-dotenv`, so cron jobs and ad-hoc
invocations both pick it up — no need to `source .env` in your shell.

### 5. Initialise the database

```bash
cd /home/pi/ln-operator
venv/bin/python3 db.py
```

This creates `ln_operator.db` with all tables. `main.py` also calls `init_db()`
on every run, so any future schema additions migrate automatically.

### 6. Test it

```bash
venv/bin/python3 main.py status
```

You should see your node info, channel list with balance bars, and on-chain balance.

---

## Usage

The full command reference and examples live in [README.md](README.md). Quick
summary:

```bash
cd /home/pi/ln-operator

# Automated pipeline (fees → rebalance → sync → healthcheck)
venv/bin/python3 main.py pipeline
venv/bin/python3 main.py pipeline --dry-run

# Interactive
venv/bin/python3 main.py status                 # node + channel overview
venv/bin/python3 main.py plan                   # channel investment planner
venv/bin/python3 main.py history 30             # recent activity from DB

# Manual fee pins (override auto-fees per channel)
venv/bin/python3 main.py set_fee LNBiG 3000 --note "..."
venv/bin/python3 main.py clear_fee LNBiG

# Individual pipeline steps (debug)
venv/bin/python3 main.py adjust_fees [--dry-run]
venv/bin/python3 main.py rebalance_channels [--dry-run] [--force RATIO]
venv/bin/python3 main.py sync_routing
venv/bin/python3 main.py healthcheck

# Skip Telegram on any pipeline command
venv/bin/python3 main.py --no-telegram pipeline
```

---

## Crontab Setup

```bash
crontab -e
```

```cron
# LN Operator — full pipeline every 2 hours
0 */2 * * * cd /home/pi/ln-operator && venv/bin/python3 main.py pipeline 2>&1
```

`config.py` reads `.env` via `python-dotenv` automatically, so no `source .env`
shim is needed. Logs go to `logs/ln_operator.log` (rotating, 5MB × 5 backups —
see `logging_config.py`).

---

## How It Works

The pipeline runs four steps in sequence: **adjust_fees → rebalance_channels →
sync_routing → healthcheck**. See [README.md](README.md) for the full design,
fee formula, and rebalance-budget tiers. Setup-specific notes:

### Fee Management
- Formula: `ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) × (1 - local_ratio)`
  with current defaults of 25–250 ppm. Base fee is always 0.
- Skips LND writes if the new ppm is within 5 ppm of the current value (avoids
  gossip spam).
- Manual pins (`set_fee` / `clear_fee`) override the formula per-channel and
  are stored in the `fee_overrides` table. Logged with reason `manual pin: N ppm`
  vs the auto reason `auto: local_ratio=...`. The dashboard's *Recent Fee Updates*
  card tags each row with a `📌 pin` or `auto` badge in the Source column.

### Rebalancing
- Triggers on channels below 20% local (depleted) and above 80% local (overfull).
- Per-channel fee budget uses a 3-tier system (discovery / proven / deadweight)
  driven by channel maturity and earned fees — not a single global cap.
- Auto-chunks on failure (halves down to 100k sats min). Fallback pairs: if
  source→target has no route, tries the same source against an alternative target.
- Uses LND's Router `SendPaymentV2` with `outgoing_chan_id` so the route is
  pinned to the intended source channel.

### Routing sync
- Pulls new forwarding events from `/v1/switch` with offset-based pagination.
- Detects manual rebalances (e.g. via `lncli`) by scanning `/v1/payments` for
  circular self-payments and importing them into `rebalance_log`.

### Health check
- Snapshots all channel states to `channel_snapshots` and updates
  `channel_maturity` (balanced-time tracking for the rebalance budget).
- Fires alerts on depleted/saturated channels, offline peers, and repeated
  rebalance failures. Sends to Telegram if `TELEGRAM_*` env vars are set.

---

## Configuration

All tuneable values are in `config.py`:

| Setting | Default | What it does |
|---------|---------|-------------|
| `FEE_MIN_PPM` | 25 | Floor fee when channel is full |
| `FEE_MAX_PPM` | 250 | Ceiling fee when channel is depleted |
| `FEE_BASE_MSAT` | 0 | Base fee (0 is modern best practice) |
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Rebalance when local drops below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Rebalance when local exceeds 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max sats moved per attempt (% of capacity) |
| `REBALANCE_DISCOVERY_PPM` | 1000 | Fee budget for new (unproven) channels |
| `REBALANCE_HARD_CAP_PPM` | 1000 | Absolute ceiling for proven channels |
| `REBALANCE_DEADWEIGHT_PPM` | 150 | Budget for zero-revenue channels |
| `REBALANCE_DISCOVERY_DAYS` | 15 | Balanced days before a channel is judged |
| `TREASURY_MIN_RATIO` | 0.025 | Wallet reserve (default 2.5%) |
| `MIN_CHANNEL_SIZE_SATS` | 1,000,000 | Minimum channel size |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Target channel size in `plan` |

---

## Known Limitations

- **Local graph capacity** is unreliable for distant nodes — channel count is
  the more trustworthy signal in candidate scoring.
- **Inbound fees** aren't visible in the local channel graph. Check Amboss
  before opening to a new peer.
- **Dashboard has no auth.** Bind it to a Tailscale/LAN-only IP via
  `DASHBOARD_HOST` in `.env`.
- **Channel opens are manual** — `plan` recommends, you execute via `lncli`.

---

## File Reference

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point — all commands |
| `config.py` | All settings + `.env` loader |
| `engine.py` | Fee management, rebalancing, monitoring, routing sync |
| `advisor.py` | Peer scoring, diversity / centrality, candidate discovery |
| `agent.py` | Optional Claude API helper (not used by current `plan`) |
| `lnd_client.py` | LND REST API client |
| `db.py` | SQLite schema, migrations, query helpers |
| `telegram_bot.py` | Telegram message formatting + sending |
| `backup.py` | Off-site `channel.backup` rsync + DB logging |
| `logging_config.py` | Rotating log setup |
| `dashboard/app.py` | Flask web dashboard (port 4000) |
| `lnd-dashboard.service` | systemd unit for the dashboard |
| `lnd-channel-backup.*` | systemd path/timer/service units for off-site backup |
| `ln_operator.db` | SQLite database (created on first run) |
