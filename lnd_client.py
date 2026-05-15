"""
LN Operator — LND REST API Client
Handles all communication with the LND node.
"""

import json
import requests
import urllib3
from config import LND_REST_URL, LND_CERT, LND_MACAROON
from logging_config import get_logger

log = get_logger("lnd_client")

# LND uses self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _headers():
    """Read macaroon and return auth headers."""
    with open(LND_MACAROON, "rb") as f:
        macaroon_hex = f.read().hex()
    return {"Grpc-Metadata-macaroon": macaroon_hex}


def _get(endpoint, params=None):
    """GET request to LND REST API."""
    url = f"{LND_REST_URL}{endpoint}"
    log.debug("GET %s", endpoint)
    r = requests.get(url, headers=_headers(), verify=LND_CERT, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(endpoint, data=None):
    """POST request to LND REST API."""
    url = f"{LND_REST_URL}{endpoint}"
    log.debug("POST %s", endpoint)
    r = requests.post(url, headers=_headers(), verify=LND_CERT, json=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


# ─── Node info ───────────────────────────────────────────────────

def get_info():
    """Get basic node information."""
    return _get("/v1/getinfo")


def get_node_info(pubkey, include_channels=False):
    """Get info about a specific node on the network."""
    params = {"include_channels": str(include_channels).lower()}
    return _get(f"/v1/graph/node/{pubkey}", params)


# ─── Channels ────────────────────────────────────────────────────

def get_channels():
    """Get all open channels with balances."""
    data = _get("/v1/channels")
    channels = data.get("channels", [])
    result = []
    for ch in channels:
        capacity = int(ch.get("capacity", 0))
        local = int(ch.get("local_balance", 0))
        remote = int(ch.get("remote_balance", 0))
        local_ratio = local / capacity if capacity > 0 else 0

        result.append({
            "chan_id": ch.get("chan_id", ""),
            "channel_point": ch.get("channel_point", ""),
            "peer_pubkey": ch.get("remote_pubkey", ""),
            "peer_alias": "",  # filled in later via get_node_info
            "capacity": capacity,
            "local_balance": local,
            "remote_balance": remote,
            "local_ratio": round(local_ratio, 4),
            "active": ch.get("active", False),
            "private": ch.get("private", False),
            "initiator": ch.get("initiator", False),
            "total_sent": int(ch.get("total_satoshis_sent", 0)),
            "total_received": int(ch.get("total_satoshis_received", 0)),
            "num_updates": int(ch.get("num_updates", 0)),
            "commit_fee": int(ch.get("commit_fee", 0)),
            "commit_weight": int(ch.get("commit_weight", 0)),
            "fee_per_kw": int(ch.get("fee_per_kw", 0)),
            "unsettled_balance": int(ch.get("unsettled_balance", 0)),
            "commitment_type": ch.get("commitment_type", ""),
        })
    return result


def get_pending_channels():
    """Get pending (opening/closing) channels."""
    return _get("/v1/channels/pending")


def get_closed_channels():
    """Get closed channels."""
    return _get("/v1/channels/closed")


# ─── Channel policy (fees) ───────────────────────────────────────

def get_channel_edge(chan_id):
    """Get the edge (fee policy) info for a channel."""
    return _get(f"/v1/graph/edge/{chan_id}")


def get_fee_report():
    """Get fee report for all our channels."""
    return _get("/v1/fees")


def update_channel_policy(channel_point, base_fee_msat, fee_rate_ppm, time_lock_delta=40):
    """Update fee policy for a specific channel.
    
    channel_point format: "txid:output_index"
    """
    txid, output_index = channel_point.split(":")
    data = {
        "chan_point": {
            "funding_txid_str": txid,
            "output_index": int(output_index),
        },
        "base_fee_msat": str(base_fee_msat),
        "fee_rate_ppm": str(fee_rate_ppm),
        "time_lock_delta": time_lock_delta,
    }
    return _post("/v1/chanpolicy", data)


# ─── Balances ────────────────────────────────────────────────────

def get_onchain_balance():
    """Get on-chain wallet balance."""
    return _get("/v1/balance/blockchain")


def get_channel_balance():
    """Get aggregate channel balance."""
    return _get("/v1/balance/channels")


# ─── Forwarding (routing) history ────────────────────────────────

def get_forwarding_history(start_time=0, end_time=None, max_events=1000):
    """Get forwarding (routing) events."""
    if end_time is None:
        import time
        end_time = int(time.time())
    data = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "num_max_events": max_events,
    }
    result = _post("/v1/switch", data)
    events = result.get("forwarding_events", [])
    parsed = []
    for ev in events:
        parsed.append({
            "timestamp": int(ev.get("timestamp", 0)),
            "chan_in": ev.get("chan_id_in", ""),
            "chan_out": ev.get("chan_id_out", ""),
            "amount_in": int(ev.get("amt_in", 0)),
            "amount_out": int(ev.get("amt_out", 0)),
            "fee_earned": int(ev.get("fee", 0)),
            "fee_msat": int(ev.get("fee_msat", 0)),
        })
    return parsed


# ─── Peers ───────────────────────────────────────────────────────

def get_peers():
    """Get connected peers."""
    data = _get("/v1/peers")
    return data.get("peers", [])


# ─── Graph ───────────────────────────────────────────────────────

def describe_graph(include_unannounced=False):
    """Get the full network graph (can be large).
    Returns nodes and edges.
    """
    params = {"include_unannounced": str(include_unannounced).lower()}
    return _get("/v1/graph", params)


def get_network_info():
    """Get high-level network statistics."""
    return _get("/v1/graph/info")


# ─── Payments & Invoices ─────────────────────────────────────────

def send_payment(payment_request, fee_limit_sat=None, timeout_seconds=60):
    """Send a payment (used for circular rebalancing).
    
    For rebalancing, we'd use the router RPC, but this is the basic version.
    """
    data = {
        "payment_request": payment_request,
        "timeout_seconds": timeout_seconds,
    }
    if fee_limit_sat is not None:
        data["fee_limit"] = {"fixed": str(fee_limit_sat)}
    return _post("/v1/channels/transactions", data)


def add_invoice(amount_sats, memo="ln-operator-rebalance", expiry=3600):
    """Create an invoice (used for circular rebalancing)."""
    data = {
        "value": str(amount_sats),
        "memo": memo,
        "expiry": str(expiry),
    }
    return _post("/v1/invoices", data)


# ─── Utility ─────────────────────────────────────────────────────

def resolve_aliases(channels):
    """Resolve peer pubkeys to aliases. Modifies channels in place."""
    cache = {}
    for ch in channels:
        pk = ch["peer_pubkey"]
        if pk not in cache:
            try:
                info = get_node_info(pk)
                cache[pk] = info.get("node", {}).get("alias", pk[:12])
            except Exception:
                log.debug("could not resolve alias for %s", pk[:12])
                cache[pk] = pk[:12]
        ch["peer_alias"] = cache[pk]
    return channels
