# Data Flow

```
LND /v1/switch → sync_routing → forwarding_log (SQLite)
  ├─ Dashboard: routing events, daily revenue, per-channel revenue/net
  ├─ CLI: history
  └─ market_multiplier nudges (nightly recompute_signals)

LND /v1/payments → sync_rebalances → rebalance_log (SQLite)
  ├─ Dashboard: rebalance history (auto + manual)
  ├─ Dashboard: per-channel rebal cost, net 30d, net lifetime
  ├─ Rebalance-failing alert
  ├─ Rebalance budget: last_refill_ppm + failure-escalation counter
  └─ Outbound fee floor: last_refill_ppm × REBALANCE_FEE_MARGIN
```

Offset-based sync — no duplicates. Manual rebalances detected by matching
circular self-payments. Channel open time used as floor to prevent
misattribution to new channels with the same peer.
