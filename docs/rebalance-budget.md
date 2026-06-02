# Rebalance Budget

Single-signal model. No tiers, no maturity gates. The most recent successful
refill ppm for a channel drives both:

- **The budget** (max fee we'll pay to refill it again)
- **The outbound fee floor** (what we charge to recoup that cost + margin)

```
budget   = (last_refill_ppm OR DEFAULT_BUDGET)
            × (1 + ESCALATION_STEP × failures_since_last_success)
            capped at REBALANCE_MAX_BUDGET_PPM

fee_floor = last_refill_ppm × REBALANCE_FEE_MARGIN
            (0 if no successful refill yet — sigmoid alone)
```

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
