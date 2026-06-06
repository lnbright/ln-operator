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
  ├─ Dashboard: rebalance history (auto + manual)
  ├─ Dashboard: per-channel rebal cost, net 30d, net lifetime
  ├─ Rebalance-failing alert
  ├─ Rebalance budget: last_refill_ppm + failure escalation, capped by the
  │   profitability gate (earned_ppm × REBALANCE_PROFIT_HORIZON for judged channels)
  └─ Outbound fee floor: soft ratchet of last_refill_ppm × REBALANCE_FEE_MARGIN
      (decays while idle, re-arms on fresh refill — state in channel_signals)
```

Offset-based sync — no duplicates. Manual rebalances detected by matching
circular self-payments. Channel open time used as floor to prevent
misattribution to new channels with the same peer. Targets resolved from the
route's last-hop chan_id (sibling-safe); auto rebalances are written against
the channel the invoice actually settled on, not the planned target.
