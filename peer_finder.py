"""
LN Operator — Targeted peer-finder.

Answers the question the daily report kept failing at: "for a sink we keep needing
to refill (a stranded / draining channel's peer), WHICH node should we open a
channel to so refills toward it get cheaper?" — with named, evidence-backed
candidates instead of "add a 2nd source".

Two stages (see CLAUDE.md design notes):

  Stage 1 — graph cache (free, broad, LIQUIDITY-BLIND). From the cached graph digest, take the
  target's neighbours (a channel to one gives us a short us→Y→target refill path),
  drop self / existing peers / noise, and score by hub quality. This only generates
  candidates — the cache can't see whether those paths are actually liquid.

  Stage 2 — QueryRoutes (live, real liquidity). For each finalist Y, ask LND to price
  the REAL refill shape: a route from Y back to US arriving over our channel to the
  target (source_pubkey=Y, dest=us, last_hop=target). That models the path our refill
  takes AFTER we open us→Y (the first hop is our own near-free new channel, excluded
  here since Y is the source) — with the target as an INTERMEDIATE forwarder that
  charges its fee, not a free terminal hop. Probing dest=target instead read 0ppm for
  every direct neighbour (the destination hop is always free), validating liquidity but
  hiding price. The probe's fee_ppm covers every hop AFTER Y (LND never charges the
  source for its own outbound), so we add Y's own first-hop fee back via one
  get_channel_edge lookup — giving the TRUE end-to-end refill cost for any path length.
  This turns a topology guess into mission-control-backed evidence, and an EMPTY result
  is itself the answer: no viable peer → the capital call is resize/close, not open.
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


def _policy_ppm(policy, amount_sats):
    """Convert a channel-edge routing policy (base_msat + rate_ppm) to an effective ppm
    at `amount_sats` — base fee amortised over the amount, so it's comparable to the
    probe's amount-derived fee_ppm."""
    base_msat = int(policy.get("fee_base_msat", 0) or 0)
    rate_ppm = int(policy.get("fee_rate_milli_msat", 0) or 0)
    fee_msat = base_msat + amount_sats * rate_ppm / 1000.0
    return fee_msat * 1000.0 / amount_sats if amount_sats else 0.0


def _first_hop_ppm(candidate_pubkey, route, amount_sats):
    """The fee `candidate_pubkey` charges on the route's FIRST hop — the ONE cost the
    probe omits. With source_pubkey=Y, LND doesn't charge Y for its own outbound, so
    probe.fee_ppm covers every hop AFTER Y (Z→…→target→us) but not Y's own first hop.
    After we open us→Y, Y becomes an intermediate forwarder and DOES pay this, so add it
    back. One get_channel_edge(hops[0]) lookup, works for any path length. A node's OWN
    advertised policy (node1_policy if it is node1, else node2_policy) is its outbound
    forwarding fee, so direction is unambiguous. Returns 0.0 on any failure / disabled
    edge (degrade to probe-only cost rather than break ranking)."""
    hops = (route or {}).get("hops") or []
    if not hops:
        return 0.0
    chan_id = hops[0].get("chan_id")
    if not chan_id:
        return 0.0
    try:
        edge = lnd_client.get_channel_edge(chan_id)
    except Exception as e:
        log.debug("peer_finder: edge lookup %s failed: %s", chan_id, e)
        return 0.0
    if not edge:
        return 0.0
    if edge.get("node1_pub") == candidate_pubkey:
        pol = edge.get("node1_policy")
    elif edge.get("node2_pub") == candidate_pubkey:
        pol = edge.get("node2_policy")
    else:
        return 0.0
    if not pol or pol.get("disabled"):
        return 0.0
    return _policy_ppm(pol, amount_sats)


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

    our_pubkey = digest["our_pubkey"]
    validated = []
    for c in cands:
        try:
            # Price the REAL refill shape, not a route that terminates at the sink.
            # A rebalance ends back at US, arriving over our channel to the target, so
            # the target is an INTERMEDIATE forwarder (charges its fee) — not the
            # destination (free). dest=us + last_hop=target prices Y → … → target → us:
            # the path a refill takes after we open us→Y (that first hop is our own
            # near-free new channel, excluded since Y is the source). Probing dest=target
            # read 0ppm for every direct neighbour (terminal hop is free) — liquidity
            # validation only, no price. (Y's own first-hop fee, which the probe also
            # omits as the source, is added back below.)
            probe = lnd_client.query_routes(
                our_pubkey, amount_sats,
                source_pubkey=c["pubkey"], last_hop_pubkey=target_pubkey)
        except Exception as e:
            log.debug("peer_finder probe failed for %s: %s", c["alias"], e)
            probe = None
        if not probe:
            continue  # no live route Y→…→target→us → not actually useful
        # probe.fee_ppm = every hop after Y; add Y's own first-hop fee for the full cost
        first_ppm = _first_hop_ppm(c["pubkey"], probe["routes"][0], amount_sats)
        c["first_hop_ppm"] = round(first_ppm)
        c["route_ppm"] = round(probe["fee_ppm"] + first_ppm)
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
