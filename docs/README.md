# LN Operator — Documentation

Deep-dive reference for the bits that don't belong in the top-level README.
Start at the [project README](../README.md) for install, usage, and the
dashboard tour.

## Fees
- [Fee Formula](fee-formula.md) — the layered outbound-fee calculation + manual pins
- [Fee Engine Internals](fee-engine-internals.md) — cadence, the layers, hysteresis, soft-floor ratchet, profitability gate, inbound-fee ladder, corner cases
- [How an Idle Channel Finds a Sellable Price](idle-channel-pricing.md) — walkthrough of floor decay, the market multiplier, the clearing target, freeze-on-forward, and the broadcast gate

## Liquidity
- [Rebalance Budget](rebalance-budget.md) — budget, failure escalation, the profitability gate, the QueryRoutes acceleration + early-out, chunking, fallback pairs
- [Plan Command](plan-command.md) — tier-segmented peer ranking (centrality → diversity)
- [Graph Cache & Peer-Finder](graph-cache.md) — the cached network graph (`refresh_graph`) and targeted `suggest_peers`

## Interface
- [Dashboard](dashboard.md) — card-by-card tour ([live demo](https://www.lnbright.com/demo/)), Sat Flow drill-downs, and the watchtower health-badge logic

## Operations
- [Daily Check](daily-check.md) — the optional, off-by-default AI health-check agent

## Reference
- [Configuration](configuration.md) — every `config.py` knob and its default
- [Data Flow](data-flow.md) — how LND data lands in SQLite and feeds each consumer
- [Improvements](improvements.md) — deferred enhancement backlog
- [Alerts](alerts.md) — alert types and triggers
- [Logging](logging.md) — log location and rotation
- [Channel Backup](channel-backup.md) — off-site `channel.backup` push
- [Known Limitations](known-limitations.md)
