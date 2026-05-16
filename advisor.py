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

    # 6b. Enrich top candidates with live graph data (diversity, fees, reachability)
    log.info("enriching top candidates with graph data...")
    scored_candidates = _enrich_candidates_with_graph_data(scored_candidates, state)

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


def _enrich_candidates_with_graph_data(candidates, state):
    """Pull per-candidate graph data from LND for richer scoring and agent context.

    For each top candidate, fetches:
    - Their peer list (to compute diversity: how many peers we don't share)
    - Their fee policies (average fee rate on their channels)
    - Whether they're reachable (have a known address)

    This is expensive (one API call per candidate) so we only do it for the
    top N candidates that will actually appear in the plan.
    """
    our_peers = state["existing_peers"]
    enriched = []

    for c in candidates[:15]:  # only enrich top 15 — graph calls are slow
        try:
            node_info = lnd_client.get_node_info(c["pubkey"], include_channels=True)
            node = node_info.get("node", {})
            channels = node_info.get("channels", [])

            # Compute diversity: what fraction of their peers are new to us?
            their_peers = set()
            total_fee_rate = 0
            fee_count = 0

            for ch in channels:
                # Find the peer on the other end
                n1 = ch.get("node1_pub", "")
                n2 = ch.get("node2_pub", "")
                peer_pk = n2 if n1 == c["pubkey"] else n1
                if peer_pk:
                    their_peers.add(peer_pk)

                # Average fee rate from their policy
                policy = ch.get("node1_policy") if n1 == c["pubkey"] else ch.get("node2_policy")
                if policy and policy.get("fee_rate_milli_msat"):
                    total_fee_rate += int(policy["fee_rate_milli_msat"]) / 1000  # convert to ppm
                    fee_count += 1

            # Diversity: fraction of their peers we're not already connected to
            if their_peers:
                new_peers = their_peers - our_peers - {state["pubkey"]}
                diversity = len(new_peers) / len(their_peers)
            else:
                diversity = 0.5

            avg_fee_ppm = int(total_fee_rate / fee_count) if fee_count > 0 else 0

            # Is the node reachable?
            addresses = node.get("addresses", [])
            has_clearnet = any(
                not a.get("addr", "").endswith(".onion")
                for a in addresses
            )

            c["graph_data"] = {
                "their_peer_count": len(their_peers),
                "new_peers_to_us": len(their_peers - our_peers - {state["pubkey"]}),
                "diversity_score": round(diversity, 3),
                "avg_fee_ppm": avg_fee_ppm,
                "has_clearnet": has_clearnet,
                "addresses": [a.get("addr", "") for a in addresses[:3]],
            }
            c["diversity_score_computed"] = diversity

        except Exception as e:
            log.debug("could not enrich %s with graph data: %s", c.get("alias", "?"), e)
            c["graph_data"] = None

        enriched.append(c)

    # Return enriched + any remaining un-enriched candidates
    enriched_pubkeys = {c["pubkey"] for c in enriched}
    for c in candidates[15:]:
        if c["pubkey"] not in enriched_pubkeys:
            enriched.append(c)

    return enriched


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

        # Diversity — use computed score from graph data if available, else placeholder
        if c.get("diversity_score_computed") is not None:
            scores["diversity"] = c["diversity_score_computed"]
        else:
            scores["diversity"] = 0.5  # placeholder

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


# Hub classification threshold — nodes in the top N by channel count are "hubs"
HUB_CHANNEL_THRESHOLD = 100   # nodes with 100+ channels are considered hubs


def _classify_existing_portfolio(channels, candidates):
    """Classify existing channels as hub or mid-tier connections.

    A hub is a node with 100+ channels (well-connected, high-traffic corridor).
    Mid-tier nodes have fewer channels but may offer good diversity.

    Existing peers are excluded from candidates so we can't use the candidates
    list to look up their channel counts. Instead we call LND directly for each
    existing peer to get their channel count from the graph.

    Returns a dict with hub_count, mid_tier_count, and the hub pubkeys.
    """
    hub_pubkeys = set()
    mid_tier_pubkeys = set()

    for ch in channels:
        pk = ch.get("peer_pubkey", ch.get("remote_pubkey", ""))
        if not pk:
            continue

        # Look up channel count from LND graph for this existing peer
        channel_count = 0
        try:
            node_info = lnd_client.get_node_info(pk, include_channels=False)
            channel_count = int(node_info.get("num_channels", 0))
            log.debug("existing peer %s has %d channels in graph",
                      ch.get("peer_alias", pk[:12]), channel_count)
        except Exception as e:
            log.warning("could not get graph info for existing peer %s: %s",
                        ch.get("peer_alias", pk[:12]), e)

        if channel_count >= HUB_CHANNEL_THRESHOLD:
            hub_pubkeys.add(pk)
            log.debug("existing peer %s classified as hub (%d channels)",
                      ch.get("peer_alias", pk[:12]), channel_count)
        else:
            mid_tier_pubkeys.add(pk)
            log.debug("existing peer %s classified as mid-tier (%d channels)",
                      ch.get("peer_alias", pk[:12]), channel_count)

    return {
        "hub_count": len(hub_pubkeys),
        "mid_tier_count": len(mid_tier_pubkeys),
        "hub_pubkeys": hub_pubkeys,
    }


def _split_candidates_by_tier(candidates):
    """Split candidates into hub (top 50 by channels) and mid-tier (50-200).

    Returns (hubs, mid_tier) lists, each sorted by score descending.
    Candidates are sorted by channel count descending so rank is stable.
    """
    sorted_by_channels = sorted(
        candidates, key=lambda c: c.get("channel_count", 0), reverse=True
    )
    hub_pubkeys = {c["pubkey"] for c in sorted_by_channels[:50]}

    hubs = [c for c in candidates if c["pubkey"] in hub_pubkeys]
    mid_tier = [c for c in candidates if c["pubkey"] not in hub_pubkeys]

    # Within each tier, sort by score
    hubs.sort(key=lambda c: c["score"], reverse=True)
    mid_tier.sort(key=lambda c: c["score"], reverse=True)

    return hubs, mid_tier


def _make_open_action(candidate, size, reason, priority=2):
    """Build a channel open action dict."""
    gd = candidate.get("graph_data") or {}
    return {
        "type": "open",
        "peer_alias": candidate["alias"],
        "peer_pubkey": candidate["pubkey"],
        "amount_sats": size,
        "score": candidate["score"],
        "channel_count": candidate.get("channel_count", 0),
        "capacity": candidate.get("capacity", 0),
        "graph_data": gd,
        "reason": reason,
        "priority": priority,
    }


def _allocate_budget(deployable, state, channel_analysis, candidates, fee_env):
    """Decide how to spend the deployable sats.

    Portfolio-aware allocation:
    1. Upsize undersized existing channels first
    2. Classify existing portfolio — how many hubs vs mid-tier nodes?
    3. If no hubs yet → recommend one hub to establish routing backbone
    4. If 1-2 hubs already → recommend mid-tier nodes (top 50-200) for diversity
    5. If well diversified → use overall score ranking
    """
    actions = []
    not_recommended = []
    remaining = deployable

    # ── Step 1: Classify existing portfolio ──────────────────────
    all_channels = state.get("channels", [])
    portfolio = _classify_existing_portfolio(all_channels, candidates)
    hub_count = portfolio["hub_count"]
    hubs, mid_tier = _split_candidates_by_tier(candidates)

    log.info("portfolio: %d hub connection(s), %d mid-tier connection(s)",
             hub_count, portfolio["mid_tier_count"])
    log.info("candidates: %d hub(s), %d mid-tier available",
             len(hubs), len(mid_tier))

    # ── Step 2: Upsize undersized channels ────────────────────────
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
            "reason": f"Current size {current_cap:,} sats is below minimum. "
                      f"Upsizing to {upsize_to:,} for better routing.",
            "priority": 1,
        })
        remaining -= additional

    if remaining < MIN_CHANNEL_SIZE_SATS:
        # Budget exhausted on upsizing
        pass

    elif remaining >= PREFERRED_CHANNEL_SIZE_SATS:
        # ── Step 3: Decide which tier to target ──────────────────
        max_new = min(remaining // PREFERRED_CHANNEL_SIZE_SATS, 3)

        if hub_count == 0:
            # No hubs yet — need at least one routing backbone connection
            pool = hubs if hubs else candidates
            strategy = "no hub connections yet — opening one large hub first"
            log.info("allocation strategy: %s", strategy)
            not_recommended.append(
                f"Strategy: {strategy}. "
                f"Once you have 1-2 hubs, future opens will target mid-tier nodes."
            )
        elif hub_count >= 2:
            # Well connected to hubs — diversify into mid-tier
            pool = mid_tier if mid_tier else candidates
            strategy = f"already have {hub_count} hub connections — targeting mid-tier nodes (rank 50-200) for diversity"
            log.info("allocation strategy: %s", strategy)
            not_recommended.append(
                f"Strategy: {strategy}. "
                f"Mid-tier nodes offer better diversity and less fee competition."
            )
        else:
            # 1 hub — one more hub OR start mid-tier depending on budget
            if remaining >= PREFERRED_CHANNEL_SIZE_SATS * 2 and mid_tier:
                # Enough for both — split: one hub, one mid-tier
                pool = [hubs[0]] + [mid_tier[0]] if hubs and mid_tier else candidates
                max_new = min(2, max_new)
                strategy = "1 hub already — adding one more hub + one mid-tier node"
            else:
                pool = mid_tier if mid_tier else candidates
                strategy = "1 hub already — moving to mid-tier nodes for diversification"
            log.info("allocation strategy: %s", strategy)

        # ── Step 4: Allocate to chosen pool ──────────────────────
        num_to_open = min(max_new, len(pool))
        if num_to_open > 0:
            channel_size = min(remaining // num_to_open, MAX_CHANNEL_SIZE_SATS)
            channel_size = max(channel_size, PREFERRED_CHANNEL_SIZE_SATS)

            for i in range(num_to_open):
                if remaining < PREFERRED_CHANNEL_SIZE_SATS or i >= len(pool):
                    break
                candidate = pool[i]
                size = min(channel_size, remaining)
                gd = candidate.get("graph_data") or {}

                reason_parts = [
                    f"Score {candidate['score']:.2f}",
                    f"{candidate['channel_count']} channels",
                    f"{candidate['capacity']:,} sats capacity",
                ]
                if gd.get("avg_fee_ppm"):
                    reason_parts.append(f"avg fee {gd['avg_fee_ppm']} ppm")
                if gd.get("diversity_score") is not None:
                    reason_parts.append(f"diversity {gd['diversity_score']:.0%}")
                reason_parts.append(f"source: {candidate['source']}")

                actions.append(_make_open_action(
                    candidate, size, " — ".join(reason_parts), priority=2
                ))
                remaining -= size

    elif remaining >= MIN_CHANNEL_SIZE_SATS:
        # Only enough for one small channel
        best = (mid_tier[0] if hub_count >= 1 and mid_tier else
                hubs[0] if hubs else
                candidates[0] if candidates else None)
        if best:
            actions.append(_make_open_action(
                best, remaining,
                f"Budget only allows one channel at {remaining:,} sats. "
                f"Consider saving more for a {PREFERRED_CHANNEL_SIZE_SATS:,} sat channel.",
                priority=2,
            ))
            remaining = 0
    else:
        not_recommended.append(
            f"Remaining {remaining:,} sats too small for a new channel "
            f"(minimum {MIN_CHANNEL_SIZE_SATS:,} sats). Added to treasury."
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
