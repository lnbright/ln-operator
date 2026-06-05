# Rebalance Budget

The most recent successful refill ppm for a channel anchors both:

- **The budget** (max fee we'll pay to refill it again)
- **The outbound fee floor** (what we charge to recoup that cost + margin)

```
escalated = (last_refill_ppm OR DEFAULT_BUDGET)
             × (1 + ESCALATION_STEP × failures_since_last_success)

budget    = min(escalated, MAX_BUDGET)                     # if UNJUDGED
budget    = min(escalated, earned_ppm × PROFIT_HORIZON, MAX_BUDGET)  # if JUDGED

fee_floor = soft ratchet of last_refill_ppm × REBALANCE_FEE_MARGIN
            (0 if no successful refill yet — sigmoid alone)
```

## Profitability gate (Layer 1)

Escalation alone would happily pay 2500 ppm to refill a channel that only earns
200 — the classic "buy expensive, sell cheap" bleed. The gate caps a **judged**
channel's budget at `earned_ppm × REBALANCE_PROFIT_HORIZON` (1.25 ≈ break-even
on the recoup price): never pay more to refill than the channel can earn back.

- **Judged** = trailing OUT-volume ≥ `EARNED_PPM_MIN_VOLUME_SATS` (2M over
  `EARNED_PPM_WINDOW_DAYS`), so `earned_ppm = Σ fee_earned / Σ amount_out` is
  trustworthy. The cap applies. If the standard 21d window is too quiet, it
  **widens** (doubling, up to `EARNED_PPM_MAX_LOOKBACK_DAYS` = 90d) before the
  channel is declared unjudged — evidence ages, it doesn't expire at a cliff.
- **Unjudged** = too little volume *even over the max lookback* to trust the
  ratio → **no cap, full escalation** (capping a low-volume channel would kill
  price discovery and it'd never bootstrap). The gate is opt-in by evidence.

A judged channel whose escalation exceeds the cap is `profit_capped`; if it has
also failed `REBALANCE_STRUCTURAL_FAIL_THRESHOLD` (5) times it is `structural` —
`plan_rebalances` drops it (refilling is a losing trade), the verdict stamps
`structural_flag_ts` and fires a one-time `structural_liquidity` alert, and the
fix becomes a capital decision (open inbound / splice / resize). With Layer 3
enabled it first gets a negative inbound-fee probe to pull organic refill.

The stamp is written by **both** the 2h fee loop (`update_all_fees`) and the
nightly `recompute_signals`, via the shared `_structural_flag_ts` helper —
first-stamp on entry, kept thereafter, cleared on recovery. Whichever runs first
wins, so the flag trips within one 2h cycle, not up to a day late. The alert is
gated on the prior stamp, so it still fires exactly once.

## Clearing a structural flag

`structural = profit_capped AND failures_since_last_success ≥ THRESHOLD`. The
flag (and its timestamp) clears automatically on the next run where **either**
term goes false. It is not permanent — but note the auto-loop has a deliberate
blind spot, so on a steady sink it tends to persist until you act:

- **A successful refill** moves the failure cutoff and zeroes
  `failures_since_last_success` → not structural. **Catch:** `plan_rebalances`
  drops structural targets, so the pipeline never *attempts* a refill and never
  produces the success that would clear it. In practice this path needs an
  **operator-forced rebalance** (`ln-operator rebalance --force …` against that
  channel) that lands.
- **Earnings climb** until `earned_ppm × PROFIT_HORIZON > escalated_budget` →
  `profit_capped` false → cleared. Realistic only if the channel starts earning
  far more on outbound than it did.
- **Channel goes UNJUDGED** — only if OUT-volume falls below
  `EARNED_PPM_MIN_VOLUME_SATS` over the *full max lookback*
  (`EARNED_PPM_MAX_LOOKBACK_DAYS`, 90d): then `earned_ppm` is `None`, the
  profit cap evaporates, and `profit_capped` is false → cleared. This used to
  fire after just 21 quiet days — a dangerous cliff, since a structural channel
  is quiet *because* it is depleted, and the budget that came back was the full
  failure-escalation (e.g. `last_refill 2,601 × 2.0 → 5,000` ppm) with no
  profitability evidence consulted. The earned-ppm window now widens
  (21 → 42 → 84 → 90d) before giving up, so adverse evidence ages out gradually
  and this path is realistic only after ~3 months of silence.

There is also a fourth, automatic path:

- **Liquidity recovers** — if the channel's `local_ratio` climbs back to
  `REBALANCE_TARGET` (≥50%), the structural verdict is cleared regardless of
  earnings or failure history: a channel that's no longer depleted isn't a
  structural emergency. Strong hysteresis — it trips below 20% and only clears
  at ≥50%, so it can't flap. This is what retires the alarm when the inbound
  discount (or any organic flow) actually refills the channel.

> ⚠️ **Attention:** the recovery escape only applies where a live `local_ratio`
> is passed to `get_channel_rebalance_budget` — the fee loop, monitor, and
> dashboard all pass it, so the flag and its timestamp clear within one 2h cycle
> of recovery. The rebalance planner calls it without a ratio, but that's moot:
> it only ever evaluates already-depleted (<20%) targets, which can't be
> recovered. `profit_capped` is *not* affected by recovery — only the
> `structural` escalation clears.

## Bootstrap & drift recovery — failure escalation

A channel with no successful refill yet starts at `REBALANCE_DEFAULT_BUDGET_PPM`
(500). Each consecutive *whole-attempt* failure since the last success raises
the budget by `REBALANCE_BUDGET_ESCALATION_STEP` (20%) per cron cycle, capped
at `REBALANCE_MAX_BUDGET_PPM` (5000). One success resets the counter and the
budget anchors to the actual paid ppm.

Example: a channel where real market price is ~2300 ppm bootstraps as
`500 → 600 → 720 → 864 → 1037 → 1244 → 1493 → 1791 → 2150 → 2580` and
succeeds on the 9th attempt (≈18h at the 2h cron).

The same mechanism handles upward market drift after bootstrap — when the
last-known price stops succeeding, failures re-escalate until a new price is
discovered, then `last_refill_ppm` and the fee floor track the new market.

## Outbound fee impact

After the first successful refill at `R` ppm:
- The fee floor becomes `R × 1.1` (e.g. 2300 → 2530).
- `update_all_fees` posts that target on the next 2h pipeline run, subject
  to hysteresis (`SNAP_PPM` usually lets it through without waiting).
- No 5-sample warmup, no median smoothing — one refill = one fee update.
- The floor is a **soft ratchet**: it holds at `R × 1.1` while the channel
  forwards, but if the channel goes idle and the floor is pricing it out, the
  effective floor decays toward the market-clearing fee (half-life
  `FLOOR_DECAY_HALFLIFE_DAYS`) so it can find a price that sells. A forward does
  **not** snap it back up (no whipsaw); only a fresh refill re-arms it to the
  full level. See [Fee Engine Internals](fee-engine-internals.md).

## Auto-Chunking

Full amount fails → halve and retry, down to 100k min. Each successful chunk
is logged as its own success row in `rebalance_log` at the chunk's actual ppm,
so `last_refill_ppm` reflects what we actually paid (very small chunks can
appear inflated because LND's base fee dominates at low amounts).

## Fallback Pairs

For every depleted target, the planner emits one or more **primary** pairs
(sources whose surplus sums to the target's deficit) and then **fallback**
pairs (every other overfull source paired with the same target).

At execution time the run keeps two ledgers:

- `target_deficits` — sats each target still needs.
- `source_remaining` — sats each source can still send.

Each plan is capped at `min(plan amount, target deficit, source remaining)`
before being attempted, and both ledgers decrement on success. A fallback
fires only when its target's deficit is still ≥ 50k *and* its source still
has ≥ 50k to send — both conditions emerge naturally from the ledgers, no
separate gating. This means:

- A primary that partially fills its target leaves the deficit open, and
  the next plan (often a fallback against the same target) picks up where
  it left off without overshooting.
- A source already drained by an earlier successful plan auto-skips the
  rest of its plans, avoiding wasted insufficient-balance attempts.
- A target fully filled removes itself from contention; remaining
  fallbacks for it are skipped.

## Force Mode

`--force 0.4` ignores thresholds, targets 40% on all channels. `--dry-run`
shows per-channel end states at different force levels so you can pick.
