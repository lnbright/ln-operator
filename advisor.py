"""
LN Operator — Advisor (peer scoring and graph analysis)

Provides the scoring and candidate discovery logic used by the `plan` command.
All data comes from the local LND graph — no external API dependencies.

Key functions used by main.py plan command:
- _gather_node_state(): pulls live data from LND (channels, balances, graph)
- _fetch_candidates_from_graph(): builds candidate list from local LND graph,
  assigns tier_hint by absolute channel count (hub / mid-tier / small)
- _score_candidates(): stage-1 centrality prefilter (channels + capacity,
  log-normalised). Sorts the list but does not rerank within tiers.
- _rerank_tiers_by_diversity(): stage-2 — within each tier, takes the top
  ENRICH_PER_TIER by centrality, fetches live graph data per candidate to
  compute diversity (% of their peers that sit outside our 2-hop reachable
  set, i.e. would genuinely expand our graph horizon), then reranks by
  diversity and returns top SHOW_PER_TIER per tier.
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
    MEMPOOL_API, ONEML_API,
    ANCHOR_RESERVE_PER_CHANNEL, ANCHOR_RESERVE_MAX,
)

# ─── Tier thresholds (absolute channel counts) ──────────────────
# Candidates are bucketed by their channel count in the public graph.
# A diversifying small node and a diversifying hub answer different
# questions, so they're ranked independently inside their own tier.
HUB_MIN_CHANNELS = 100   # well-connected routing hubs
MID_MIN_CHANNELS = 30    # serious mid-tier routing nodes
SMALL_MIN_CHANNELS = 10  # modest operators; below this, treated as noise and dropped

# Two-stage ranking budget — see _rerank_tiers_by_diversity.
ENRICH_PER_TIER = 30  # candidates per tier sent through live graph enrichment
SHOW_PER_TIER = 10    # final candidates surfaced per tier after diversity rerank
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
    # Cap at ENRICH_PER_TIER × 3 so this legacy path can't fan out into thousands
    # of LND calls if resurrected. New plan flow uses _rerank_tiers_by_diversity.
    log.info("enriching top candidates with graph data...")
    _enrich_candidates_with_graph_data(scored_candidates[:ENRICH_PER_TIER * 3], state)

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
    2. Filter out nodes we're already connected to and ourselves
    3. Assign network_rank (by channel count) for display
    4. Assign tier_hint by absolute channel count (HUB_MIN / MID_MIN / SMALL_MIN)
    5. Drop nodes below SMALL_MIN_CHANNELS as noise

    Returns a list of candidate dicts sorted by channel count descending.
    The graph is already stored locally by LND — no external API needed.
    """
    log.info("fetching candidates from local LND graph...")
    candidates = []
    # Set a safe default so downstream consumers (e.g. _rerank_tiers_by_diversity)
    # don't KeyError if describe_graph() times out before we build the real set.
    state.setdefault("reachable_2hop", set())

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
                    # Only count channels with a real policy and non-zero fee rate
                    # to avoid 0-fee channels (common on large hubs) dragging avg down
                    policy = edge.get(policy_field)
                    if policy:
                        fee_rate = int(policy.get("fee_rate_milli_msat", 0))
                        if fee_rate > 0:
                            node_map[pk]["fee_ppm_sum"] += fee_rate
                            node_map[pk]["fee_ppm_count"] += 1

        # Build 2-hop reachable set from the same edge list: our direct peers
        # plus every node sharing a public channel with one of our peers. This
        # is the "graph horizon" diversity is measured against — see
        # _enrich_candidates_with_graph_data.
        our_peers_pk = state["existing_peers"]
        reachable_2hop = set(our_peers_pk)
        for edge in edges_raw:
            n1 = edge.get("node1_pub", "")
            n2 = edge.get("node2_pub", "")
            if n1 in our_peers_pk:
                reachable_2hop.add(n2)
            if n2 in our_peers_pk:
                reachable_2hop.add(n1)
        reachable_2hop.discard(state["pubkey"])
        state["reachable_2hop"] = reachable_2hop
        log.info("2-hop reachable graph: %d nodes (from %d direct peers)",
                 len(reachable_2hop), len(our_peers_pk))

        # Filter out nodes with zero channels (likely inactive/phantom nodes)
        active_nodes = [n for n in node_map.values() if n["channel_count"] > 0]

        # Sort by channel count descending to establish network rank
        active_nodes.sort(key=lambda n: n["channel_count"], reverse=True)

        log.info("graph: %d total nodes, %d active (have channels), %d excluded (existing peers)",
                 len(nodes_raw), len(active_nodes), len(state["existing_peers"]))

        # Assign network rank (by channel count, for display) and tier_hint
        # (by absolute channel count, so classification is stable regardless of
        # how many nodes the local graph happens to know about).
        for i, node in enumerate(active_nodes):
            ch = node["channel_count"]
            if ch >= HUB_MIN_CHANNELS:
                tier = "hub"
            elif ch >= MID_MIN_CHANNELS:
                tier = "mid-tier"
            elif ch >= SMALL_MIN_CHANNELS:
                tier = "small"
            else:
                continue  # drop sub-SMALL nodes as noise
            node["network_rank"] = i + 1
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

        log.info("graph candidates: %d hub (>=%d ch), %d mid-tier (%d-%d ch), %d small (%d-%d ch)",
                 sum(1 for c in candidates if c["tier_hint"] == "hub"), HUB_MIN_CHANNELS,
                 sum(1 for c in candidates if c["tier_hint"] == "mid-tier"),
                 MID_MIN_CHANNELS, HUB_MIN_CHANNELS - 1,
                 sum(1 for c in candidates if c["tier_hint"] == "small"),
                 SMALL_MIN_CHANNELS, MID_MIN_CHANNELS - 1)

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
    """Pull live per-candidate graph data from LND.

    For each candidate, fetches:
    - Their peer list (to compute diversity: how many of their peers sit
      outside our 2-hop reachable set, i.e. would actually expand our graph)
    - Their fee policies (average outbound fee rate across their channels)
    - Whether they have a clearnet address

    Sets c["graph_data"] and c["diversity_score_computed"] in place.
    One get_node_info call per candidate — slow, so callers should prefilter
    (see _rerank_tiers_by_diversity).
    """
    reachable = state["reachable_2hop"]
    our_pubkey = state["pubkey"]

    for c in candidates:
        try:
            node_info = lnd_client.get_node_info(c["pubkey"], include_channels=True)
            node = node_info.get("node", {})
            channels = node_info.get("channels", [])

            their_peers = set()
            total_fee_rate = 0
            fee_count = 0

            for ch in channels:
                n1 = ch.get("node1_pub", "")
                n2 = ch.get("node2_pub", "")
                peer_pk = n2 if n1 == c["pubkey"] else n1
                if peer_pk:
                    their_peers.add(peer_pk)

                policy = ch.get("node1_policy") if n1 == c["pubkey"] else ch.get("node2_policy")
                if policy and policy.get("fee_rate_milli_msat"):
                    total_fee_rate += int(policy["fee_rate_milli_msat"]) / 1000
                    fee_count += 1

            if their_peers:
                new_peers = their_peers - reachable - {our_pubkey}
                diversity = len(new_peers) / len(their_peers)
                new_peer_count = len(new_peers)
            else:
                diversity = 0.5
                new_peer_count = 0

            avg_fee_ppm = int(total_fee_rate / fee_count) if fee_count > 0 else 0

            addresses = node.get("addresses", [])
            has_clearnet = any(
                not a.get("addr", "").endswith(".onion") for a in addresses
            )

            c["graph_data"] = {
                "their_peer_count": len(their_peers),
                "new_peers_beyond_2hop": new_peer_count,
                "diversity_score": round(diversity, 3),
                "avg_fee_ppm": avg_fee_ppm,
                "has_clearnet": has_clearnet,
                "addresses": [a.get("addr", "") for a in addresses[:3]],
            }
            c["diversity_score_computed"] = diversity

        except Exception as e:
            log.debug("could not enrich %s with graph data: %s", c.get("alias", "?"), e)
            c["graph_data"] = None
            c["diversity_score_computed"] = None

    return candidates


def _rerank_tiers_by_diversity(candidates, state):
    """Two-stage tier-segmented ranking.

    Stage 1 (cheap): _score_candidates already ranked the full list by centrality.
    Within each tier we take the top ENRICH_PER_TIER as the prefilter.

    Stage 2 (slow): for each prefiltered candidate, fetch live graph data from
    LND to compute diversity (% of their peers that sit outside our 2-hop
    reachable set — i.e. would actually expand our graph horizon, not just
    add another edge into nodes we can already reach). Rerank each tier by
    diversity descending and return the top SHOW_PER_TIER.

    Why tiered: a small node's peers are often obscure leaves outside our
    horizon, so it tends to score high; a hub's peers are mostly other hubs
    we can already reach in 2 hops through any existing peer, so it scores
    low. A single global diversity ranking would just surface backwater nodes.
    Per-tier ranking asks the right question — "the most diversifying hub I
    could add", "the most diversifying mid-tier", "the most diversifying
    small node" — independently.

    Returns (hubs, mid_tier, small), each ≤ SHOW_PER_TIER long, sorted by diversity.
    """
    hubs, mid_tier, small = _split_candidates_by_tier(candidates)

    result = []
    for tier_name, bucket in (("hub", hubs), ("mid-tier", mid_tier), ("small", small)):
        prefilter = bucket[:ENRICH_PER_TIER]
        log.info("enriching %d %s candidates with live graph data...",
                 len(prefilter), tier_name)
        print(f"    {tier_name:<8} tier: enriching {len(prefilter)} candidates "
              f"(get_node_info per peer)...", flush=True)
        _enrich_candidates_with_graph_data(prefilter, state)
        prefilter.sort(
            key=lambda c: (c.get("diversity_score_computed") or 0),
            reverse=True,
        )
        result.append(prefilter[:SHOW_PER_TIER])

    return tuple(result)


def _score_candidates(candidates, state):
    """First-stage scoring: centrality only. Acts as prefilter within each tier.

    Centrality = log-normalised mean of channel count and total capacity, so
    well-connected, high-capacity nodes rank high inside their own bucket.

    Diversity is the final ranking signal but it requires a live LND call per
    candidate, so it's deferred to _rerank_tiers_by_diversity — which uses
    centrality to pick which candidates are worth the round-trip.

    Sets c["centrality"] on each candidate and sorts the list by it descending.
    Fee rate is shown elsewhere but not scored (local graph fee data is unreliable).
    """
    if not candidates:
        return []

    max_channels = max(c.get("channel_count", 0) for c in candidates) or 1
    max_capacity = max(c.get("capacity", 0) for c in candidates) or 1

    for c in candidates:
        ch = c.get("channel_count", 0)
        cap = c.get("capacity", 0)
        ch_score = math.log(1 + ch) / math.log(1 + max_channels) if ch > 0 else 0
        cap_score = math.log(1 + cap) / math.log(1 + max_capacity) if cap > 0 else 0
        centrality = (ch_score + cap_score) / 2

        # Penalise previously unreliable peers from DB history
        peer_hist = db.get_peer_history(c["pubkey"])
        if peer_hist:
            for record in peer_hist:
                if record["action"] == "closed" and "unreliable" in (record["reason"] or ""):
                    centrality *= 0.5
                    c["history_note"] = f"Previously closed: {record['reason']}"

        c["centrality"] = round(centrality, 4)

    candidates.sort(key=lambda c: c["centrality"], reverse=True)
    if candidates:
        log.debug("top centrality: %s (%.2f, %d channels)",
                  candidates[0].get("alias", "?"),
                  candidates[0]["centrality"],
                  candidates[0].get("channel_count", 0))
    return candidates


# ─── Tier-based candidate ranking ───────────────────────────────
# Tiers are defined by absolute channel count (HUB_MIN_CHANNELS,
# MID_MIN_CHANNELS, SMALL_MIN_CHANNELS at the top of this module), so
# classification stays stable regardless of how many nodes the local
# graph happens to know about.


def _classify_existing_portfolio(channels, candidates):
    """Classify existing channels as hub or mid-tier connections.

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

        channel_count = 0
        try:
            node_info = lnd_client.get_node_info(pk, include_channels=False)
            channel_count = int(node_info.get("num_channels", 0))
        except Exception as e:
            log.warning("could not get graph info for existing peer %s: %s",
                        ch.get("peer_alias", pk[:12]), e)

        if channel_count >= HUB_MIN_CHANNELS:
            hub_pubkeys.add(pk)
        else:
            mid_tier_pubkeys.add(pk)

    return {
        "hub_count": len(hub_pubkeys),
        "mid_tier_count": len(mid_tier_pubkeys),
        "hub_pubkeys": hub_pubkeys,
    }


def _split_candidates_by_tier(candidates):
    """Split candidates into hub, mid-tier, and small lists by tier_hint.

    Each list is sorted by centrality descending — that's the prefilter signal
    used to decide which candidates are worth enriching with live graph data.
    """
    hubs, mid_tier, small = [], [], []

    for c in candidates:
        tier = c.get("tier_hint")
        if tier == "hub":
            hubs.append(c)
        elif tier == "mid-tier":
            mid_tier.append(c)
        elif tier == "small":
            small.append(c)
        # candidates without a tier_hint are dropped — _fetch_candidates_from_graph
        # filters sub-SMALL nodes out before we get here.

    for bucket in (hubs, mid_tier, small):
        bucket.sort(key=lambda c: c.get("centrality", 0), reverse=True)

    log.debug("tier split: %d hub, %d mid-tier, %d small",
              len(hubs), len(mid_tier), len(small))
    return hubs, mid_tier, small


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
