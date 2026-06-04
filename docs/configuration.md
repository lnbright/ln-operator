# Configuration

Key settings in `config.py`:

## Fee curve
| Setting | Default | |
|---------|---------|---|
| `SIGMOID_MIN_PPM` | 25 | Lower asymptote (channel full → drain) |
| `SIGMOID_MAX_PPM` | 750 | Upper asymptote (channel depleted → defend). Lifted from 250 so a draining channel can defend with price before a paid rebalance is needed |
| `SIGMOID_K` | 8.0 | Steepness; higher = sharper midpoint transition |
| `SIGMOID_MIDPOINT` | 0.5 | local_ratio at curve midpoint |
| `FEE_HARD_CEILING_PPM` | 5000 | Absolute cap — even floor can't exceed this |

## Hysteresis (when to broadcast fee changes)
| Setting | Default | |
|---------|---------|---|
| `FEE_HYSTERESIS_TOLERANCE_PPM` | 10 | Min absolute change to broadcast |
| `FEE_HYSTERESIS_TOLERANCE_PCT` | 0.10 | Also need ≥10% relative change |
| `FEE_HYSTERESIS_COOLDOWN_SEC` | 21600 | Don't update same channel within 6h |
| `FEE_HYSTERESIS_SNAP_PPM` | 30 | Big jumps skip the cooldown |

## Market multiplier (slow demand signal)
| Setting | Default | |
|---------|---------|---|
| `MARKET_MULT_STEP` | 0.15 | Per-recompute nudge size (~7 nights to full ramp-up at MAX 1.0) |
| `MARKET_MULT_MIN` | -0.5 | Max downward adjustment |
| `MARKET_MULT_MAX` | 1.0 | Max upward adjustment (2× base). With SIGMOID_MAX 750 the demand-amplified outbound max is 1500 |
| `MARKET_MULT_BUSY_HOURS` | 24 | Forwards within → nudge up |
| `MARKET_MULT_SILENT_DAYS` | 3 | No forwards for → nudge down |
| `MARKET_MULT_FASTDRAIN_STEP` | 0.40 | Up-only emergency bump applied in the 2h loop when a depleted channel drops forwards (forward_fail_log INSUFFICIENT_BALANCE); routine ±STEP drift stays nightly |

## Rebalancer (budget, profitability gate, fee coupling)
| Setting | Default | |
|---------|---------|---|
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Trigger below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Trigger above 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max per attempt |
| `REBALANCE_DEFAULT_BUDGET_PPM` | 500 | Bootstrap budget when no refill history |
| `REBALANCE_MAX_BUDGET_PPM` | 5000 | Hard ceiling on rebalance fee |
| `REBALANCE_BUDGET_ESCALATION_STEP` | 0.20 | +20% per consecutive failure since last success |
| `REBALANCE_FEE_MARGIN` | 1.1 | Outbound fee floor = last_refill × this (soft — decays while idle) |

## Profitability gate (Layer 1 — don't overpay to refill)
| Setting | Default | |
|---------|---------|---|
| `EARNED_PPM_WINDOW_DAYS` | 21 | Trailing window for per-channel earned-ppm |
| `EARNED_PPM_MIN_VOLUME_SATS` | 2,000,000 | Min OUT-traffic to trust the ratio; below → "unjudged" (full escalation, no cap) |
| `REBALANCE_PROFIT_HORIZON` | 1.25 | Judged budget cap = earned_ppm × this (≈ break-even on the recoup price) |
| `REBALANCE_STRUCTURAL_FAIL_THRESHOLD` | 5 | Consecutive fails while profit-capped → flag structural (capital decision) |

## Soft outbound floor decay (Layer 2)
| Setting | Default | |
|---------|---------|---|
| `FLOOR_DECAY_HALFLIFE_DAYS` | 3.0 | Idle floor halves toward the clearing fee every N days; 0 disables decay |
| `FLOOR_DECAY_IDLE_SECONDS` | 259200 | Only decay after this much silence (3d) |
| `FLOOR_DECAY_MIN_PPM` | 25 | Decay never drops the floor below this |

## Node-level inbound fees (Layer 3 — off by default)
| Setting | Default | |
|---------|---------|---|
| `INBOUND_FEE_ENABLED` | False | Master switch for inbound-fee management |
| `INBOUND_DISCOUNT_MAX_PPM` | 200 | Largest negative inbound (discount), applied when most depleted |
| `INBOUND_DISCOUNT_CLEAR_RATIO` | 0.35 | Taper the discount to 0 by this local ratio ("out of danger") |
| `INBOUND_DISCOUNT_SAFETY_MARGIN_PPM` | 10 | Discount ≤ our outbound − this (keeps summed forward fee > 0) |
| `INBOUND_CHARGE_PPM` | 0 | Positive inbound on heavy-sink sources; 0 = disabled (not backward-compatible) |
| `INBOUND_HYSTERESIS_PPM` | 25 | Min inbound-fee move before re-broadcast |
| `INBOUND_DEFENSE_WINDOW_DAYS` | 14 | Inbound-discount defense duration before flagging structural |

## Planner
| Setting | Default | |
|---------|---------|---|
| `TREASURY_MIN_RATIO` | 0.025 | Wallet reserve |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Min channel size |
