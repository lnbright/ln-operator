"""
LN Operator — Channel Management Engine.

Deterministic operations only — given the same channel state and config, you
always get the same output. No LLM calls. Reads live data from LND (via
lnd_client), reads historical state from SQLite (via db), and writes results
back to SQLite for the dashboard and CLI to read.

This package is split by concern; this __init__ re-exports every public
symbol so existing callers can keep using `engine.X`:

  fees                — sigmoid curve, hysteresis, fee broadcast loop,
                        market-multiplier compute/recompute (nightly).
  rebalance_planner   — budget, candidate selection, plan_rebalances.
  rebalance_executor  — execute_rebalance + chunked retry.
  sync                — sync_forwarding_history, sync_rebalances,
                        chan_open_ts_from_id helper.
  monitor             — get_channel_health_report (snapshots + alerts).
"""

from engine.fees import (
    sigmoid_fee_ppm,
    calculate_fee_ppm,
    _edge_zone,
    compute_fee_target,
    _should_broadcast,
    update_all_fees,
    compute_market_multiplier,
    recompute_all_signals,
)
from engine.rebalance_planner import (
    get_channel_rebalance_budget,
    find_rebalance_candidates,
    calculate_rebalance_amount,
    plan_rebalances,
)
from engine.rebalance_executor import (
    execute_rebalance,
    _attempt_single_rebalance,
)
from engine.sync import (
    chan_open_ts_from_id,
    sync_forwarding_history,
    sync_rebalances,
)
from engine.monitor import (
    get_channel_health_report,
)

__all__ = [
    # fees
    "sigmoid_fee_ppm", "calculate_fee_ppm", "compute_fee_target",
    "update_all_fees", "compute_market_multiplier", "recompute_all_signals",
    # rebalance
    "get_channel_rebalance_budget", "find_rebalance_candidates",
    "calculate_rebalance_amount", "plan_rebalances", "execute_rebalance",
    # sync
    "chan_open_ts_from_id", "sync_forwarding_history", "sync_rebalances",
    # monitor
    "get_channel_health_report",
]
