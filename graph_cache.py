"""
LN Operator — Network graph cache (B1).

`describe_graph()` is a multi-MB pull (up to ~300s on the Pi), so we pull it once
(from a cron — `ln-operator refresh_graph`) and cache a compact, processed digest
to disk. The daily-check agent and the B2 peer-finder read the digest with `load()`
instead of re-pulling LND — for structural / counterfactual reasoning only:
reachability, who-connects-to-whom, candidate metrics, "if I opened to Y, what
does Y reach".

LIQUIDITY-BLIND by design. This is announced topology + fee policy, NEVER used for
costed pathfinding or "will it route" decisions — that is QueryRoutes' job, which
sees real (mission-control) liquidity the gossip graph cannot. See CLAUDE.md
(B1/B2 design notes).

The digest:
    {
      "ts": int, "our_pubkey": str,
      "our_peers": [pubkey, ...],
      "reachable_2hop": [pubkey, ...],     # peers + everyone sharing a channel with a peer
      "stats": {"total_nodes", "total_channels", "total_capacity"},
      "nodes": { pubkey: {alias, channels, capacity, avg_fee_ppm, neighbors[]} }
    }
"""

import json
import os
import time

import lnd_client
import db
from config import DB_PATH
from logging_config import get_logger

log = get_logger("graph_cache")

# Cache sits next to the DB so it travels with the deployment / LN_OPERATOR_DB.
CACHE_PATH = os.getenv(
    "LN_OPERATOR_GRAPH_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "graph_cache.json"),
)


def build_digest(graph, our_pubkey, our_peers, now=None):
    """Pure: a full `describe_graph()` dict → the compact cached digest.

    No LND calls — testable in isolation. Every node with at least one public
    channel gets {alias, channels, capacity, avg_fee_ppm, neighbors}; channel-less
    nodes (stale gossip / phantoms) are dropped. `neighbors` is the adjacency B2
    walks for reachability. Pubkeys, not interned indices — simple over compact;
    it's a once-a-day file.
    """
    now = now or int(time.time())
    our_peers = set(p for p in our_peers if p)
    nodes_raw = graph.get("nodes", []) or []
    edges_raw = graph.get("edges", []) or []

    nodes = {}
    for n in nodes_raw:
        pk = n.get("pub_key", "")
        if pk:
            nodes[pk] = {"alias": n.get("alias", "") or pk[:12], "channels": 0,
                         "capacity": 0, "_fee_sum": 0, "_fee_count": 0,
                         "neighbors": set()}

    total_capacity = 0
    for e in edges_raw:
        cap = int(e.get("capacity", 0) or 0)
        total_capacity += cap
        n1, n2 = e.get("node1_pub", ""), e.get("node2_pub", "")
        for pk, other, pol_field in ((n1, n2, "node1_policy"), (n2, n1, "node2_policy")):
            nd = nodes.get(pk)
            if nd is None:
                continue
            nd["channels"] += 1
            nd["capacity"] += cap
            if other:
                nd["neighbors"].add(other)
            policy = e.get(pol_field)
            if policy:
                fr = int(policy.get("fee_rate_milli_msat", 0) or 0)
                if fr > 0:
                    nd["_fee_sum"] += fr
                    nd["_fee_count"] += 1

    digest_nodes = {}
    for pk, nd in nodes.items():
        if nd["channels"] == 0:
            continue  # no public channels → stale gossip / phantom node
        digest_nodes[pk] = {
            "alias": nd["alias"],
            "channels": nd["channels"],
            "capacity": nd["capacity"],
            "avg_fee_ppm": (nd["_fee_sum"] // nd["_fee_count"]) if nd["_fee_count"] else 0,
            "neighbors": sorted(nd["neighbors"]),
        }

    # 2-hop reachable: our peers + every node sharing a channel with a peer.
    reachable = set(our_peers)
    for pk in our_peers:
        nd = digest_nodes.get(pk)
        if nd:
            reachable.update(nd["neighbors"])
    reachable.discard(our_pubkey)

    return {
        "ts": now,
        "our_pubkey": our_pubkey,
        "our_peers": sorted(our_peers),
        "reachable_2hop": sorted(reachable),
        "stats": {
            "total_nodes": len(digest_nodes),
            "total_channels": len(edges_raw),
            "total_capacity": total_capacity,
        },
        "nodes": digest_nodes,
    }


def _write(digest):
    """Atomic write so a concurrent load() never sees a half-written file."""
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(digest, f)
    os.replace(tmp, CACHE_PATH)


def refresh():
    """Pull the graph once, write the cached digest, and record a stats row.

    The historical `graph_snapshots` row lets us trend our network position over
    time (are we growing / going dark?). Returns the digest.
    """
    info = lnd_client.get_info()
    our_pubkey = info.get("identity_pubkey", "")
    channels = lnd_client.get_channels()
    our_peers = [c["peer_pubkey"] for c in channels]

    log.info("graph cache: pulling describe_graph() (multi-MB, may take a while)…")
    graph = lnd_client.describe_graph()
    digest = build_digest(graph, our_pubkey, our_peers)
    _write(digest)

    db.save_graph_snapshot(
        total_nodes=digest["stats"]["total_nodes"],
        total_channels=digest["stats"]["total_channels"],
        total_capacity=digest["stats"]["total_capacity"],
        our_channels=len(channels),
        our_capacity=sum(c["capacity"] for c in channels),
        our_peers=len(set(p for p in our_peers if p)),
    )
    log.info("graph cache: %d nodes, %d channels, 2-hop reach %d nodes → %s",
             digest["stats"]["total_nodes"], digest["stats"]["total_channels"],
             len(digest["reachable_2hop"]), CACHE_PATH)
    return digest


def load(max_age_hours=None):
    """Load the cached digest, or None if missing / unreadable / stale.

    Callers that need freshness pass max_age_hours; the daily agent should warn
    (and consider a refresh) when load() returns None.
    """
    try:
        with open(CACHE_PATH) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.debug("graph cache load failed: %s", e)
        return None
    if max_age_hours is not None:
        age = int(time.time()) - int(d.get("ts", 0))
        if age > max_age_hours * 3600:
            log.debug("graph cache stale: %dh old (max %dh)", age // 3600, max_age_hours)
            return None
    return d


def age_seconds():
    """Seconds since the cache was last refreshed, or None if absent."""
    d = load()
    return None if not d else int(time.time()) - int(d.get("ts", 0))
