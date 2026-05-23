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
git clone https://github.com/YOUR_USERNAME/ln-operator.git
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
0 */2 * * * cd /path/to/ln-operator && venv/bin/python3 main.py pipeline 2>&1
```

### Dashboard

```bash
venv/bin/python3 dashboard/app.py    # or install lnd-dashboard.service
```

Access at `http://YOUR_IP:4000`. No auth — use Tailscale or LAN only.

---

## Fee Formula

```
ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) × (1 - local_ratio)
```

| Local | Fee | |
|-------|-----|---|
| 100% | 50 ppm | Cheap — attract routing |
| 50% | 275 ppm | Balanced |
| 0% | 500 ppm | Protect liquidity |

Updates only when change >5 ppm. Base fee always 0.

### Manual Fee Pins

The auto-fee formula can be overridden per-channel with `set_fee`. A pinned
channel keeps its fixed ppm across every pipeline run until you clear the pin
with `clear_fee`. Pins are stored in the `fee_overrides` table and are
honored by both the `pipeline` and `adjust_fees` commands.

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

**Discovery** (< 15 balanced days) — 1000 ppm. New channel proving itself.
Balanced time only counts when local ratio is 30-70%.

**Proven** (15+ balanced days, earns fees) — 50% of earned ppm.
Floor: 150 ppm. Ceiling: 1000 ppm.

**Deadweight** (15+ balanced days, zero revenue) — 150 ppm.
Consider closing.

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

| Setting | Default | |
|---------|---------|---|
| `FEE_MIN_PPM` | 50 | Fee when full |
| `FEE_MAX_PPM` | 500 | Fee when depleted |
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Trigger below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Trigger above 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max per attempt |
| `REBALANCE_DISCOVERY_PPM` | 1000 | New channel budget |
| `REBALANCE_HARD_CAP_PPM` | 1000 | Proven ceiling |
| `REBALANCE_DEADWEIGHT_PPM` | 150 | Zero-revenue budget |
| `REBALANCE_DISCOVERY_DAYS` | 15 | Days before judging |
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

## Known Limitations

- **Local graph capacity** is unreliable for distant nodes. Channel count is reliable.
- **Inbound fees** not visible in local graph — check Amboss before opening channels.
- **Dashboard has no auth** — Tailscale or LAN only.
- **Channel opens are manual** — plan recommends, you execute via `lncli`.

---

## License

MIT
