# Graph Cache & Peer-Finder

Two related features that share one cached copy of the Lightning network graph:
the **graph cache** (so we don't re-pull a multi-MB graph from LND repeatedly) and
the **targeted peer-finder** (`suggest_peers`), which uses it to answer *"which node
should I open a channel to so refills into this sink get cheaper?"* with named,
validated candidates.

## The cache (`graph_cache.py` · `ln-operator refresh_graph`)

`describe_graph()` returns the entire announced network — on mainnet that's ~26k
nodes / ~98k channels / ~16 MB, and ~30 s to pull on a Raspberry Pi. So instead of
pulling it live wherever it's needed, a nightly cron (`refresh_graph`, 03:30, ahead
of the daily-check) pulls it **once** and writes a compact processed digest to
`graph_cache.json` (next to the DB; gitignored; path overridable via
`LN_OPERATOR_GRAPH_CACHE`). The write is atomic (temp file + rename), so a reader
never sees a half-written file.

The digest holds, for every node with at least one public channel:

```
{alias, channels, capacity, avg_fee_ppm, neighbors[]}
```

plus your **2-hop reachable set** (your peers + everyone sharing a channel with a
peer) and network-level stats. `neighbors` is the adjacency the peer-finder walks.
`graph_cache.load()` reads it instantly; `build_digest()` is a pure function (graph
dict → digest) unit-tested without LND.

A small historical row also lands in the `graph_snapshots` table on each refresh
(total nodes / channels / capacity + your channels / capacity / peers), so your
**network position over time** — are you growing or going dark? — is trendable.

**Liquidity-blind by design.** This is announced topology + fee policy only. It is
*never* used for costed pathfinding or "will it route" decisions — that's
QueryRoutes' job (it sees real, mission-control liquidity the gossip graph cannot).
See the rebalance QueryRoutes acceleration in [rebalance-budget.md](rebalance-budget.md).

**Full re-pull each night, no incremental diff.** `describe_graph` is read-only and
runs once off-peak — the same call `lncli describegraph` makes; not a hot path, no
meaningful load on LND. A true incremental graph would use `SubscribeChannelGraph`'s
gossip stream, but that's an unjustified long-running daemon for a slowly-changing,
liquidity-blind cache that only needs daily freshness.

Consumers: the `plan` command (both its candidate generation **and** the diversity
metric — see [plan-command.md](plan-command.md)), `suggest_peers` below, and the
daily-check agent's capital suggestions.

## The targeted peer-finder (`peer_finder.py` · `ln-operator suggest_peers`)

```
ln-operator suggest_peers <alias|pubkey> [--no-validate]
```

Turns "add a 2nd source" into named, validated candidates. Two stages:

1. **Stage 1 — graph cache (free, liquidity-blind).** Take the target sink's
   neighbours (a channel to one gives a short `you → Y → target` refill path), drop
   yourself / existing peers / sub-10-channel noise, and score by hub quality
   (channels + capacity, low fee). Reports `diversity` = the fraction of a
   candidate's neighbours *outside* your 2-hop horizon (does opening to it expand
   reach or just duplicate paths you have?).
2. **Stage 2 — QueryRoutes (live, real liquidity).** For each finalist `Y`, price the
   **real refill shape**: the route `Y → … → target → you`
   (`query_routes(dest=you, source_pubkey=Y, last_hop_pubkey=target)`) — the path a
   refill takes *after* you open `you → Y` (that first hop is your own near-free new
   channel, excluded since `Y` is the source). The target is an **intermediate
   forwarder that charges its fee**, not a free destination. This drops candidates with
   no real liquidity and ranks survivors by **true end-to-end refill cost**.

   The probe omits one thing — LND never charges the **source** (`Y`) for its own first
   hop — so `Y`'s outbound fee toward the next hop is added back from the channel edge
   (`get_channel_edge`), shown as `(peer-hop N)`. With that, **every hop on the path is
   counted** for any path length.

   > **Why this matters.** The earlier probe pointed at the sink as the *destination*
   > (`query_routes(dest=target, source=Y)`). The final hop into a destination is free,
   > so every direct neighbour read a meaningless `route 0ppm/1h` and the ranking fell
   > to the reach tiebreak — which put the **most expensive** candidates on top. On a
   > real bfx sink, WalletOfSatoshi ranked #1 at "0ppm" while its true refill cost was
   > **~5002 ppm** (its own 5000 ppm fee toward bfx was hidden). The honest probe flips
   > the order: cheap hubs like Binance (2 ppm) / bfx-lnd1 (5 ppm) surface first.

An **empty result is itself the answer**: no peer has a cheap live route to this
sink → the capital move is resize/close, **not** open. `--no-validate` returns the
stage-1 shortlist without the live probes. Needs a fresh graph cache; if it's
missing, run `refresh_graph` first.

```
⚡ Peers to open toward bfx-lnd0 (033d86562194…) — cheaper refills into this sink
  Binance               341ch 11293M  fee~   0  reach+16%  route    2ppm/2h
  bfx-lnd1              997ch  2936M  fee~ 212  reach+23%  route    5ppm/2h (peer-hop 3)
  okx                  702ch  7280M  fee~ 713  reach+16%  route  502ppm/2h (peer-hop 500)
  WalletOfSatoshi.com 2481ch  1901M  fee~1401  reach+48%  route 5002ppm/2h (peer-hop 5000)
```

`route Nppm/Hh (peer-hop M)` = true refill cost: `M` ppm is the candidate's own
forwarding fee toward the sink (the first hop you'd pay after opening to it), the rest
is the path from there back to you. Lowest total wins. The daily-check agent calls
`suggest_peers_for(<sink peer pubkey>)` to name peers in its capital suggestions
instead of hand-waving.

**Why cache + QueryRoutes beats re-running `plan`'s graph walk:** the cache narrows
broad and free; `source_pubkey` lets QueryRoutes simulate a **not-yet-open**
channel's path, which `plan` (which only knows the graph as it is) cannot.
