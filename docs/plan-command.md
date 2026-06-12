# Plan Command

```
Wallet balance         3,528,518 sats
Existing anchor        -30,000 sats  (already locked by LND)
Treasury (2.5%)        -88,212 sats
New anchor             -10,000 sats  (1 × 10,000)
Open fees                 -250 sats  (1 × 1 sat/vB × 250 vB)
────────────────────────────────────
Deployable             3,400,056 sats → 1 channel at 3,400,056 sats
```

Two-stage tier-segmented ranking. Tiers are absolute channel-count
buckets — **hub** (≥100 channels), **mid-tier** (30-99), **small** (10-29);
anything below 10 is dropped as noise. Within each tier:

1. **Centrality** (log-normalised mean of channel count + total capacity)
   prefilters the top 30 candidates — cheap, derived from the local graph.
2. **Diversity** (fraction of the candidate's peers that sit **outside your
   2-hop reachable set** — i.e. would actually expand your graph horizon,
   not just add another edge into nodes you can already reach through an
   existing peer) reranks the prefiltered candidates; top 10 per tier are
   surfaced.

Both stages read the **cached network graph** ([graph-cache.md](graph-cache.md),
refreshed nightly by `refresh_graph`) — the digest carries each node's metrics
*and* its adjacency, so candidate generation and the diversity metric both come
from the cache with **no live `describe_graph` pull and no per-candidate
`get_node_info` round-trips** (the old path did ~90 of them and was the slow part).
The 2-hop set is recomputed from your *current* peers via the cached adjacency, so
it stays correct even if channels changed since the refresh. A live pull is used
only as a fallback when the cache is absent (before the first `refresh_graph`).
For a *targeted* "which peer to open toward this sink" question, see `suggest_peers`
in [graph-cache.md](graph-cache.md).

Why 2-hop, not direct-peer overlap: if "already in your graph" means just
your direct peers, the metric collapses when your channel count is low —
almost everyone scores near 100% diversity because almost no candidate
shares a *direct* edge with you. Measuring against the 2-hop horizon
preserves discrimination at any node size: a hub whose peers are mostly
other hubs you can already reach in 2 hops scores low; a curated small
node with peers genuinely off your map scores high.

Why tiered: a small node's peers are often obscure leaves outside your
horizon (high diversity by default) and a hub's peers tend to be other
hubs you can already reach (low diversity). A single global ranking
would just surface backwater nodes. Per-tier ranking asks the right
question — "the most diversifying hub", "the most diversifying mid-tier",
"the most diversifying small" — independently.

Avg outbound fee is shown for reference but not scored — local graph fee
data is unreliable.
