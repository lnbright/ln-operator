"""
LN Operator — Targeted peer-finder (B2).

Answers the question the daily report kept failing at: "for a sink we keep needing
to refill (a stranded / draining channel's peer), WHICH node should we open a
channel to so refills toward it get cheaper?" — with named, evidence-backed
candidates instead of "add a 2nd source".

Two stages (see CLAUDE.md B1/B2 design notes):

  Stage 1 — graph cache (free, broad, LIQUIDITY-BLIND). From the B1 digest, take the
  target's neighbours (a channel to one gives us a short us→Y→target refill path),
  drop self / existing peers / noise, and score by hub quality. This only generates
  candidates — the cache can't see whether those paths are actually liquid.

  Stage 2 — QueryRoutes (live, real liquidity). For each finalist Y, ask LND for the
  cheapest route FROM Y TO the target (source_pubkey=Y) — i.e. simulate the path our
  refill would take AFTER we open us→Y (that first hop is our own near-free new
  channel). This turns a topology guess into mission-control-backed evidence, and an
  EMPTY result is itself the answer: no viable peer → the capital call is resize/close,
  not open.
"""

import math

import lnd_client
import graph_cache
from logging_config import get_logger

log = get_logger("peer_finder")

MIN_CANDIDATE_CHANNELS = 10        # drop noise nodes from stage 1
DEFAULT_PROBE_SATS = 500_000       # representative refill chunk for the route probe


def _diversity(neighbors, reachable_2hop):
    """Fraction of this node's neighbours OUTSIDE our 2-hop horizon — i.e. how much
    opening to it would genuinely expand our reach rather than duplicate paths we
    already have. Reported as evidence; a minor ranking tiebreak."""
    if not neighbors:
        return 0.0
    new = sum(1 for n in neighbors if n not in reachable_2hop)
    return new / len(neighbors)


def _stage1_candidates(digest, target_pubkey, limit):
    """Graph-cache prefilter (pure): the target's neighbours, minus self / existing
    peers / the target, scored by hub quality (channels + capacity, low fee) so
    stage 2 only spends live probes on the best few. Returns up to `limit` dicts."""
    nodes = digest["nodes"]
    our_pubkey = digest["our_pubkey"]
    our_peers = set(digest["our_peers"])
    reach = set(digest["reachable_2hop"])

    target = nodes.get(target_pubkey)
    if not target:
        return []

    cands = []
    for pk in target["neighbors"]:
        if pk == our_pubkey or pk == target_pubkey or pk in our_peers:
            continue
        nd = nodes.get(pk)
        if not nd or nd["channels"] < MIN_CANDIDATE_CHANNELS:
            continue
        score = (math.log1p(nd["channels"])
                 + math.log1p(nd["capacity"] / 1_000_000)
                 - 0.001 * nd["avg_fee_ppm"])
        cands.append({
            "pubkey": pk,
            "alias": nd["alias"],
            "channels": nd["channels"],
            "capacity": nd["capacity"],
            "avg_fee_ppm": nd["avg_fee_ppm"],
            "diversity": round(_diversity(nd["neighbors"], reach), 2),
            "_score": score,
        })
    cands.sort(key=lambda c: c["_score"], reverse=True)
    return cands[:limit]


def suggest_peers_for(target_pubkey, digest=None, amount_sats=DEFAULT_PROBE_SATS,
                      max_candidates=12, validate=True):
    """Rank peers to open a channel to so refills toward `target_pubkey` get cheaper.

    Returns a list ranked by validated route cost (cheapest first). An EMPTY list
    means no viable peer was found — the capital answer is resize/close, not open.
    `validate=False` returns the stage-1 shortlist only (no LND calls).
    """
    if digest is None:
        digest = graph_cache.load()
    if not digest:
        log.warning("peer_finder: no graph cache — run `ln-operator refresh_graph`")
        return []

    cands = _stage1_candidates(digest, target_pubkey, max_candidates)
    if not validate:
        for c in cands:
            c.pop("_score", None)
        return cands

    validated = []
    for c in cands:
        try:
            probe = lnd_client.query_routes(
                target_pubkey, amount_sats, source_pubkey=c["pubkey"])
        except Exception as e:
            log.debug("peer_finder probe failed for %s: %s", c["alias"], e)
            probe = None
        if not probe:
            continue  # topologically close but no live route → not actually useful
        c["route_ppm"] = probe["fee_ppm"]
        c["route_hops"] = probe["hops"]
        c.pop("_score", None)
        validated.append(c)

    # cheapest live route first; break ties toward the reach-expanding, bigger node
    validated.sort(key=lambda c: (c["route_ppm"], -c["diversity"], -c["channels"]))
    return validated


def resolve_target(query, digest):
    """Resolve a 66-hex pubkey or an alias substring to a node pubkey via the cache.
    For an alias match, prefer the biggest node (most channels) — most likely the
    intended one. Returns None if nothing matches."""
    q = (query or "").strip()
    if len(q) == 66 and all(c in "0123456789abcdef" for c in q.lower()):
        return q.lower()
    ql = q.lower()
    matches = [(pk, nd) for pk, nd in digest["nodes"].items()
               if ql and ql in (nd["alias"] or "").lower()]
    if not matches:
        return None
    matches.sort(key=lambda m: m[1]["channels"], reverse=True)
    return matches[0][0]
