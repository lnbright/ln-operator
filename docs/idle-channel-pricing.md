# How an Idle Channel Finds a Sellable Price

When a channel gets refilled, its outbound fee floor is set high enough to
recoup the refill cost (`last_refill_ppm × REBALANCE_FEE_MARGIN`). That's the
right price *if the channel is forwarding*. But a channel can end up priced out
of its own market — the floor is above what anyone will pay — and then it just
sits there earning nothing.

This guide explains the machinery that gets a stuck channel back to a price that
actually sells, and how to watch it happen. It ties together three moving parts:
the **market multiplier**, the **clearing fee**, and the **floor decay**.

> Reference companion: [Fee Engine Internals](fee-engine-internals.md) and
> [Fee Formula](fee-formula.md). This doc is the narrative "why/how it moves"
> version of those.

## The clearing fee — the target

Every fee update computes a *market-clearing fee*: the price the channel would
rest at on pure demand signals, ignoring the refill-cost floor.

```
clearing = sigmoid(local_ratio) × (1 + market_multiplier)
```

- **`sigmoid(local_ratio)`** — the base curve. Low local balance → high fee
  (defend the little liquidity left); high local balance → low fee (sell it).
- **`market_multiplier`** (`mult`) — a per-channel demand adjustment, in
  `[-0.5, +1.0]`. At `-0.5` it halves the base; at `+1.0` it doubles it.

This is **not stored as its own column** — it's recomputed each run and printed
in the `fee_updates.reason` string, e.g.:

```
LNBiG: sigmoid=388 mult=-0.50 floor=2669(↓from 2861) → 2669 [floor-decaying]
```

Here the clearing fee is `388 × (1 − 0.50) ≈ 194 ppm`. The floor is still 2669,
decaying down from the 2861 it was armed at. The 194 is the target it's heading
toward.

## The market multiplier — the demand signal

`mult` is a coarse, **volume-blind** proxy for "is anyone forwarding through this
channel." It moves two ways.

### Nightly drift (slow baseline)

The `recompute_signals` job (3:15am cron) nudges each channel by
`±MARKET_MULT_STEP` (0.15):

| condition | nudge |
|---|---|
| a forward in the last 24h (`MARKET_MULT_BUSY_HOURS`) | **+0.15** |
| 1–3 days idle | no change |
| silent ≥3 days (`MARKET_MULT_SILENT_DAYS`) | **−0.15** |
| never forwarded | −0.15 |

Clamped to `[-0.5, +1.0]`. **It is volume-blind** — a single 1-sat forward bumps
`mult` exactly as much as a 1M-sat forward. Both just flip the channel to "busy"
for the night. (This is deliberate: no PROVEN/volume tiers.)

A channel silent for several days steps down 0.15/night until it pins at the
`-0.5` floor and can't go lower. That's why a long-idle channel shows
`mult=-0.50` — the system wants to keep discounting but this lever is maxed out.

### Fast-drain bump (intra-day, up-only)

The nightly ±0.15 is too slow for a channel that's actively bleeding. So the **2h
pipeline** adds an immediate up-bump when a channel is *depleted and dropping
forwards*. All of these must hold:

1. `local_ratio < FEE_HYSTERESIS_EDGE_LOW` (0.20) — the channel is in the
   low-local defense zone.
2. At least one `INSUFFICIENT_BALANCE` drop since the last fee update
   (recorded in `forward_fail_log` — a forward we *couldn't serve* for lack of
   liquidity).
3. `mult` isn't already at the `+1.0` cap.

When it fires: `mult += MARKET_MULT_FASTDRAIN_STEP` (0.40), capped at +1.0,
**up-only**, applied **this same cycle** so the resting fee climbs right away
instead of waiting for the nightly tick.

Successful forwards drive the nightly +0.15; *dropped* forwards drive the
intra-day +0.40. Nothing in the 2h loop ever lowers `mult` — downward movement is
exclusively the nightly silent-step.

> This is what pins a hammered channel at the `+1.0` ceiling: repeated drops
> bump it +0.40 per cycle until it can't defend with price any further. Past that
> point the leftover demand is pure lost revenue — the fix is more inbound
> liquidity, not a higher fee.

## The floor decay — walking toward the target

The refill-cost floor (`last_refill × REBALANCE_FEE_MARGIN`) is a **ratchet, not
a snap-back**. It relaxes toward the clearing fee only while the channel is idle:

```
new_floor = clearing + (old_floor − clearing) × 0.5 ^ (idle_days / FLOOR_DECAY_HALFLIFE_DAYS)
```

- Decay does **not start** until the channel has been silent for
  `FLOOR_DECAY_IDLE_SECONDS` (3 days) — a grace period before discounting.
- Once decaying, the **gap** between the floor and the clearing fee halves every
  `FLOOR_DECAY_HALFLIFE_DAYS` (currently **3 days**).
- It never drops below `FLOOR_DECAY_MIN_PPM` (25 ppm).

### Why it's fast at first and slow at the end

It's exponential. The step each cycle is proportional to the *remaining gap*:

- Big gap (floor 2861, clearing 194 → gap ~2667): half of a big number is a big
  absolute move. You'll see hundreds of ppm shed per day.
- As the floor nears the clearing fee, the gap shrinks, so each halving moves
  fewer absolute ppm. It crawls the last stretch and asymptotically approaches —
  never quite touching — the clearing fee.

The decay is **time-based**, recomputed every 2h pipeline run (`dt = now − last
update`). The 2h cadence just samples a continuous curve; total decay over a day
is the same regardless of how often it runs.

## What happens when a forward lands

This is the subtle, important part. Decay only runs while the channel is *idle*,
and the inputs to the clearing fee only move while the channel is *active* — so
the two states are mutually exclusive.

- A "forward" means an HTLC **routed through** the channel by someone else
  (`forwarding_log`). **Your own payments don't count** — paying an invoice is an
  outbound payment, not a forward, and does not freeze the decay.
- The instant a forward lands, `idle` flips false. **Decay pauses and the floor
  freezes at its current level** — it does *not* snap back up to the full floor.
- The floor stays frozen until **3 more days of silence**, then decay resumes
  *from the frozen level*.
- The floor re-arms to the **full** `last_refill × margin` floor only on a
  **fresh refill** (new cost to recoup), never on a forward.

### Dropped forwards gate decay too

Decay's diagnosis is "idle because priced out" — but a **depleted** channel is
idle because it's *empty*. The two look identical in `forwarding_log` and are
distinguished by `forward_fail_log`: an INSUFFICIENT_BALANCE drop is a sender
who already accepted the advertised fee (it's baked into their onion) but found
no liquidity — demand *at the current price*. So `idle` means **true silence**:
no forwards AND no such drops within `FLOOR_DECAY_IDLE_SECONDS`. Drops hold the
floor like forwards do (and like forwards, never lift an already-decayed
level). Without this gate, a stocked-out channel with senders queuing at its
price would keep discounting anyway — selling the eventual refill below a
proven price and dragging `earned_ppm` (hence the rebalance budget cap) down,
blocking the very refills that would fix it.

### Can the target move while we converge?

Yes, but tamely:

- **Drifts down:** in the first few idle nights `mult` is still stepping toward
  the `-0.5` clamp, so the clearing fee slides down a little each night, then
  settles once `mult` pins. The floor chases a slowly-receding target, then it's
  stationary.
- **Jumps up:** can't happen *while idle* — silence only pushes `mult` down and
  the balance is static, so the clearing fee can't rise above the floor without
  activity.
- **A forward mid-decay** both freezes the floor *and* (via the nightly busy
  bump, plus the drain lowering `local_ratio`) can lift the clearing fee above
  the frozen floor. When decay later resumes it converges toward the *new* target
  — which can mean nudging the floor slightly **up**. That's intended: the
  channel proved it can sell and showed demand, so the clearing price is genuinely
  higher than we'd discounted to.

So you never chase a target running away upward — upward movement and decay can't
coexist.

## When does a new fee actually broadcast?

The floor level updates every 2h, but a fee is only *pushed to LND* when it
clears the hysteresis gate (`_should_broadcast`), evaluated top to bottom:

1. **Tolerance** — skip if the move is *both* < 10 ppm **and** < 10% relative.
   (Pass on ≥10 ppm **or** ≥10%.)
2. **Snap** — a change ≥ 30 ppm always broadcasts, even inside the cooldown.
3. **Edge-zone crossing** — if `local_ratio` crossed a zone boundary
   (low <0.20 / mid / high >0.80) it broadcasts and escapes the cooldown.
4. **Cooldown** — otherwise, skip if the last broadcast was < 6h ago.
5. **Normal** — cleared tolerance, no snap, no crossing, ≥6h elapsed → send it.

A fast-decaying channel shedding ~40+ ppm per 2h cycle clears the 30-ppm snap and
broadcasts every cycle. A slow one moving ~10 ppm/cycle batches up under the
cooldown and broadcasts roughly every 6h.

## Where to watch it

Everything is in the `fee_updates.reason` string — current floor, what it's
decaying from, and the clearing target all in one line:

```sql
SELECT peer_alias, new_fee_ppm AS floor_now, reason
FROM fee_updates
WHERE reason LIKE '%decay%'
  AND ts > strftime('%s','now') - 2*86400
GROUP BY chan_id
HAVING ts = MAX(ts);
```

Live ratchet state (current floor + when it last moved) is in `channel_signals`:

```sql
SELECT chan_id,
       round(floor_decay_anchor_ppm, 1) AS floor,
       datetime(floor_decay_started_ts, 'unixepoch', 'localtime') AS moved,
       round(market_multiplier, 2) AS mult
FROM channel_signals
WHERE floor_decay_anchor_ppm IS NOT NULL;
```

## The three clocks, summarised

| signal | cadence | direction |
|---|---|---|
| **Floor decay** | every 2h (time-based) | down toward clearing while idle ≥3d; frozen by a forward; re-armed up by a fresh refill |
| **Fast-drain mult bump** (+0.40) | every 2h, only when depleted + dropping forwards | up only, capped at +1.0 |
| **Routine mult drift** (±0.15) | nightly | up if a forward in 24h, down if silent ≥3d |

The net intent: a refilled channel defends its cost while it's selling, and a
priced-out channel is gently walked down to a price the market will actually pay
— without whipsawing between the two.
