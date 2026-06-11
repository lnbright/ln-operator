# ⚡ LN Operator

Automated Lightning Network node management for LND. Handles channel fee
optimisation, liquidity rebalancing, channel planning, health monitoring,
and alerting — with a web dashboard for real-time visibility.

Built for home node operators running LND on a Raspberry Pi or similar hardware.

Visit **[www.lnbright.com](https://www.lnbright.com)** for more info.

---

## What It Does

### Pipeline — Automated Channel Management

Runs unattended on a cron schedule. Four steps execute in sequence:

**1. Rebalancing** — Moves sats from overfull channels (>80% local) to depleted
ones (<20% local) via circular self-payments.
Each channel's "rebalance fee budget" is its refill history with failure escalation, **capped
by a profitability gate**: after a period of observation, we never
pay more to refill than the channel can earn back. Channels that can't be
profitably refilled are flagged as a capital decision. When rebalancing, if
the full amount can't route, it halves and retries several times, down to 100k sats; if one
source→target pair has no route, it tries alternatives before giving up. Optional, off by default: a node-level
**inbound-fee** ladder that pulls organic refill with a negative inbound fee
instead of paying for a circular rebalance.

**2. Automatic Fee adjustment** — Sets each channel's fee rate based on its local/remote
balance ratio. Full channels get low fees to attract routing;
depleted channels get high fees to protect remaining liquidity and to recoup
refill cost. Runs *after* rebalancing so each refilled channel is priced off the
cost it actually paid this run, not the previous cycle's anchor. On top of this, a per-channel **market
multiplier** — a slow demand signal that nudges the fee up while a channel
forwards daily and back down when it goes quiet, with an immediate up-only
bump when a depleted channel starts *dropping* forwarding requests. There is a **refill-cost floor** which is a *soft ratchet* — it decays toward the market-clearing fee while a
channel sits truly idle (no forwards *and* no dropped forwards — senders
attempting at the current price count as proof the price is right) so it never
gets priced out and stranded, and re-arms only on a fresh refill. Base fee is
always zero. Fee changes are rate-limited by hysteresis (tolerance + cooldown)
so the node doesn't spam gossip.

**3. Manual rebalancing sync** — Pulls new forwarding events from LND into SQLite, to detect manual rebalances done via `lncli` and
imports them so the dashboard tracks all rebalancing activity.

**4. Health check** — Snapshots channel states, updates maturity tracking, fires
alerts for depleted channels, offline peers, and repeated rebalance failures.

### HTLC Failure Monitor — the Lost-Revenue Signal

An always-on systemd daemon subscribed to LND's HTLC event stream, recording
every forward the node **dropped** (and why) into `forward_fail_log`. This is the node's "demand-you-couldn't-serve signal", and
it drives parts of the fee setting: the fast-drain fee bump, the floor-decay gate, the
dashboard's lost-revenue watch, and the daily check's capital suggestions
(a channel dropping millions of sats at its advertised price needs more
liquidity, not a better price).

### Channel Backup — Off-Site, Event-Driven

A systemd path unit watches `channel.backup` and rsyncs it to a backup host the
moment it changes, with a 2h timer as heartbeat. Attempts are logged and the
dashboard shows a freshness badge.

### Plan — Channel Investment Planner

Reads your on-chain wallet balance from LND and calculates how many channels you
can afford after deducting anchor reserves, treasury, and on-chain fees. Shows the
top 10 candidate peers per tier (hub / mid-tier / small) using a two-stage rank:
centrality (channels + capacity) prefilters within each tier, then a live LND
graph call computes diversity (% of their peers that sit outside your 2-hop
reachable set — i.e. would actually expand your graph horizon) and reranks.
Offers to generate a deposit address with QR code at the end.

No external API dependencies — everything from your own LND node.

### Dashboard — Web Interface

Single-page Flask app (port 4000, VPN/LAN only) showing live LND data and
historical SQLite data: node status, watchtower health, per-channel balance bars
and profit/loss, daily revenue chart, the "Sat Flow routing" map, rebalance history
(auto + manual), fee updates, the forwarding-failure lost-revenue watch, alerts,
payments, and invoices.

See the **[Dashboard deep dive](docs/dashboard.md)** for a card-by-card tour with
screenshots, the Sat Flow drill-downs, and the watchtower health-badge logic.

### Status — CLI Overview

Node summary with per-channel balance bars and fee rates (your fees, their fees,
their inbound fees).

### Agent - daily review (Optional)

Agent skill run every day reviewing the last 24hr of logs and data. Lands bug fixes and suggests actions to the user where human decision is needed.  

---

## Project Structure

```
ln-operator/
├── ln-operator          CLI wrapper — run from anywhere (symlink to /usr/local/bin)
├── main.py              CLI entry — commands, display, orchestration
├── config.py            All tuneable settings
├── engine/              Channel-management engine (package)
│   ├── fees.py             Sigmoid + hysteresis + soft-floor ratchet + market-mult/fast-drain
│   ├── rebalance_planner.py  Budget + profitability gate, candidate selection, plan generation
│   ├── rebalance_executor.py Per-plan execution with chunked retry
│   ├── liquidity_policy.py   Node-level decision ladder (rebalance / inbound discount / structural)
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
├── scripts/             Operator helpers (agent daily-check, etc.)
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
ln-operator pipeline                             # full 2h loop: rebalance → fees → sync → health
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
ln-operator manual_rebalance <src> <tgt> <amount_sats> <max_ppm>  # pin ONE pair, bypass the gate (recorded as manual)
ln-operator manual_rebalance Boltz bfx-lnd0 1778389 773 --dry-run # preview that exact pair; move nothing
# manual_rebalance forces the source→target you name (alias or chan_id), skipping
# both the ratio-based pair selection AND the profit/structural gate — the only way
# to refill a channel flagged STRANDED. Streams the engine's INFO logs live, auto-
# chunks down to 100k on failure, and writes a triggered_by='manual' row (blue badge
# on the dashboard). A success re-anchors last_refill_ppm like any rebalance.

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

```cron
# Fast loop — fees, rebalances, sync, healthcheck
0 */2 * * * cd /path/to/ln-operator && ./ln-operator pipeline 2>&1

# Nightly — refresh slow market signals (market multiplier per channel)
15 3 * * * cd /path/to/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
```

These two lines are all the automation the node needs. The optional AI
daily-check agent is a **separate, opt-in** cron line (off by default — see the
[Security](#security) section and [docs/daily-check.md](docs/daily-check.md)
before enabling). It is gated by an env flag set **on the cron line itself**, and
should run with its own read-only macaroon:

```cron
# Daily at 09:00 — optional AI health-check agent (OFF unless the flag is set).
# The opt-in flag and read-only macaroon are set inline so they apply to this
# job only. Use absolute paths — cron has a minimal PATH and no shell profile.
0 9 * * * LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1 \
  DAILY_CHECK_LND_MACAROON=/home/youruser/.lnd-macaroons/ln-operator-readonly.macaroon \
  /path/to/ln-operator/scripts/daily-check.sh >> /path/to/ln-operator/logs/daily-check.log 2>&1
```

Drop the `LN_OPERATOR_ENABLE_AI_DAILY_CHECK=1` (or the whole line) to disable it —
without the flag the script logs `disabled` and exits 0. The agent needs the
`claude` CLI on `PATH` (override with `CLAUDE_BIN`) and Telegram configured for
delivery.

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
off by default. See the [Crontab](#crontab) section above for its full cron line,
and [docs/daily-check.md](docs/daily-check.md) for the details.

---

## Security

This tool talks to your LND node and serves your node's financials. A few
things to get right before you run it — especially if you're not on a
single-operator home network.

### Dashboard exposure (no built-in auth)

The dashboard has **no authentication** and runs on Flask's development
server. Security is entirely the bind address: it defaults to `127.0.0.1`
(loopback) and is set via `DASHBOARD_BIND_IP` in `.env`.

- **Home / VPN use:** bind to `127.0.0.1` or your VPN IP. Never
  bind to `0.0.0.0` on a WAN-facing host — anyone who reaches the port sees
  balances, channel points, peer pubkeys, and routing/payment history.
- **Exposing it more widely:** don't point it at the internet.
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
- [Fee Engine Internals](docs/fee-engine-internals.md) — cadence, the layers, hysteresis, soft-floor ratchet, profitability gate, inbound-fee ladder, corner cases

**Liquidity**
- [Rebalance Budget](docs/rebalance-budget.md) — budget, failure escalation, the profitability gate, chunking, fallback pairs
- [Plan Command](docs/plan-command.md) — tier-segmented peer ranking (centrality → diversity)

**Interface**
- [Dashboard deep dive](docs/dashboard.md) — card-by-card tour with screenshots, Sat Flow drill-downs, and the watchtower health-badge logic

**Operations**
- [Daily Check](docs/daily-check.md) — the optional, off-by-default AI health-check agent

**Reference**
- [Configuration](docs/configuration.md) — every `config.py` knob and its default
- [Data Flow](docs/data-flow.md) — how LND data lands in SQLite and feeds each consumer
- [Alerts](docs/alerts.md) · [Logging](docs/logging.md) · [Channel Backup](docs/channel-backup.md) · [Known Limitations](docs/known-limitations.md)

---

## License

MIT
