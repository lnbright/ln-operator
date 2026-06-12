# Data Flow

```
LND /v1/switch → sync_routing → forwarding_log (SQLite)
  ├─ Dashboard: routing events, daily revenue, per-channel revenue/net
  ├─ CLI: history
  ├─ market_multiplier nudges (nightly recompute_signals)
  └─ earned_ppm (Σ fee_earned / Σ amount_out, by chan_out) → profitability gate

LND SubscribeHtlcEvents → htlc_monitor → forward_fail_log (SQLite)
  ├─ Dashboard: dropped-forward glance (lost revenue)
  ├─ Fast-drain market-mult bump (INSUFFICIENT_BALANCE in the 2h loop)
  ├─ Floor-decay gate: recent drops = demand at the current price → floor holds
  └─ Daily check: capital suggestions for structural channels

LND /v1/payments → sync_rebalances → rebalance_log (SQLite)
  ├─ Dashboard: rebalance history (auto + manual; QR_NO_AFFORDABLE_ROUTE = skipped)
  ├─ Dashboard: per-channel rebal cost, net 30d, net lifetime
  ├─ Rebalance-failing alert
  ├─ Rebalance budget: last_refill_ppm + failure escalation, capped by the
  │   profitability gate (earned_ppm × REBALANCE_PROFIT_HORIZON for judged channels)
  └─ Outbound fee floor: soft ratchet of last_refill_ppm × REBALANCE_FEE_MARGIN
      (decays while idle, re-arms on fresh refill — state in channel_signals)

LND QueryRoutes (dry-run, no payment — same pathfinder as SendPaymentV2 + MC)
  ├─ Planner (pricing): read the live route price → set the bid to the cheapest
  │   feasible source (bounded by the affordable ceiling) so a refill lands this run
  ├─ Planner (early-out): no route via ANY source → skip the attempt + record a
  │   synthetic QR_NO_AFFORDABLE_ROUTE cycle (advances the structural ladder)
  └─ suggest_peers stage 2: validate a candidate's live route to a sink (source_pubkey)

LND describe_graph → refresh_graph (nightly) → graph_cache.json + graph_snapshots
  ├─ plan: candidate generation + 2-hop diversity (no live pull / get_node_info)
  ├─ suggest_peers stage 1: the sink's neighbours, scored by hub quality
  └─ graph_snapshots: our network-position trend over time

daily-check agent → reconcile.run_checks (DB arithmetic) + daily_findings (dedup)
  ├─ reconcile: deterministic data-integrity issues the agent reports (not recomputes)
  └─ daily_findings: report a finding once; re-surface only on a material change
```

Offset-based sync — no duplicates. Manual rebalances are detected two ways and
both land as `triggered_by='manual'`: (1) circular self-payments made outside
the tool (e.g. via `lncli`), matched here by `sync_rebalances`; and (2) the
`manual_rebalance` command, which writes its row directly through the executor
(deduped by payment_hash, so sync won't re-import it). Channel open time used as
a floor to prevent misattribution to new channels with the same peer. Targets
resolved from the route's last-hop chan_id (sibling-safe); auto rebalances are
written against the channel the invoice actually settled on, not the planned
target.
