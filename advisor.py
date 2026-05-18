"""
LN Operator — Advisor (peer scoring and graph analysis)

Provides the scoring and candidate discovery logic used by the `plan` command.
All data comes from the local LND graph — no external API dependencies.

Key functions used by main.py plan command:
- _gather_node_state(): pulls live data from LND (channels, balances, graph)
- _fetch_candidates_from_graph(): builds candidate list from local LND graph
- _score_candidates(): scores by channel count, diversity, centrality
- _classify_existing_portfolio(): classifies existing channels as hub or mid-tier
- _calculate_treasury(): calculates reserve (treasury % + anchor reserve + open fees)
- _check_fee_environment(): gets fee rate from LND, falls back to mempool.space

The build_investment_plan() function is kept for legacy/DB logging purposes
but the main CLI entry point is now cmd_plan() in main.py which calls
these functions directly for a cleaner flow.

NOTE: The Claude API agent layer (agent.py) has been removed from the plan
workflow. Peer research is now done by the user using the top 10 candidates
output from the local graph.
"""

import time
import math
import requests
from config import (
    TREASURY_MIN_RATIO, TREASURY_MONTHS_RESERVE,
    MIN_CHANNEL_SIZE_SATS, PREFERRED_CHANNEL_SIZE_SATS, MAX_CHANNEL_SIZE_SATS,
    PEER_SCORE_WEIGHTS, MEMPOOL_API, ONEML_API,
    ANCHOR_RESERVE_PER_CHANNEL, ANCHOR_RESERVE_MAX,
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

    # 2. Check on-chain fee environment first — needed for accurate treasury calc
    fee_env = _check_fee_environment()

    # 3. Calculate treasury reserve (uses real fee rate from mempool.space)
    # Estimate number of new channels based on deployable budget
    estimated_channels = min(max(1, (total_sats * (1 - TREASURY_MIN_RATIO)) // PREFERRED_CHANNEL_SIZE_SATS), 10)
    treasury = _calculate_treasury(
        total_sats, state,
        num_new_channels=int(estimated_channels),
        fee_rate_sat_vb=fee_env.get("fastest_fee", 3)
    )

    deployable = total_sats - treasury["reserve_sats"]

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

    # Current anchor reserve already locked by LND
    current_anchor_reserve = int(onchain.get("reserved_balance_anchor_chan", 0))

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
        "current_anchor_reserve": current_anchor_reserve,
        "pending_channels": pending,
        "existing_peers": set(c["peer_pubkey"] for c in channels),
    }


def _calculate_treasury(total_sats, state, num_new_channels=2, fee_rate_sat_vb=3):
    """Determine how much to keep in reserve.

    Accounts for:
    - Treasury minimum ratio (2.5% default)
    - Historical rebalancing costs (3 months average)
    - Anchor reserve for new channels being opened (10,000 sats each, capped at 100,000)
    - On-chain fees for opening channels (real fee rate from mempool.space × ~250 vBytes)

    num_new_channels: how many new channels we plan to open (affects anchor reserve calc)
    fee_rate_sat_vb: current on-chain fee rate in sat/vB from mempool.space
    """
    # Minimum percentage-based reserve
    min_reserve = int(total_sats * TREASURY_MIN_RATIO)

    # Anchor reserve for new channels
    existing_reserve = state.get("current_anchor_reserve", 0)
    new_anchor_needed = min(
        num_new_channels * ANCHOR_RESERVE_PER_CHANNEL,
        max(0, ANCHOR_RESERVE_MAX - existing_reserve)
    )

    # On-chain opening fees: real fee rate x ~250 vBytes per channel open tx
    channel_open_tx_vbytes = 250
    onchain_open_fees = num_new_channels * fee_rate_sat_vb * channel_open_tx_vbytes

    # Cost-based reserve from historical data
    avg_monthly_cost = db.get_avg_monthly_rebalance_cost(months=3)
    cost_reserve = int(avg_monthly_cost * TREASURY_MONTHS_RESERVE)

    # Channel close buffer
    close_buffer = 50_000 * max(1, state["num_channels"] // 5)

    # Build reasoning
    reasoning_parts = []
    if cost_reserve > 0:
        reasoning_parts.append(f"{TREASURY_MONTHS_RESERVE}mo avg rebalance cost: {cost_reserve:,} sats")
    else:
        reasoning_parts.append("no rebalancing history yet — using minimum ratio")
    reasoning_parts.append(f"anchor reserve for {num_new_channels} new channel(s): {new_anchor_needed:,} sats")
    reasoning_parts.append(
        f"est. open fees: {onchain_open_fees:,} sats "
        f"({num_new_channels} x {fee_rate_sat_vb} sat/vB x {channel_open_tx_vbytes} vB)"
    )
    reasoning_parts.append(f"close buffer: {close_buffer:,} sats")

    # Total reserve
    total_reserve = max(min_reserve, cost_reserve + close_buffer)
    total_reserve += new_anchor_needed + onchain_open_fees

    # Cap at 30%
    max_reserve = int(total_sats * 0.30)
    if total_reserve > max_reserve:
        total_reserve = max_reserve
        reasoning_parts.append(f"capped at 30% ({max_reserve:,} sats)")

    return {
        "reserve_sats": total_reserve,
        "reasoning": "; ".join(reasoning_parts),
    }



def _check_fee_environment():
    """Check current on-chain fee environment.

    Primary source: LND's fee estimator (uses your Bitcoin Core node — no external call).
    Fallback: mempool.space if LND estimate is unavailable.
    """
    def _assess(fastest):
        if fastest > 100:
            return "very_high", f"On-chain fees very high ({fastest} sat/vB). Consider waiting unless urgent."
        elif fastest > 50:
            return "high", f"On-chain fees elevated ({fastest} sat/vB). Batch opens if possible."
        elif fastest > 20:
            return "moderate", f"On-chain fees moderate ({fastest} sat/vB). Reasonable time to open."
        else:
            return "low", f"On-chain fees low ({fastest} sat/vB). Good time to open channels."

    # ── Step 1: Try LND (primary) ─────────────────────────────────
    lnd_fee = lnd_client.estimate_fee(conf_target=2)
    if lnd_fee:
        log.info("on-chain fees: %d sat/vB (from LND)", lnd_fee)
        assessment, note = _assess(lnd_fee)
        return {
            "fastest_fee": lnd_fee,
            "half_hour_fee": lnd_client.estimate_fee(conf_target=6) or lnd_fee,
            "hour_fee": lnd_client.estimate_fee(conf_target=12) or lnd_fee,
            "economy_fee": lnd_client.estimate_fee(conf_target=144) or lnd_fee,
            "assessment": assessment,
            "note": note,
            "source": "lnd",
        }

    # ── Step 2: Fall back to mempool.space ────────────────────────
    log.debug("LND fee estimate unavailable — falling back to mempool.space")
    try:
        r = requests.get(f"{MEMPOOL_API}/v1/fees/recommended", timeout=10)
        r.raise_for_status()
        fees = r.json()
        fastest = fees.get("fastestFee", 3)
        assessment, note = _assess(fastest)
        log.info("on-chain fees: %d sat/vB (from mempool.space)", fastest)
        return {
            "fastest_fee": fastest,
            "half_hour_fee": fees.get("halfHourFee", fastest),
            "hour_fee": fees.get("hourFee", fastest),
            "economy_fee": fees.get("economyFee", fastest),
            "assessment": assessment,
            "note": note,
            "source": "mempool.space",
        }
    except Exception as e:
        log.warning("mempool.space also unavailable: %s — using safe default", e)
        return {
            "fastest_fee": 3,
            "half_hour_fee": 3,
            "hour_fee": 2,
            "economy_fee": 1,
            "assessment": "unknown",
            "note": "Could not fetch fee data — using 3 sat/vB as safe default.",
            "source": "default",
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


def _fetch_candidates_from_graph(state):
    """Build candidate list from LND's local graph — the primary data source.

    WHY local graph and not 1ML:
    - Always available, always up to date, no external dependency
    - Contains topology data (who connects to whom) which 1ML doesn't expose well
    - Capacity numbers may be incomplete for distant nodes (gossip propagation
      depends on your channel connections) — this improves as you add channels
    - 1ML is used only to cross-check/confirm aliases, not as primary source

    Traverses the full network graph to:
    1. Build a map of every node with their channel count and total capacity
    2. Rank them by channel count to determine hub vs mid-tier
    3. Filter out nodes we're already connected to and ourselves
    4. Assign network_rank and tier_hint based on channel count rank

    Returns a list of candidate dicts sorted by channel count descending.
    The graph is already stored locally by LND — no external API needed.
    """
    log.info("fetching candidates from local LND graph...")
    candidates = []

    try:
        graph = lnd_client.describe_graph()
        nodes_raw = graph.get("nodes", [])
        edges_raw = graph.get("edges", [])

        # Build node map — exclude existing peers and ourselves
        node_map = {}
        for node in nodes_raw:
            pk = node.get("pub_key", "")
            if pk and pk not in state["existing_peers"] and pk != state["pubkey"]:
                node_map[pk] = {
                    "pubkey": pk,
                    "alias": node.get("alias", pk[:12]),
                    "capacity": 0,
                    "channel_count": 0,
                    "source": "graph",
                    "last_update": node.get("last_update", 0),
                    "fee_ppm_sum": 0,      # sum of outbound fee rates across channels
                    "fee_ppm_count": 0,    # number of channels with fee data
                }

        # Count channels, sum capacity, and collect fee rates from edges
        for edge in edges_raw:
            cap = int(edge.get("capacity", 0))
            for pk_field, policy_field in [("node1_pub", "node1_policy"), ("node2_pub", "node2_policy")]:
                pk = edge.get(pk_field, "")
                if pk in node_map:
                    node_map[pk]["capacity"] += cap
                    node_map[pk]["channel_count"] += 1
                    # Collect outbound fee rate for this node's side of the channel
                    policy = edge.get(policy_field) or {}
                    fee_rate = int(policy.get("fee_rate_milli_msat", 0))
                    if policy:  # only count if policy exists
                        node_map[pk]["fee_ppm_sum"] += fee_rate
                        node_map[pk]["fee_ppm_count"] += 1

        # Filter out nodes with zero channels (likely inactive/phantom nodes)
        active_nodes = [n for n in node_map.values() if n["channel_count"] > 0]

        # Sort by channel count descending to establish network rank
        active_nodes.sort(key=lambda n: n["channel_count"], reverse=True)

        log.info("graph: %d total nodes, %d active (have channels), %d excluded (existing peers)",
                 len(nodes_raw), len(active_nodes), len(state["existing_peers"]))

        # Assign network rank and tier based on channel count rank
        # Top 50 = hubs, 51-250 = mid-tier, 251-500 = small
        for i, node in enumerate(active_nodes[:500]):
            rank = i + 1
            if rank <= 50:
                tier = "hub"
            elif rank <= 250:
                tier = "mid-tier"
            else:
                tier = "small"
            node["network_rank"] = rank
            node["tier_hint"] = tier
            # avg_channel_size is a quality metric — larger avg = more serious routing partner
            # Note: local graph capacity may be incomplete for distant nodes;
            # this improves as you open more channels.
            node["avg_channel_size"] = (
                node["capacity"] // node["channel_count"]
                if node["channel_count"] > 0 else 0
            )
            # Average outbound fee rate — lower is better for routing through them
            node["avg_fee_ppm"] = (
                node["fee_ppm_sum"] // node["fee_ppm_count"]
                if node["fee_ppm_count"] > 0 else 0
            )
            candidates.append(node)

        log.info("graph candidates: %d hubs (rank 1-50), %d mid-tier (rank 51-250), %d small (rank 251-500)",
                 sum(1 for c in candidates if c["tier_hint"] == "hub"),
                 sum(1 for c in candidates if c["tier_hint"] == "mid-tier"),
                 sum(1 for c in candidates if c["tier_hint"] == "small"))

    except Exception as e:
        log.warning("graph candidate fetch failed: %s", e)

    return candidates


def _enrich_with_1ml_aliases(candidates):
    """Optionally enrich candidate aliases from 1ML.

    1ML is no longer the primary candidate source. We use it only to confirm
    or correct the alias names the LND graph gossips. This matters because
    aliases in the graph can be stale if a node updated their name recently.
    Fails completely silently — if 1ML is down, candidates still have their
    LND graph aliases which are good enough.

    1ML is no longer the primary source — it's used only to cross-reference
    aliases and confirm node names for the top candidates.
    Fails silently if 1ML is down.
    """
    try:
        r = requests.get(
            f"{ONEML_API}/node?order=capacity&json=true",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        if not r.ok:
            return candidates

        nodes = r.json()
        if not isinstance(nodes, list):
            return candidates

        # Build alias lookup from 1ML
        alias_lookup = {}
        for node in nodes[:200]:
            pk = node.get("pub_key", node.get("pubkey", ""))
            alias = node.get("alias", "")
            if pk and alias:
                alias_lookup[pk] = alias

        # Update aliases where 1ML has a better/confirmed name
        enriched = 0
        for c in candidates:
            if c["pubkey"] in alias_lookup:
                c["alias"] = alias_lookup[c["pubkey"]]
                c["alias_confirmed"] = True
                enriched += 1

        log.debug("1ML enriched %d candidate aliases", enriched)

    except Exception as e:
        log.debug("1ML alias enrichment skipped: %s", e)

    return candidates


def _fetch_external_candidates(state):
    """Build candidate peer list for scoring.

    Primary source: local LND graph (always available, always fresh).
    Secondary enrichment: 1ML for alias confirmation (optional, fails silently).
    """
    # Step 1: Get all candidates from local graph
    candidates = _fetch_candidates_from_graph(state)

    if not candidates:
        log.warning("no candidates found from graph — something may be wrong with graph sync")
        return []

    # Step 2: Enrich aliases from 1ML (best-effort)
    candidates = _enrich_with_1ml_aliases(candidates)

    return candidates


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

    for c in candidates[:10]:  # enrich shortlisted 10 — graph calls are slow
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

    # Normalisation maximums
    max_channels = max(c.get("channel_count", 0) for c in candidates) or 1
    max_capacity = max(c.get("capacity", 0) for c in candidates) or 1
    max_fee_ppm  = max(c.get("avg_fee_ppm", 0) for c in candidates) or 1

    for c in candidates:
        scores = {}

        # Centrality proxy — combination of channels and capacity normalised
        # Captures network importance: well-connected, high-capacity nodes score high
        ch = c.get("channel_count", 0)
        cap = c.get("capacity", 0)
        ch_score = math.log(1 + ch) / math.log(1 + max_channels) if ch > 0 else 0
        cap_score = math.log(1 + cap) / math.log(1 + max_capacity) if cap > 0 else 0
        scores["centrality"] = (ch_score + cap_score) / 2

        # Diversity — what % of their peers are new to you
        # Most important metric for a small node — maximises your reach
        if c.get("diversity_score_computed") is not None:
            scores["diversity"] = c["diversity_score_computed"]
        else:
            scores["diversity"] = 0.5  # placeholder until graph enrichment runs

        # Low fee — inverted: lower avg outbound fee = higher score
        # A 0 ppm node scores 1.0, expensive nodes score lower
        # Using inverted log scale so the penalty is gradual, not cliff-like
        fee = c.get("avg_fee_ppm", 0)
        if max_fee_ppm > 0 and fee > 0:
            scores["low_fee"] = 1.0 - (math.log(1 + fee) / math.log(1 + max_fee_ppm))
        else:
            scores["low_fee"] = 1.0  # no fee data or 0 fee = best score

        # Penalise previously unreliable peers from DB history
        peer_hist = db.get_peer_history(c["pubkey"])
        if peer_hist:
            for record in peer_hist:
                if record["action"] == "closed" and "unreliable" in (record["reason"] or ""):
                    scores["centrality"] *= 0.5
                    c["history_note"] = f"Previously closed: {record['reason']}"

        # Weighted final score
        w = PEER_SCORE_WEIGHTS
        c["score"] = round(
            scores["diversity"]   * w["diversity"] +
            scores["centrality"]  * w["centrality"] +
            scores["low_fee"]     * w["low_fee"],
            4
        )
        c["score_breakdown"] = scores

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if candidates:
        log.debug("top candidate: %s (score %.2f, %d channels)", 
                  candidates[0].get("alias","?"), candidates[0]["score"], candidates[0].get("channel_count",0))
    return candidates


# ─── Hub/mid-tier classification ────────────────────────────────
# A hub is defined by absolute channel count, not relative rank.
# This is intentional — we want classification to be stable regardless
# of how many nodes your local graph knows about.
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
    """Split candidates into hub (network rank 1-50) and mid-tier (rank 51-200).

    Uses network_rank from 1ML if available (rank by capacity on the network).
    Falls back to sorting by channel count if rank not set (e.g. graph-sourced nodes).
    Returns (hubs, mid_tier) lists, each sorted by score descending.
    """
    hubs = []
    mid_tier = []

    for c in candidates:
        rank = c.get("network_rank")
        tier_hint = c.get("tier_hint")

        if tier_hint == "hub" or (rank is not None and rank <= 50):
            hubs.append(c)
        elif tier_hint == "mid-tier" or (rank is not None and rank > 50):
            mid_tier.append(c)
        else:
            # No rank info — classify by channel count
            if c.get("channel_count", 0) >= HUB_CHANNEL_THRESHOLD:
                hubs.append(c)
            else:
                mid_tier.append(c)

    # Within each tier, sort by score
    hubs.sort(key=lambda c: c["score"], reverse=True)
    mid_tier.sort(key=lambda c: c["score"], reverse=True)

    log.debug("tier split: %d hubs, %d mid-tier candidates", len(hubs), len(mid_tier))
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
        "network_rank": candidate.get("network_rank"),
        "tier_hint": candidate.get("tier_hint"),
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
    hubs, mid_tier, small = _split_candidates_by_tier(candidates)

    log.info("portfolio: %d hub connection(s), %d mid-tier connection(s)",
             hub_count, portfolio["mid_tier_count"])
    log.info("candidates: %d hub(s), %d mid-tier, %d small available",
             len(hubs), len(mid_tier), len(small))

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
        max_new = min(remaining // PREFERRED_CHANNEL_SIZE_SATS, 10)

        # Portfolio strategy decision:
        # The goal is balanced connectivity — not all hubs (too much competition,
        # all traffic flows through the same corridors) and not all mid-tier
        # (less initial traffic). Start with hubs for backbone, then diversify.
        if hub_count == 0:
            # No hubs yet — shortlist top 10 hubs for agent to evaluate
            pool = hubs[:10] if hubs else candidates[:10]
            strategy = "no hub connections yet — shortlisting top 10 hubs"
            log.info("allocation strategy: %s", strategy)
            not_recommended.append(
                f"Strategy: {strategy}. "
                f"Once you have 1-2 hubs, future opens will target mid-tier nodes."
            )
        elif hub_count >= 2:
            # Well connected to hubs — shortlist top 10 mid-tier for agent
            pool = mid_tier[:10] if mid_tier else candidates[:10]
            strategy = f"already have {hub_count} hub connections — shortlisting top 10 mid-tier nodes"
            log.info("allocation strategy: %s", strategy)
            not_recommended.append(
                f"Strategy: {strategy}. "
                f"Mid-tier nodes offer better diversity and less fee competition."
            )
        else:
            # 1 hub — mix of one more hub + mid-tier nodes
            hub_picks = hubs[:2] if hubs else []
            mid_picks = mid_tier[:8] if mid_tier else []
            pool = hub_picks + mid_picks if (hub_picks or mid_picks) else candidates
            strategy = f"1 hub already — shortlisting {len(hub_picks)} hub(s) + {len(mid_picks)} mid-tier"
            log.info("allocation strategy: %s", strategy)

        # ── Step 4: Shortlist top 10 from pool for agent evaluation ─
        # These are candidates for the agent to research — NOT final allocation.
        # The agent picks the best 1-3 from this list based on Amboss/1ML data.
        # Budget allocation happens based on how many the agent recommends.
        shortlist = pool[:10]
        log.info("shortlisting %d candidates", len(shortlist))

        for candidate in shortlist:
            gd = candidate.get("graph_data") or {}
            reason_parts = [
                f"Score {candidate['score']:.2f}",
                f"rank {candidate.get('network_rank','?')}",
                f"{candidate['channel_count']} channels",
                f"{candidate['capacity']:,} sats capacity (local graph)",
            ]
            if gd.get("avg_fee_ppm"):
                reason_parts.append(f"local avg fee {gd['avg_fee_ppm']} ppm")
            if gd.get("diversity_score") is not None:
                reason_parts.append(f"diversity {gd['diversity_score']:.0%}")

            # Amount is 0 here — agent decides which to open, budget allocated after
            actions.append(_make_open_action(
                candidate,
                0,  # no allocation yet — agent picks from shortlist
                " — ".join(reason_parts),
                priority=2
            ))

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
