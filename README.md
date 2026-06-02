# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee
optimisation, liquidity rebalancing, channel planning, health monitoring,
and alerting — with a web dashboard for real-time visibility.

Built for home node operators running LND on a Raspberry Pi or similar hardware.

---

## Dashboard

A single-page Flask app (port 4000, no auth — Tailscale/LAN only) giving
real-time visibility into node health, liquidity, routing, and profit/loss.

**Sat Flow — where routed sats come from and go to** (in→out pairs by volume,
plus inbound/outbound rankings; 30d / 7d / all-time selector):

![Sat Flow card](docs/screenshots/03-sat-flow.png)

**At-a-glance node health** — sync, channels, Bitcoin backend, watchtowers:

![Node overview](docs/screenshots/01-overview.png)

**Total funds controlled + per-channel health** — balance bars, your/their
fees, 30d revenue, rebalance cost, and net P/L per channel:

![Balance and channel details](docs/screenshots/02-balance-channels.png)

**Routing events + daily fee revenue:**

![Routing events and daily revenue](docs/screenshots/04-routing-revenue.png)

**Rebalance history (auto + manual) + recent fee updates:**

![Rebalance history and fee updates](docs/screenshots/05-rebalance-fees.png)

**Forwarding-failure lost-revenue watch** — dropped forwards split by cause,
with estimated lost fees on empty channels (a rebalance signal):

![Forwarding failures](docs/screenshots/06-forwarding-failures.png)

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
graph call computes diversity (% of their peers that sit outside your 2-hop
reachable set — i.e. would actually expand your graph horizon) and reranks.
Runs only on demand — the per-candidate graph calls are too slow for
the pipeline. Offers to generate a deposit address with QR code at the end.

No external API dependencies — everything from your own LND node.

### Dashboard — Web Interface

Single-page Flask app showing live LND data and historical SQLite data:
node status, watchtower health, channel health with balance bars and
profit/loss, daily revenue chart, sat-flow routing map, rebalance history
(auto + manual), fee updates, forwarding-failure lost-revenue watch,
alerts, payments, and invoices.

The channel table shows local and remote outbound fees side-by-side,
pulled per-channel from `/v1/graph/edge/{chan_id}` so you can see at a
glance whether a peer is undercharging or overcharging relative to you.

The **Sat Flow** card answers "where do routed sats come from and where do
they go?" It reads `forwarding_log` (every routed HTLC records both the
inbound and outbound channel) and shows three views of the same data over a
selectable window (30d / 7d / all time): the top in→out peer **pairs**
ranked by volume routed (with a bar, forward count, and fee earned), plus
ranked **inbound** (where liquidity enters) and **outbound** (where it
leaves) bar-lists. **In** and **Out** dropdowns filter every view to a single
channel by peer alias, so you can drill into one peer ("where do sats coming
in from Boltz go?" or "where did the sats leaving via LNBiG come from?").
Channel ids are resolved to peer aliases from the live channel list; channels
closed since a flow occurred show as raw scids, most visible under "all time".

The watchtower card reports tower count, deactivated count, lifetime
backups delivered, pending/failed counters, and an overall health
badge. It requires `wtclient.active=1` in `lnd.conf` (LND only reads
the config at startup, so add it then restart `lnd`); otherwise the
card shows "wtclient disabled" in red. Note that
`active_session_candidate` is LND's admin flag, not a liveness probe —
a tower may still be backing up state on existing sessions even when
flagged inactive.

Health badge: **red** when wtclient is disabled, no towers are
configured, or any backup has permanently failed; **yellow** when
towers exist but none are active (all deactivated) or the status read
errors; **green** otherwise. Pending is shown as a number but does not
affect the badge — a transient `pending=1` is normal when a session
fills its 1024-update cap and LND negotiates a fresh one, so it is not
treated as a fault. Multiple towers can be configured for failover, but
LND assigns each backup to a single tower rather than mirroring every
update to all of them.

### Status — CLI Overview

Node summary with per-channel balance bars and fee rates (your fees, their fees,
their inbound fees).

---

## Project Structure

```
ln-operator/
├── ln-operator          CLI wrapper — run from anywhere (symlink to /usr/local/bin)
├── main.py              CLI entry — commands, display, orchestration
├── config.py            All tuneable settings
├── engine/              Channel-management engine (package)
│   ├── fees.py             Sigmoid + hysteresis + market-mult recompute
│   ├── rebalance_planner.py  Budget, candidate selection, plan generation
│   ├── rebalance_executor.py Per-plan execution with chunked retry
│   ├── sync.py             Forwarding + manual-rebalance pull from LND
│   └── monitor.py          Channel health report + alerts
├── advisor.py           Peer ranking (tier-segmented, centrality → diversity)
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
git clone https://github.com/lnbright/ln-operator.git
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
(LND, Telegram, backup destination, DB path, dashboard bind).
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
ln-operator sync_routing
ln-operator status
```

### 6. Optional: QR codes in plan command

```bash
sudo apt install qrencode
```

---

## Usage

All commands run through the `ln-operator` wrapper, which executes `main.py`
inside the project virtualenv from any directory. To call it globally:

```bash
sudo ln -s "$PWD/ln-operator" /usr/local/bin/ln-operator
```

Without the symlink, run it from the repo as `./ln-operator <command>`.

```bash
# ── AUTOMATED ──────────────────────────────────────────────
ln-operator pipeline                             # full 2h loop: fees → rebalance → sync → health
ln-operator pipeline --dry-run                   # preview the loop; broadcast & move nothing

# ── PLANNING ───────────────────────────────────────────────
ln-operator plan                                 # read wallet balance, rank candidate peers to open to
ln-operator plan --min-channel 3000000           # override the minimum channel size (sats)
ln-operator plan --treasury 0.01                 # override the wallet-reserve (treasury) ratio

# ── REBALANCING ────────────────────────────────────────────
ln-operator rebalance_channels                   # auto-rebalance using the 20/80 thresholds
ln-operator rebalance_channels --dry-run         # show channel status + per-force-level scenarios
ln-operator rebalance_channels --force           # ignore thresholds, target 50% local on all
ln-operator rebalance_channels --force 0.4       # ignore thresholds, target 40% local on all

# ── FEE ADJUSTMENT ─────────────────────────────────────────
ln-operator adjust_fees                          # recompute + broadcast outbound fees (one pipeline step)
ln-operator adjust_fees --dry-run                # show which fees would change; broadcast nothing
ln-operator overwrite_fee <alias|chan_id> <ppm> [--note "..."]  # pin a channel's fee, suppressing auto
ln-operator clear_fee <alias|chan_id>            # remove a pin; auto resumes next pipeline run
# Pins live in the fee_overrides table, are shown by `status`, and tagged
# 📌 pin in the dashboard's Recent Fee Updates card. See docs/fee-formula.md.

# ── MONITORING ─────────────────────────────────────────────
ln-operator status                               # per-channel balance bars + fee rates (yours/theirs/inbound)
ln-operator history [days]                       # forwarding + rebalance activity from the database
ln-operator healthcheck                          # snapshot channel state, update maturity, fire alerts

# ── SYNC & SIGNALS ─────────────────────────────────────────
ln-operator sync_routing                         # pull new forwards + manual rebalances from LND into SQLite
ln-operator recompute_signals                    # nightly job — refresh slow per-channel market signals

# ── OFF-SITE BACKUP (run by systemd, not invoked manually) ──
ln-operator backup [--trigger path|timer|manual] # rsync channel.backup to the configured BACKUP_SSH_HOST
```

### Crontab

```
# Fast loop — fees, rebalances, sync, healthcheck
0 */2 * * * cd /path/to/ln-operator && ./ln-operator pipeline 2>&1

# Nightly — refresh slow market signals (market multiplier per channel)
15 3 * * * cd /path/to/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
```

### Dashboard

```bash
venv/bin/python3 dashboard/app.py    # or install services/lnd-dashboard.service
```

Access at `http://YOUR_IP:4000`. No auth — use Tailscale or LAN only.

### Services (systemd)

Unit files for the dashboard, off-site channel-backup, and HTLC monitor live
in [`services/`](services/). **They are not portable as-is** — each one
hardcodes `User=pi` and `/home/pi/ln-operator/...` paths plus an
`EnvironmentFile`. Edit the `User=`, `WorkingDirectory=`, `ExecStart=`, and
`EnvironmentFile=` lines in every unit to match your host **before** enabling
them:

```bash
$EDITOR services/lnd-dashboard.service        # repeat for each unit you use
sudo cp services/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lnd-dashboard.service
```

The daily-check AI agent is *not* a systemd service — it runs from cron and is
off by default. See [docs/daily-check.md](docs/daily-check.md).

---

## Security

This tool talks to your LND node and serves your node's financials. A few
things to get right before you run it — especially if you're not on a
single-operator home network.

### Dashboard exposure (no built-in auth)

The dashboard has **no authentication** and runs on Flask's development
server. Security is entirely the bind address: it defaults to `127.0.0.1`
(loopback) and is set via `DASHBOARD_BIND_IP` in `.env`.

- **Home / tailnet use:** bind to `127.0.0.1` or your Tailscale IP. Never
  bind to `0.0.0.0` on a WAN-facing host — anyone who reaches the port sees
  balances, channel points, peer pubkeys, and routing/payment history.
- **Exposing it more widely:** don't point the dev server at the internet.
  Put it behind a reverse proxy (nginx/Caddy) that terminates TLS and adds
  HTTP Basic Auth or mTLS, and bind the app itself to loopback so only the
  proxy can reach it.

### Use a least-privilege macaroon

`LND_MACAROON` defaults to `admin.macaroon`, which grants **total node
control** (move funds, force-close channels, etc.). The tool does not need
that. Bake a custom macaroon with only what it uses and point `LND_MACAROON`
at it:

```bash
lncli bakemacaroon \
  info:read \
  offchain:read offchain:write \
  onchain:read \
  address:write \
  invoices:read invoices:write \
  peers:read \
  --save_to ~/ln-operator.macaroon
```

This still allows rebalancing (`offchain:write` covers Router send and channel
policy updates) and generating deposit addresses (`address:write`), but **not**
moving on-chain funds, opening/closing channels, or baking further macaroons.

### Daily-check AI agent (off by default)

`scripts/daily-check.sh` can run an **autonomous Claude agent** that edits
code, `git commit`s, and `git push origin main` unattended, with whatever
macaroon is in your environment. It is **disabled by default** and must be
explicitly opted into:

```bash
# In the cron line or environment:
LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1
```

If you enable it, give it a **read-only** macaroon so it cannot move funds
even though the prompt instructs it to stay read-only — bake one with only
`info:read offchain:read onchain:read peers:read invoices:read` and point the
script at it via `DAILY_CHECK_LND_MACAROON`. Also note it needs the `claude`
CLI at `/usr/bin/claude` and pins a specific model. Review
`scripts/daily-check-prompt.md` (which authorizes the auto-commit/push) before
turning it on.

### Off-site backup host key

The channel-backup upload uses SSH with `StrictHostKeyChecking=accept-new`
(trust-on-first-use). For a backup that contains `channel.backup`, pre-pin the
host key instead: `ssh-keyscan -H backup-host >> ~/.ssh/known_hosts` before the
first run.

---

## Documentation

The deep-dive docs live in [`docs/`](docs/) — kept out of this README so it stays
skimmable. Full index: [docs/README.md](docs/README.md).

**Fees**
- [Fee Formula](docs/fee-formula.md) — layered outbound-fee calculation + manual pins
- [Fee Engine Internals](docs/fee-engine-internals.md) — cadence, the four layers, hysteresis, corner cases

**Liquidity**
- [Rebalance Budget](docs/rebalance-budget.md) — single-signal budget, failure escalation, chunking, fallback pairs
- [Plan Command](docs/plan-command.md) — tier-segmented peer ranking (centrality → diversity)

**Operations**
- [Daily Check](docs/daily-check.md) — the optional, off-by-default AI health-check agent

**Reference**
- [Configuration](docs/configuration.md) — every `config.py` knob and its default
- [Data Flow](docs/data-flow.md) — how LND data lands in SQLite and feeds each consumer
- [Alerts](docs/alerts.md) · [Logging](docs/logging.md) · [Channel Backup](docs/channel-backup.md) · [Known Limitations](docs/known-limitations.md)

---

## License

MIT
