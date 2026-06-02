"""
LN Operator — LND REST API Client

All communication with the LND node goes through this module. Every function
maps to one LND REST endpoint. The rest of the codebase never calls LND directly.

Authentication: LND uses macaroon-based auth. The macaroon file (a binary token)
is read from disk, hex-encoded, and sent as an HTTP header on every request.

TLS: LND uses a self-signed TLS cert. We pass the cert path to requests for
verification rather than disabling TLS entirely (verify=LND_CERT, not verify=False).

Key endpoints used:
- /v1/getinfo: node identity, sync status, channel counts
- /v1/channels: all open channels with balances
- /v1/fees + /v1/chanpolicy: read and update fee policies
- /v1/switch: forwarding history (routing events through your node)
- /v2/router/send: circular rebalance payments (SendPaymentV2 with forced routing)
- /v1/balance/*: on-chain and channel balances
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


def _get(endpoint, params=None, timeout=30):
    """GET request to LND REST API."""
    url = f"{LND_REST_URL}{endpoint}"
    log.debug("GET %s", endpoint)
    r = requests.get(url, headers=_headers(), verify=LND_CERT, params=params, timeout=timeout)
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
            "scid": ch.get("scid", ""),
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

def estimate_fee(conf_target=2):
    """Get on-chain fee estimate from LND (uses Bitcoin Core's estimatesmartfee).

    conf_target: number of blocks to confirm in (1=fastest, 6=economy)
    Returns fee rate in sat/vB, or None if unavailable.

    This is preferred over external APIs (mempool.space) since it uses
    your own Bitcoin Core node — no external dependency.
    """
    try:
        # LND's fee estimate endpoint returns sat/kw (satoshis per kilo-weight)
        # 1 vByte = 4 weight units, so sat/vB = sat/kw × 4 / 1000
        result = _get(f"/v2/wallet/estimatefee/{conf_target}")
        sat_per_kw = int(result.get("sat_per_kw", 0))
        if sat_per_kw > 0:
            sat_per_vb = max(1, round(sat_per_kw * 4 / 1000))
            log.debug("fee estimate: %d sat/kw → %d sat/vB (conf_target=%d)",
                      sat_per_kw, sat_per_vb, conf_target)
            return sat_per_vb
    except Exception as e:
        log.debug("fee estimate from LND failed: %s", e)
    return None


def get_onchain_balance():
    """Get on-chain wallet balance."""
    return _get("/v1/balance/blockchain")


def get_channel_balance():
    """Get aggregate channel balance."""
    return _get("/v1/balance/channels")


# ─── Forwarding (routing) history ────────────────────────────────

def get_forwarding_history(start_time=0, end_time=None, max_events=1000,
                              index_offset=0):
    """Get forwarding (routing) events.
    
    index_offset: fetch only events with index > this value (0 = all).
    Returns (events, last_offset_index) tuple.
    """
    if end_time is None:
        import time
        end_time = int(time.time())
    data = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "num_max_events": max_events,
        "index_offset": index_offset,
    }
    result = _post("/v1/switch", data)
    events = result.get("forwarding_events", [])
    last_offset = int(result.get("last_offset_index", 0))
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
    return parsed, last_offset


# ─── Graph ───────────────────────────────────────────────────────

def describe_graph(include_unannounced=False):
    """Get the full network graph (can be large).
    Returns nodes and edges.
    """
    params = {"include_unannounced": str(include_unannounced).lower()}
    # Full graph dump is multi-MB JSON — on a Pi the serialize+transfer
    # can comfortably exceed the default 30s.
    return _get("/v1/graph", params, timeout=300)


# ─── Payments & Invoices ─────────────────────────────────────────

def send_payment_v2(payment_request, outgoing_chan_id, last_hop_pubkey,
                    fee_limit_sat, timeout_seconds=120):
    """Send a circular rebalance payment using the Router RPC.

    Forces the payment out through a specific channel (outgoing_chan_id)
    and back in through a specific peer (last_hop_pubkey).

    - outgoing_chan_id: chan_id of the overfull channel (source)
    - last_hop_pubkey: pubkey of the depleted channel peer (target)
    - fee_limit_sat: max fee we will pay in sats
    - allow_self_payment: must be True for circular payments

    Returns dict with keys: status, fee_sat, failure_reason
    The endpoint streams JSON results — we read until we get a terminal status.
    """
    import base64
    log.debug("send_payment_v2: outgoing_chan=%s last_hop=%s fee_limit=%d",
              outgoing_chan_id, last_hop_pubkey[:12], fee_limit_sat)

    # last_hop_pubkey must be base64-encoded bytes
    last_hop_bytes = bytes.fromhex(last_hop_pubkey)
    last_hop_b64 = base64.b64encode(last_hop_bytes).decode()

    data = {
        "payment_request": payment_request,
        "outgoing_chan_id": str(outgoing_chan_id),
        "last_hop_pubkey": last_hop_b64,
        "fee_limit_sat": str(fee_limit_sat),
        "timeout_seconds": timeout_seconds,
        "allow_self_payment": True,
        "no_inflight_updates": False,
    }

    url = f"{LND_REST_URL}/v2/router/send"

    # This endpoint streams responses — read chunks until we get a terminal status
    r = requests.post(
        url,
        headers=_headers(),
        verify=LND_CERT,
        json=data,
        stream=True,
        timeout=timeout_seconds + 30,
    )
    r.raise_for_status()

    last_result = None
    for line in r.iter_lines():
        if not line:
            continue
        try:
            import json as _json
            chunk = _json.loads(line)
            # Each chunk is wrapped: {"result": {...}} or {"error": {...}}
            if "error" in chunk:
                return {
                    "status": "FAILED",
                    "fee_sat": 0,
                    "failure_reason": chunk["error"].get("message", "unknown error"),
                }
            result = chunk.get("result", chunk)
            last_result = result
            status = result.get("status", "")
            if status in ("SUCCEEDED", "FAILED", "FAILED_NO_ROUTE"):
                break
        except Exception as e:
            log.warning("could not parse payment stream chunk: %s", e)
            continue

    if last_result is None:
        return {"status": "FAILED", "fee_sat": 0, "failure_reason": "no response from router"}

    status = last_result.get("status", "FAILED")
    fee_sat = 0

    if status == "SUCCEEDED":
        # Prefer the canonical Payment.fee_sat — LND populates it from
        # SUCCEEDED HTLCs only. Falling back to summing HTLC route fees
        # overcounts when LND retried failed shards: every attempt's route
        # shows up in last_result.htlcs and adds its quoted fee to the total
        # even though only the succeeded HTLC actually settled.
        fee_sat = int(last_result.get("fee_sat") or 0)
        if fee_sat == 0:
            fee_msat = int(last_result.get("fee_msat") or 0)
            if fee_msat:
                fee_sat = fee_msat // 1000
            else:
                for htlc in last_result.get("htlcs", []):
                    if htlc.get("status") != "SUCCEEDED":
                        continue
                    fee_sat += int(htlc.get("route", {}).get("total_fees", 0))

    failure_reason = last_result.get("failure_reason", "") if status != "SUCCEEDED" else ""
    payment_hash = last_result.get("payment_hash", "")

    log.debug("send_payment_v2 result: status=%s fee=%d sats reason=%s",
              status, fee_sat, failure_reason)

    return {
        "status": status,
        "fee_sat": fee_sat,
        "failure_reason": failure_reason,
        "payment_hash": payment_hash,
    }


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


def new_address(addr_type="p2wkh"):
    """Generate a new on-chain address from LND.

    addr_type: p2wkh (native segwit bech32, recommended) or p2tr (taproot)
    Returns the address string or None on failure.
    """
    type_map = {"p2wkh": 0, "np2wkh": 1, "p2tr": 4}
    addr_int = type_map.get(addr_type, 0)
    result = _get(f"/v1/newaddress?type={addr_int}")
    return result.get("address") if result else None
