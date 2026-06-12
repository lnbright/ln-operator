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
2. **Stage 2 — QueryRoutes (live, real liquidity).** For each finalist `Y`, ask LND
   for the cheapest **live** route *from Y to the target* (`source_pubkey=Y`) — i.e.
   the path a refill would take *after* you open `you → Y` (that first hop is your
   own near-free new channel). This drops candidates that look good on the map but
   have no real liquidity, and ranks the survivors by validated route cost.

An **empty result is itself the answer**: no peer has a cheap live route to this
sink → the capital move is resize/close, **not** open. `--no-validate` returns the
stage-1 shortlist without the live probes. Needs a fresh graph cache; if it's
missing, run `refresh_graph` first.

```
⚡ Peers to open toward ACINQ (03864ef025fd…) — cheaper refills into this sink
  WalletOfSatoshi.com   2481ch  1901M  fee~1401  reach+48%  route 0ppm/1h
  River Financial 1     1569ch  1423M  fee~ 853  reach+32%  route 0ppm/1h
  bfx-lnd1               997ch  2936M  fee~ 212  reach+23%  route 0ppm/1h
```

`route 0ppm/1h` = a direct, live 1-hop path to the sink (the ideal — opening to it
gives the shortest refill path). The daily-check agent calls `suggest_peers_for(<sink
peer pubkey>)` to name peers in its capital suggestions instead of hand-waving.

**Why cache + QueryRoutes beats re-running `plan`'s graph walk:** the cache narrows
broad and free; `source_pubkey` lets QueryRoutes simulate a **not-yet-open**
channel's path, which `plan` (which only knows the graph as it is) cannot.
