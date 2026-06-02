# Fee Formula

Per-channel outbound fee is computed in layers, in this order:

```
1. Pin set?                 → use pin (warns if below floor)
2. base    = sigmoid(local_ratio)                # liquidity state
3. mult    = market_multiplier  (slow, demand-derived)
4. floor   = last_refill_ppm × REBALANCE_FEE_MARGIN  # 0 if never refilled
5. target  = clamp(max(base × (1+mult), floor), 0, FEE_HARD_CEILING_PPM)
6. Broadcast only if hysteresis permits          # no gossip spam
```

The sigmoid replaces the old linear curve. It has clean plateaus near 0% and
100% local — small drift inside the healthy middle doesn't snap fees around.
Sample shape with defaults (`SIGMOID_MIN=25`, `SIGMOID_MAX=250`, `K=8`):

| Local | Fee |
|-------|-----|
| 5% | 244 ppm |
| 20% | 231 ppm |
| 50% | 138 ppm |
| 80% | 44 ppm |
| 95% | 31 ppm |

Base fee is always 0. See [Fee Engine Internals](fee-engine-internals.md) for
the full pipeline, hysteresis, signals, and corner cases.

## Manual Fee Pins

The auto-fee formula can be overridden per-channel with `overwrite_fee`. A pinned
channel keeps its fixed ppm across every pipeline run until you clear the pin
with `clear_fee`. Pins are stored in the `fee_overrides` table and are
honored by both the `pipeline` and `adjust_fees` commands. If a pin is set
*below* the rebalance-cost floor, `adjust_fees` logs a warning so you know
you're selling outbound below what refilling costs.

```bash
ln-operator overwrite_fee LNBiG 3000 --note "experimenting with high outbound"
ln-operator status              # 📌 next to the pinned channel + details block
ln-operator clear_fee LNBiG     # auto resumes on next pipeline run
```

The dashboard's *Recent Fee Updates* card tags each row as `auto` or `📌 pin`
in a Source column so you can tell at a glance which changes came from the
formula vs. a manual pin.
