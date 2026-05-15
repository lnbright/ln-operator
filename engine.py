"""
LN Operator — Channel Management Engine (60% layer)
Handles fee updates, rebalancing, and channel monitoring.
All deterministic logic — no LLM calls.
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


def find_rebalance_candidates(channels=None):
    """Identify channels that need rebalancing.

    Returns two lists:
    - needs_inbound: channels with local_ratio < LOW threshold (depleted, need sats back)
    - needs_outbound: channels with local_ratio > HIGH threshold (overfull, can donate sats)
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound = []   # depleted — need to push sats IN
    needs_outbound = []  # overfull — can push sats OUT

    for ch in channels:
        if not ch["active"]:
            continue
        if ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
            needs_inbound.append(ch)
        elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
            needs_outbound.append(ch)

    # Sort: most depleted first for inbound, most overfull first for outbound
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


def plan_rebalances(channels=None):
    """Create a rebalancing plan: which channels to rebalance and how.

    Pairs depleted channels with overfull ones for circular rebalancing.
    Uses per-channel budget based on historical performance.
    Returns a list of planned rebalance operations.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound, needs_outbound = find_rebalance_candidates(channels)

    if not needs_inbound or not needs_outbound:
        return []

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
        max_fee = int(amount * max_fee_ppm / 1_000_000)

        plans.append({
            "source_chan_id": source_ch["chan_id"],
            "source_alias": source_ch["peer_alias"],
            "source_channel_point": source_ch["channel_point"],
            "source_local_ratio": source_ch["local_ratio"],
            "target_chan_id": target_ch["chan_id"],
            "target_alias": target_ch["peer_alias"],
            "target_channel_point": target_ch["channel_point"],
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

    return plans


# Note: actual circular rebalance execution requires the router RPC
# (SendPaymentV2 with outgoing_chan_id). This is a placeholder that
# logs the plan. Full implementation requires lnrpc routerrpc calls.
def execute_rebalance(plan, dry_run=False):
    """Execute a single rebalance operation.
    
    In production this would:
    1. Create an invoice for the amount
    2. Use router SendPaymentV2 with outgoing_chan_id = source
       and last_hop_pubkey = target peer to force the circular path
    3. Track the result
    
    For now, returns the plan with a placeholder.
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
        result["failure_reason"] = "dry_run"
        return result

    start = time.time()
    try:
        # Step 1: Create invoice
        invoice = lnd_client.add_invoice(
            plan["amount_sats"],
            memo=f"rebal:{plan['source_alias'][:10]}→{plan['target_alias'][:10]}"
        )
        payment_request = invoice.get("payment_request", "")

        if not payment_request:
            result["failure_reason"] = "failed to create invoice"
            return result

        # Step 2: Pay the invoice with fee limit
        # NOTE: basic sendpayment doesn't support outgoing_chan_id.
        # Full circular rebalance requires router RPC (SendPaymentV2).
        # This is a simplified version that lets LND pick the route.
        pay_result = lnd_client.send_payment(
            payment_request,
            fee_limit_sat=plan["max_fee_sats"],
            timeout_seconds=120
        )

        if pay_result.get("payment_error"):
            result["failure_reason"] = pay_result["payment_error"]
        else:
            result["success"] = True
            route = pay_result.get("payment_route", {})
            result["fee_paid"] = int(route.get("total_fees", 0))
            result["fee_ppm"] = (
                result["fee_paid"] / plan["amount_sats"] * 1_000_000
                if plan["amount_sats"] > 0 else 0
            )

    except Exception as e:
        result["failure_reason"] = str(e)

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

    return report


def sync_forwarding_history(hours=24):
    """Fetch recent forwarding events from LND and save to database."""
    start = int(time.time()) - (hours * 3600)
    events = lnd_client.get_forwarding_history(start_time=start)
    if events:
        db.save_forwarding_events(events)
    return len(events)
