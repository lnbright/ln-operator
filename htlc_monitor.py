"""
LN Operator — HTLC failure monitor (always-on daemon).

Subscribes to LND's SubscribeHtlcEvents stream (REST: /v2/router/htlcevents)
and records every forward we FAILED to route into the forward_fail_log table.

Why a daemon and not the 2h pipeline: these events are live-only. LND emits a
link-failure event the instant it drops an HTLC and persists it nowhere — there
is no "list failed forwards" RPC to poll after the fact. So this listener is the
*only* way to capture routing demand we couldn't serve (a channel too depleted
to forward, a sender who under-paid our fee, an expiry mismatch). The daily check
then reads the accumulated table; any window this daemon is down is simply a gap.

Run via systemd (services/lnd-htlc-monitor.service) or `ln-operator monitor_htlcs`.
The stream reconnects with backoff on drop, and one malformed event never kills
the loop.
"""
import json
import time

import requests

from config import LND_REST_URL, LND_CERT, LND_MACAROON
from logging_config import get_logger
import db
import lnd_client

log = get_logger("htlc_monitor")

# Reconnect backoff bounds (seconds) for genuine stream drops.
_RECONNECT_MIN = 2
_RECONNECT_MAX = 60
# Read timeout: a healthy but idle node sends no events, so a long read
# timeout just triggers a quiet resubscribe rather than a stuck socket.
_READ_TIMEOUT = 3600
# How often the scid→alias cache may refresh on a miss.
_ALIAS_REFRESH_SEC = 600


def parse_link_failure(event: dict):
    """Pure: map a raw htlcevent dict → a forward_fail_log row, or None.

    Returns None for anything that isn't an HTLC we failed at our own link —
    settles, plain forwards, and downstream forward-fails (where some *other*
    node failed and the error merely passed back through us) all return None.
    Only `link_fail_event` carries the wire_failure / failure_detail that tells
    us why *we* dropped it, which is the whole point of this table.
    """
    lf = event.get("link_fail_event")
    if not lf:
        return None
    info = lf.get("info") or {}
    # Prefer the outgoing amount (what we'd have forwarded); fall back to the
    # incoming amount for failures caught before the outgoing leg is built.
    amt = info.get("outgoing_amt_msat") or info.get("incoming_amt_msat") or 0
    return {
        "ts_ns": str(event.get("timestamp_ns") or ""),
        "chan_in": str(event.get("incoming_channel_id") or ""),
        "chan_out": str(event.get("outgoing_channel_id") or ""),
        "htlc_in": str(event.get("incoming_htlc_id") or ""),
        "htlc_out": str(event.get("outgoing_htlc_id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "amount_msat": int(amt),
        "wire_failure": str(lf.get("wire_failure") or ""),
        "failure_detail": str(lf.get("failure_detail") or ""),
        "failure_string": str(lf.get("failure_string") or ""),
    }


class _AliasCache:
    """scid → peer alias, refreshed lazily on a miss (throttled).

    Resolving at write time keeps the stored rows readable even if a channel
    later closes, and the cache means a burst of failures on one channel costs
    at most one /v1/channels round-trip per refresh window.
    """

    def __init__(self):
        self._map = {}
        self._last_refresh = 0.0

    def get(self, scid: str) -> str:
        if not scid or scid == "0":
            return ""
        if scid in self._map:
            return self._map[scid]
        now = time.monotonic()
        if now - self._last_refresh > _ALIAS_REFRESH_SEC:
            self._refresh()
        return self._map.get(scid, "")

    def _refresh(self):
        self._last_refresh = time.monotonic()
        try:
            chans = lnd_client.resolve_aliases(lnd_client.get_channels())
            self._map = {c["chan_id"]: c["peer_alias"]
                         for c in chans if c.get("chan_id")}
        except Exception as e:  # never let an alias lookup break the stream
            log.warning("alias cache refresh failed: %s", e)


def _macaroon_hex() -> str:
    with open(LND_MACAROON, "rb") as f:
        return f.read().hex()


def _stream_once(aliases: _AliasCache):
    """Open the htlcevents stream and process events until it closes/errors."""
    url = f"{LND_REST_URL}/v2/router/htlcevents"
    headers = {"Grpc-Metadata-macaroon": _macaroon_hex()}
    # (connect timeout, read timeout): connect should be quick; reads block on
    # an idle node until _READ_TIMEOUT, which surfaces as a clean resubscribe.
    r = requests.get(url, headers=headers, verify=LND_CERT, stream=True,
                     timeout=(10, _READ_TIMEOUT))
    r.raise_for_status()
    log.info("subscribed to htlcevents stream")

    for line in r.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except Exception as e:
            log.warning("could not parse htlcevent line: %s", e)
            continue
        if "error" in chunk:
            log.warning("htlcevents error frame: %s", chunk["error"])
            return
        event = chunk.get("result", chunk)
        row = parse_link_failure(event)
        if not row:
            continue
        row["alias_in"] = aliases.get(row["chan_in"])
        row["alias_out"] = aliases.get(row["chan_out"])
        try:
            db.save_forward_failure(row)
            log.info("dropped forward %s→%s %d msat (%s / %s)",
                     row["alias_in"] or row["chan_in"] or "?",
                     row["alias_out"] or row["chan_out"] or "?",
                     row["amount_msat"],
                     row["wire_failure"] or "?",
                     row["failure_detail"] or "?")
        except Exception as e:
            log.error("could not save forward failure: %s", e)


def run():
    """Subscribe forever, reconnecting on drop. Returns only on KeyboardInterrupt."""
    log.info("htlc_monitor starting")
    aliases = _AliasCache()
    backoff = _RECONNECT_MIN
    while True:
        try:
            _stream_once(aliases)
            # Clean return (LND closed the stream) — resubscribe promptly.
            backoff = _RECONNECT_MIN
            continue
        except requests.exceptions.ReadTimeout:
            # Idle node, no events within _READ_TIMEOUT — not an error.
            log.debug("htlcevents idle read-timeout — resubscribing")
            backoff = _RECONNECT_MIN
            continue
        except KeyboardInterrupt:
            log.info("htlc_monitor stopping")
            return
        except Exception as e:
            log.warning("htlcevents stream dropped: %s — reconnecting in %ds",
                        e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)
