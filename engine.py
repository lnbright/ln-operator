"""
LN Operator — Channel Management Engine (60% layer)

This is the core of the tool. All operations are deterministic — given the same
channel state and config, you always get the same output. No LLM calls.

Responsibilities:
- Dynamic fee management: adjust each channel's fee rate based on its balance ratio
- Smart rebalancing: move sats between channels with per-channel budget limits
- Health monitoring: snapshot channel states, sync routing history, fire alerts
- Forwarding history sync: pull new routing events from LND using offset pagination

The engine reads live data from LND (via lnd_client) and historical data from
SQLite (via db). It writes results back to SQLite for the dashboard and CLI to read.
"""

import time
import math
from config import (
    REBALANCE_LOW_THRESHOLD, REBALANCE_HIGH_THRESHOLD, REBALANCE_TARGET,
    FEE_BASE_MSAT,
    SIGMOID_MIN_PPM, SIGMOID_MAX_PPM, SIGMOID_K, SIGMOID_MIDPOINT,
    FEE_HARD_CEILING_PPM,
    FEE_HYSTERESIS_TOLERANCE_PPM, FEE_HYSTERESIS_TOLERANCE_PCT,
    FEE_HYSTERESIS_COOLDOWN_SEC, FEE_HYSTERESIS_SNAP_PPM,
    FEE_HYSTERESIS_EDGE_LOW, FEE_HYSTERESIS_EDGE_HIGH,
    MARKET_MULT_STEP, MARKET_MULT_MIN, MARKET_MULT_MAX,
    MARKET_MULT_BUSY_HOURS, MARKET_MULT_SILENT_DAYS,
    REBALANCE_MAX_AMOUNT_RATIO,
    REBALANCE_DEFAULT_BUDGET_PPM, REBALANCE_MAX_BUDGET_PPM,
    REBALANCE_BUDGET_ESCALATION_STEP, REBALANCE_FEE_MARGIN,
    REBALANCE_BALANCED_RATIO, REBALANCE_BALANCED_RATIO_HIGH,
)
import lnd_client
import db
from logging_config import get_logger


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

log = get_logger('engine')


# ─── Fee Management ──────────────────────────────────────────────

def sigmoid_fee_ppm(local_ratio):
    """Sigmoid mapping from local_ratio to base outbound fee ppm.

    Flat across the healthy middle (~30-70% local), steep at the edges.
    Avoids the linear curve's problem of triggering broadcasts on tiny
    healthy drift while being sluggish in the defense zone.

    f(ratio) = MIN + (MAX - MIN) / (1 + exp( K * (ratio - MIDPOINT) ))
    """
    try:
        x = SIGMOID_K * (local_ratio - SIGMOID_MIDPOINT)
        if x > 700:    # avoid math.exp overflow at extreme ratios
            sig = 0.0
        elif x < -700:
            sig = 1.0
        else:
            sig = 1.0 / (1.0 + math.exp(x))
    except (TypeError, ValueError):
        sig = 0.5
    ppm = SIGMOID_MIN_PPM + (SIGMOID_MAX_PPM - SIGMOID_MIN_PPM) * sig
    return int(round(max(SIGMOID_MIN_PPM, min(SIGMOID_MAX_PPM, ppm))))


# Backwards-compatible alias for any caller still using the old name.
calculate_fee_ppm = sigmoid_fee_ppm


def _edge_zone(local_ratio):
    """Classify which hysteresis edge zone a ratio falls in: 'low', 'high', or 'mid'."""
    if local_ratio < FEE_HYSTERESIS_EDGE_LOW:
        return "low"
    if local_ratio > FEE_HYSTERESIS_EDGE_HIGH:
        return "high"
    return "mid"


def compute_fee_target(channel, signals, now):
    """Compute the target outbound fee for a channel + decide whether to broadcast.

    The outbound floor is the last successful refill ppm × REBALANCE_FEE_MARGIN
    (read live from rebalance_log). Activates after the first successful refill;
    sigmoid alone drives fees before any refill history exists.

    Returns dict with: target_ppm, base_ppm, mult, floor_ppm, source, reason.
    """
    chan_id = channel["chan_id"]
    local_ratio = channel["local_ratio"]

    base = sigmoid_fee_ppm(local_ratio)
    mult = float(signals.get("market_multiplier", 0.0) or 0.0)

    last_refill = db.get_last_refill_ppm(chan_id)
    floor = int(round(last_refill * REBALANCE_FEE_MARGIN)) if last_refill else 0

    # Adjusted base with market multiplier. In the low-local defense zone,
    # the multiplier may never push the fee BELOW the sigmoid output — that
    # would invite the very drain we're defending against.
    adjusted = base * (1.0 + mult)
    if local_ratio < FEE_HYSTERESIS_EDGE_LOW:
        adjusted = max(adjusted, base)

    target = max(adjusted, float(floor))
    target = min(target, float(FEE_HARD_CEILING_PPM))
    target_ppm = int(round(target))

    if floor and floor >= adjusted:
        source = "floor"
    elif abs(mult) > 0.01:
        source = "sigmoid+market"
    else:
        source = "sigmoid"

    reason = (
        f"sigmoid={base} mult={mult:+.2f} floor={floor} "
        f"local={local_ratio:.2f} → {target_ppm} [{source}]"
    )

    return {
        "target_ppm": target_ppm,
        "base_ppm": base,
        "mult": mult,
        "floor_ppm": floor,
        "source": source,
        "reason": reason,
    }


def _should_broadcast(target_ppm, current_ppm, signals, local_ratio, now):
    """Hysteresis gate. Returns (broadcast: bool, why: str)."""
    delta = target_ppm - current_ppm
    abs_delta = abs(delta)

    # Tolerance — must clear both the absolute and the relative thresholds.
    pct = abs_delta / max(current_ppm, 1)
    if abs_delta < FEE_HYSTERESIS_TOLERANCE_PPM and pct < FEE_HYSTERESIS_TOLERANCE_PCT:
        return False, f"within tolerance (Δ={delta:+d})"

    # Snap escapes — always broadcast big jumps.
    if abs_delta >= FEE_HYSTERESIS_SNAP_PPM:
        return True, f"snap (Δ={delta:+d})"

    # Edge-zone crossing escapes the cooldown.
    last_ratio = signals.get("last_local_ratio")
    if last_ratio is not None:
        if _edge_zone(local_ratio) != _edge_zone(last_ratio):
            return True, f"edge crossing ({_edge_zone(last_ratio)}→{_edge_zone(local_ratio)})"

    # Cooldown — otherwise rate-limit to once per FEE_HYSTERESIS_COOLDOWN_SEC.
    last_ts = int(signals.get("last_fee_update_ts") or 0)
    if last_ts and (now - last_ts) < FEE_HYSTERESIS_COOLDOWN_SEC:
        remaining = FEE_HYSTERESIS_COOLDOWN_SEC - (now - last_ts)
        return False, f"cooldown ({remaining // 60}m left)"

    return True, f"normal (Δ={delta:+d})"


def update_all_fees(dry_run=False):
    """Update fee policies on all channels.

    Pipeline per channel:
      1. Pin in fee_overrides? → use pin, done.
      2. base   = sigmoid(local_ratio)
      3. mult   = market_multiplier  (defense zone only allows positive)
      4. floor  = last_refill_ppm × REBALANCE_FEE_MARGIN (0 if never refilled)
      5. target = clamp( max(base*(1+mult), floor), 0, FEE_HARD_CEILING_PPM )
      6. Broadcast only if hysteresis permits.

    Pins below the rebalance floor are honoured but logged as a warning.
    Returns list of changes attempted.
    """
    now = int(time.time())
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    fee_report = lnd_client.get_fee_report()
    overrides = db.get_fee_overrides()

    current_fees = {}
    for item in fee_report.get("channel_fees", []):
        cp = item.get("channel_point", "")
        current_fees[cp] = {
            "base_fee_msat": int(item.get("base_fee_msat", 0)),
            "fee_rate_ppm": int(item.get("fee_per_mil", 0)),
        }

    updates = []
    for ch in channels:
        chan_id = ch["chan_id"]
        cp = ch["channel_point"]
        signals = db.get_channel_signals(chan_id)
        old = current_fees.get(cp, {})
        old_ppm = old.get("fee_rate_ppm", 0)
        old_base = old.get("base_fee_msat", 0)

        pin = overrides.get(chan_id)
        if pin is not None:
            new_ppm = int(pin["pinned_ppm"])
            last_refill = db.get_last_refill_ppm(chan_id)
            floor = int(round(last_refill * REBALANCE_FEE_MARGIN)) if last_refill else 0
            if floor and new_ppm < floor:
                log.warning("fees: %s pin %d ppm is BELOW rebalance floor %d ppm — "
                            "you may be selling below refill cost",
                            ch["peer_alias"], new_ppm, floor)
            reason = f"manual pin: {new_ppm} ppm"
            target_info = {
                "target_ppm": new_ppm, "base_ppm": new_ppm, "mult": 0.0,
                "floor_ppm": floor, "source": "pin", "reason": reason,
            }
            broadcast = (new_ppm != old_ppm) or (old_base != FEE_BASE_MSAT)
            why = "pin enforced" if broadcast else "pin unchanged"
        else:
            target_info = compute_fee_target(ch, signals, now)
            new_ppm = target_info["target_ppm"]
            reason = target_info["reason"]
            broadcast, why = _should_broadcast(
                new_ppm, old_ppm, signals, ch["local_ratio"], now
            )

        change = {
            "chan_id": chan_id,
            "channel_point": cp,
            "alias": ch["peer_alias"],
            "old_ppm": old_ppm,
            "new_ppm": new_ppm,
            "old_base": old_base,
            "new_base": FEE_BASE_MSAT,
            "local_ratio": ch["local_ratio"],
            "pinned": pin is not None,
            "source": target_info["source"],
            "base_ppm": target_info["base_ppm"],
            "mult": target_info["mult"],
            "floor_ppm": target_info["floor_ppm"],
            "broadcast": broadcast,
            "broadcast_reason": why,
        }

        if not broadcast:
            log.debug("fees: %s skip — %s (target %d, current %d)",
                      ch["peer_alias"], why, new_ppm, old_ppm)
            continue

        if not dry_run:
            try:
                lnd_client.update_channel_policy(cp, FEE_BASE_MSAT, new_ppm)
                change["applied"] = True
            except Exception as e:
                change["applied"] = False
                change["error"] = str(e)

            db.save_fee_update(
                chan_id, ch["peer_alias"], old_ppm, new_ppm,
                old_base, FEE_BASE_MSAT, ch["local_ratio"], reason
            )
            # Stamp last_fee_update_ts + last_local_ratio for next-run hysteresis.
            db.upsert_channel_signals(
                chan_id,
                last_fee_update_ts=now,
                last_local_ratio=ch["local_ratio"],
            )
        else:
            change["applied"] = "dry_run"

        updates.append(change)

    if updates:
        applied = sum(1 for u in updates if u.get("applied") is True)
        log.info("fees: %d change(s) applied, %d failed",
                 applied, len(updates) - applied)
    else:
        log.info("fees: no broadcasts needed (all within hysteresis bands)")
    return updates


# ─── Signal recomputation (nightly job) ──────────────────────────

def compute_market_multiplier(chan_id, prev_mult):
    """Nudge the per-channel market multiplier based on observed forward activity.

    Slow-moving by design (±MARKET_MULT_STEP per recompute):
      - If forwarded in the last MARKET_MULT_BUSY_HOURS → nudge up.
      - If no forwards for MARKET_MULT_SILENT_DAYS → nudge down.
      - Otherwise unchanged.

    Clamped to [MARKET_MULT_MIN, MARKET_MULT_MAX]. The defense-zone
    asymmetry (multiplier can only RAISE the fee at low local) is enforced
    later, in compute_fee_target — the multiplier itself stays unconstrained.
    """
    now = int(time.time())
    last_ts = db.get_last_forward_ts(chan_id)
    mult = float(prev_mult or 0.0)

    if last_ts is not None:
        age = now - last_ts
        if age <= MARKET_MULT_BUSY_HOURS * 3600:
            mult += MARKET_MULT_STEP
        elif age >= MARKET_MULT_SILENT_DAYS * 86400:
            mult -= MARKET_MULT_STEP
    else:
        # Never forwarded — treat as silent.
        mult -= MARKET_MULT_STEP

    return max(MARKET_MULT_MIN, min(MARKET_MULT_MAX, mult))


def recompute_all_signals():
    """Recompute slow signals for every active channel and write to channel_signals.

    Designed for a nightly cron. Cheap and idempotent. Doesn't touch fees
    or broadcast anything — just refreshes the cached inputs that the 2h
    pipeline reads.
    """
    now = int(time.time())
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    log.info("recompute_signals: processing %d channels", len(channels))

    results = []
    for ch in channels:
        chan_id = ch["chan_id"]
        prev = db.get_channel_signals(chan_id)

        mult = compute_market_multiplier(chan_id, prev.get("market_multiplier", 0.0))
        last_refill = db.get_last_refill_ppm(chan_id)
        failures = db.count_failures_since_last_success(chan_id)

        db.upsert_channel_signals(
            chan_id,
            market_multiplier=mult,
            signals_updated_ts=now,
        )
        results.append({
            "chan_id": chan_id,
            "alias": ch.get("peer_alias", ""),
            "last_refill_ppm": last_refill,
            "failures_since_success": failures,
            "mult": mult,
        })
        log.info("recompute_signals: %s last_refill=%s ppm failures=%d mult=%+.2f",
                 ch.get("peer_alias", chan_id[:12]),
                 last_refill if last_refill is not None else "none",
                 failures, mult)

    return results


# ─── Rebalancing ─────────────────────────────────────────────────

def get_channel_rebalance_budget(chan_id):
    """Max fee ppm we'll pay to refill this channel.

    Single-signal model — no tiers, no maturity gates:

      budget = (last_refill_ppm or DEFAULT_BUDGET)
               × (1 + ESCALATION_STEP × failures_since_last_success)
               capped at REBALANCE_MAX_BUDGET_PPM

    Bootstrap: with no successful refill yet, starts at REBALANCE_DEFAULT_BUDGET_PPM
    and walks up by ESCALATION_STEP per consecutive failure until a success lands.
    Post-bootstrap: the same mechanism handles upward market drift — failures at the
    last-known price escalate the budget until the new price is discovered.
    """
    last_refill = db.get_last_refill_ppm(chan_id)
    failures = db.count_failures_since_last_success(chan_id)

    if last_refill is None:
        base = REBALANCE_DEFAULT_BUDGET_PPM
        anchor = "default"
    else:
        base = last_refill
        anchor = "last_refill"

    budget = base * (1.0 + REBALANCE_BUDGET_ESCALATION_STEP * failures)
    budget = min(int(round(budget)), REBALANCE_MAX_BUDGET_PPM)

    if failures > 0:
        reason = (f"{anchor} {base} ppm × (1 + {REBALANCE_BUDGET_ESCALATION_STEP:.0%}"
                  f" × {failures} fails) → {budget} ppm")
    else:
        reason = f"{anchor} {base} ppm → {budget} ppm"

    return {
        "max_fee_ppm": budget,
        "reason": reason,
        "last_refill_ppm": last_refill,
        "failures_since_success": failures,
    }


def find_rebalance_candidates(channels=None, force=None):
    """Identify channels that need rebalancing.

    Returns two lists:
    - needs_inbound: channels with local_ratio < LOW threshold (depleted, need sats back)
    - needs_outbound: channels with local_ratio > HIGH threshold (overfull, can donate sats)

    force: if set to a float (0.0-1.0), ignore thresholds and use that value as the target.
           Any channel below force% is inbound, above force% is outbound.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound = []
    needs_outbound = []

    for ch in channels:
        if not ch["active"]:
            continue
        if force is not None:
            # Ignore thresholds — use the specified target ratio
            if ch["local_ratio"] < force:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > force:
                needs_outbound.append(ch)
        else:
            if ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
                needs_outbound.append(ch)

    needs_inbound.sort(key=lambda c: c["local_ratio"])
    needs_outbound.sort(key=lambda c: c["local_ratio"], reverse=True)

    return needs_inbound, needs_outbound


def calculate_rebalance_amount(channel, direction="inbound", target_ratio=None):
    """Calculate how many sats to move for a channel.

    direction='inbound':  channel is depleted, we want to push sats to local side
    direction='outbound': channel is overfull, we want to push sats to remote side
    target_ratio: override the default REBALANCE_TARGET (e.g. from --force 0.4)
    """
    capacity = channel["capacity"]
    local = channel["local_balance"]
    target_local = int(capacity * (target_ratio if target_ratio is not None else REBALANCE_TARGET))
    max_amount = int(capacity * REBALANCE_MAX_AMOUNT_RATIO)

    if direction == "inbound":
        # Need to increase local balance
        amount = target_local - local
    else:
        # Need to decrease local balance
        amount = local - target_local

    # Cap at max ratio
    amount = min(amount, max_amount)
    # Minimum useful rebalance: 50k sats
    if amount < 50_000:
        return 0
    return amount


def plan_rebalances(channels=None, force=None):
    """Create a rebalancing plan with primary and fallback pairs.

    Generates ALL possible source→target pairs, ordered by priority.
    Primary plans: most depleted target paired with most overfull source.
    Fallback plans: alternative pairings for each target, tried if the
    primary pair fails at execution time (e.g. no route between those nodes).

    This means if a primary source→target pair fails due to no route, the
    executor will try the same source against an alternative target before
    giving up on the run.

    force: if set to a float (0.0-1.0), ignore thresholds and use that ratio as target.
    Returns a (plans, reason) tuple.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound, needs_outbound = find_rebalance_candidates(channels, force=force)

    # Use force target ratio if specified, otherwise use config default
    rebalance_target = force if force is not None else REBALANCE_TARGET

    log.info("rebalance: %d depleted, %d overfull channel(s) found",
             len(needs_inbound), len(needs_outbound))
    for ch in needs_inbound:
        log.info("  depleted: %s at %.0f%% local", ch["peer_alias"], ch["local_ratio"] * 100)
    for ch in needs_outbound:
        log.info("  overfull: %s at %.0f%% local", ch["peer_alias"], ch["local_ratio"] * 100)

    if not needs_inbound and not needs_outbound:
        return [], "all channels balanced"
    if not needs_inbound:
        return [], (f"{len(needs_outbound)} channel(s) overfull but none depleted — "
                    f"no circular rebalance possible")
    if not needs_outbound:
        depleted = ", ".join(f"{c['peer_alias']} ({c['local_ratio']:.0%})" for c in needs_inbound)
        return [], (f"{len(needs_inbound)} channel(s) depleted ({depleted}) but no overfull "
                    f"channel to rebalance from — need more channels or top up on-chain")

    # Generate ALL possible source→target pairs, ordered by priority:
    # most depleted targets first, each paired with all available sources.
    # The executor tries pairs in order — if one fails, the next pair is tried.
    # This ensures that if source→targetA fails, source→targetB is tried next.
    plans = []
    used_source_sats = {}  # track how much each source has committed

    for target_ch in needs_inbound:
        target_amount = calculate_rebalance_amount(target_ch, "inbound", target_ratio=rebalance_target)
        if target_amount <= 0:
            continue

        budget = get_channel_rebalance_budget(target_ch["chan_id"])
        max_fee_ppm = budget["max_fee_ppm"]

        # Try ALL sources for this target — a single source may not have
        # enough capacity to fully restore the target. Multiple sources can
        # each contribute their share.
        remaining_target = target_amount
        for source_ch in needs_outbound:
            if remaining_target < 50_000:
                break  # target nearly satisfied

            source_total = calculate_rebalance_amount(source_ch, "outbound", target_ratio=rebalance_target)
            already_used = used_source_sats.get(source_ch["chan_id"], 0)
            source_available = source_total - already_used

            if source_available < 50_000:
                continue

            amount = min(remaining_target, source_available)
            max_fee = int(amount * max_fee_ppm / 1_000_000 * 1.1)

            log.info("rebalance plan: %s→%s %s sats [%d ppm cap — %s]",
                     source_ch["peer_alias"], target_ch["peer_alias"],
                     f"{amount:,}", max_fee_ppm, budget["reason"])

            plans.append({
                "source_chan_id": source_ch["chan_id"],
                "source_alias": source_ch["peer_alias"],
                "source_channel_point": source_ch["channel_point"],
                "source_local_ratio": source_ch["local_ratio"],
                "target_chan_id": target_ch["chan_id"],
                "target_alias": target_ch["peer_alias"],
                "target_channel_point": target_ch["channel_point"],
                "target_peer_pubkey": target_ch["peer_pubkey"],
                "target_local_ratio": target_ch["local_ratio"],
                "amount_sats": amount,
                "max_fee_sats": max_fee,
                "max_fee_ppm": max_fee_ppm,
                "budget_reason": budget["reason"],
            })

            # Reserve this source capacity and reduce target remaining
            used_source_sats[source_ch["chan_id"]] = already_used + amount
            remaining_target -= amount

    # ── Fallback plans ───────────────────────────────────────────
    # For each target, add alternative source pairings.
    # These are only executed if the primary plan for that target fails.
    # This ensures the engine tries all available routes before giving up.
    primary_pairs = {(p["source_chan_id"], p["target_chan_id"]) for p in plans}
    for target_ch in needs_inbound:
        target_amount = calculate_rebalance_amount(target_ch, "inbound")
        if target_amount <= 0:
            continue
        budget = get_channel_rebalance_budget(target_ch["chan_id"])
        max_fee_ppm = budget["max_fee_ppm"]

        for source_ch in needs_outbound:
            pair_key = (source_ch["chan_id"], target_ch["chan_id"])
            if pair_key in primary_pairs:
                continue  # already in the primary plan

            source_available = calculate_rebalance_amount(source_ch, "outbound")
            if source_available < 50_000:
                continue

            amount = min(target_amount, source_available)
            max_fee = int(amount * max_fee_ppm / 1_000_000 * 1.1)

            log.info("rebalance fallback: %s→%s %s sats [%d ppm cap — %s]",
                     source_ch["peer_alias"], target_ch["peer_alias"],
                     f"{amount:,}", max_fee_ppm, budget["reason"])

            plans.append({
                "source_chan_id": source_ch["chan_id"],
                "source_alias": source_ch["peer_alias"],
                "source_channel_point": source_ch["channel_point"],
                "source_local_ratio": source_ch["local_ratio"],
                "target_chan_id": target_ch["chan_id"],
                "target_alias": target_ch["peer_alias"],
                "target_channel_point": target_ch["channel_point"],
                "target_peer_pubkey": target_ch["peer_pubkey"],
                "target_local_ratio": target_ch["local_ratio"],
                "amount_sats": amount,
                "max_fee_sats": max_fee,
                "max_fee_ppm": max_fee_ppm,
                "budget_reason": budget["reason"],
                "is_fallback": True,
            })

    return plans, None


def _attempt_single_rebalance(plan, amount, max_fee_sats):
    """Attempt one circular rebalance payment at a specific amount.

    Returns dict with: success, fee_paid, fee_ppm, failure_reason
    """
    try:
        # Create invoice
        invoice = lnd_client.add_invoice(
            amount,
            memo=f"rebal:{plan['source_alias'][:10]}→{plan['target_alias'][:10]}"
        )
        payment_request = invoice.get("payment_request", "")

        if not payment_request:
            return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                    "failure_reason": "failed to create invoice"}

        log.info("  attempt %s sats (fee limit %d sats) via /v2/router/send",
                 f"{amount:,}", max_fee_sats)

        pay_result = lnd_client.send_payment_v2(
            payment_request=payment_request,
            outgoing_chan_id=plan["source_chan_id"],
            last_hop_pubkey=plan["target_peer_pubkey"],
            fee_limit_sat=max_fee_sats,
            timeout_seconds=120,
        )

        if pay_result["status"] == "SUCCEEDED":
            fee = pay_result["fee_sat"]
            ppm = fee / amount * 1_000_000 if amount > 0 else 0
            return {"success": True, "fee_paid": fee, "fee_ppm": ppm,
                    "failure_reason": "",
                    "payment_hash": pay_result.get("payment_hash", "")}
        else:
            return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                    "failure_reason": pay_result.get("failure_reason", "unknown"),
                    "payment_hash": pay_result.get("payment_hash", "")}

    except Exception as e:
        return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                "failure_reason": str(e), "payment_hash": ""}


def execute_rebalance(plan, dry_run=False):
    """Execute a circular rebalance using Router SendPaymentV2.

    If the full amount fails (e.g. no route with enough liquidity), automatically
    splits into smaller chunks and retries. Halves the amount on each failure,
    down to a minimum of 100,000 sats. Successful chunks accumulate — the goal
    is to move as much as possible toward the target, not all-or-nothing.

    Forces the payment:
    - OUT through plan["source_chan_id"]  (the overfull channel)
    - BACK IN through plan["target_peer_pubkey"] (the depleted channel peer)
    """
    result = {
        "source_chan_id": plan["source_chan_id"],
        "target_chan_id": plan["target_chan_id"],
        "source_alias": plan["source_alias"],
        "target_alias": plan["target_alias"],
        "amount": plan["amount_sats"],
        "max_fee": plan["max_fee_sats"],
        "success": False,
        "fee_paid": 0,
        "fee_ppm": 0,
        "failure_reason": "",
    }

    if dry_run:
        log.info("dry run: would rebalance %s→%s %s sats [%d ppm cap]",
                 plan["source_alias"], plan["target_alias"],
                 f"{plan['amount_sats']:,}", plan["max_fee_ppm"])
        result["failure_reason"] = "dry_run"
        return result

    log.info("executing rebalance: %s→%s %s sats (max fee %d ppm)",
             plan["source_alias"], plan["target_alias"],
             f"{plan['amount_sats']:,}", plan["max_fee_ppm"])
    start = time.time()

    total_moved = 0
    total_fees = 0
    remaining = plan["amount_sats"]
    chunk_amount = remaining  # start with full amount
    min_chunk = 100_000       # never try less than 100k sats
    max_chunks = 10           # safety limit to prevent infinite splitting
    last_failure_reason = ""
    succeeded_chunks = 0

    for chunk_num in range(1, max_chunks + 1):
        if remaining < min_chunk:
            log.info("remaining %s sats is below minimum chunk %s — stopping",
                     f"{remaining:,}", f"{min_chunk:,}")
            break

        # Calculate fee limit for this chunk based on the budget ppm
        chunk_fee_limit = int(chunk_amount * plan["max_fee_ppm"] / 1_000_000 * 1.1)

        log.info("rebalance chunk %d: trying %s of %s remaining sats",
                 chunk_num, f"{chunk_amount:,}", f"{remaining:,}")

        chunk_start = time.time()
        attempt = _attempt_single_rebalance(plan, chunk_amount, chunk_fee_limit)
        chunk_duration = time.time() - chunk_start

        if attempt["success"]:
            total_moved += chunk_amount
            total_fees += attempt["fee_paid"]
            remaining -= chunk_amount
            succeeded_chunks += 1
            log.info("  chunk %d succeeded: %s sats moved, fee %d sats (%.0f ppm)",
                     chunk_num, f"{chunk_amount:,}", attempt["fee_paid"], attempt["fee_ppm"])

            # Persist this chunk as its own row so sync_rebalances can dedup by
            # payment_hash instead of misattributing it to a "manual" send.
            db.save_rebalance_attempt(
                plan["source_chan_id"], plan["target_chan_id"],
                plan["source_alias"], plan["target_alias"],
                chunk_amount, attempt["fee_paid"],
                True, "", chunk_duration,
                payment_hash=attempt.get("payment_hash") or None,
            )

            # Try another chunk at same size if there's remaining
            if remaining < min_chunk:
                break
            chunk_amount = min(chunk_amount, remaining)

        else:
            log.info("  chunk %d failed: %s — halving amount",
                     chunk_num, attempt["failure_reason"])
            last_failure_reason = attempt["failure_reason"]
            # Halve the amount and retry
            chunk_amount = chunk_amount // 2
            if chunk_amount < min_chunk:
                log.info("  chunk size %s below minimum %s — giving up",
                         f"{chunk_amount:,}", f"{min_chunk:,}")
                result["failure_reason"] = last_failure_reason
                break

    duration = time.time() - start

    if total_moved > 0:
        result["success"] = True
        result["amount"] = total_moved
        result["fee_paid"] = total_fees
        result["fee_ppm"] = total_fees / total_moved * 1_000_000 if total_moved > 0 else 0
        log.info("rebalance complete: %s→%s moved %s of %s sats in %.1fs across %d chunk(s), "
                 "total fee %d sats (%.0f ppm)",
                 plan["source_alias"], plan["target_alias"],
                 f"{total_moved:,}", f"{plan['amount_sats']:,}",
                 duration, succeeded_chunks, total_fees, result["fee_ppm"])
    else:
        log.warning("rebalance failed completely: %s→%s — no sats moved after %d attempts in %.1fs",
                    plan["source_alias"], plan["target_alias"], chunk_num, duration)
        result["failure_reason"] = result["failure_reason"] or last_failure_reason
        # Only log a row for total failures — successful chunks were already saved above.
        db.save_rebalance_attempt(
            plan["source_chan_id"], plan["target_chan_id"],
            plan["source_alias"], plan["target_alias"],
            plan["amount_sats"], 0,
            False, result["failure_reason"], duration,
        )

    return result


# ─── Monitoring ──────────────────────────────────────────────────

def get_channel_health_report(channels=None):
    """Generate a health report for all channels.
    
    Returns a structured report with alerts for unhealthy channels.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    report = {
        "timestamp": int(time.time()),
        "total_channels": len(channels),
        "active_channels": sum(1 for c in channels if c["active"]),
        "inactive_channels": sum(1 for c in channels if not c["active"]),
        "total_capacity": sum(c["capacity"] for c in channels),
        "total_local": sum(c["local_balance"] for c in channels),
        "total_remote": sum(c["remote_balance"] for c in channels),
        "overall_local_ratio": 0,
        "channels": [],
        "alerts": [],
    }

    total_cap = report["total_capacity"]
    if total_cap > 0:
        report["overall_local_ratio"] = round(report["total_local"] / total_cap, 4)

    for ch in channels:
        ch_report = {
            "chan_id": ch["chan_id"],
            "peer_alias": ch["peer_alias"],
            "capacity": ch["capacity"],
            "local_ratio": ch["local_ratio"],
            "active": ch["active"],
            "status": "healthy",
        }

        # Track channel maturity (balanced time accumulator)
        is_balanced = (
            REBALANCE_BALANCED_RATIO <= ch["local_ratio"] <= REBALANCE_BALANCED_RATIO_HIGH
        )
        db.update_channel_maturity(ch["chan_id"], is_balanced)

        # Enrich with historical performance and current rebalance budget
        try:
            perf = db.get_channel_performance(ch["chan_id"])
            ch_report["fee_revenue_30d"] = perf["fee_revenue"]
            ch_report["forwards_30d"] = perf["forwards"]
            ch_report["rebalance_cost_30d"] = perf["rebalance_cost"]
            ch_report["net_profit_30d"] = perf["net_profit"]
        except Exception:
            pass

        try:
            budget = get_channel_rebalance_budget(ch["chan_id"])
            ch_report["budget_ppm"] = budget["max_fee_ppm"]
            ch_report["budget_reason"] = budget["reason"]
        except Exception:
            pass

        # Generate alerts
        if not ch["active"]:
            ch_report["status"] = "offline"
            report["alerts"].append({
                "type": "peer_offline",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} is offline ({ch['capacity']:,} sats locked)",
            })
        elif ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
            ch_report["status"] = "depleted"
            report["alerts"].append({
                "type": "channel_depleted",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} depleted at {ch['local_ratio']:.0%} local",
            })
        elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
            ch_report["status"] = "saturated"
            report["alerts"].append({
                "type": "channel_saturated",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} saturated at {ch['local_ratio']:.0%} local",
            })

        # Check for repeated rebalance failures regardless of health status
        try:
            failure_count = db.get_repeated_rebalance_failures(ch["chan_id"], min_failures=3)
            if failure_count >= 3:
                report["alerts"].append({
                    "type": "rebalance_failing",
                    "chan_id": ch["chan_id"],
                    "alias": ch["peer_alias"],
                    "message": (
                        f"{ch['peer_alias']} has failed to rebalance {failure_count} times in a row "
                        f"— check route availability or raise fee budget"
                    ),
                })
                ch_report["rebalance_failing"] = True
                log.warning("repeated rebalance failures for %s: %d consecutive failures",
                            ch["peer_alias"], failure_count)
        except Exception:
            pass

        report["channels"].append(ch_report)

    # Save snapshot to database
    db.save_channel_snapshot(channels)

    log.info("healthcheck: %d channel(s) active, %d inactive, overall local %.0f%%",
             report["active_channels"], report["inactive_channels"],
             report["overall_local_ratio"] * 100)
    if report["alerts"]:
        for alert in report["alerts"]:
            log.warning("healthcheck alert [%s]: %s", alert["type"], alert["message"])
    else:
        log.info("healthcheck: all channels healthy")

    return report


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
