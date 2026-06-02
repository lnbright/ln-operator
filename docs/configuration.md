# Configuration

Key settings in `config.py`:

## Fee curve
| Setting | Default | |
|---------|---------|---|
| `SIGMOID_MIN_PPM` | 25 | Lower asymptote (channel full → drain) |
| `SIGMOID_MAX_PPM` | 250 | Upper asymptote (channel depleted → defend) |
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
| `MARKET_MULT_STEP` | 0.15 | Per-recompute nudge size (~14 nights to full ramp-up) |
| `MARKET_MULT_MIN` | -0.5 | Max downward adjustment |
| `MARKET_MULT_MAX` | 2.0 | Max upward adjustment (3× base) |
| `MARKET_MULT_BUSY_HOURS` | 24 | Forwards within → nudge up |
| `MARKET_MULT_SILENT_DAYS` | 3 | No forwards for → nudge down |

## Rebalancer (single-signal budget + fee coupling)
| Setting | Default | |
|---------|---------|---|
| `REBALANCE_LOW_THRESHOLD` | 0.20 | Trigger below 20% |
| `REBALANCE_HIGH_THRESHOLD` | 0.80 | Trigger above 80% |
| `REBALANCE_MAX_AMOUNT_RATIO` | 0.50 | Max per attempt |
| `REBALANCE_DEFAULT_BUDGET_PPM` | 500 | Bootstrap budget when no refill history |
| `REBALANCE_MAX_BUDGET_PPM` | 5000 | Hard ceiling on rebalance fee |
| `REBALANCE_BUDGET_ESCALATION_STEP` | 0.20 | +20% per consecutive failure since last success |
| `REBALANCE_FEE_MARGIN` | 1.1 | Outbound fee floor = last_refill × this |

## Planner
| Setting | Default | |
|---------|---------|---|
| `TREASURY_MIN_RATIO` | 0.025 | Wallet reserve |
| `PREFERRED_CHANNEL_SIZE_SATS` | 3,000,000 | Min channel size |
