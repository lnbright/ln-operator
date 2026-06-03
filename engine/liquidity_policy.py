"""
LN Operator — node-level liquidity decision ladder (Layer 3).

Given a channel's state, its rebalance budget verdict (profit gate), and whether
any overfull source exists, decide what to DO about its liquidity:

  rebalance        — depleted, refill is profitable and a source exists; let the
                     planner/executor handle it at the (profit-capped) budget.
  inbound_discount — refill isn't worth a paid rebalance (structural / no source);
                     set a NEGATIVE inbound fee to pull organic refill instead.
                     A rescue subsidy: largest when most depleted, tapering to 0
                     by INBOUND_DISCOUNT_CLEAR_RATIO. Cheaper than a circular
                     rebalance and it doesn't raise outbound / price out demand.
  inbound_charge   — an overfull heavy sink; optional POSITIVE inbound so flow
                     draining scarce liquidity pays on the way in (off by default).
  flag_structural  — the discount probe ran the whole defense window without the
                     channel recovering: organic demand doesn't exist, so this is
                     a capital decision (splice / more inbound / close), not a
                     rebalance/fee problem.
  none             — healthy, nothing to do.

Pure functions, no LND writes. update_all_fees consumes inbound_fee_ppm to set
the inbound side; plan_rebalances consumes action == "rebalance" to decide
whether to emit plans for a target.

Negative inbound is backward-compatible (older senders ignore it). Positive
inbound is not — see config for the safety caveat. When INBOUND_FEE_ENABLED is
False the ladder never emits an inbound fee (inbound_fee_ppm is always 0) and
non-rebalance depleted channels collapse to "none" — but the rebalance/skip
verdict is unchanged, so the Layer-1 grind-stop works with inbound disabled.
"""

from config import (
    REBALANCE_LOW_THRESHOLD, REBALANCE_HIGH_THRESHOLD,
    INBOUND_FEE_ENABLED, INBOUND_DISCOUNT_MAX_PPM, INBOUND_DISCOUNT_CLEAR_RATIO,
    INBOUND_DISCOUNT_SAFETY_MARGIN_PPM, INBOUND_CHARGE_PPM,
    INBOUND_DEFENSE_WINDOW_DAYS,
)


def inbound_discount_ppm(local_ratio, outbound_ppm):
    """Signed inbound fee (≤ 0) to pull organic refill into a depleted channel.

    Largest near 0% local, tapering linearly to 0 at INBOUND_DISCOUNT_CLEAR_RATIO.
    Capped at our own outbound fee minus a margin so the summed forward fee on a
    same-channel route can't go negative (LND's forward-time check is the
    cross-channel backstop). Returns a negative int (or 0)."""
    if local_ratio >= INBOUND_DISCOUNT_CLEAR_RATIO:
        return 0
    frac = 1.0 - (local_ratio / INBOUND_DISCOUNT_CLEAR_RATIO)   # 1.0 at 0%, 0.0 at clear ratio
    raw = INBOUND_DISCOUNT_MAX_PPM * frac
    cap = max(0, outbound_ppm - INBOUND_DISCOUNT_SAFETY_MARGIN_PPM)
    return -int(round(min(raw, cap)))


def decide_channel_action(channel, signals, budget_info, outbound_ppm,
                          has_overfull_source, now):
    """Decide the liquidity action for one channel. Returns
    {action, inbound_fee_ppm, reason}. See module docstring for the ladder."""
    lr = channel["local_ratio"]
    structural = bool(budget_info.get("structural"))
    prev_inbound = int(signals.get("inbound_fee_ppm") or 0)
    discounting_now = prev_inbound < 0

    if lr < REBALANCE_LOW_THRESHOLD:
        # Rebalance is the first rung — but only if it's still worth it and a
        # source exists. `structural` means we've tried at the capped budget
        # enough times to give up on a paid refill.
        if not structural and has_overfull_source:
            result = {"action": "rebalance", "inbound_fee_ppm": 0,
                      "reason": "depleted, profitable refill available"}
        else:
            flag_ts = int(signals.get("structural_flag_ts") or 0)
            defended_long = (structural and flag_ts
                             and (now - flag_ts) >= INBOUND_DEFENSE_WINDOW_DAYS * 86400)
            if defended_long:
                result = {"action": "flag_structural", "inbound_fee_ppm": 0,
                          "reason": "organic defense failed over window → capital decision"}
            else:
                result = {"action": "inbound_discount",
                          "inbound_fee_ppm": inbound_discount_ppm(lr, outbound_ppm),
                          "reason": "rebalance unprofitable/no source → inbound discount"}
    elif lr < INBOUND_DISCOUNT_CLEAR_RATIO and discounting_now:
        # Recovering through the taper band and we were already discounting →
        # keep tapering it off (don't start a discount on a channel that rose
        # here via rebalancing or organic balanced flow).
        result = {"action": "inbound_discount",
                  "inbound_fee_ppm": inbound_discount_ppm(lr, outbound_ppm),
                  "reason": "recovering, tapering inbound discount"}
    elif lr > REBALANCE_HIGH_THRESHOLD and INBOUND_CHARGE_PPM > 0:
        result = {"action": "inbound_charge", "inbound_fee_ppm": INBOUND_CHARGE_PPM,
                  "reason": "heavy-sink source → inbound charge"}
    else:
        result = {"action": "none", "inbound_fee_ppm": 0,
                  "reason": "healthy / out of danger"}

    # Master switch: when inbound fees are off, never emit an inbound fee, and
    # collapse inbound-only actions to "none" so the rebalance/skip verdict is
    # all that the rest of the system sees.
    if not INBOUND_FEE_ENABLED:
        result["inbound_fee_ppm"] = 0
        if result["action"] in ("inbound_discount", "inbound_charge"):
            result["action"] = "none"
    return result
