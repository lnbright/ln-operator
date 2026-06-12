# Improvement backlog

Deferred enhancements — not yet built. Each is independent; pick by value.
(Completed items from the same design pass: B1 graph cache, B2 peer-finder, B3
reconciliation, B6 finding-dedup, B8 v1/v2 QueryRoutes acceleration + early-out.)

## B4 — Fee elasticity (measured, not guessed)
The §4 capital menu's "raise the outbound fee" option hinges on *"is this channel's
demand fee-tolerant?"* — which the daily-check agent currently asserts. We already
store `fee_updates` and `forwarding_log`, so a helper could **correlate past fee
changes against forwarded volume per channel** and produce an elasticity estimate.
That turns "demand looks fee-tolerant" into "fee went 200→400 last month, volume
held — room to push." Feeds the capital reasoning and the fee engine.

## B5 — Trend series (slope, not just level)
The daily check is point-in-time + a baseline. A rolling 30-day per-channel series
(earned_ppm, out-volume, in/out split) would let the agent catch **slow drifts** —
a channel's earnings declining over weeks, a peer gradually going dark, volume
bleeding away — before they become incidents. Catching the slope is worth more than
reporting today's level. (The `graph_snapshots` table already trends our *network*
position; B5 is the per-channel economic equivalent.)

## B7 — Peer-reliability score (cross-day memory)
The agent already reads inactive-channel timelines each run but starts cold every
time. Accumulating **flap/disconnect events per peer** (from the log scans /
htlc_monitor) into a table would let it flag *deteriorating* peers proactively —
"peer X has flapped 4× in 3 days, trending worse" — instead of rediscovering each
outage in isolation.

## Also considered / parked
- **Amboss (or other probed-liquidity API).** Only worth it once named-peer
  suggestions (B2) prove valuable AND we want the reliability/liquidity layer the
  local graph lacks. The local graph + QueryRoutes already cover B1/B2/B8; a paid
  graph service buys probed liquidity (which would also help rebalance route-finding
  beyond LND's local mission control), not topology. Don't pay for it to make
  suggestions we still can't act on automatically.
- **`refresh_graph` freshness guard.** Skip the pull if the cache is < N hours old,
  so an accidental double-run doesn't re-pull. The daily cron makes this a non-issue
  today; belt-and-suspenders only.
- **True incremental graph** via `SubscribeChannelGraph` instead of the daily full
  pull — unjustified daemon complexity for a slowly-changing, liquidity-blind cache.
