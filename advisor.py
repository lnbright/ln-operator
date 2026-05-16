"""
LN Operator — Investment Advisor (60% deterministic layer)

Given "I have X sats to deploy", this module produces a full investment plan:

1. Gathers current node state from LND (channels, balances, graph)
2. Calculates a treasury reserve (max of 10% or 3 months of rebalancing costs)
3. Checks on-chain fee environment via mempool.space (is it cheap to open channels?)
4. Analyses existing channels for problems (undersized, inactive, unprofitable)
5. Fetches candidate peers from 1ML API + local graph analysis
6. Scores candidates on capacity, channels, uptime, centrality, diversity
7. Allocates the deployable budget: upsize undersized channels first, then open new ones

The output is a structured dict that can be:
- Displayed in the terminal (main.py)
- Sent to the Claude API agent for a plain-English summary (agent.py)
- Saved to SQLite for historical reference (db.py)

This module does NOT execute any channel opens — it only recommends.
The operator reviews the plan and acts on it manually.
"""

import time
import math
import requests
from config import (
    TREASURY_MIN_RATIO, TREASURY_MONTHS_RESERVE,
    MIN_CHANNEL_SIZE_SATS, PREFERRED_CHANNEL_SIZE_SATS, MAX_CHANNEL_SIZE_SATS,
    PEER_SCORE_WEIGHTS, MEMPOOL_API, ONEML_API,
)
import lnd_client
import db
from logging_config import get_logger

log = get_logger('advisor')


def build_investment_plan(total_sats):
    """Main entry point: given X sats, produce a full investment plan.
    
    Returns a structured dict with:
    - treasury_reserve
    - deployable_sats
    - actions (open/upsize/close recommendations)
    - peer_candidates (scored)
    - current_state (node summary)
    - not_recommended (things to avoid right now)
    """
    log.info("building investment plan for %s sats", f"{total_sats:,}")

    # 1. Gather current node state
    state = _gather_node_state()

    # 2. Calculate treasury reserve
    treasury = _calculate_treasury(total_sats, state)

    deployable = total_sats - treasury["reserve_sats"]

    # 3. Check on-chain fee environment
    fee_env = _check_fee_environment()

    # 4. Analyse existing channels — find underperformers and upsize candidates
    channel_analysis = _analyse_existing_channels(state["channels"])

    # 5. Find candidate peers from external sources
    external_candidates = _fetch_external_candidates(state)

    # 6. Score and rank all candidates
    scored_candidates = _score_candidates(external_candidates, state)

    # 7. Allocate budget across actions
    actions, not_recommended = _allocate_budget(
        deployable, state, channel_analysis, scored_candidates, fee_env
    )

    plan = {
        "total_sats": total_sats,
        "treasury_reserve": treasury["reserve_sats"],
        "treasury_pct": treasury["reserve_sats"] / total_sats if total_sats > 0 else 0,
        "treasury_reasoning": treasury["reasoning"],
        "deployable_sats": deployable,
        "actions": actions,
        "not_recommended": not_recommended,
        "current_state": {
            "num_channels": state["num_channels"],
            "total_capacity": state["total_capacity"],
            "total_local": state["total_local"],
            "overall_ratio": state["overall_ratio"],
            "num_active": state["num_active"],
            "num_inactive": state["num_inactive"],
        },
        "channel_analysis": channel_analysis,
        "fee_environment": fee_env,
        "peer_candidates": scored_candidates[:10],  # top 10
        "timestamp": int(time.time()),
    }

    # Save to database
    db.save_investment_plan(total_sats, treasury["reserve_sats"], deployable, plan)

    return plan


# ─── Internal helpers ────────────────────────────────────────────

def _gather_node_state():
    """Pull all relevant state from LND."""
    info = lnd_client.get_info()
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    onchain = lnd_client.get_onchain_balance()
    chan_balance = lnd_client.get_channel_balance()
    pending = lnd_client.get_pending_channels()

    total_cap = sum(c["capacity"] for c in channels)
    total_local = sum(c["local_balance"] for c in channels)

    return {
        "info": info,
        "pubkey": info.get("identity_pubkey", ""),
        "alias": info.get("alias", ""),
        "channels": channels,
        "num_channels": len(channels),
        "num_active": sum(1 for c in channels if c["active"]),
        "num_inactive": sum(1 for c in channels if not c["active"]),
        "total_capacity": total_cap,
        "total_local": total_local,
        "total_remote": total_cap - total_local,
        "overall_ratio": total_local / total_cap if total_cap > 0 else 0,
        "onchain_confirmed": int(onchain.get("confirmed_balance", 0)),
        "onchain_unconfirmed": int(onchain.get("unconfirmed_balance", 0)),
        "pending_channels": pending,
        "existing_peers": set(c["peer_pubkey"] for c in channels),
    }


def _calculate_treasury(total_sats, state):
    """Determine how much to keep in reserve.
    
    Uses the higher of:
    - TREASURY_MIN_RATIO of the investment
    - TREASURY_MONTHS_RESERVE * average monthly rebalancing cost
    """
    # Minimum percentage-based reserve
    min_reserve = int(total_sats * TREASURY_MIN_RATIO)

    # Cost-based reserve from historical data
    avg_monthly_cost = db.get_avg_monthly_rebalance_cost(months=3)
    cost_reserve = int(avg_monthly_cost * TREASURY_MONTHS_RESERVE)

    # Also factor in potential channel close costs (on-chain fees)
    # Rough estimate: 1 close could cost ~10-50k sats depending on fees
    close_buffer = 50_000 * max(1, state["num_channels"] // 5)

    reasoning_parts = []

    if cost_reserve > 0:
        reasoning_parts.append(
            f"{TREASURY_MONTHS_RESERVE}mo avg rebalance cost: {cost_reserve:,} sats"
        )
    else:
        reasoning_parts.append("no rebalancing history yet — using minimum ratio")

    total_reserve = max(min_reserve, cost_reserve + close_buffer)

    # Don't reserve more than 30% of the total — that defeats the purpose
    max_reserve = int(total_sats * 0.30)
    if total_reserve > max_reserve:
        total_reserve = max_reserve
        reasoning_parts.append(f"capped at 30% ({max_reserve:,} sats)")

    reasoning_parts.append(f"close buffer: {close_buffer:,} sats")

    return {
        "reserve_sats": total_reserve,
        "reasoning": "; ".join(reasoning_parts),
    }


def _check_fee_environment():
    """Check current on-chain fee environment via mempool.space."""
    try:
        r = requests.get(f"{MEMPOOL_API}/v1/fees/recommended", timeout=10)
        r.raise_for_status()
        fees = r.json()
        fastest = fees.get("fastestFee", 0)
        half_hour = fees.get("halfHourFee", 0)
        hour = fees.get("hourFee", 0)
        economy = fees.get("economyFee", 0)

        # Determine if it's a good time to open channels
        if fastest > 100:
            assessment = "very_high"
            note = f"On-chain fees very high ({fastest} sat/vB). Consider waiting unless urgent."
        elif fastest > 50:
            assessment = "high"
            note = f"On-chain fees elevated ({fastest} sat/vB). Batch opens if possible."
        elif fastest > 20:
            assessment = "moderate"
            note = f"On-chain fees moderate ({fastest} sat/vB). Reasonable time to open."
        else:
            assessment = "low"
            note = f"On-chain fees low ({fastest} sat/vB). Good time to open channels."

        log.info("on-chain fees: %d sat/vB (%s)", fastest, assessment)
        return {
            "fastest_fee": fastest,
            "half_hour_fee": half_hour,
            "hour_fee": hour,
            "economy_fee": economy,
            "assessment": assessment,
            "note": note,
        }
    except Exception as e:
        log.warning("could not fetch on-chain fees: %s", e)
        return {
            "fastest_fee": 0,
            "assessment": "unknown",
            "note": f"Could not fetch fee data: {e}",
        }


def _analyse_existing_channels(channels):
    """Analyse existing channels for issues and opportunities."""
    analysis = {
        "undersized": [],     # channels below preferred minimum
        "inactive": [],       # offline peers
        "unprofitable": [],   # channels costing more than they earn
        "healthy": [],        # good channels
        "top_earners": [],    # best performing channels
    }

    for ch in channels:
        perf = None
        try:
            perf = db.get_channel_performance(ch["chan_id"])
        except Exception:
            pass

        entry = {
            "chan_id": ch["chan_id"],
            "peer_alias": ch["peer_alias"],
            "peer_pubkey": ch["peer_pubkey"],
            "capacity": ch["capacity"],
            "local_ratio": ch["local_ratio"],
            "active": ch["active"],
            "performance": perf,
        }

        if not ch["active"]:
            analysis["inactive"].append(entry)
        elif ch["capacity"] < MIN_CHANNEL_SIZE_SATS:
            entry["reason"] = f"only {ch['capacity']:,} sats — below {MIN_CHANNEL_SIZE_SATS:,} minimum"
            analysis["undersized"].append(entry)
        elif perf and perf["net_profit"] < 0 and perf["forwards"] > 0:
            entry["reason"] = (
                f"net loss of {abs(perf['net_profit']):,} sats/month "
                f"(earned {perf['fee_revenue']:,}, spent {perf['rebalance_cost']:,} rebalancing)"
            )
            analysis["unprofitable"].append(entry)
        else:
            analysis["healthy"].append(entry)

    # Identify top earners
    if any(ch.get("performance") for ch in analysis["healthy"]):
        by_revenue = sorted(
            [ch for ch in analysis["healthy"] if ch.get("performance")],
            key=lambda c: c["performance"]["fee_revenue"],
            reverse=True,
        )
        analysis["top_earners"] = by_revenue[:3]

    return analysis


def _fetch_external_candidates(state):
    """Fetch candidate peers from external sources."""
    candidates = []

    # 1. Try 1ML top nodes
    try:
        r = requests.get(
            f"{ONEML_API}/node?order=capacity&json=true",
            timeout=15,
            headers={"Accept": "application/json"},
        )
        if r.ok:
            nodes = r.json()
            if isinstance(nodes, list):
                for node in nodes[:50]:  # top 50 by capacity
                    pubkey = node.get("pub_key", node.get("pubkey", ""))
                    if pubkey and pubkey not in state["existing_peers"]:
                        candidates.append({
                            "pubkey": pubkey,
                            "alias": node.get("alias", pubkey[:12]),
                            "capacity": int(node.get("capacity", 0)),
                            "channel_count": int(node.get("channelcount", node.get("channel_count", 0))),
                            "source": "1ml",
                        })
    except Exception as e:
        log.warning("1ML fetch failed: %s", e)

    # 2. Use LND's local graph to find well-connected nodes we're not connected to
    try:
        net_info = lnd_client.get_network_info()
        # Get graph (this can be large — only do if we have few external candidates)
        if len(candidates) < 10:
            graph = lnd_client.describe_graph()
            node_map = {}
            for node in graph.get("nodes", []):
                pk = node.get("pub_key", "")
                if pk and pk not in state["existing_peers"] and pk != state["pubkey"]:
                    node_map[pk] = {
                        "pubkey": pk,
                        "alias": node.get("alias", pk[:12]),
                        "capacity": 0,
                        "channel_count": 0,
                        "source": "graph",
                    }

            # Count channels per node from edges
            for edge in graph.get("edges", []):
                cap = int(edge.get("capacity", 0))
                for pk_field in ["node1_pub", "node2_pub"]:
                    pk = edge.get(pk_field, "")
                    if pk in node_map:
                        node_map[pk]["capacity"] += cap
                        node_map[pk]["channel_count"] += 1

            # Take top nodes by channel count that we're not connected to
            graph_candidates = sorted(
                node_map.values(),
                key=lambda n: n["channel_count"],
                reverse=True,
            )[:30]
            candidates.extend(graph_candidates)

    except Exception as e:
        log.warning("graph analysis failed: %s", e)

    # Deduplicate by pubkey
    seen = set()
    unique = []
    for c in candidates:
        if c["pubkey"] not in seen:
            seen.add(c["pubkey"])
            unique.append(c)

    return unique


def _score_candidates(candidates, state):
    """Score and rank candidate peers.
    
    Score components (from config.PEER_SCORE_WEIGHTS):
    - capacity: total node capacity (bigger = better connected)
    - channels: number of channels (more = more routing paths)
    - uptime: estimated from graph data (not always available)
    - centrality: how many paths go through them
    - diversity: how much they improve YOUR connectivity
    """
    if not candidates:
        return []

    # Normalize capacity and channel counts for scoring
    max_capacity = max(c["capacity"] for c in candidates) if candidates else 1
    max_channels = max(c["channel_count"] for c in candidates) if candidates else 1

    for c in candidates:
        scores = {}

        # Capacity score (0-1, log scale)
        if c["capacity"] > 0 and max_capacity > 0:
            scores["capacity"] = math.log(1 + c["capacity"]) / math.log(1 + max_capacity)
        else:
            scores["capacity"] = 0

        # Channel count score (0-1, log scale)
        if c["channel_count"] > 0 and max_channels > 0:
            scores["channels"] = math.log(1 + c["channel_count"]) / math.log(1 + max_channels)
        else:
            scores["channels"] = 0

        # Uptime score — we don't have this from basic graph data
        # Default to 0.5 (unknown), could be enriched from 1ML or Terminal
        scores["uptime"] = 0.5

        # Centrality — approximate from channel count * capacity
        # Real betweenness centrality would require full graph traversal
        if max_capacity > 0 and max_channels > 0:
            scores["centrality"] = (scores["capacity"] + scores["channels"]) / 2
        else:
            scores["centrality"] = 0

        # Diversity — check if connecting to this node reaches new parts of the graph
        # Simple proxy: do we share any peers? Fewer shared = more diverse
        scores["diversity"] = 0.5  # placeholder — enriched if we have graph data

        # Check historical data
        peer_hist = db.get_peer_history(c["pubkey"])
        if peer_hist:
            # We've interacted with this peer before
            for record in peer_hist:
                if record["action"] == "closed" and "unreliable" in (record["reason"] or ""):
                    scores["uptime"] = 0.1  # penalise previously bad peers
                    c["history_note"] = f"Previously closed: {record['reason']}"

        # Weighted final score
        w = PEER_SCORE_WEIGHTS
        c["score"] = round(
            scores["capacity"] * w["capacity"] +
            scores["channels"] * w["channels"] +
            scores["uptime"] * w["uptime"] +
            scores["centrality"] * w["centrality"] +
            scores["diversity"] * w["diversity"],
            4
        )
        c["score_breakdown"] = scores

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if candidates:
        log.debug("top candidate: %s (score %.2f, %d channels)", 
                  candidates[0].get("alias","?"), candidates[0]["score"], candidates[0].get("channel_count",0))
    return candidates


def _allocate_budget(deployable, state, channel_analysis, candidates, fee_env):
    """Decide how to spend the deployable sats.
    
    Priority order:
    1. Upsize undersized existing channels (if they're performing)
    2. Open new channels to top-scored candidates
    3. Note anything not recommended
    """
    actions = []
    not_recommended = []
    remaining = deployable

    # ── Priority 1: Upsize undersized channels ───────────────────
    for ch in channel_analysis["undersized"]:
        if remaining < PREFERRED_CHANNEL_SIZE_SATS:
            break

        current_cap = ch["capacity"]
        upsize_to = PREFERRED_CHANNEL_SIZE_SATS
        additional = upsize_to - current_cap

        if additional > remaining:
            continue

        actions.append({
            "type": "upsize",
            "peer_alias": ch["peer_alias"],
            "peer_pubkey": ch["peer_pubkey"],
            "current_capacity": current_cap,
            "amount_sats": additional,
            "new_total": upsize_to,
            "reason": f"Current size {current_cap:,} is below minimum. "
                      f"Upsizing to {upsize_to:,} for better routing.",
            "priority": 1,
        })
        remaining -= additional

    # ── Priority 2: Open new channels ────────────────────────────
    # Determine optimal channel size given remaining budget
    if remaining >= PREFERRED_CHANNEL_SIZE_SATS:
        # How many channels can we afford at preferred size?
        max_new_channels = remaining // PREFERRED_CHANNEL_SIZE_SATS
        # Don't open too many at once — 2-3 is reasonable
        num_to_open = min(max_new_channels, 3, len(candidates))

        if num_to_open > 0:
            channel_size = min(
                remaining // num_to_open,
                MAX_CHANNEL_SIZE_SATS,
            )
            channel_size = max(channel_size, PREFERRED_CHANNEL_SIZE_SATS)

            for i in range(num_to_open):
                if remaining < PREFERRED_CHANNEL_SIZE_SATS:
                    break
                if i >= len(candidates):
                    break

                candidate = candidates[i]
                size = min(channel_size, remaining)

                actions.append({
                    "type": "open",
                    "peer_alias": candidate["alias"],
                    "peer_pubkey": candidate["pubkey"],
                    "amount_sats": size,
                    "score": candidate["score"],
                    "reason": (
                        f"Score {candidate['score']:.2f} — "
                        f"{candidate['channel_count']} channels, "
                        f"{candidate['capacity']:,} sats capacity. "
                        f"Source: {candidate['source']}"
                    ),
                    "priority": 2,
                })
                remaining += 0  # track it but don't subtract yet (this is a plan)
                remaining -= size

    elif remaining >= MIN_CHANNEL_SIZE_SATS:
        # Can afford one small channel
        if candidates:
            actions.append({
                "type": "open",
                "peer_alias": candidates[0]["alias"],
                "peer_pubkey": candidates[0]["pubkey"],
                "amount_sats": remaining,
                "score": candidates[0]["score"],
                "reason": (
                    f"Budget only allows one channel at {remaining:,} sats. "
                    f"Consider saving more for a {PREFERRED_CHANNEL_SIZE_SATS:,} channel."
                ),
                "priority": 2,
            })
            remaining = 0
    else:
        not_recommended.append(
            f"Remaining {remaining:,} sats too small for a new channel "
            f"(minimum {MIN_CHANNEL_SIZE_SATS:,}). Added to treasury."
        )

    # ── Close recommendations ────────────────────────────────────
    for ch in channel_analysis["inactive"]:
        not_recommended.append(
            f"Consider closing inactive channel to {ch['peer_alias']} "
            f"({ch['capacity']:,} sats locked). "
            f"Monitor for {72}h before closing."
        )

    for ch in channel_analysis["unprofitable"]:
        not_recommended.append(
            f"Channel to {ch['peer_alias']} is unprofitable: {ch.get('reason', '')}. "
            f"Consider closing and redeploying sats."
        )

    # ── Fee environment warning ──────────────────────────────────
    if fee_env.get("assessment") in ("high", "very_high"):
        not_recommended.insert(0, fee_env["note"])

    # Add onchain fee note to plan
    plan_note = fee_env.get("note", "")

    log.info("allocation: %d action(s), %d concern(s)", len(actions), len(not_recommended))
    for a in actions:
        log.info("  → %s %s: %s sats", a["type"], a["peer_alias"], f"{a['amount_sats']:,}")
    # Sort actions by priority
    actions.sort(key=lambda a: a.get("priority", 99))

    return actions, not_recommended
