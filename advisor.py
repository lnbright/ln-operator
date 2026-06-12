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

The CLI entry point is cmd_plan() in main.py, which calls these functions
directly. Peer research is done by the user from the top candidates the local
graph surfaces — there is no Claude API agent layer.
"""

import math
import requests
from config import ONEML_API

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
import graph_cache
from logging_config import get_logger

log = get_logger('advisor')


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


def _candidates_from_digest(digest, state):
    """Build the candidate list from the cached graph digest (no live pull).

    Mirrors the live path's tiering/ranking but reads precomputed node metrics.
    Recomputes the 2-hop reachable set from OUR CURRENT peers (via the digest
    adjacency) rather than trusting the cache's snapshot, so it stays correct even
    if our channels changed since the last refresh.
    """
    existing = state["existing_peers"]
    our_pubkey = state["pubkey"]
    nodes = digest["nodes"]

    reachable = set(existing)
    for pk in existing:
        nd = nodes.get(pk)
        if nd:
            reachable.update(nd["neighbors"])
    reachable.discard(our_pubkey)
    state["reachable_2hop"] = reachable

    active = [
        {"pubkey": pk, "alias": nd["alias"], "capacity": nd["capacity"],
         "channel_count": nd["channels"], "source": "graph-cache",
         "avg_fee_ppm": nd["avg_fee_ppm"]}
        for pk, nd in nodes.items()
        if pk not in existing and pk != our_pubkey and nd["channels"] > 0
    ]
    active.sort(key=lambda n: n["channel_count"], reverse=True)

    candidates = []
    for i, node in enumerate(active):
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
        node["avg_channel_size"] = node["capacity"] // ch if ch else 0
        candidates.append(node)

    log.info("graph cache: %d candidates (%d hub, %d mid, %d small) of %d nodes, "
             "2-hop reach %d", len(candidates),
             sum(1 for c in candidates if c["tier_hint"] == "hub"),
             sum(1 for c in candidates if c["tier_hint"] == "mid-tier"),
             sum(1 for c in candidates if c["tier_hint"] == "small"),
             len(nodes), len(reachable))
    return candidates


def _enrich_from_digest(candidates, state, digest):
    """Diversity + avg fee from the cached digest's adjacency — no live get_node_info
    round-trips. has_clearnet/addresses aren't in the topology cache (and aren't
    displayed), so they're omitted."""
    reachable = state["reachable_2hop"]
    our_pubkey = state["pubkey"]
    nodes = digest["nodes"]
    for c in candidates:
        nd = nodes.get(c["pubkey"])
        their_peers = set(nd["neighbors"]) if nd else set()
        if their_peers:
            new_peers = their_peers - reachable - {our_pubkey}
            diversity = len(new_peers) / len(their_peers)
            new_peer_count = len(new_peers)
        else:
            diversity = 0.5
            new_peer_count = 0
        c["graph_data"] = {
            "their_peer_count": len(their_peers),
            "new_peers_beyond_2hop": new_peer_count,
            "diversity_score": round(diversity, 3),
            "avg_fee_ppm": nd["avg_fee_ppm"] if nd else 0,
            "has_clearnet": None,
            "addresses": [],
        }
        c["diversity_score_computed"] = diversity
    return candidates


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
    candidates = []
    # Set a safe default so downstream consumers (e.g. _rerank_tiers_by_diversity)
    # don't KeyError if the graph isn't available.
    state.setdefault("reachable_2hop", set())

    # Prefer the graph cache (built daily by `refresh_graph`) over a fresh multi-MB
    # describe_graph() pull. The digest carries node metrics + adjacency, so both
    # candidate generation here AND the diversity enrichment can read it — no live
    # graph pull, no per-candidate get_node_info round-trips. Live pull stays as a
    # fallback when the cache is missing (e.g. before the first refresh).
    digest = graph_cache.load()
    state["graph_digest"] = digest
    if digest:
        age_h = (graph_cache.age_seconds() or 0) // 3600
        log.info("using cached graph (%dh old) — no live describe_graph pull", age_h)
        return _candidates_from_digest(digest, state)

    log.info("no graph cache — falling back to a live describe_graph() pull "
             "(run `ln-operator refresh_graph` to avoid this)")
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


def _enrich_candidates_with_graph_data(candidates, state):
    """Pull live per-candidate graph data from LND.

    For each candidate, fetches:
    - Their peer list (to compute diversity: how many of their peers sit
      outside our 2-hop reachable set, i.e. would actually expand our graph)
    - Their fee policies (average outbound fee rate across their channels)
    - Whether they have a clearnet address

    Sets c["graph_data"] and c["diversity_score_computed"] in place.
    One get_node_info call per candidate — slow, so callers should prefilter
    (see _rerank_tiers_by_diversity). When the graph cache is available it is
    served from the digest's adjacency instead — no live round-trips at all.
    """
    digest = state.get("graph_digest")
    if digest:
        return _enrich_from_digest(candidates, state, digest)

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
        src = "graph cache" if state.get("graph_digest") else "live get_node_info per peer"
        log.info("enriching %d %s candidates (%s)...", len(prefilter), tier_name, src)
        print(f"    {tier_name:<8} tier: enriching {len(prefilter)} candidates "
              f"({src})...", flush=True)
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
