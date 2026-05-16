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
    FEE_BASE_MSAT, FEE_MIN_PPM, FEE_MAX_PPM,
    REBALANCE_MAX_AMOUNT_RATIO, REBALANCE_HARD_CAP_PPM,
    REBALANCE_REVENUE_RATIO, REBALANCE_DISCOVERY_PPM,
    REBALANCE_DEADWEIGHT_PPM, REBALANCE_DISCOVERY_DAYS,
    REBALANCE_BALANCED_RATIO, REBALANCE_BALANCED_RATIO_HIGH,
)
import lnd_client
import db
from logging_config import get_logger

log = get_logger('engine')


# ─── Fee Management ──────────────────────────────────────────────

def calculate_fee_ppm(local_ratio):
    """Calculate the optimal fee rate based on local balance ratio.
    
    local_ratio close to 1.0 (full)     → low fees (attract routing)
    local_ratio close to 0.5 (balanced) → mid fees
    local_ratio close to 0.0 (depleted) → high fees (protect liquidity + reputation)
    
    Uses a linear curve: ppm = min_ppm + (max_ppm - min_ppm) * (1 - local_ratio)
    """
    ppm = FEE_MIN_PPM + (FEE_MAX_PPM - FEE_MIN_PPM) * (1 - local_ratio)
    return int(max(FEE_MIN_PPM, min(FEE_MAX_PPM, round(ppm))))


def update_all_fees(dry_run=False):
    """Update fee policies on all channels based on current balance ratios.
    
    Returns list of changes made.
    """
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    fee_report = lnd_client.get_fee_report()

    # Build lookup of current fees by channel point
    current_fees = {}
    for item in fee_report.get("channel_fees", []):
        cp = item.get("channel_point", "")
        current_fees[cp] = {
            "base_fee_msat": int(item.get("base_fee_msat", 0)),
            "fee_rate_ppm": int(item.get("fee_per_mil", 0)),
        }

    updates = []
    for ch in channels:
        new_ppm = calculate_fee_ppm(ch["local_ratio"])
        cp = ch["channel_point"]
        old = current_fees.get(cp, {})
        old_ppm = old.get("fee_rate_ppm", 0)
        old_base = old.get("base_fee_msat", 0)

        # Only update if fee changed by more than 5 ppm (avoid gossip spam)
        if abs(new_ppm - old_ppm) < 5 and old_base == FEE_BASE_MSAT:
            log.debug("fees: %s unchanged at %d ppm (local %.0f%%)",
                      ch["peer_alias"], old_ppm, ch["local_ratio"] * 100)
            continue

        change = {
            "chan_id": ch["chan_id"],
            "channel_point": cp,
            "alias": ch["peer_alias"],
            "old_ppm": old_ppm,
            "new_ppm": new_ppm,
            "old_base": old_base,
            "new_base": FEE_BASE_MSAT,
            "local_ratio": ch["local_ratio"],
        }

        if not dry_run:
            try:
                lnd_client.update_channel_policy(cp, FEE_BASE_MSAT, new_ppm)
                change["applied"] = True
            except Exception as e:
                change["applied"] = False
                change["error"] = str(e)

            # Log to database
            reason = f"auto: local_ratio={ch['local_ratio']:.2f}"
            db.save_fee_update(
                ch["chan_id"], ch["peer_alias"], old_ppm, new_ppm,
                old_base, FEE_BASE_MSAT, ch["local_ratio"], reason
            )
        else:
            change["applied"] = "dry_run"

        updates.append(change)

    if updates:
        applied = sum(1 for u in updates if u.get("applied") is True)
        log.info("fees: %d change(s) applied, %d failed",
                 applied, len(updates) - applied)
    else:
        log.info("fees: all channels within 5 ppm tolerance — no changes needed")
    return updates


# ─── Rebalancing ─────────────────────────────────────────────────

def get_channel_rebalance_budget(chan_id):
    """Determine the max fee (in ppm) we're willing to pay to rebalance this channel.

    Three tiers:
    1. PROVEN — has 30+ days of balanced time and routing history.
       Budget = earned_ppm × 0.5 (never pay more than half what it earns).

    2. DISCOVERY — new channel, or hasn't had enough balanced time to judge.
       Budget = REBALANCE_DISCOVERY_PPM (default 150). Give it a fair shot.

    3. DEADWEIGHT — had 30+ balanced days but earned little or nothing.
       Budget = REBALANCE_DEADWEIGHT_PPM (default 50). Minimal life support.

    All tiers capped at REBALANCE_HARD_CAP_PPM (default 500).

    Returns dict with max_fee_ppm and the tier/reasoning for logging.
    """
    maturity = db.get_channel_maturity(chan_id)
    earned_ppm = db.get_channel_earned_ppm(chan_id, days=30)
    balanced_days = maturity["balanced_days"]

    if balanced_days >= REBALANCE_DISCOVERY_DAYS:
        # Channel has had enough balanced time — judge it on performance
        if earned_ppm > 0:
            # PROVEN: it routes and earns. Budget based on actual revenue.
            budget = earned_ppm * REBALANCE_REVENUE_RATIO
            budget = max(budget, REBALANCE_DEADWEIGHT_PPM)  # floor
            budget = min(budget, REBALANCE_HARD_CAP_PPM)    # ceiling
            return {
                "max_fee_ppm": int(budget),
                "tier": "proven",
                "reason": f"earns {earned_ppm:.0f} ppm, budget {budget:.0f} ppm "
                          f"({REBALANCE_REVENUE_RATIO:.0%} of revenue)",
                "earned_ppm": earned_ppm,
                "balanced_days": balanced_days,
            }
        else:
            # DEADWEIGHT: had its chance, earned nothing.
            return {
                "max_fee_ppm": REBALANCE_DEADWEIGHT_PPM,
                "tier": "deadweight",
                "reason": f"{balanced_days:.0f} balanced days, 0 ppm earned — "
                          f"minimal budget {REBALANCE_DEADWEIGHT_PPM} ppm",
                "earned_ppm": 0,
                "balanced_days": balanced_days,
            }
    else:
        # DISCOVERY: not enough data yet. Give it a fair budget.
        remaining = REBALANCE_DISCOVERY_DAYS - balanced_days
        return {
            "max_fee_ppm": REBALANCE_DISCOVERY_PPM,
            "tier": "discovery",
            "reason": f"{balanced_days:.0f}/{REBALANCE_DISCOVERY_DAYS} balanced days — "
                      f"discovery budget {REBALANCE_DISCOVERY_PPM} ppm "
                      f"({remaining:.0f} days until judged)",
            "earned_ppm": earned_ppm,
            "balanced_days": balanced_days,
        }


def find_rebalance_candidates(channels=None, force=False):
    """Identify channels that need rebalancing.

    Returns two lists:
    - needs_inbound: channels with local_ratio < LOW threshold (depleted, need sats back)
    - needs_outbound: channels with local_ratio > HIGH threshold (overfull, can donate sats)

    force=True: ignore thresholds — any channel below 50% is inbound, above 50% is outbound.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound = []
    needs_outbound = []

    for ch in channels:
        if not ch["active"]:
            continue
        if force:
            # Ignore thresholds — target 50% on everything
            if ch["local_ratio"] < REBALANCE_TARGET:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > REBALANCE_TARGET:
                needs_outbound.append(ch)
        else:
            if ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
                needs_outbound.append(ch)

    needs_inbound.sort(key=lambda c: c["local_ratio"])
    needs_outbound.sort(key=lambda c: c["local_ratio"], reverse=True)

    return needs_inbound, needs_outbound


def calculate_rebalance_amount(channel, direction="inbound"):
    """Calculate how many sats to move for a channel.

    direction='inbound':  channel is depleted, we want to push sats to local side
    direction='outbound': channel is overfull, we want to push sats to remote side
    """
    capacity = channel["capacity"]
    local = channel["local_balance"]
    target_local = int(capacity * REBALANCE_TARGET)
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


def plan_rebalances(channels=None, force=False):
    """Create a rebalancing plan: which channels to rebalance and how.

    Pairs depleted channels with overfull ones for circular rebalancing.
    Uses per-channel budget based on historical performance.
    force=True: ignores 20/80 thresholds, targets 50% on all channels.
    Returns a (plans, reason) tuple.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound, needs_outbound = find_rebalance_candidates(channels, force=force)

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

    plans = []
    outbound_idx = 0

    for target_ch in needs_inbound:
        if outbound_idx >= len(needs_outbound):
            break

        target_amount = calculate_rebalance_amount(target_ch, "inbound")
        if target_amount <= 0:
            continue

        source_ch = needs_outbound[outbound_idx]
        source_available = calculate_rebalance_amount(source_ch, "outbound")

        if source_available <= 0:
            outbound_idx += 1
            continue

        # Use the smaller of what target needs and source can give
        amount = min(target_amount, source_available)

        # Per-channel budget for the TARGET (the depleted channel we're restoring)
        budget = get_channel_rebalance_budget(target_ch["chan_id"])
        max_fee_ppm = budget["max_fee_ppm"]
        # Add 10% headroom to avoid off-by-one failures where the route
        # costs exactly the ppm limit — rounding can cause rejection.
        max_fee = int(amount * max_fee_ppm / 1_000_000 * 1.1)

        log.info("rebalance plan: %s→%s %s sats [%s, %d ppm cap]",
                 source_ch["peer_alias"], target_ch["peer_alias"],
                 f"{amount:,}", budget["tier"], max_fee_ppm)
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
            "budget_tier": budget["tier"],
            "budget_reason": budget["reason"],
        })

        # If source still has excess after this plan, keep it for next target
        if source_available - amount < 50_000:
            outbound_idx += 1

    return plans, None


def execute_rebalance(plan, dry_run=False):
    """Execute a single circular rebalance using Router SendPaymentV2.

    Forces the payment:
    - OUT through plan["source_chan_id"]  (the overfull channel)
    - BACK IN through plan["target_peer_pubkey"] (the depleted channel peer)

    This guarantees liquidity moves exactly where we need it.
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
        log.info("dry run: would rebalance %s→%s %s sats [%s, %d ppm cap]",
                 plan["source_alias"], plan["target_alias"],
                 f"{plan['amount_sats']:,}", plan.get("budget_tier","?"), plan["max_fee_ppm"])
        result["failure_reason"] = "dry_run"
        return result

    log.info("executing rebalance: %s→%s %s sats (max fee %d ppm) [%s]",
             plan["source_alias"], plan["target_alias"],
             f"{plan['amount_sats']:,}", plan["max_fee_ppm"], plan.get("budget_tier","?"))
    start = time.time()

    try:
        # Step 1: Create invoice on our own node
        # POST /v1/invoices — creates a BOLT11 invoice payable to ourselves
        log.info("rebalance step 1/2: creating invoice for %s sats (POST /v1/invoices)",
                 f"{plan['amount_sats']:,}")
        invoice = lnd_client.add_invoice(
            plan["amount_sats"],
            memo=f"rebal:{plan['source_alias'][:10]}→{plan['target_alias'][:10]}"
        )
        payment_request = invoice.get("payment_request", "")

        if not payment_request:
            result["failure_reason"] = "failed to create invoice"
            log.error("rebalance aborted: could not create invoice")
            return result

        log.info("rebalance step 1/2: invoice created OK")

        # Step 2: Pay via Router SendPaymentV2
        # POST /v2/router/send with:
        #   outgoing_chan_id = source channel (overfull) — forces first hop out this channel
        #   last_hop_pubkey  = target peer (depleted)   — forces last hop in through this peer
        #   fee_limit_sat    = max fee based on budget tier
        #   allow_self_payment = true (required for circular payments)
        log.info("rebalance step 2/2: sending circular payment "
                 "(POST /v2/router/send, outgoing=%s, last_hop=%s, fee_limit=%d sats)",
                 plan["source_chan_id"], plan["target_peer_pubkey"][:12] + "...",
                 plan["max_fee_sats"])
        pay_result = lnd_client.send_payment_v2(
            payment_request=payment_request,
            outgoing_chan_id=plan["source_chan_id"],
            last_hop_pubkey=plan["target_peer_pubkey"],
            fee_limit_sat=plan["max_fee_sats"],
            timeout_seconds=120,
        )
        log.info("rebalance step 2/2: payment returned status=%s", pay_result.get("status","?"))

        if pay_result["status"] == "SUCCEEDED":
            result["success"] = True
            result["fee_paid"] = pay_result["fee_sat"]
            result["fee_ppm"] = (
                result["fee_paid"] / plan["amount_sats"] * 1_000_000
                if plan["amount_sats"] > 0 else 0
            )
            log.info("rebalance success: %s→%s fee %d sats (%.0f ppm)",
                     plan["source_alias"], plan["target_alias"],
                     result["fee_paid"], result["fee_ppm"])
        else:
            result["failure_reason"] = pay_result.get("failure_reason", "unknown")
            log.warning("rebalance failed: %s→%s: %s",
                        plan["source_alias"], plan["target_alias"], result["failure_reason"])

    except Exception as e:
        result["failure_reason"] = str(e)
        log.error("rebalance exception: %s→%s: %s",
                  plan["source_alias"], plan["target_alias"], e)

    duration = time.time() - start

    # Log to database
    db.save_rebalance_attempt(
        plan["source_chan_id"], plan["target_chan_id"],
        plan["source_alias"], plan["target_alias"],
        plan["amount_sats"], result["fee_paid"],
        result["success"], result["failure_reason"], duration
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

        # Enrich with historical performance and budget tier
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
            ch_report["budget_tier"] = budget["tier"]
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
