# Rebalance Budget

The most recent successful refill ppm for a channel anchors both:

- **The budget** (max fee we'll pay to refill it again)
- **The outbound fee floor** (what we charge to recoup that cost + margin)

```
escalated = (last_refill_ppm OR DEFAULT_BUDGET)
             × (1 + ESCALATION_STEP × failures_since_last_success)

# JUDGED channels only — earn-ceiling accelerator (raises escalated, never lowers):
ceiling   = min(earned_ppm × PROFIT_HORIZON, MAX_BUDGET)
escalated = max(escalated,
                anchor + (ceiling − anchor) × min(1, ESCALATION_STEP × failures))

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
also failed `REBALANCE_STRUCTURAL_FAIL_THRESHOLD` (5) **runs** (see
[Failure unit](#failure-unit-the-run-not-the-attempt)) it is `structural` —
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
  **operator-forced rebalance** that lands. The precise tool is
  `ln-operator manual_rebalance <src> <tgt> <amount_sats> <max_ppm>`, which pins
  exactly that pair and bypasses the gate (`rebalance_channels --force` also
  bypasses the gate but auto-selects pairs across all channels by ratio).
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

There are also two further automatic paths:

- **Failures expire** — refusals older than `EARNED_PPM_MAX_LOOKBACK_DAYS`
  (90d) stop counting (same clock as the earnings evidence), so a flag that
  nothing else clears drops below the fail threshold roughly once a quarter
  and the channel gets a free 5-run re-probe at the cap price. Still
  refused → re-flags within hours at zero cost; market moved → it quietly
  comes back to life.

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

## Failure unit: the run, not the attempt

`failures_since_last_success` counts failed **pipeline runs** (refill cycles),
not individual rebalance attempts. This matters because one run fans out a
primary plan plus several *fallback* plans at the same depleted channel — each
is its own `execute_rebalance` call and writes its own failure row. Counting
those as N separate failures would escalate the budget and trip the structural
threshold inside a single ~hour-long run, stranding a channel before it ever
got a real refill-discovery window (this is exactly how a freshly-opened
channel could go "stranded" in under an hour).

So every plan executed in one run shares a `run_id` (stamped in
`execute_rebalance_plans`, the run's start ts), and
`count_failures_since_last_success` counts **distinct failed `run_id`s** since
the last success. A run that landed *any* sats — a fallback or chunk that
succeeded, writing a success row under that `run_id` — is a partial refill, not
a failed cycle, and is excluded. Rows with no `run_id` (legacy pre-migration
rows, one-off manual sends) each count as their own episode, preserving the old
per-row behaviour for them.

> **Why keep `TIMEOUT`/`NO_ROUTE` counted?** A rebalance runs `SendPaymentV2`
> with a fee limit, and LND's pathfinder won't return routes above that limit —
> so `NO_ROUTE`/`TIMEOUT` can genuinely mean "a route exists, but only above
> your cap," which *is* price evidence the structural gate wants. We can't
> cleanly separate "priced out" from "no path at all" from the failure reason,
> so all failure reasons count. The only fix was the *unit* (cycle, not attempt).

The `run_id` column is added (and historical rows backfilled by time-clustering,
>1h gap = new run) by the `_migrate_rebalance_run_id` migration.

## Bootstrap & drift recovery — failure escalation

A channel with no successful refill yet starts at `REBALANCE_DEFAULT_BUDGET_PPM`
(500). Each consecutive failed *run* since the last success raises the budget by
`REBALANCE_BUDGET_ESCALATION_STEP` (20%) per cron cycle, capped at
`REBALANCE_MAX_BUDGET_PPM` (5000). One success resets the counter and the budget
anchors to the actual paid ppm.

**Failures expire** after `EARNED_PPM_MAX_LOOKBACK_DAYS` (90d) — the same
clock that ages out earned-ppm evidence. Escalation's semantics assume each
failure tested a price; a profit-capped channel's failures all tested the
*cap*, not the escalated levels, so carrying them into a later re-entry
(e.g. after the channel goes unjudged) would bid prices that were never
actually refused. With expiry, a channel returning from a long quiet resumes
at `last_refill × 1.0` and rebuilds escalation from fresh runs. Side
effect, deliberate: a standing structural flag on a channel that stays judged
gets a free 5-run re-probe roughly once a quarter instead of being
permanent — markets move, and failed attempts cost nothing.

Example: a channel where real market price is ~2300 ppm bootstraps as
`500 → 600 → 720 → 864 → 1037 → 1244 → 1493 → 1791 → 2150 → 2580` and
succeeds on the 9th attempt (≈18h at the 2h cron).

The same mechanism handles upward market drift after bootstrap — when the
last-known price stops succeeding, failures re-escalate until a new price is
discovered, then `last_refill_ppm` and the fee floor track the new market.

## Earn-ceiling accelerator (poisoned-anchor escape)

Plain escalation grows by `STEP × base` per run, so when `base` (the anchor) is
tiny the budget barely moves. A **single lucky-cheap refill** can pin
`last_refill_ppm` far below the real clearing price — e.g. one fluke fill at
7 ppm on a channel that *earns* 576 ppm. From there `7 → 8 → 10 → 11 → 14 …`
crawls for days, never high enough to route, so the channel sits depleted
losing revenue with no alarm (it isn't `structural` — it's plainly profitable).

For a **judged** channel the accelerator fixes this by escalating against what
the channel can *afford* instead of the poisoned anchor. Each failed run closes
`STEP` of the gap between the anchor and the affordable ceiling
(`min(earned_ppm × PROFIT_HORIZON, MAX_BUDGET)`):

```
escalated = max(escalated,
                anchor + (ceiling − anchor) × min(1, STEP × failures))
```

So it reaches the ceiling in `1/STEP` (= 5) failed runs — the bfx case jumps
from 14 to ~721 ppm. Properties (all deliberate, all tested):

- **No new knob** — reuses `REBALANCE_BUDGET_ESCALATION_STEP` as both the
  per-run rate and the fraction-of-gap-per-failure.
- **Up-only** — it's a `max()`, so it can only raise the budget. Channels
  earning at or below their refill cost are untouched.
- **Judged-only** — `earned_ppm is None` (unjudged) → skipped entirely, so
  bootstrap price discovery on low-volume channels is unchanged.
- **Inert unless it matters** — the gap only beats plain escalation once
  `ceiling > 2 × anchor` (i.e. `earned × horizon > 2 × last_refill`). A channel
  earning roughly what it last paid sees no change.
- **Never strands** — it climbs only *up to* the ceiling, so it can never make
  `escalated` exceed the cap. `profit_capped` is therefore measured against
  *plain* escalation, not the accelerated value (rounding `gap_climb` up could
  otherwise land one ppm over a fractional cap and spuriously flag
  `structural` — exactly the channel the accelerator is rescuing). The
  accelerator firing and `profit_capped` are mutually exclusive.
- **Self-limiting** — it only climbs while runs keep failing; the first success
  resets `failures` and re-anchors `last_refill_ppm`, so it stops at the real
  clearing price and never overshoots.

The budget dict carries `accelerated: bool` alongside `profit_capped` /
`structural`, and the reason string reads
`last_refill 7 ppm accelerated toward earn-ceiling (20% of gap × 5 failed runs) → 721 ppm`.

## QueryRoutes intelligence — read the price instead of grinding for it

The escalation ladder and the earn-ceiling accelerator both *discover* the clearing
price by **failing** over several runs. A **QueryRoutes dry-run** reads it directly
— it's the same pathfinder `SendPaymentV2` uses (mission-control liquidity included),
run with no payment. So the planner can act on the real route price now instead of
groping toward it over days. (`_queryroutes_probe` → `lnd_client.query_routes`; runs
only in the planner — `get_channel_rebalance_budget` stays call-free, since
fees/monitor invoke it per channel every run.) Knobs: `REBALANCE_QUERYROUTES_ENABLED`,
`REBALANCE_QUERYROUTES_EARLYOUT_ENABLED`, `REBALANCE_QUERYROUTES_MIN_CHUNK_SATS`.

Each depleted target's budget dict gains `affordable_ceiling_ppm` =
`min(profit_cap, MAX)` for a judged channel, else `MAX` — the most it could ever
justify paying.

### One probe per source, doing two jobs

For each *judged* depleted target the planner runs **one min-chunk probe per overfull
source**, capped at the ceiling, and that single set of probes drives both halves
(sources are ranked cheapest-first on the way out so the executor pays the cheapest):

- **Pricing.** Price the bid off the **cheapest feasible source** — raise this run's
  `max_fee_ppm` up to its live cost (bounded by the ceiling). An affordable refill
  lands now *and* via the cheapest source, instead of the ~5-run grind (bfx 14→721) or
  paying whichever source happens to be most overfull. The bid only ever *raises*
  (a `max`) and never above the ceiling → never overpays, never moves `last_refill`.
- **Early-out.** If **every** source returns a definite no-route, refilling is a
  capital problem, not price discovery: the channel is dropped from planning (skip the
  wasted attempt) **and** — only on a real run, never a dry-run — a synthetic failed
  cycle is recorded (`failure_reason='QR_NO_AFFORDABLE_ROUTE'`, fee 0, its own
  `run_id`). That advances `count_failures_since_last_success` to the structural
  threshold — *the early-out replaces the wasted attempts, not the stranding they'd
  eventually trigger.* Gated by `REBALANCE_QUERYROUTES_EARLYOUT_ENABLED`; off → the
  probe still prices/ranks but never strands. (These rows show a muted `skipped` badge
  on the dashboard, not a failed attempt.)

### Why it works this way (the design reasoning)

- **Probe every source, not just the most-overfull.** *Feasibility is existential* —
  one working source proves a channel is refillable, and a cheaper source might exist.
  *Infeasibility is universal* — only **all** sources failing justifies a drop. Since
  the drop is consequential (it advances stranding), a single source's no-route must
  never strand a channel, nor price the bid. The old single-source version could both
  falsely strand a channel another source would refill, and overpay by pricing off the
  overfull source when a cheaper one existed. Probing every source fixes both.
- **Min chunk for pricing too** (so there's no separate full-amount probe). ppm is
  amount-dependent: each hop charges `base_fee + amount × rate`, and the fixed base
  fee amortises over fewer sats, so a 100k chunk reads a *higher* effective ppm than
  the whole amount. That makes the 100k price the **worst case** — a safe upper bound
  for the cap (larger/whole-amount routes settle comfortably under it), and one probe
  also covers refills that can only go through in chunks. The budget is a *cap*, not
  the price paid: the executor's pathfinder still finds and pays the cheapest route
  under it, and `last_refill` anchors to the actual paid ppm, so the conservative cap
  never inflates anything.
- **Safety rails.** Judged-only (unjudged keep full price discovery via real
  attempts); a probe that's **unavailable** (LND down) is *unknown*, never no-route,
  so a transport blip can't strand; `force` bypasses the probe entirely.

The probe returns `{drop, budget, source_order}`; the planner threads `source_order`
into both the primary and fallback plan loops so the cheapest feasible source is
tried first.

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

## Sibling Channels (2+ channels to one peer)

`SendPaymentV2` pins the target by `last_hop_pubkey` — the **peer**, not the
channel — and LND's non-strict forwarding pools sibling liquidity at forward
time anyway, so a chunk may settle on either sibling. The books follow the
sats, not the plan: after each successful chunk the executor resolves the
actual landing channel from the invoice's settled HTLC records
(`lnd_client.get_invoice_landing_chan`) and writes the `rebalance_log` row —
hence `last_refill_ppm` and the fee floor — against that channel. The deficit
ledger is credited the same way; sats landing on an untracked sibling leave
the planned target's deficit open for later plans. `sync_rebalances` resolves
manual rebalances from the route's last-hop chan_id (`resolve_target_chan`)
and skips rather than guesses when ambiguous.

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
