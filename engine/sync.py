"""
LN Operator — Routing sync.

Two pull-only loops:

  sync_forwarding_history — offset-paginates LND's forwarding events into
                            forwarding_log so the dashboard and fee math
                            have something to read.
  sync_rebalances         — scans payments for circular self-payments and
                            imports any we didn't initiate (manual rebalances
                            done via lncli) into rebalance_log.

Plus chan_open_ts_from_id, a small helper used by sync_rebalances to ignore
payments older than the target channel itself (prevents attributing pre-open
activity to a freshly re-opened channel with the same peer).
"""

import time
import lnd_client
import db
from logging_config import get_logger

log = get_logger('engine.sync')


def chan_open_ts_from_id(chan_id, current_block_height, now):
    """Estimate a channel's open timestamp from its chan_id.

    LND's chan_id encodes the funding tx's block height in the high bits
    (block_height << 40). Combined with the current chain tip we can
    approximate when the channel was funded at ~600 s/block. Used as a
    floor when attributing self-payments — if a payment is older than the
    target channel's open time, it must belong to a previous channel with
    the same peer.

    Returns 0 if the chan_id can't be parsed. Subtracts a 1-day margin to
    stay safely earlier than the true open time (block intervals vary).
    """
    try:
        open_block = int(chan_id) >> 40
    except (ValueError, TypeError):
        return 0
    if open_block <= 0 or current_block_height <= 0 or open_block > current_block_height:
        return 0
    return max(0, now - (current_block_height - open_block) * 600 - 86400)


def sync_forwarding_history():
    """Fetch new forwarding events from LND using offset-based pagination.

    Reads the last seen offset from sync_state, fetches only new events,
    saves them (with duplicate protection via lnd_index), then updates
    the offset. Safe to call from both cron and manual runs — will never
    write the same event twice.
    """
    # Get the last offset we successfully synced
    last_offset = int(db.get_sync_state("forwarding_index", 0))
    log.debug("sync_forwarding_history: starting from offset %d", last_offset)

    total_synced = 0
    batch_size = 1000

    while True:
        events, new_offset = lnd_client.get_forwarding_history(
            index_offset=last_offset,
            max_events=batch_size,
        )

        if not events:
            break

        db.save_forwarding_events(events)
        total_synced += len(events)
        log.debug("synced batch of %d events, new offset %d", len(events), new_offset)

        # Update the stored offset
        db.set_sync_state("forwarding_index", new_offset)
        last_offset = new_offset

        # If we got fewer events than requested, we're caught up
        if len(events) < batch_size:
            break

    if total_synced > 0:
        log.info("sync_routing: %d new event(s) saved (offset now %d)",
                 total_synced, last_offset)
    else:
        log.info("sync_routing: no new events since last run (offset %d)", last_offset)

    return total_synced


def sync_rebalances():
    """Sync circular rebalance payments from LND into rebalance_log.

    Identifies self-payments by checking if destination == our pubkey.
    For each self-payment that succeeded, extracts the outgoing channel
    (first hop) and incoming channel (last hop's peer) and logs it.
    Skips payments already in the DB (by payment_hash).

    This captures manually-executed rebalances done via lncli payinvoice
    that bypassed our automated rebalancer.
    """
    my_info = lnd_client.get_info()
    my_pubkey = my_info.get("identity_pubkey", "")
    if not my_pubkey:
        log.error("sync_rebalances: could not get node pubkey")
        return 0
    # Chain tip — needed to derive open times from chan_ids
    current_block_height = int(my_info.get("block_height", 0))

    # Build maps of our channel IDs to aliases, peer pubkeys, and open times
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    chan_alias_map = {}
    chan_peer_map = {}    # peer_pubkey -> chan_id
    chan_open_ts = {}     # chan_id -> timestamp when channel opened
    now = int(time.time())
    for ch in channels:
        chan_alias_map[ch["chan_id"]] = ch.get("peer_alias", ch["chan_id"][:12])
        chan_peer_map[ch.get("peer_pubkey", "")] = ch["chan_id"]
        chan_open_ts[ch["chan_id"]] = chan_open_ts_from_id(
            ch["chan_id"], current_block_height, now
        )

    # Fetch all payments from LND
    payments_data = lnd_client._get("/v1/payments?include_incomplete=false&max_payments=100")
    payments = payments_data.get("payments", []) if payments_data else []

    synced = 0
    for pay in payments:
        if pay.get("status") != "SUCCEEDED":
            continue

        payment_hash = pay.get("payment_hash", "")
        if not payment_hash:
            continue

        # Skip if already in DB
        if db.rebalance_exists_by_hash(payment_hash):
            continue

        # Check each HTLC for self-payment pattern
        for htlc in pay.get("htlcs", []):
            if htlc.get("status") != "SUCCEEDED":
                continue

            route = htlc.get("route", {})
            hops = route.get("hops", [])
            if len(hops) < 2:
                continue

            # Last hop destination should be our pubkey (self-payment)
            last_hop = hops[-1]
            if last_hop.get("pub_key") != my_pubkey:
                continue

            # This is a circular self-payment — extract channel info
            # First hop: outgoing channel (source)
            first_hop_chan = hops[0].get("chan_id", "")
            # Second-to-last hop: the peer whose channel received the payment (target)
            second_last_hop = hops[-2]
            target_peer_pubkey = second_last_hop.get("pub_key", "")
            target_chan_id = chan_peer_map.get(target_peer_pubkey, "")

            # Skip if source and target are the same channel — not a real rebalance
            # (e.g. a test self-payment that goes out and comes back on same peer)
            if first_hop_chan == target_chan_id or not target_chan_id:
                continue

            source_alias = chan_alias_map.get(first_hop_chan, first_hop_chan[:12])
            target_alias = chan_alias_map.get(target_chan_id, target_peer_pubkey[:12])

            amount = int(pay.get("value_sat", 0))
            fee = int(pay.get("fee_sat", 0))
            ts = int(pay.get("creation_date", 0))

            # Skip payments older than the target channel's open time
            # Prevents attributing old rebalances to new channels with same peer
            target_opened = chan_open_ts.get(target_chan_id, 0)
            if ts < target_opened:
                log.debug("sync_rebalances: skipping payment %s — older than channel open time",
                          payment_hash[:16])
                continue

            # Backfill payment_hash on a legacy hash-less auto row that matches
            # this payment exactly. The guard `payment_hash IS NULL OR ''` is
            # critical — without it, a second chunk with the same amount/time
            # would overwrite the hash of the first chunk that just synced,
            # silently losing rows. Auto rows now save with hash from the
            # start (engine.execute_rebalance), so this only fires for old
            # data; it never matches a row we already populated.
            with db.get_conn() as conn:
                existing = conn.execute("""
                    SELECT id FROM rebalance_log
                    WHERE source_chan_id = ? AND target_chan_id = ?
                    AND amount_sats = ? AND abs(ts - ?) < 10
                    AND success = 1
                    AND (payment_hash IS NULL OR payment_hash = '')
                """, (first_hop_chan, target_chan_id, amount, ts)).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE rebalance_log SET payment_hash = ?, fee_paid_sats = ?,
                        fee_ppm = ? WHERE id = ?
                    """, (payment_hash, fee,
                          fee / amount * 1_000_000 if amount > 0 else 0,
                          existing["id"]))
                    log.info("sync_rebalances: backfilled hash on legacy auto entry %s→%s",
                             source_alias, target_alias)
                    synced += 1
                    break

            db.save_manual_rebalance(
                source_chan_id=first_hop_chan,
                target_chan_id=target_chan_id,
                source_alias=source_alias,
                target_alias=target_alias,
                amount_sats=amount,
                fee_paid_sats=fee,
                payment_hash=payment_hash,
                ts=ts,
            )
            synced += 1
            log.info("sync_rebalances: found manual rebalance %s→%s %s sats (fee %d sats)",
                     source_alias, target_alias, f"{amount:,}", fee)
            break  # one HTLC per payment is enough

    if synced > 0:
        log.info("sync_rebalances: synced %d manual rebalance(s) from LND", synced)
    return synced
