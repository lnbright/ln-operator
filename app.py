#!/usr/bin/env python3
"""
LND Node Health Dashboard

A single-page Flask app showing your LND node's health at a glance. Combines
live data from LND's REST API with historical data from ln_operator.db.

Data sources:
- LIVE from LND (on every page load): node status, channel balances, sync state,
  on-chain balance, recent payments and invoices
- FROM SQLITE (populated by cron): routing fee revenue, rebalance history,
  fee update log, per-channel profitability, alerts, channel maturity/tier

The dashboard is read-only — it never modifies LND state or the database.
Designed to be accessed via Tailscale only (bound to Tailscale IP, not 0.0.0.0).

Run: python3 dashboard/app.py
Or install as systemd service — see lnd-dashboard.service.
"""

import os
import sqlite3
import requests
import urllib3
import psutil
from flask import Flask, render_template_string
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ─── Config ──────────────────────────────────────────────────────
LND_REST_URL = "https://127.0.0.1:9000"
LND_CERT     = "/home/lnd/tls.cert"
LND_MACAROON = "/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon"
TAILSCALE_IP = "10.0.0.1"
PORT         = 4000
DB_PATH      = os.getenv("LN_OPERATOR_DB", "/home/pi/ln-operator/ln_operator.db")


# ─── LND helpers ─────────────────────────────────────────────────

def get_macaroon_header():
    """Read the macaroon file and return it as a hex-encoded HTTP header."""
    with open(LND_MACAROON, "rb") as f:
        return {"Grpc-Metadata-macaroon": f.read().hex()}

def lnd_get(path):
    """GET request to LND REST API. Returns (dict, None) or (None, error)."""
    try:
        r = requests.get(f"{LND_REST_URL}{path}", headers=get_macaroon_header(),
                         verify=LND_CERT, timeout=5)
        return r.json(), None
    except Exception as e:
        return None, str(e)

def get_lnd_uptime():
    """Find the lnd process and calculate how long it's been running."""
    for proc in psutil.process_iter(['name', 'create_time']):
        try:
            if proc.info['name'] == 'lnd':
                delta = datetime.now() - datetime.fromtimestamp(proc.info['create_time'])
                days = delta.days
                hours, remainder = divmod(delta.seconds, 3600)
                minutes = remainder // 60
                if days > 0:
                    return f"{days}d {hours}h {minutes}m"
                return f"{hours}h {minutes}m"
        except:
            pass
    return "—"


# ─── DB helpers ──────────────────────────────────────────────────

def db_one(sql, params=()):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None

def db_all(sql, params=()):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def get_channel_perf(chan_id, days30):
    """Get performance stats for a channel from the operator DB.

    Returns both 30-day and lifetime (all-time) revenue, cost, and net.
    """
    # 30-day stats
    rev30 = db_one("""
        SELECT COALESCE(SUM(fee_earned_sats),0) as fee_rev, COUNT(*) as forwards
        FROM forwarding_log
        WHERE (chan_in=? OR chan_out=?) AND ts>?
    """, (chan_id, chan_id, days30))
    reb30 = db_one("""
        SELECT COALESCE(SUM(fee_paid_sats),0) as reb_cost
        FROM rebalance_log
        WHERE (source_chan_id=? OR target_chan_id=?) AND ts>? AND success=1
    """, (chan_id, chan_id, days30))

    # Lifetime stats (all-time)
    rev_all = db_one("""
        SELECT COALESCE(SUM(fee_earned_sats),0) as fee_rev, COUNT(*) as forwards
        FROM forwarding_log
        WHERE (chan_in=? OR chan_out=?)
    """, (chan_id, chan_id))
    reb_all = db_one("""
        SELECT COALESCE(SUM(fee_paid_sats),0) as reb_cost
        FROM rebalance_log
        WHERE (source_chan_id=? OR target_chan_id=?) AND success=1
    """, (chan_id, chan_id))

    mat = db_one("SELECT balanced_seconds FROM channel_maturity WHERE chan_id=?", (chan_id,))

    fee_rev_30  = rev30["fee_rev"] if rev30 else 0
    reb_cost_30 = reb30["reb_cost"] if reb30 else 0
    fee_rev_all  = rev_all["fee_rev"] if rev_all else 0
    reb_cost_all = reb_all["reb_cost"] if reb_all else 0
    bal_days = (mat["balanced_seconds"] / 86400) if mat else 0

    if bal_days >= 30:
        tier = "proven" if fee_rev_30 > 0 else "deadweight"
    else:
        tier = "discovery"

    return {
        "fee_rev":  fee_rev_30,
        "reb_cost": reb_cost_30,
        "net":      fee_rev_30 - reb_cost_30,
        "fee_rev_all":  fee_rev_all,
        "reb_cost_all": reb_cost_all,
        "net_all":      fee_rev_all - reb_cost_all,
        "forwards": rev30["forwards"] if rev30 else 0,
        "bal_days": round(bal_days, 1),
        "tier":     tier,
    }


# ─── Data gathering ──────────────────────────────────────────────

def get_dashboard_data():
    import time
    now    = int(time.time())
    days30 = now - 30 * 86400

    data = {}

    info, err = lnd_get("/v1/getinfo")
    if err:
        data["error"] = err
        return data
    data["info"]       = info
    data["lnd_uptime"] = get_lnd_uptime()

    # Channels — enriched with operator DB performance
    channels_raw, _ = lnd_get("/v1/channels")
    raw_channels = channels_raw.get("channels", []) if channels_raw else []
    channels_enriched = []
    for ch in raw_channels:
        chan_id   = ch.get("chan_id", "")
        capacity  = int(ch.get("capacity", 0))
        local     = int(ch.get("local_balance", 0))
        ch["perf"]      = get_channel_perf(chan_id, days30)
        ch["local_pct"] = round(local / capacity * 100, 1) if capacity > 0 else 0
        channels_enriched.append(ch)
    data["channels"] = channels_enriched

    invoices, _     = lnd_get("/v1/invoices?reversed=true&num_max_invoices=10")
    data["invoices"] = list(reversed(invoices.get("invoices", []))) if invoices else []

    payments, _     = lnd_get("/v1/payments?reversed=true&max_payments=10&include_incomplete=true")
    data["payments"] = list(reversed(payments.get("payments", []))) if payments else []

    onchain, _      = lnd_get("/v1/balance/blockchain")
    data["onchain"] = onchain if onchain else {}

    channel_bal, _      = lnd_get("/v1/balance/channels")
    data["channel_bal"] = channel_bal if channel_bal else {}

    data["bitcoin_online"] = info.get("synced_to_chain", False)

    # ── Operator DB sections ──────────────────────────────────────
    data["daily_revenue"] = db_all("""
        SELECT date(ts,'unixepoch') as day, SUM(fee_earned_sats) as fees
        FROM forwarding_log WHERE ts > ?
        GROUP BY day ORDER BY day
    """, (days30,))

    data["forwarding"] = db_all("""
        SELECT ts as timestamp, chan_in, chan_out,
               amount_in_sats as amt_in, amount_out_sats as amt_out,
               fee_earned_sats as fee
        FROM forwarding_log
        ORDER BY ts DESC LIMIT 10
    """)

    data["rebalances"] = db_all("""
        SELECT ts, source_alias, target_alias, amount_sats,
               fee_paid_sats, fee_ppm, success, failure_reason,
               COALESCE(triggered_by, 'auto') as triggered_by
        FROM rebalance_log ORDER BY ts DESC LIMIT 10
    """)

    data["fee_updates"] = db_all("""
        SELECT ts, peer_alias, old_fee_ppm, new_fee_ppm, local_ratio
        FROM fee_updates ORDER BY ts DESC LIMIT 10
    """)

    data["alerts"] = db_all("""
        SELECT ts, alert_type, message
        FROM alerts ORDER BY ts DESC LIMIT 10
    """)

    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


# ─── Template ────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚡ LND Node Health</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f; --surface: #111118; --border: #1e1e2e;
    --accent: #f7931a; --accent2: #7b61ff;
    --green: #00d97e; --red: #ff4d6d; --yellow: #ffd60a;
    --text: #e8e8f0; --muted: #6b6b80; --card: #13131d;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Space Mono', monospace; min-height: 100vh; padding: 0; }
  body::before {
    content: ''; position: fixed; inset: 0;
    background-image: linear-gradient(rgba(247,147,26,0.03) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(247,147,26,0.03) 1px, transparent 1px);
    background-size: 40px 40px; pointer-events: none; z-index: 0;
  }
  .wrap { position: relative; z-index: 1; max-width: 1200px; margin: 0 auto; padding: 32px 24px; }
  header { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 40px; padding-bottom: 24px; border-bottom: 1px solid var(--border); }
  .logo { font-family: 'Syne', sans-serif; font-weight: 800; font-size: 28px; letter-spacing: -0.5px; }
  .logo span { color: var(--accent); }
  .node-alias { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .timestamp { font-size: 11px; color: var(--muted); text-align: right; }
  .refresh-btn { display: inline-block; margin-top: 8px; padding: 6px 14px; background: var(--accent); color: #000; font-family: 'Space Mono', monospace; font-size: 11px; font-weight: 700; text-decoration: none; border: none; cursor: pointer; letter-spacing: 0.05em; transition: opacity 0.15s; }
  .refresh-btn:hover { opacity: 0.85; }

  .badge { display: inline-block; padding: 2px 8px; font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
  .badge-green  { background: rgba(0,217,126,0.15);  color: var(--green);  border: 1px solid rgba(0,217,126,0.3);  }
  .badge-red    { background: rgba(255,77,109,0.15);  color: var(--red);    border: 1px solid rgba(255,77,109,0.3);  }
  .badge-yellow { background: rgba(255,214,10,0.15);  color: var(--yellow); border: 1px solid rgba(255,214,10,0.3);  }
  .badge-blue   { background: rgba(123,97,255,0.15);  color: var(--accent2);border: 1px solid rgba(123,97,255,0.3);  }
  .badge-muted  { background: rgba(107,107,128,0.15); color: var(--muted);  border: 1px solid rgba(107,107,128,0.3); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  .grid-1 { margin-bottom: 16px; }
  @media (max-width: 900px) { .grid-3 { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 600px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }

  .card { background: var(--card); border: 1px solid var(--border); padding: 20px; }
  .card-title { font-family: 'Syne', sans-serif; font-size: 11px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
  .card-title::before { content: ''; display: block; width: 3px; height: 12px; background: var(--accent); }

  .stat-row { display: flex; justify-content: space-between; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { font-size: 11px; color: var(--muted); }
  .stat-value { font-size: 13px; color: var(--text); text-align: right; max-width: 60%; word-break: break-all; }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }

  .balance-grid { display: grid; grid-template-columns: repeat(5,1fr); gap: 10px; margin-bottom: 12px; overflow-x: auto; }
  @media (max-width: 768px) { .balance-grid { grid-template-columns: 1fr 1fr; } }
  .balance-box { text-align: center; padding: 10px 6px; }
  .b-lbl { font-size: 9px; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 5px; }
  .b-val { font-family: 'Space Mono', monospace; font-weight: 700; font-size: 13px; word-break: break-all; }
  .b-unit { font-size: 9px; color: var(--muted); margin-top: 2px; }

  /* Channel table */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .chan-table { width: 100%; border-collapse: collapse; font-size: 11px; min-width: 700px; }
  .chan-table th { text-align: left; font-size: 10px; color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; padding: 0 10px 10px; border-bottom: 1px solid var(--border); }
  .chan-table td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
  .chan-table tr:last-child td { border-bottom: none; }
  .chan-table tr:hover td { background: rgba(247,147,26,0.03); }

  .balance-bar { margin: 6px 0 3px; height: 6px; background: var(--border); overflow: hidden; min-width: 80px; }
  .balance-bar-fill { height: 100%; transition: width 0.5s ease; }
  .bar-depleted  { background: var(--red); }
  .bar-saturated { background: var(--accent2); }
  .bar-healthy   { background: linear-gradient(90deg, var(--accent), var(--accent2)); }

  /* Generic data table */
  .data-table { width: 100%; border-collapse: collapse; font-size: 11px; min-width: 400px; }
  .data-table th { text-align: left; padding: 8px 12px; color: var(--muted); font-weight: 400; border-bottom: 1px solid var(--border); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
  .data-table td { padding: 10px 12px; border-bottom: 1px solid rgba(30,30,46,0.5); vertical-align: middle; }
  .data-table tr:last-child td { border-bottom: none; }
  .data-table tr:hover td { background: rgba(247,147,26,0.03); }
  .truncate { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .amount-positive { color: var(--green); }
  .amount-negative { color: var(--red); }
  .amount-muted    { color: var(--muted); }

  /* Bar chart */
  .chart-bars { display: flex; align-items: flex-end; gap: 3px; height: 64px; }
  .chart-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; }
  .chart-bar { width: 100%; background: var(--accent); opacity: 0.7; min-height: 2px; }
  .chart-bar:hover { opacity: 1; }
  .chart-lbl { font-size: 8px; color: var(--muted); margin-top: 4px; white-space: nowrap; }

  .empty-state { padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }
  .error-card { background: rgba(255,77,109,0.08); border: 1px solid rgba(255,77,109,0.3); padding: 20px; margin-bottom: 16px; color: var(--red); font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <div class="logo">⚡ <span>LND</span> Health</div>
      {% if data.info %}
      <div class="node-alias">{{ data.info.alias }} &nbsp;·&nbsp; {{ data.info.identity_pubkey[:16] }}...</div>
      {% endif %}
    </div>
    <div class="timestamp">
      Last fetched<br>{{ data.timestamp }}<br>
      <a href="/" class="refresh-btn">↻ Refresh</a>
    </div>
  </header>

  {% if data.error %}
  <div class="error-card">⚠ Could not connect to LND: {{ data.error }}</div>
  {% endif %}

  {% if data.info %}
  {% set info = data.info %}

  <!-- Top stats row -->
  <div class="grid-3">
    <div class="card">
      <div class="card-title">Node Status</div>
      <div class="stat-row">
        <span class="stat-label">Sync</span>
        <span>{% if info.synced_to_chain %}<span class="badge badge-green">Synced</span>{% else %}<span class="badge badge-yellow">Syncing</span>{% endif %}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Block Height</span>
        <span class="stat-value">{{ "{:,}".format(info.block_height) }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">LND Version</span>
        <span class="stat-value">{{ info.version.split(' ')[0] if info.version else 'N/A' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Uptime</span>
        <span class="stat-value">{{ data.lnd_uptime }}</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Channels</div>
      <div class="stat-row">
        <span class="stat-label">Active</span>
        <span class="stat-value green">{{ info.num_active_channels }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Inactive</span>
        <span class="stat-value {% if info.num_inactive_channels > 0 %}red{% endif %}">{{ info.num_inactive_channels }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Pending</span>
        <span class="stat-value">{{ info.num_pending_channels }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Peers</span>
        <span class="stat-value">{{ info.num_peers }}</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Bitcoin Backend</div>
      <div class="stat-row">
        <span class="stat-label">Chain</span>
        <span class="stat-value">{{ info.chains[0].chain if info.chains else 'N/A' }} / {{ info.chains[0].network if info.chains else 'N/A' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Chain Synced</span>
        <span>{% if info.synced_to_chain %}<span class="badge badge-green">Yes</span>{% else %}<span class="badge badge-yellow">Syncing</span>{% endif %}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Graph Synced</span>
        <span>{% if info.synced_to_graph %}<span class="badge badge-green">Yes</span>{% else %}<span class="badge badge-yellow">Syncing</span>{% endif %}</span>
      </div>
    </div>
  </div>

  <!-- Node Balance -->
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Node Balance — Total Funds Controlled</div>
      {% set onchain = data.onchain %}
      {% set cb = data.channel_bal %}
      {% set confirmed    = onchain.confirmed_balance | int if onchain.confirmed_balance is defined else 0 %}
      {% set unconfirmed  = onchain.unconfirmed_balance | int if onchain.unconfirmed_balance is defined else 0 %}
      {% set local_chan   = cb.local_balance.sat | int if cb.local_balance is defined else 0 %}
      {% set pending_open = cb.pending_open_local_balance.sat | int if cb.pending_open_local_balance is defined else 0 %}
      {% set unsettled    = cb.unsettled_local_balance.sat | int if cb.unsettled_local_balance is defined else 0 %}
      {% set total = confirmed + unconfirmed + local_chan + pending_open + unsettled %}
      <div class="balance-grid">
        <div class="balance-box" style="background:rgba(247,147,26,0.06);border:1px solid rgba(247,147,26,0.15);">
          <div class="b-lbl">On-Chain</div>
          <div class="b-val" style="color:var(--accent)">{{ "{:,}".format(confirmed) }}</div>
          <div class="b-unit">sats confirmed</div>
        </div>
        <div class="balance-box" style="background:rgba(255,214,10,0.06);border:1px solid rgba(255,214,10,0.15);">
          <div class="b-lbl">Unconfirmed</div>
          <div class="b-val" style="color:var(--yellow)">{{ "{:,}".format(unconfirmed) }}</div>
          <div class="b-unit">sats pending</div>
        </div>
        <div class="balance-box" style="background:rgba(123,97,255,0.06);border:1px solid rgba(123,97,255,0.15);">
          <div class="b-lbl">In Channels</div>
          <div class="b-val" style="color:var(--accent2)">{{ "{:,}".format(local_chan) }}</div>
          <div class="b-unit">sats local</div>
        </div>
        <div class="balance-box" style="background:rgba(0,217,126,0.06);border:1px solid rgba(0,217,126,0.15);">
          <div class="b-lbl">Pending Open</div>
          <div class="b-val" style="color:var(--green)">{{ "{:,}".format(pending_open) }}</div>
          <div class="b-unit">sats</div>
        </div>
        <div class="balance-box" style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);">
          <div class="b-lbl">Total</div>
          <div class="b-val" style="color:var(--text)">{{ "{:,}".format(total) }}</div>
          <div class="b-unit">sats controlled</div>
        </div>
      </div>
      {% if unsettled > 0 %}
      <div style="font-size:11px;color:var(--muted);padding-top:8px;border-top:1px solid var(--border);">
        Unsettled HTLCs: <span style="color:var(--yellow)">{{ "{:,}".format(unsettled) }} sats</span>
      </div>
      {% endif %}

      <!-- Channel liquidity split (sendable vs receivable) -->
      {% set remote_chan = cb.remote_balance.sat | int if cb.remote_balance is defined else 0 %}
      {% set total_channel = local_chan + remote_chan %}
      {% set local_pct = (local_chan / total_channel * 100) | round(1) if total_channel > 0 else 0 %}
      {% set remote_pct = (100 - local_pct) | round(1) %}
      {% if total_channel > 0 %}
      <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:6px;">
          <span>Channel Liquidity — Sendable vs Receivable</span>
          <span>
            <span style="color:var(--accent)">Sendable {{ local_pct }}%</span>
            &nbsp;/&nbsp;
            <span style="color:var(--accent2)">Receivable {{ remote_pct }}%</span>
          </span>
        </div>
        <div style="height:8px;background:var(--border);overflow:hidden;position:relative;">
          <div style="position:absolute;left:0;top:0;height:100%;width:{{ local_pct }}%;background:var(--accent);"></div>
          <div style="position:absolute;right:0;top:0;height:100%;width:{{ remote_pct }}%;background:var(--accent2);"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;">
          <span>{{ "{:,}".format(local_chan) }} sats sendable</span>
          <span>{{ "{:,}".format(remote_chan) }} sats receivable</span>
        </div>
      </div>
      {% endif %}
    </div>
  </div>

  <!-- Channel Details — enriched with operator performance data -->
  {% if data.channels %}
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Channel Details</div>
      <div class="table-wrap"><table class="chan-table">
        <thead>
          <tr>
            <th style="width:16%">Peer</th>
            <th style="width:18%">Balance</th>
            <th>Capacity</th>
            <th>Sent / Received</th>
            <th>Revenue 30d</th>
            <th>Rebal Cost 30d</th>
            <th>Net 30d</th>
            <th>Net Lifetime</th>
            <th>Tier</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {% for ch in data.channels %}
          {% set capacity = ch.capacity | int %}
          {% set local    = ch.local_balance | int %}
          {% set remote   = ch.remote_balance | int %}
          {% set ratio    = ch.local_pct %}
          {% set perf     = ch.perf %}
          {% set bar_cls  = 'bar-depleted' if ratio < 20 else ('bar-saturated' if ratio > 80 else 'bar-healthy') %}
          <tr>
            <td>
              <div style="font-weight:700;font-size:12px;">{{ ch.peer_alias if ch.peer_alias else ch.remote_pubkey[:16] ~ '...' }}</div>
              <div style="font-size:10px;color:var(--muted);margin-top:2px;">{{ ch.commitment_type }}</div>
            </td>
            <td>
              <div class="balance-bar"><div class="balance-bar-fill {{ bar_cls }}" style="width:{{ ratio }}%"></div></div>
              <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);">
                <span>{{ ratio }}% local</span><span>{{ "{:,}".format(local) }}</span>
              </div>
            </td>
            <td style="color:var(--muted);">{{ "{:,}".format(capacity) }}</td>
            <td style="font-size:10px;color:var(--muted);">
              ↑ {{ "{:,}".format(ch.total_satoshis_sent | int) }}<br>
              ↓ {{ "{:,}".format(ch.total_satoshis_received | int) }}
            </td>
            <td class="{% if perf.fee_rev > 0 %}amount-positive{% else %}amount-muted{% endif %}">
              {% if perf.fee_rev > 0 %}+{{ "{:,}".format(perf.fee_rev) }}{% else %}—{% endif %}
            </td>
            <td class="{% if perf.reb_cost > 0 %}amount-negative{% else %}amount-muted{% endif %}">
              {% if perf.reb_cost > 0 %}-{{ "{:,}".format(perf.reb_cost) }}{% else %}—{% endif %}
            </td>
            <td class="{% if perf.net > 0 %}amount-positive{% elif perf.net < 0 %}amount-negative{% else %}amount-muted{% endif %}">
              {% if perf.net != 0 %}{% if perf.net > 0 %}+{% endif %}{{ "{:,}".format(perf.net) }}{% else %}—{% endif %}
            </td>
            <td class="{% if perf.net_all > 0 %}amount-positive{% elif perf.net_all < 0 %}amount-negative{% else %}amount-muted{% endif %}">
              {% if perf.net_all != 0 %}{% if perf.net_all > 0 %}+{% endif %}{{ "{:,}".format(perf.net_all) }}{% else %}—{% endif %}
            </td>
            <td>
              {% if perf.tier == 'proven' %}<span class="badge badge-green">proven</span>
              {% elif perf.tier == 'deadweight' %}<span class="badge badge-red">dead</span>
              {% else %}<span class="badge badge-yellow">discovery</span>{% endif %}
            </td>
            <td>
              {% if not ch.active %}<span class="badge badge-red">offline</span>
              {% elif ratio < 20 %}<span class="badge badge-red">depleted</span>
              {% elif ratio > 80 %}<span class="badge badge-blue">saturated</span>
              {% else %}<span class="badge badge-green">healthy</span>{% endif %}
              {% if ch.private %}&nbsp;<span class="badge badge-muted">private</span>{% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

  <!-- Payments & Invoices -->
  <div class="grid-2">
    <div class="card">
      <div class="card-title">Recent Payments</div>
      {% if data.payments %}
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Date</th><th>Amount</th><th>Fee</th><th>Status</th></tr></thead>
        <tbody>
          {% for p in data.payments %}
          <tr>
            <td>{{ p.creation_date | int | format_ts }}</td>
            <td class="amount-negative">{{ "{:,}".format(p.value_sat | int) }}</td>
            <td style="color:var(--muted)">{{ p.fee_sat | int }}</td>
            <td>{% if p.status == 'SUCCEEDED' %}<span class="badge badge-green">OK</span>{% else %}<span class="badge badge-red">{{ p.status }}</span>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table></div>
      {% else %}
      <div class="empty-state">No payments found</div>
      {% endif %}
    </div>
    <div class="card">
      <div class="card-title">Recent Invoices</div>
      {% if data.invoices %}
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Date</th><th>Amount</th><th>Memo</th><th>Status</th></tr></thead>
        <tbody>
          {% for inv in data.invoices %}
          <tr>
            <td>{{ inv.creation_date | int | format_ts }}</td>
            <td class="amount-positive">{{ "{:,}".format(inv.value | int) }}</td>
            <td class="truncate" style="max-width:100px;">{{ inv.memo if inv.memo else '—' }}</td>
            <td>{% if inv.state == 'SETTLED' %}<span class="badge badge-green">Paid</span>{% elif inv.state == 'OPEN' %}<span class="badge badge-yellow">Open</span>{% else %}<span class="badge badge-red">{{ inv.state }}</span>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table></div>
      {% else %}
      <div class="empty-state">No invoices found</div>
      {% endif %}
    </div>
  </div>

  <!-- Routing -->
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Recent Routing Events</div>
      {% if data.forwarding %}
      <table class="data-table">
        <thead><tr><th>Date</th><th>Amount In</th><th>Amount Out</th><th>Fee Earned</th></tr></thead>
        <tbody>
          {% for fwd in data.forwarding %}
          <tr>
            <td>{{ fwd.timestamp | int | format_ts }}</td>
            <td>{{ "{:,}".format(fwd.amt_in | int) }} sats</td>
            <td>{{ "{:,}".format(fwd.amt_out | int) }} sats</td>
            <td class="amount-positive">{{ (fwd.amt_in | int) - (fwd.amt_out | int) }} sats</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty-state">No routing events yet — run <code>main.py monitor</code> to sync from LND</div>
      {% endif %}
    </div>
  </div>

  <!-- ── Operator sections ─────────────────────────────────────── -->

  <!-- Daily Fee Revenue (30d) -->
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Daily Fee Revenue (30d)</div>
      {% if data.daily_revenue %}
        {% set max_fees = namespace(val=1) %}
        {% for row in data.daily_revenue %}{% if row.fees > max_fees.val %}{% set max_fees.val = row.fees %}{% endif %}{% endfor %}
        <div class="chart-bars">
          {% for row in data.daily_revenue %}
            {% set bar_h = ((row.fees / max_fees.val) * 60) | int %}{% if bar_h < 2 %}{% set bar_h = 2 %}{% endif %}
            <div class="chart-bar-wrap">
              <div class="chart-bar" style="height:{{ bar_h }}px" title="{{ row.day }}: {{ row.fees }} sats"></div>
              {% if loop.index == 1 or loop.index == loop.length or loop.index % 7 == 0 %}
              <div class="chart-lbl">{{ row.day[5:] }}</div>
              {% else %}<div class="chart-lbl">&nbsp;</div>{% endif %}
            </div>
          {% endfor %}
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:12px;text-align:right;">
          Total 30d: <span style="color:var(--green)">{{ "{:,}".format(data.daily_revenue | sum(attribute='fees')) }} sats</span>
        </div>
      {% else %}
        <div class="empty-state">No routing history yet — fees will appear here as your node routes payments</div>
      {% endif %}
    </div>
  </div>

  <!-- Rebalance History + Fee Updates -->
  <div class="grid-2">
    <div class="card">
      <div class="card-title">Rebalance History</div>
      {% if data.rebalances %}
      <table class="data-table">
        <thead><tr><th>Time</th><th>Route</th><th>Amount</th><th>Fee</th><th></th><th>Source</th></tr></thead>
        <tbody>
          {% for r in data.rebalances %}
          <tr>
            <td style="color:var(--muted);font-size:10px;white-space:nowrap;">{{ r.ts | format_ts }}</td>
            <td>{{ r.source_alias[:10] }} → {{ r.target_alias[:10] }}</td>
            <td>{{ "{:,}".format(r.amount_sats) }}</td>
            <td class="{% if r.success %}amount-negative{% else %}amount-muted{% endif %}">
              {% if r.success %}-{{ "{:,}".format(r.fee_paid_sats) }} <span style="color:var(--muted)">({{ r.fee_ppm | int }}ppm)</span>{% else %}—{% endif %}
            </td>
            <td>{% if r.success %}<span class="badge badge-green">✓</span>{% else %}<span class="badge badge-red" title="{{ r.failure_reason }}">✗</span>{% endif %}</td>
            <td>{% if r.triggered_by == 'manual' %}<span class="badge badge-blue">manual</span>{% else %}<span class="badge badge-muted">auto</span>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty-state">No rebalances yet</div>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Recent Fee Updates</div>
      {% if data.fee_updates %}
      <table class="data-table">
        <thead><tr><th>Time</th><th>Peer</th><th>Old</th><th>New</th><th>Local</th></tr></thead>
        <tbody>
          {% for u in data.fee_updates %}
          <tr>
            <td style="color:var(--muted);font-size:10px;white-space:nowrap;">{{ u.ts | format_ts }}</td>
            <td>{{ u.peer_alias }}</td>
            <td style="color:var(--muted);">{{ u.old_fee_ppm }} ppm</td>
            <td>
              {% if u.new_fee_ppm > u.old_fee_ppm %}<span style="color:var(--red)">↑ {{ u.new_fee_ppm }} ppm</span>
              {% else %}<span style="color:var(--green)">↓ {{ u.new_fee_ppm }} ppm</span>{% endif %}
            </td>
            <td style="color:var(--muted);">{{ "%.0f"|format(u.local_ratio * 100) }}%</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty-state">No fee updates yet — run <code>main.py fees</code></div>
      {% endif %}
    </div>
  </div>

  <!-- Recent Alerts -->
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Recent Alerts</div>
      {% if data.alerts %}
      <table class="data-table">
        <thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead>
        <tbody>
          {% for a in data.alerts %}
          <tr>
            <td style="color:var(--muted);font-size:10px;white-space:nowrap;">{{ a.ts | format_ts }}</td>
            <td>
              {% if 'offline' in a.alert_type %}<span class="badge badge-red">offline</span>
              {% elif 'depleted' in a.alert_type %}<span class="badge badge-yellow">depleted</span>
              {% elif 'saturated' in a.alert_type %}<span class="badge badge-blue">saturated</span>
              {% elif 'rebalance_failing' in a.alert_type %}<span class="badge badge-red">rebal failing</span>
              {% else %}<span class="badge badge-muted">{{ a.alert_type }}</span>{% endif %}
            </td>
            <td style="color:var(--muted);">{{ a.message }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty-state">No alerts — all clear ✓</div>
      {% endif %}
    </div>
  </div>

  {% endif %}

</div>
</body>
</html>
"""


@app.template_filter("format_ts")
def format_ts(ts):
    """Jinja2 filter: converts Unix timestamp to readable date string including year."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except:
        return "—"


@app.route("/")
def index():
    """Single route — fetches fresh data on every page load."""
    data = get_dashboard_data()
    return render_template_string(TEMPLATE, data=data)


if __name__ == "__main__":
    app.run(host=TAILSCALE_IP, port=PORT, debug=False)
