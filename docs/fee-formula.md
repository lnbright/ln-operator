# Fee Formula

Per-channel outbound fee is computed in layers, in this order:

```
1. Pin set?                 → use pin (warns if below floor)
2. base    = sigmoid(local_ratio)                # liquidity state
3. mult    = market_multiplier  (slow, demand-derived; +fast-drain bump)
4. floor   = soft ratchet of last_refill_ppm × REBALANCE_FEE_MARGIN  # 0 if never refilled
5. target  = clamp(max(base × (1+mult), floor), 0, FEE_HARD_CEILING_PPM)
6. Broadcast only if hysteresis permits          # no gossip spam
```

The sigmoid replaces the old linear curve. It has clean plateaus near 0% and
100% local — small drift inside the healthy middle doesn't snap fees around.
Sample shape with defaults (`SIGMOID_MIN=25`, `SIGMOID_MAX=750`, `K=8`):

| Local | Fee |
|-------|-----|
| 5% | 731 ppm |
| 20% | 690 ppm |
| 50% | 388 ppm |
| 80% | 85 ppm |
| 95% | 44 ppm |

The market multiplier modulates this base (`× (1+mult)`, mult ∈ [−0.5, 1.0]), so
the demand-amplified outbound max is `750 × 2 = 1500` (still under the 5000 hard
ceiling). A depleted channel that drops forwards for lack of liquidity also gets
an up-only **fast-drain bump** in the 2h loop so its resting fee climbs after the
first bad cycle instead of waiting for the nightly drift.

**The floor is a soft ratchet, not a hard minimum.** `last_refill_ppm ×
REBALANCE_FEE_MARGIN` recoups refill cost, but if a channel sits idle at a floor
that's pricing it out, the *effective* floor decays toward the market-clearing
fee so it can find a price that sells. It ratchets DOWN while idle, HOLDS while
forwarding (a forward does not snap it back up), and is re-armed to the full
floor only by a fresh refill. See [Fee Engine Internals](fee-engine-internals.md).

Base fee is always 0. See [Fee Engine Internals](fee-engine-internals.md) for
the full pipeline, hysteresis, signals, profitability gate, and corner cases.

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
