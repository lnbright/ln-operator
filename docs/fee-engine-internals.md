# Fee Engine Internals

The 2h pipeline reads cached signals; a nightly job recomputes them. This
split keeps the fast loop cheap and the slow signals stable.

## Cadence

```
Every 2h (cron):
  1. rebalance_channels ← refills depleted channels; each landed chunk writes
                          last_refill_ppm BEFORE fees are computed
  2. adjust_fees        ← reads channel_signals + the refill cost just paid,
                          decides target, gated broadcast (hysteresis)
  3. sync_routing
  4. healthcheck

Nightly (cron, separate line):
  recompute_signals    ← refreshes per-channel market_multiplier, stamps the
                          structural-liquidity flag, logs last_refill / earned
                          ppm / failure counts for visibility
```

The routine ±`MARKET_MULT_STEP` drift is nightly (slow baseline). The 2h loop
adds only an *up-only fast-drain bump* (`MARKET_MULT_FASTDRAIN_STEP`) when a
depleted channel is dropping forwards, so a fast drainer's resting fee climbs
after the first bad cycle rather than waiting days for the nightly drift.

Suggested cron line for the nightly job:

```
15 3 * * * cd /path/to/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
```

## The outbound-fee stack

The target outbound fee is `clamp(max(base × (1+mult), floor), 0, ceiling)`:

1. **Sigmoid base** — `sigmoid_fee_ppm(local_ratio)`. Liquidity-driven base fee
   with clean plateaus near 0% and 100% local. No clamps needed at the edges —
   the curve naturally asymptotes to `SIGMOID_MIN/MAX_PPM`.

2. **Market multiplier** — slow per-channel scalar in `channel_signals`. Each
   nightly run nudges `+MARKET_MULT_STEP` if the channel forwarded in the last
   24h, `-MARKET_MULT_STEP` if silent ≥ `MARKET_MULT_SILENT_DAYS`. Bounded by
   `[MARKET_MULT_MIN, MARKET_MULT_MAX]` (−0.5..1.0). Modulates the sigmoid:
   `adjusted = base × (1 + mult)`. Separately, the 2h loop applies an up-only
   `MARKET_MULT_FASTDRAIN_STEP` bump when a depleted channel drops forwards
   (`forward_fail_log` INSUFFICIENT_BALANCE) — fast up, slow down.

3. **Soft rebalance-cost floor (ratchet)** — "recoup what refilling costs, but
   don't price yourself into the ground." Floor = `last_refill_ppm ×
   REBALANCE_FEE_MARGIN`, read live from `rebalance_log`, but applied as a
   ratchet: it holds at the full level while the channel forwards, decays toward
   the market-clearing fee while the channel sits **idle** (`FLOOR_DECAY_*`,
   half-life `FLOOR_DECAY_HALFLIFE_DAYS`), and does **not** snap back up on a
   forward (that would whipsaw a priced-out channel). It re-arms to the full
   floor only on a **fresh refill** (detected via `floor_armed_refill_ts` vs the
   latest refill ts). **Idle means true silence — no forwards AND no dropped
   forwards.** An INSUFFICIENT_BALANCE drop is a sender who accepted the
   advertised fee (it's in their onion) but found the channel empty: demand at
   the current price. Decay's diagnosis is "idle because priced out"; a
   stocked-out channel with drops is idle because it's *empty*, and decaying a
   price the drops are validating just discounts the liquidity the eventual
   refill delivers — and drags `earned_ppm` (and with it the rebalance budget
   cap) down, blocking the recovery it's waiting for. Drops gate further decay
   but never restore an already-decayed level (same no-whipsaw rule). State lives in `channel_signals.floor_decay_anchor_ppm`
   (current level), `floor_decay_started_ts` (last update), `floor_armed_refill_ts`.
   No refill history → no floor (sigmoid alone decides).

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

## Rebalance budget, failure escalation & profitability gate

`get_channel_rebalance_budget` reads `last_refill_ppm`,
`failures_since_last_success`, and `get_channel_earned_ppm` live from the DB and
returns:

```
escalated = (last_refill OR DEFAULT_BUDGET) × (1 + STEP × failures)
earned_ppm, out_vol = get_channel_earned_ppm(chan)    # None if out_vol < MIN_VOLUME
                                                      # (window widens 21→42→84→90d
                                                      #  before giving up — see below)

if earned_ppm is None:                 # UNJUDGED — full escalation, no cap
    budget = min(escalated, MAX_BUDGET)
else:                                  # JUDGED — earn-ceiling accelerator + cap
    ceiling   = min(earned_ppm × REBALANCE_PROFIT_HORIZON, MAX_BUDGET)
    escalated = max(escalated, anchor + (ceiling − anchor) × min(1, STEP × failures))
    budget    = min(escalated, earned_ppm × REBALANCE_PROFIT_HORIZON, MAX_BUDGET)

# profit_capped is measured against PLAIN escalation (pre-accelerator): the
# accelerator climbs up TO the ceiling, so it must never register as "capped".
```

**Earn-ceiling accelerator** — when a judged channel's anchor sits far below
what it earns (e.g. a single lucky-cheap refill pinned `last_refill` to 7 ppm on
a channel earning 576), plain `STEP × tiny-anchor` escalation crawls and never
rediscovers the clearing price. Each failed run instead closes `STEP` of the gap
to the affordable ceiling, reaching it in `1/STEP` (= 5) runs. It only ever
*raises* the budget, only for judged channels, and only up to the ceiling — so
it never creates a `profit_capped`/`structural` state and leaves unjudged price
discovery untouched. Inert unless `earned × horizon > 2 × anchor`. The dict adds
`accelerated: bool`. Full detail in [Rebalance Budget](rebalance-budget.md#earn-ceiling-accelerator-poisoned-anchor-escape).

`get_channel_earned_ppm` widens its window when the standard 21 days hold less
than `EARNED_PPM_MIN_VOLUME_SATS`: it doubles the lookback (21 → 42 → 84 →
`EARNED_PPM_MAX_LOOKBACK_DAYS`, 90) until the volume suffices, and only returns
the unjudged sentinel when even the max lookback is too quiet. This is the
unjudged-cliff fix: a profit-capped channel that goes silent (often *because*
it is depleted and can't forward) used to shed its cap — and its structural
verdict — the moment the 21d window drained, snapping the budget back to full
escalation (`last_refill × (1 + 0.2 × failures)`, up to `MAX_BUDGET`) with no
profitability evidence consulted. Now adverse evidence ages gradually instead
of expiring at a cliff.

Escalation handles bootstrap, drift, and re-bootstrap. **Layer 1 — the
profitability gate** adds the second clamp: for channels with enough trailing
OUT-volume to judge, never pay more to refill than the channel can earn back
(`earned_ppm × 1.25` ≈ break-even on the recoup price). Channels we can't judge
keep full escalation untouched — capping them would kill the price discovery
escalation exists for. The returned dict carries `earned_ppm`, `profit_capped`,
and `structural` (profit-capped *and* `failures ≥ REBALANCE_STRUCTURAL_FAIL_THRESHOLD`).
`plan_rebalances` drops targets whose ladder verdict ≠ `rebalance`, so structural
channels stop being ground; `recompute_signals` stamps `structural_flag_ts` and
fires a one-time `structural_liquidity` alert (a capital decision — see Layer 3).

## Node-level liquidity ladder (Layer 3 — `engine/liquidity_policy.py`)

Off by default (`INBOUND_FEE_ENABLED=False`). For a depleted channel,
`decide_channel_action` chooses: **rebalance** (profitable + a source exists) →
**inbound_discount** (a negative inbound fee pulling organic refill when a paid
rebalance isn't worth it; a rescue subsidy tapering to 0 by
`INBOUND_DISCOUNT_CLEAR_RATIO`, capped at `our_outbound − safety_margin`) →
**flag_structural** (organic defense failed over `INBOUND_DEFENSE_WINDOW_DAYS` →
capital decision). Optional **inbound_charge** (positive inbound) on heavy sinks
is off by default — positive inbound is not backward-compatible. Inbound and
outbound are set in one `/v1/chanpolicy` POST.

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
| Refilled channel sits idle, floor prices it out | Floor decays toward the clearing fee (`floor↓`) so it can sell; doesn't sit dead at an unsellable price |
| Decayed-floor channel forwards once | Floor HOLDS at the cleared level — does not snap back to full (no whipsaw); only a fresh refill re-arms it |
| Judged channel, refill cost > earned×1.25 | `profit_capped` — budget held to the recoup price; if it keeps failing → `structural`, dropped from planning, capital alert |
| Judged channel, anchor ≪ earnings (lucky-cheap refill poisoned `last_refill`) | **Earn-ceiling accelerator** — each failed run closes 20% of the gap to `earned×1.25`, reaching it in 5 runs instead of crawling. `accelerated` flag set; never strands (climbs only up to the cap) |
| Quiet/new channel, low out-volume | "unjudged" — no profit cap, full escalation (price discovery preserved). Only if out-volume < MIN_VOLUME over the full 90d max lookback |
| Judged channel goes silent (e.g. depleted, can't forward) | Stays judged on older evidence — the earned-ppm window widens up to 90d, so the profit cap and structural verdict persist instead of evaporating with the 21d window |

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
