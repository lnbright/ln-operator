# Fee Engine Internals

The 2h pipeline reads cached signals; a nightly job recomputes them. This
split keeps the fast loop cheap and the slow signals stable.

## Cadence

```
Every 2h (cron):
  1. adjust_fees       ← reads channel_signals, decides target,
                          gated broadcast (hysteresis)
  2. rebalance_channels
  3. sync_routing
  4. healthcheck

Nightly (cron, separate line):
  recompute_signals    ← refreshes per-channel market_multiplier and logs
                          last_refill_ppm / failure counts for visibility
```

Suggested cron line for the nightly job:

```
15 3 * * * cd /path/to/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
```

## The four layers

1. **Sigmoid base** — `sigmoid_fee_ppm(local_ratio)`. Liquidity-driven base fee
   with clean plateaus near 0% and 100% local. No clamps needed at the edges —
   the curve naturally asymptotes to `SIGMOID_MIN/MAX_PPM`.

2. **Market multiplier** — slow per-channel scalar in `channel_signals`. Each
   nightly run nudges `+MARKET_MULT_STEP` if the channel forwarded in the last
   24h, `-MARKET_MULT_STEP` if silent ≥ `MARKET_MULT_SILENT_DAYS`. Bounded by
   `[MARKET_MULT_MIN, MARKET_MULT_MAX]`. Modulates the sigmoid: `adjusted =
   base × (1 + mult)`.

3. **Rebalance-cost floor** — "don't sell outbound below what refilling costs."
   Floor = `last_refill_ppm × REBALANCE_FEE_MARGIN`, read live from
   `rebalance_log`. Activates on the first successful refill — no warmup, no
   median smoothing. No refill history → no floor (sigmoid alone decides).

4. **Hard ceiling** — `FEE_HARD_CEILING_PPM` (5000). Last line of defense
   against runaway data. Matched to `REBALANCE_MAX_BUDGET_PPM` so a channel
   can always charge enough outbound to recoup what we'd pay to refill it.

## Hysteresis (`_should_broadcast`)

A computed target only becomes a broadcast `channel_update` if:
- Δ from current fee ≥ both `TOLERANCE_PPM` AND `TOLERANCE_PCT` of current, AND
- one of:
  - cooldown expired (`COOLDOWN_SEC` since last broadcast), OR
  - Δ ≥ `SNAP_PPM` (urgent — skip cooldown), OR
  - channel crossed an edge zone (`EDGE_LOW`/`EDGE_HIGH`) since last update.

This is what actually prevents gossip spam. The sigmoid shape is for *what*
fee, not *whether to broadcast*.

## Rebalance budget & failure escalation

`get_channel_rebalance_budget` reads `last_refill_ppm` and
`failures_since_last_success` live from `rebalance_log` and returns:

```
budget = (last_refill OR DEFAULT_BUDGET) × (1 + STEP × failures)
         capped at REBALANCE_MAX_BUDGET_PPM
```

This single formula handles bootstrap, drift, and re-bootstrap after a long
idle period. There are no tiers, no maturity windows, no separate adaptive
cap or revenue-ratio gate — the budget tracks the actual paid price, and
failures walk it back up if the market has moved.

## Corner cases & how each is handled

| Case | Behavior |
|---|---|
| Brand-new channel, no refill yet | Budget = `DEFAULT_BUDGET` (500), no fee floor (sigmoid alone). Failures escalate budget at 20% per cron cycle |
| Manual urgency refill at high cost | Stored as success row with actual ppm → next budget = that ppm, next fee floor = ppm × REBALANCE_FEE_MARGIN. No filtering of manual rows |
| Single chunk succeeded at small amount | Logged as success at chunk ppm. May be inflated vs full-amount price — accepted as the cost of having any signal at all |
| Market drifted upward, refills fail | Failure counter ticks, budget escalates 20%/cycle until new price is discovered |
| Channel idle 30+ days, then drains | `last_refill_ppm` still anchors — budget starts at last known price + escalation if it has drifted |
| Pin below floor | Pin wins (explicit intent), warning logged |
| Channel at <20% local, market says "lower" | **Blocked** — in defense zone, multiplier can only raise |
| Channel at >80% local, market says "raise" | **Allowed** — earn more on outflow |
| Just paid expensive refill → big fee jump | `SNAP_PPM` escapes cooldown so the floor jump goes live in the next cron cycle |
| Crossing 20%/80% boundary | Edge-zone crossing escapes cooldown |
| Tiny fee drift (1-2 ppm) | Caught by tolerance — no broadcast |
| Channel offline | Skipped — no policy update |

## When data is missing

Channels with no refill history start at `DEFAULT_BUDGET` (500) and use the
sigmoid alone for outbound fees (no floor). Failure escalation discovers the
real market price within ~9 cron cycles (18h at 2h cron) for a 2300-ppm peer.

## Inspecting state

```bash
# Most recent successful refill ppm per channel (drives budget + fee floor)
sqlite3 ln_operator.db "SELECT target_chan_id, fee_ppm, ts FROM rebalance_log \
  WHERE success=1 ORDER BY target_chan_id, ts DESC;"

ln-operator recompute_signals       # manual trigger; prints per-channel signal table
ln-operator adjust_fees --dry-run   # see what would change without broadcasting
```
