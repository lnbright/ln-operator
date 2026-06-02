#!/usr/bin/env python3
"""
LND Node Health Dashboard

A single-page Flask app showing your LND node's health at a glance. Combines
live data from LND's REST API with historical data from ln_operator.db.

Data sources:
- LIVE from LND (on every page load): node status, channel balances, sync state,
  on-chain balance, recent payments and invoices
- FROM SQLITE (populated by cron): routing fee revenue, rebalance history,
  fee update log, per-channel profitability, alerts, channel maturity

The dashboard is read-only — it never modifies LND state or the database.
Designed to be accessed via Tailscale only (bound to Tailscale IP, not 0.0.0.0).

Run: python3 dashboard/app.py
Or install as systemd service — see services/lnd-dashboard.service.
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import requests
import urllib3
import psutil
from flask import Flask, render_template_string, request
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ─── Config ──────────────────────────────────────────────────────
# Read from env (.env) so the dashboard isn't pinned to one host's layout;
# defaults match config.py. The dashboard is read-only, so it prefers a
# read-only macaroon via DASHBOARD_LND_MACAROON, falling back to the main
# LND_MACAROON, then the legacy admin path.
LND_REST_URL = os.getenv("LND_REST_URL", "https://127.0.0.1:9000")
LND_CERT     = os.getenv("LND_CERT", "/home/lnd/tls.cert")
LND_MACAROON = os.getenv("DASHBOARD_LND_MACAROON",
                         os.getenv("LND_MACAROON",
                                   "/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon"))
# Bind address — set DASHBOARD_BIND_IP in .env to your Tailscale IP so the
# dashboard is reachable across the tailnet but not the public internet.
# Defaults to 127.0.0.1 (localhost only) if unset.
BIND_IP      = os.getenv("DASHBOARD_BIND_IP", "127.0.0.1")
PORT         = int(os.getenv("DASHBOARD_PORT", "4000"))
DB_PATH      = os.getenv("LN_OPERATOR_DB", "/home/pi/ln-operator/ln_operator.db")


# ─── LND helpers ─────────────────────────────────────────────────

def get_macaroon_header():
    """Read the macaroon file and return it as a hex-encoded HTTP header."""
    with open(LND_MACAROON, "rb") as f:
        return {"Grpc-Metadata-macaroon": f.read().hex()}

def lnd_get(path, timeout=5):
    """GET request to LND REST API. Returns (dict, None) or (None, error)."""
    try:
        r = requests.get(f"{LND_REST_URL}{path}", headers=get_macaroon_header(),
                         verify=LND_CERT, timeout=timeout)
        return r.json(), None
    except Exception as e:
        return None, str(e)


def lnd_get_status(path, timeout=5):
    """Like lnd_get but also returns the HTTP status code so callers can
    distinguish 'wtclient disabled' (501) from generic errors.
    Returns (status_code, dict_or_none, error_or_none)."""
    try:
        r = requests.get(f"{LND_REST_URL}{path}", headers=get_macaroon_header(),
                         verify=LND_CERT, timeout=timeout)
        if r.status_code != 200:
            return r.status_code, None, f"HTTP {r.status_code}"
        return r.status_code, r.json(), None
    except Exception as e:
        return 0, None, str(e)

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
        WHERE target_chan_id=? AND ts>? AND success=1
    """, (chan_id, days30))

    # Lifetime stats (all-time)
    rev_all = db_one("""
        SELECT COALESCE(SUM(fee_earned_sats),0) as fee_rev, COUNT(*) as forwards
        FROM forwarding_log
        WHERE (chan_in=? OR chan_out=?)
    """, (chan_id, chan_id))
    reb_all = db_one("""
        SELECT COALESCE(SUM(fee_paid_sats),0) as reb_cost
        FROM rebalance_log
        WHERE target_chan_id=? AND success=1
    """, (chan_id,))

    mat = db_one("SELECT balanced_seconds FROM channel_maturity WHERE chan_id=?", (chan_id,))

    fee_rev_30  = rev30["fee_rev"] if rev30 else 0
    reb_cost_30 = reb30["reb_cost"] if reb30 else 0
    fee_rev_all  = rev_all["fee_rev"] if rev_all else 0
    reb_cost_all = reb_all["reb_cost"] if reb_all else 0
    bal_days = (mat["balanced_seconds"] / 86400) if mat else 0

    return {
        "fee_rev":  fee_rev_30,
        "reb_cost": reb_cost_30,
        "net":      fee_rev_30 - reb_cost_30,
        "fee_rev_all":  fee_rev_all,
        "reb_cost_all": reb_cost_all,
        "net_all":      fee_rev_all - reb_cost_all,
        "forwards": rev30["forwards"] if rev30 else 0,
        "bal_days": round(bal_days, 1),
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

    # Channels — enriched with operator DB performance.
    # peer_alias_lookup=true asks LND to resolve aliases from its graph view,
    # otherwise the field comes back empty and the UI falls back to pubkey.
    channels_raw, _ = lnd_get("/v1/channels?peer_alias_lookup=true")
    raw_channels = channels_raw.get("channels", []) if channels_raw else []
    our_pubkey = info.get("identity_pubkey", "")
    channels_enriched = []
    for ch in raw_channels:
        chan_id   = ch.get("chan_id", "")
        capacity  = int(ch.get("capacity", 0))
        local     = int(ch.get("local_balance", 0))
        ch["perf"]      = get_channel_perf(chan_id, days30)
        ch["local_pct"] = round(local / capacity * 100, 1) if capacity > 0 else 0
        local_ppm, remote_ppm = get_remote_fee_ppm(chan_id, our_pubkey)
        ch["local_fee_ppm"]  = local_ppm
        ch["remote_fee_ppm"] = remote_ppm
        channels_enriched.append(ch)
    data["channels"] = channels_enriched

    data["watchtowers"] = get_watchtower_status()

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
               fee_paid_sats, fee_ppm, success, failure_reason, budget_ppm,
               COALESCE(triggered_by, 'auto') as triggered_by
        FROM rebalance_log ORDER BY ts DESC LIMIT 10
    """)

    data["fee_updates"] = db_all("""
        SELECT ts, peer_alias, old_fee_ppm, new_fee_ppm, local_ratio,
               COALESCE(reason, '') as reason,
               CASE WHEN COALESCE(reason,'') LIKE 'manual pin%' THEN 'pin' ELSE 'auto' END as source
        FROM fee_updates ORDER BY ts DESC LIMIT 10
    """)

    data["alerts"] = db_all("""
        SELECT ts, alert_type, message
        FROM alerts ORDER BY ts DESC LIMIT 10
    """)

    data["backup"] = get_backup_status(now)
    data["fwd_fail"] = get_forward_failures(now, channels_enriched)
    data["sat_flow"] = get_sat_flow(now, channels_enriched,
                                    request.args.get("flow_window", "30"),
                                    request.args.get("flow_in", ""),
                                    request.args.get("flow_out", ""))

    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


def get_watchtower_status():
    """Read wtclient state. Returns a dict with:
      count        — total towers configured (incl. deactivated)
      active_count — towers with active_session_candidate=true
                     (LND will open new sessions with them)
      inactive_count — total - active
      num_backups  — lifetime state updates successfully backed up
      pending      — backups queued, not yet sent
      failed       — backups that failed permanently
      enabled      — False iff wtclient subsystem is off (501 on /stats)
      error        — non-None on unexpected failures

    Endpoint note: `/v2/watchtower/client/towers` returns 501 on this LND
    build (REST gateway quirk), but `/v2/watchtower/client` (no suffix)
    and `/v2/watchtower/client/stats` work. We use the working pair.

    Health rationale (set by the template, not here):
      red    — wtclient disabled / no towers / failed > 0
      yellow — towers configured but 0 active (existing sessions still
               back up, but no new sessions can be opened once they fill)
      green  — ≥1 active tower, 0 failed
    Pending does NOT affect the badge — a transient pending=1 during session
    rollover (current session hits max-updates, LND negotiates a new one) is
    normal. It's surfaced as a number only.
    `active_session_candidate` is an *administrative* flag (set by
    lncli wtclient activate/deactivate), not a liveness probe — a
    deactivated tower can still be exchanging state updates on its
    existing sessions until they exhaust.
    """
    blank = {"count": 0, "active_count": 0, "inactive_count": 0,
             "num_backups": 0, "pending": 0, "failed": 0,
             "enabled": True, "error": None}

    code_s, stats, err_s = lnd_get_status("/v2/watchtower/client/stats")
    if code_s == 501:
        return {**blank, "enabled": False}
    if err_s:
        return {**blank, "error": err_s}

    blank["num_backups"] = int(stats.get("num_backups", 0) or 0)
    blank["pending"]     = int(stats.get("num_pending_backups", 0) or 0)
    blank["failed"]      = int(stats.get("num_failed_backups", 0) or 0)

    towers_resp, err_t = lnd_get("/v2/watchtower/client")
    if not towers_resp or err_t:
        return {**blank, "error": err_t or "tower list unavailable"}
    towers = towers_resp.get("towers", []) or []
    blank["count"]          = len(towers)
    blank["active_count"]   = sum(1 for t in towers if t.get("active_session_candidate"))
    blank["inactive_count"] = blank["count"] - blank["active_count"]
    return blank


def get_remote_fee_ppm(chan_id, our_pubkey):
    """Look up a channel's edge in LND's graph and return the *peer's*
    outbound fee_rate_milli_msat. Returns (local_ppm, remote_ppm) where
    local is our own outbound policy and remote is the peer's. Either
    value is None if the policy isn't published yet (new/private channel).
    """
    edge, _ = lnd_get(f"/v1/graph/edge/{chan_id}", timeout=5)
    if not edge:
        return None, None
    n1 = edge.get("node1_pub", "")
    n2 = edge.get("node2_pub", "")
    p1 = edge.get("node1_policy") or {}
    p2 = edge.get("node2_policy") or {}

    def ppm(policy):
        if not policy:
            return None
        v = policy.get("fee_rate_milli_msat")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if our_pubkey == n1:
        return ppm(p1), ppm(p2)
    if our_pubkey == n2:
        return ppm(p2), ppm(p1)
    return None, None


def get_backup_status(now):
    """Read channel-backup state for the dashboard card.

    States:
      fresh  — last success ≤ 12h ago AND last attempt also succeeded.
      error  — most recent attempt failed (regardless of last-success age).
      stale  — last success > 12h ago.
      never  — backup_log is empty.
    """
    last_attempt = db_one("SELECT * FROM backup_log ORDER BY ts DESC LIMIT 1")
    last_success = db_one("SELECT * FROM backup_log WHERE success=1 ORDER BY ts DESC LIMIT 1")

    if last_success is None:
        status = "never"
    else:
        age = now - last_success["ts"]
        last_failed = last_attempt and not last_attempt["success"]
        if last_failed:
            status = "error"
        elif age > 12 * 3600:
            status = "stale"
        else:
            status = "fresh"

    return {
        "status":       status,
        "last_attempt": last_attempt,
        "last_success": last_success,
    }


def service_is_active(unit):
    """Return 'active' / 'inactive' / 'unknown' for a systemd unit.

    `pi` can query unit state without sudo. Any error (systemctl missing,
    permission) degrades to 'unknown' rather than breaking the page."""
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return out if out in ("active", "inactive", "failed", "activating") else "unknown"
    except Exception:
        return "unknown"


def get_forward_failures(now, channels):
    """Forwards we DROPPED in the last 24h (from forward_fail_log), framed as
    potentially-lost revenue rather than a bare count.

    Splits by cause because the remedy differs:
      - liquidity (INSUFFICIENT_BALANCE): channel too empty to forward —
        recoverable by rebalancing. We estimate the lost fee as
        dropped_sats × our outbound ppm on the channel that ran dry.
      - fee (FEE_INSUFFICIENT): sender under-paid our fee — not a liquidity
        problem, shown separately so it isn't mistaken for one.
      - other: expiry / unknown wire failures, counted but not actioned.

    Also reports the htlc_monitor daemon's liveness: an empty table with a dead
    daemon is a blind spot, not a clean day."""
    day_ago = now - 86400
    rows = db_all("""
        SELECT chan_out, alias_out, failure_detail,
               COUNT(*) AS n, COALESCE(SUM(amount_msat), 0) AS msat
        FROM forward_fail_log
        WHERE ts > ?
        GROUP BY chan_out, failure_detail
    """, (day_ago,))

    # our outbound fee ppm per channel, to value the dropped liquidity flow
    ppm_by_chan = {str(ch.get("chan_id", "")): ch.get("local_fee_ppm") or 0
                   for ch in channels}

    total_n = liq_n = fee_n = other_n = 0
    total_sats = liq_sats = 0
    est_lost_fee = 0.0
    per_chan = {}  # chan_out -> {alias, sats, n} for liquidity failures

    for r in rows:
        n = r["n"]
        sats = (r["msat"] or 0) // 1000
        total_n += n
        total_sats += sats
        detail = r["failure_detail"] or ""
        if detail == "INSUFFICIENT_BALANCE":
            liq_n += n
            liq_sats += sats
            est_lost_fee += sats * (ppm_by_chan.get(str(r["chan_out"]), 0) / 1_000_000)
            agg = per_chan.setdefault(r["chan_out"],
                                      {"alias": r["alias_out"] or r["chan_out"], "sats": 0, "n": 0})
            agg["sats"] += sats
            agg["n"] += n
        elif detail == "FEE_INSUFFICIENT":
            fee_n += n
        else:
            other_n += n

    top = max(per_chan.values(), key=lambda x: x["sats"], default=None)
    last_event = db_one("SELECT ts FROM forward_fail_log ORDER BY ts DESC LIMIT 1")

    return {
        "service":        service_is_active("lnd-htlc-monitor"),
        "total_n":        total_n,
        "total_sats":     total_sats,
        "liq_n":          liq_n,
        "liq_sats":       liq_sats,
        "fee_n":          fee_n,
        "other_n":        other_n,
        "est_lost_fee":   int(round(est_lost_fee)),
        "top":            top,
        "last_event_ts":  last_event["ts"] if last_event else None,
    }


# Selectable windows for the sat-flow card. key -> (label, days; None = all time)
SAT_FLOW_WINDOWS = {"30": ("30d", 30), "7": ("7d", 7), "all": ("all time", None)}


def get_sat_flow(now, channels, window_key="30", flow_in="", flow_out=""):
    """Routing-flow map: where sats come IN and where they go OUT, over a
    selectable window (30d / 7d / all time), optionally filtered to a single
    inbound and/or outbound channel.

    Reads forwarding_log (every successfully-routed HTLC carries chan_in +
    chan_out). Returns three views of the same flow:
      - pairs:   in→out peer pairs ranked by sats routed (the actual routes)
      - sources: total sats received per inbound peer (where liquidity enters)
      - sinks:   total sats sent per outbound peer (where liquidity leaves)
    chan_in/chan_out are scids; we map them to peer aliases via the live channel
    list. Scids with no current channel (closed since) fall back to the raw id —
    most visible under 'all time'.

    flow_in / flow_out are scids: when set, rows are filtered to that channel so
    you can drill into a single peer ("where do sats from Boltz go?"). The
    dropdown option lists are built from the *unfiltered* window so the full
    menu stays available regardless of the current selection."""
    if window_key not in SAT_FLOW_WINDOWS:
        window_key = "30"
    label, days = SAT_FLOW_WINDOWS[window_key]

    if days is None:
        rows = db_all("""
            SELECT chan_in, chan_out,
                   COUNT(*)             AS n,
                   SUM(amount_out_sats) AS sats,
                   SUM(fee_earned_sats) AS fee
            FROM forwarding_log
            GROUP BY chan_in, chan_out
        """)
    else:
        rows = db_all("""
            SELECT chan_in, chan_out,
                   COUNT(*)             AS n,
                   SUM(amount_out_sats) AS sats,
                   SUM(fee_earned_sats) AS fee
            FROM forwarding_log
            WHERE ts > ?
            GROUP BY chan_in, chan_out
        """, (now - days * 86400,))

    alias_by_chan = {str(ch.get("chan_id", "")): (ch.get("peer_alias")
                     or (ch.get("remote_pubkey", "") or "")[:10] or str(ch.get("chan_id", "")))
                     for ch in channels}

    def alias(scid):
        return alias_by_chan.get(str(scid), str(scid))

    # Dropdown menus — every channel that appears on each side in the window,
    # built before filtering so changing one filter never empties the other menu.
    in_opts, out_opts = {}, {}
    for r in rows:
        in_opts[str(r["chan_in"])]   = alias(r["chan_in"])
        out_opts[str(r["chan_out"])] = alias(r["chan_out"])
    in_options  = sorted(in_opts.items(),  key=lambda kv: kv[1].lower())
    out_options = sorted(out_opts.items(), key=lambda kv: kv[1].lower())

    # Only honour a filter if it names a channel actually present in the window.
    flow_in  = flow_in  if flow_in  in in_opts  else ""
    flow_out = flow_out if flow_out in out_opts else ""

    pairs = []
    sources = {}  # chan_in  -> {alias, sats, n}
    sinks   = {}  # chan_out -> {alias, sats, n}
    total_sats = total_n = total_fee = 0

    for r in rows:
        if flow_in and str(r["chan_in"]) != flow_in:
            continue
        if flow_out and str(r["chan_out"]) != flow_out:
            continue
        sats = int(r["sats"] or 0)
        n    = int(r["n"] or 0)
        fee  = int(r["fee"] or 0)
        total_sats += sats
        total_n    += n
        total_fee  += fee
        pairs.append({
            "in_alias":  alias(r["chan_in"]),
            "out_alias": alias(r["chan_out"]),
            "sats": sats, "n": n, "fee": fee,
        })
        si = sources.setdefault(r["chan_in"],  {"alias": alias(r["chan_in"]),  "sats": 0, "n": 0})
        si["sats"] += sats; si["n"] += n
        so = sinks.setdefault(r["chan_out"], {"alias": alias(r["chan_out"]), "sats": 0, "n": 0})
        so["sats"] += sats; so["n"] += n

    pairs.sort(key=lambda x: x["sats"], reverse=True)
    sources_list = sorted(sources.values(), key=lambda x: x["sats"], reverse=True)
    sinks_list   = sorted(sinks.values(),   key=lambda x: x["sats"], reverse=True)

    return {
        "window_key":  window_key,
        "window_label": label,
        "flow_in":     flow_in,
        "flow_out":    flow_out,
        "in_options":  in_options,
        "out_options": out_options,
        "filtered":    bool(flow_in or flow_out),
        "total_sats":  total_sats,
        "total_n":     total_n,
        "total_fee":   total_fee,
        "pairs":       pairs[:15],
        "max_pair":    pairs[0]["sats"] if pairs else 0,
        "sources":     sources_list[:8],
        "sinks":       sinks_list[:8],
        "max_source":  sources_list[0]["sats"] if sources_list else 0,
        "max_sink":    sinks_list[0]["sats"] if sinks_list else 0,
    }


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
  .logo { font-family: 'Syne', sans-serif; font-weight: 600; font-size: 28px; letter-spacing: -0.5px; }
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
  .data-table { width: 100%; border-collapse: collapse; font-size: 11px; }
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

  /* Sat-flow card */
  .flow-select { margin-left: auto; background: var(--surface); color: var(--text); border: 1px solid var(--border); font-family: 'Space Mono', monospace; font-size: 10px; padding: 3px 6px; cursor: pointer; text-transform: none; letter-spacing: 0; }
  .flow-filters { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; margin-bottom: 14px; }
  .flow-filters label { font-size: 10px; color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; display: flex; align-items: center; }
  .flow-filters .flow-select { margin-left: 4px; }
  .flow-clear { font-size: 10px; color: var(--red); text-decoration: none; border: 1px solid rgba(255,77,109,0.3); padding: 3px 8px; }
  .flow-clear:hover { background: rgba(255,77,109,0.1); }
  .flow-row { margin-bottom: 9px; }
  .flow-row-top { display: flex; justify-content: space-between; font-size: 11px; gap: 8px; margin-bottom: 3px; }

  .empty-state { padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }
  .error-card { background: rgba(255,77,109,0.08); border: 1px solid rgba(255,77,109,0.3); padding: 20px; margin-bottom: 16px; color: var(--red); font-size: 13px; }

  /* ─── Mobile responsive fixes ─── */
  @media (max-width: 600px) {
    .wrap { padding: 12px 8px; }
    .card { padding: 12px; }
    .card-title { font-size: 13px; }
    .data-table { font-size: 10px; min-width: 300px; }
    .data-table th { padding: 6px 6px; font-size: 9px; }
    .data-table td { padding: 6px 6px; }
    .chan-table { font-size: 10px; }
    .chan-table th, .chan-table td { padding: 6px 4px; }
    .stat-label { font-size: 10px; }
    .stat-value { font-size: 11px; max-width: 55%; }
    .balance-grid { gap: 6px; }
    h1 { font-size: 16px; }
    h2 { font-size: 14px; }
  }

  @media (max-width: 400px) {
    .wrap { padding: 8px 4px; }
    .data-table { font-size: 9px; }
    .data-table th { padding: 4px 3px; }
    .data-table td { padding: 5px 3px; }
  }

  /* Ensure all tables scroll horizontally on small screens */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }

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

  <!-- Watchtowers -->
  {% if data.watchtowers is defined %}
  {% set wt = data.watchtowers %}
  {#
    Health rule:
      red    — wtclient off / no towers configured / failed_backups > 0
      yellow — towers configured but 0 active (existing sessions still
               back up but no new ones can be opened)
      green  — ≥1 active tower, 0 failed
    Pending is shown as a number but does NOT affect the badge: a transient
    pending=1 during session rollover (current session hits max-updates and
    LND negotiates a new one) is normal, not a fault.
    'active_session_candidate' is LND's admin flag; not a liveness probe.
  #}
  {% set wt_red    = (not wt.enabled) or (wt.enabled and wt.count == 0) or (wt.failed > 0) %}
  {% set wt_yellow = (not wt_red) and (wt.active_count == 0 or wt.error) %}
  {% set wt_green  = (not wt_red) and (not wt_yellow) %}
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Watchtowers</div>
      <div class="stat-row">
        <span class="stat-label">Active</span>
        <span class="stat-value {% if wt.active_count == 0 %}red{% else %}green{% endif %}" style="font-size:18px;font-weight:700;">
          {{ wt.active_count }}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Configured (incl. deactivated)</span>
        <span class="stat-value">
          {{ wt.count }}{% if wt.inactive_count > 0 %} <span style="color:var(--muted);font-size:11px;">({{ wt.inactive_count }} deactivated)</span>{% endif %}
        </span>
      </div>
      {% if wt.enabled and not wt.error %}
      <div class="stat-row">
        <span class="stat-label">Backups delivered (lifetime)</span>
        <span class="stat-value">{{ "{:,}".format(wt.num_backups) }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Pending / failed</span>
        <span class="stat-value {% if wt.failed > 0 %}red{% endif %}">{{ wt.pending }} / {{ wt.failed }}</span>
      </div>
      {% endif %}
      <div class="stat-row">
        <span class="stat-label">Status</span>
        <span>
          {% if not wt.enabled %}<span class="badge badge-red">wtclient disabled</span>
          {% elif wt.error %}<span class="badge badge-yellow" title="{{ wt.error }}">error</span>
          {% elif wt.count == 0 %}<span class="badge badge-red">no towers</span>
          {% elif wt.failed > 0 %}<span class="badge badge-red" title="permanent backup failures recorded">failed backups</span>
          {% elif wt.active_count == 0 %}<span class="badge badge-yellow" title="towers exist but all deactivated — existing sessions still work until they fill, then no new ones will open">deactivated</span>
          {% else %}<span class="badge badge-green">healthy</span>{% endif %}
        </span>
      </div>
    </div>
  </div>
  {% endif %}

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
            <th>Local Fee</th>
            <th>Remote Fee</th>
            <th>Revenue 30d</th>
            <th>Rebal Cost 30d</th>
            <th>Net 30d</th>
            <th>Net Lifetime</th>
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
            <td style="font-size:11px;">
              {% if ch.local_fee_ppm is not none %}{{ ch.local_fee_ppm }} <span style="color:var(--muted);font-size:9px;">ppm</span>{% else %}<span style="color:var(--muted);">—</span>{% endif %}
            </td>
            <td style="font-size:11px;">
              {% if ch.remote_fee_ppm is not none %}{{ ch.remote_fee_ppm }} <span style="color:var(--muted);font-size:9px;">ppm</span>{% else %}<span style="color:var(--muted);">—</span>{% endif %}
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
  </div>
  {% endif %}

  <!-- Channel Backup -->
  {% if data.backup %}
  {% set bk = data.backup %}
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Channel Backup — Off-site</div>
      {% set badge_class = {'fresh':'badge-green','error':'badge-yellow','stale':'badge-red','never':'badge-muted'}[bk.status] %}
      {% set badge_label = {'fresh':'fresh','error':'last attempt failed','stale':'stale','never':'no backup yet'}[bk.status] %}
      <div class="stat-row">
        <span class="stat-label">Status</span>
        <span><span class="badge {{ badge_class }}">{{ badge_label }}</span></span>
      </div>
      {% if bk.last_success %}
      <div class="stat-row">
        <span class="stat-label">Last successful upload</span>
        <span class="stat-value">{{ bk.last_success.ts | format_ts }} &nbsp;<span style="color:var(--muted)">({{ bk.last_success.ts | format_age }})</span></span>
      </div>
      <div class="stat-row">
        <span class="stat-label">File size</span>
        <span class="stat-value">{{ "{:,}".format(bk.last_success.file_bytes or 0) }} bytes</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Destination</span>
        <span class="stat-value">{{ bk.last_success.destination }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Trigger</span>
        <span class="stat-value">{{ bk.last_success.trigger }}</span>
      </div>
      {% else %}
      <div class="stat-row">
        <span class="stat-label">Last successful upload</span>
        <span class="stat-value red">never</span>
      </div>
      {% endif %}
      {% if bk.last_attempt and not bk.last_attempt.success %}
      <div class="stat-row">
        <span class="stat-label">Last error ({{ bk.last_attempt.ts | format_age }})</span>
        <span class="stat-value red" style="max-width:75%;">{{ bk.last_attempt.error or '—' }}</span>
      </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

  <!-- Forwarding Failures — Lost-Revenue Watch -->
  {% if data.fwd_fail %}
  {% set ff = data.fwd_fail %}
  <div class="grid-1">
    <div class="card">
      <div class="card-title">Forwarding Failures — Lost-Revenue Watch (24h)</div>
      {#
        Service badge tracks the htlc_monitor daemon. These events are live-only
        (LND persists them nowhere), so a dead daemon = a blind window, NOT a
        clean day — that's why an inactive service is flagged red even at 0.
      #}
      <div class="stat-row">
        <span class="stat-label">Monitor service</span>
        <span>
          {% if ff.service == 'active' %}<span class="badge badge-green" title="lnd-htlc-monitor running — stream subscribed">alive &amp; polling</span>
          {% elif ff.service == 'unknown' %}<span class="badge badge-muted" title="could not query systemctl">unknown</span>
          {% else %}<span class="badge badge-red" title="lnd-htlc-monitor not running — failures going uncaptured">{{ ff.service }} — not capturing</span>{% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Dropped forwards</span>
        <span class="stat-value {% if ff.total_n > 0 %}yellow{% endif %}">
          {{ ff.total_n }} {% if ff.total_n %}<span style="color:var(--muted);font-size:11px;">({{ "{:,}".format(ff.total_sats) }} sats of flow)</span>{% endif %}
        </span>
      </div>
      {% if ff.liq_n > 0 %}
      <div class="stat-row">
        <span class="stat-label">↳ Empty-channel (recoverable)</span>
        <span class="stat-value red">{{ ff.liq_n }} drops · {{ "{:,}".format(ff.liq_sats) }} sats</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Est. lost routing fees</span>
        <span class="stat-value red" style="font-weight:700;" title="dropped sats × our outbound ppm on the starved channel">~{{ "{:,}".format(ff.est_lost_fee) }} sats</span>
      </div>
      {% if ff.top %}
      <div class="stat-row">
        <span class="stat-label">Worst channel</span>
        <span class="stat-value">{{ ff.top.alias }} <span style="color:var(--muted);font-size:11px;">({{ "{:,}".format(ff.top.sats) }} sats over {{ ff.top.n }} drops — refill it)</span></span>
      </div>
      {% endif %}
      {% endif %}
      {% if ff.fee_n > 0 %}
      <div class="stat-row">
        <span class="stat-label">↳ Fee-too-low (our fee &gt; route)</span>
        <span class="stat-value">{{ ff.fee_n }} <span style="color:var(--muted);font-size:11px;">— sender under-paid; not a liquidity issue</span></span>
      </div>
      {% endif %}
      {% if ff.other_n > 0 %}
      <div class="stat-row">
        <span class="stat-label">↳ Other (expiry / misc)</span>
        <span class="stat-value" style="color:var(--muted)">{{ ff.other_n }}</span>
      </div>
      {% endif %}
      {% if ff.total_n == 0 %}
      <div class="stat-row">
        <span class="stat-label">{% if ff.service == 'active' %}Clean — no forwards dropped in 24h{% else %}No data{% endif %}</span>
        <span class="stat-value {% if ff.service == 'active' %}green{% else %}red{% endif %}">{% if ff.service == 'active' %}✓{% else %}service down{% endif %}</span>
      </div>
      {% endif %}
      {% if ff.last_event_ts %}
      <div class="stat-row">
        <span class="stat-label">Last drop captured</span>
        <span class="stat-value" style="color:var(--muted)">{{ ff.last_event_ts | format_age }}</span>
      </div>
      {% endif %}
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

  <!-- Sat Flow — routing map (in→out) -->
  {% if data.sat_flow %}
  {% set sf = data.sat_flow %}
  <div class="grid-1">
    <div class="card" id="sat-flow">
      <div class="card-title">
        Sat Flow — Where Sats Route ({{ sf.window_label }})
        <select class="flow-select" onchange="location=this.value">
          <option value="?flow_window=30&flow_in={{ sf.flow_in }}&flow_out={{ sf.flow_out }}#sat-flow"  {% if sf.window_key=='30'  %}selected{% endif %}>Last 30d</option>
          <option value="?flow_window=7&flow_in={{ sf.flow_in }}&flow_out={{ sf.flow_out }}#sat-flow"   {% if sf.window_key=='7'   %}selected{% endif %}>Last 7d</option>
          <option value="?flow_window=all&flow_in={{ sf.flow_in }}&flow_out={{ sf.flow_out }}#sat-flow" {% if sf.window_key=='all' %}selected{% endif %}>All time</option>
        </select>
      </div>

      <div class="flow-filters">
        <label>In&nbsp;
          <select class="flow-select" onchange="location=this.value">
            <option value="?flow_window={{ sf.window_key }}&flow_out={{ sf.flow_out }}#sat-flow" {% if not sf.flow_in %}selected{% endif %}>All sources</option>
            {% for cid, al in sf.in_options %}
            <option value="?flow_window={{ sf.window_key }}&flow_out={{ sf.flow_out }}&flow_in={{ cid }}#sat-flow" {% if sf.flow_in==cid %}selected{% endif %}>{{ al }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Out&nbsp;
          <select class="flow-select" onchange="location=this.value">
            <option value="?flow_window={{ sf.window_key }}&flow_in={{ sf.flow_in }}#sat-flow" {% if not sf.flow_out %}selected{% endif %}>All destinations</option>
            {% for cid, al in sf.out_options %}
            <option value="?flow_window={{ sf.window_key }}&flow_in={{ sf.flow_in }}&flow_out={{ cid }}#sat-flow" {% if sf.flow_out==cid %}selected{% endif %}>{{ al }}</option>
            {% endfor %}
          </select>
        </label>
        {% if sf.filtered %}<a class="flow-clear" href="?flow_window={{ sf.window_key }}#sat-flow">clear ✕</a>{% endif %}
      </div>

      {% if sf.pairs %}
      <div style="font-size:11px;color:var(--muted);margin-bottom:14px;">
        {{ "{:,}".format(sf.total_sats) }} sats routed across
        {{ "{:,}".format(sf.total_n) }} forwards ·
        <span style="color:var(--green)">{{ "{:,}".format(sf.total_fee) }} sats earned</span>
      </div>

      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>In (source)</th><th>Out (destination)</th><th style="width:30%">Sats routed</th><th>Fwds</th><th>Fee</th></tr></thead>
        <tbody>
          {% for p in sf.pairs %}
          <tr>
            <td class="truncate">{{ p.in_alias }}</td>
            <td class="truncate" style="color:var(--accent)">→ {{ p.out_alias }}</td>
            <td>
              {{ "{:,}".format(p.sats) }}
              <div class="balance-bar"><div class="balance-bar-fill bar-healthy" style="width:{{ ((p.sats / sf.max_pair) * 100) | round | int if sf.max_pair else 0 }}%"></div></div>
            </td>
            <td class="amount-muted">{{ p.n }}</td>
            <td class="amount-positive">{{ "{:,}".format(p.fee) }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table></div>

      <div class="grid-2" style="margin-top:18px;margin-bottom:0;">
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px;">↓ Inbound — where sats come from</div>
          {% for s in sf.sources %}
          <div class="flow-row">
            <div class="flow-row-top"><span class="truncate">{{ s.alias }}</span><span class="amount-muted">{{ "{:,}".format(s.sats) }}</span></div>
            <div class="balance-bar"><div class="balance-bar-fill bar-saturated" style="width:{{ ((s.sats / sf.max_source) * 100) | round | int if sf.max_source else 0 }}%"></div></div>
          </div>
          {% endfor %}
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px;">↑ Outbound — where sats go to</div>
          {% for s in sf.sinks %}
          <div class="flow-row">
            <div class="flow-row-top"><span class="truncate">{{ s.alias }}</span><span class="amount-muted">{{ "{:,}".format(s.sats) }}</span></div>
            <div class="balance-bar"><div class="balance-bar-fill" style="width:{{ ((s.sats / sf.max_sink) * 100) | round | int if sf.max_sink else 0 }}%;background:var(--accent)"></div></div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% else %}
      <div class="empty-state">{% if sf.filtered %}No flows match this filter in the window — try “clear ✕”.{% else %}No routing flows in this window yet{% endif %}</div>
      {% endif %}
    </div>
  </div>
  {% endif %}

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
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Time</th><th>Route</th><th>Amount</th><th>Fee</th><th></th><th>Source</th></tr></thead>
        <tbody>
          {% for r in data.rebalances %}
          <tr>
            <td style="color:var(--muted);font-size:10px;white-space:nowrap;">{{ r.ts | format_ts }}</td>
            <td>{{ r.source_alias[:10] }} → {{ r.target_alias[:10] }}</td>
            <td>{{ "{:,}".format(r.amount_sats) }}</td>
            <td class="{% if r.success %}amount-negative{% else %}amount-muted{% endif %}">
              {% if r.success %}-{{ "{:,}".format(r.fee_paid_sats) }} <span style="color:var(--muted)">({{ r.fee_ppm | int }}ppm)</span>
              {% elif r.budget_ppm %}<span style="color:var(--muted)" title="max-fee budget we tried with — nothing was paid (failed)">≤{{ r.budget_ppm | int }}ppm</span>
              {% else %}—{% endif %}
            </td>
            <td>{% if r.success %}<span class="badge badge-green">✓</span>{% else %}<span class="badge badge-red" title="{{ r.failure_reason }}">✗</span>{% endif %}</td>
            <td>{% if r.triggered_by == 'manual' %}<span class="badge badge-blue">manual</span>{% else %}<span class="badge badge-muted">auto</span>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
      {% else %}
      <div class="empty-state">No rebalances yet</div>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Recent Fee Updates</div>
      {% if data.fee_updates %}
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Time</th><th>Peer</th><th>Old</th><th>New</th><th>Local</th><th>Source</th></tr></thead>
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
            <td>
              {% if u.source == 'pin' %}<span class="badge badge-blue" title="{{ u.reason }}">📌 pin</span>
              {% else %}<span class="badge badge-muted">auto</span>{% endif %}
            </td>
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
      <div class="table-wrap"><table class="data-table">
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


@app.template_filter("format_age")
def format_age(ts):
    """Jinja2 filter: returns 'N min ago' / 'Nh Nm ago' / 'Nd ago' for a unix ts."""
    try:
        delta = int(datetime.now().timestamp()) - int(ts)
        if delta < 0:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60} min ago"
        if delta < 86400:
            h, m = divmod(delta // 60, 60)
            return f"{h}h {m}m ago" if m else f"{h}h ago"
        d, rem = divmod(delta, 86400)
        h = rem // 3600
        return f"{d}d {h}h ago" if h else f"{d}d ago"
    except Exception:
        return "—"


@app.route("/")
def index():
    """Single route — fetches fresh data on every page load."""
    data = get_dashboard_data()
    return render_template_string(TEMPLATE, data=data)


if __name__ == "__main__":
    app.run(host=BIND_IP, port=PORT, debug=False)
