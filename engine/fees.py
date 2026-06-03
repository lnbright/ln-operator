"""
LN Operator — Fee policy and market-multiplier feedback.

Two cooperating loops live here:

1. update_all_fees (2h pipeline) reads each channel's current fee, computes
   a target via the sigmoid + market multiplier + last-refill floor, and
   broadcasts the new policy if hysteresis permits. Pins in fee_overrides
   short-circuit the whole chain.

2. recompute_all_signals (nightly) nudges each channel's market_multiplier
   based on observed forwarding activity. Slow-moving; doesn't broadcast
   anything, just refreshes the cached input the 2h loop reads.
"""

import time
import math

from config import (
    FEE_BASE_MSAT,
    SIGMOID_MIN_PPM, SIGMOID_MAX_PPM, SIGMOID_K, SIGMOID_MIDPOINT,
    FEE_HARD_CEILING_PPM,
    FEE_HYSTERESIS_TOLERANCE_PPM, FEE_HYSTERESIS_TOLERANCE_PCT,
    FEE_HYSTERESIS_COOLDOWN_SEC, FEE_HYSTERESIS_SNAP_PPM,
    FEE_HYSTERESIS_EDGE_LOW, FEE_HYSTERESIS_EDGE_HIGH,
    MARKET_MULT_STEP, MARKET_MULT_MIN, MARKET_MULT_MAX,
    MARKET_MULT_BUSY_HOURS, MARKET_MULT_SILENT_DAYS,
    MARKET_MULT_FASTDRAIN_STEP,
    FLOOR_DECAY_HALFLIFE_DAYS, FLOOR_DECAY_IDLE_SECONDS, FLOOR_DECAY_MIN_PPM,
    REBALANCE_FEE_MARGIN, REBALANCE_HIGH_THRESHOLD,
    INBOUND_FEE_ENABLED, INBOUND_HYSTERESIS_PPM,
)
import lnd_client
import db
from engine.liquidity_policy import decide_channel_action
from logging_config import get_logger

log = get_logger('engine.fees')


# ─── Fee curve ──────────────────────────────────────────────────

def sigmoid_fee_ppm(local_ratio):
    """Sigmoid mapping from local_ratio to base outbound fee ppm.

    Flat across the healthy middle (~30-70% local), steep at the edges.
    Avoids the linear curve's problem of triggering broadcasts on tiny
    healthy drift while being sluggish in the defense zone.

    f(ratio) = MIN + (MAX - MIN) / (1 + exp( K * (ratio - MIDPOINT) ))
    """
    try:
        x = SIGMOID_K * (local_ratio - SIGMOID_MIDPOINT)
        if x > 700:    # avoid math.exp overflow at extreme ratios
            sig = 0.0
        elif x < -700:
            sig = 1.0
        else:
            sig = 1.0 / (1.0 + math.exp(x))
    except (TypeError, ValueError):
        sig = 0.5
    ppm = SIGMOID_MIN_PPM + (SIGMOID_MAX_PPM - SIGMOID_MIN_PPM) * sig
    return int(round(max(SIGMOID_MIN_PPM, min(SIGMOID_MAX_PPM, ppm))))


# Backwards-compatible alias for any caller still using the old name.
calculate_fee_ppm = sigmoid_fee_ppm


def _edge_zone(local_ratio):
    """Classify which hysteresis edge zone a ratio falls in: 'low', 'high', or 'mid'."""
    if local_ratio < FEE_HYSTERESIS_EDGE_LOW:
        return "low"
    if local_ratio > FEE_HYSTERESIS_EDGE_HIGH:
        return "high"
    return "mid"


def compute_fee_target(channel, signals, now, last_forward_ts=None, last_refill_ts=None):
    """Compute the target outbound fee for a channel + decide whether to broadcast.

    The outbound floor recoups refill cost (last refill ppm × REBALANCE_FEE_MARGIN).
    It is a RATCHET, not a snap-back: it sits at the full recoup level while the
    channel forwards, and relaxes toward the market-clearing fee only while the
    channel is IDLE (no forwards for FLOOR_DECAY_IDLE_SECONDS), ratcheting DOWN.
    Crucially it does NOT jump back up when a forward lands — that would whipsaw a
    priced-out channel between a sellable price and an unsellable one. It is
    re-armed to the full floor ONLY by a FRESH refill (a refill newer than the one
    it's armed against = new cost to recoup); demand-driven upside is the market
    multiplier's job. Sigmoid alone drives fees before any refill history exists.

    State (persisted by the caller): floor_decay_anchor_ppm = the current floor
    level (ratchet), floor_decay_started_ts = when the level was last updated,
    floor_armed_refill_ts = the refill ts the floor is armed against.

    Returns dict with: target_ppm, base_ppm, mult, floor_ppm (effective),
    hard_floor_ppm, source, reason, and the ratchet state to persist.
    """
    chan_id = channel["chan_id"]
    local_ratio = channel["local_ratio"]

    base = sigmoid_fee_ppm(local_ratio)
    mult = float(signals.get("market_multiplier", 0.0) or 0.0)

    last_refill = db.get_last_refill_ppm(chan_id)
    hard_floor = int(round(last_refill * REBALANCE_FEE_MARGIN)) if last_refill else 0

    # Adjusted base with market multiplier. In the low-local defense zone,
    # the multiplier may never push the fee BELOW the sigmoid output — that
    # would invite the very drain we're defending against.
    adjusted = base * (1.0 + mult)
    if local_ratio < FEE_HYSTERESIS_EDGE_LOW:
        adjusted = max(adjusted, base)
    market_clearing = adjusted

    # Soft-floor RATCHET (see docstring): down while idle, hold while forwarding,
    # re-arm to full only on a fresh refill.
    prev_level    = signals.get("floor_decay_anchor_ppm")
    prev_ts       = int(signals.get("floor_decay_started_ts") or 0)
    prev_armed_ts = int(signals.get("floor_armed_refill_ts") or 0)
    new_armed_ts  = prev_armed_ts
    decaying = False

    if not hard_floor or FLOOR_DECAY_HALFLIFE_DAYS <= 0:
        effective_floor = float(hard_floor)
        new_level = None
    else:
        idle = (now - (last_forward_ts or 0)) > FLOOR_DECAY_IDLE_SECONDS
        fresh_refill = (last_refill_ts or 0) > prev_armed_ts
        if prev_level is None or fresh_refill:
            level = float(hard_floor)            # (re)arm to the full recoup floor
            new_armed_ts = last_refill_ts or now
        elif idle:
            # Ratchet down toward the clearing fee by the time since last update.
            dt_days = max(0.0, (now - prev_ts) / 86400.0) if prev_ts else 0.0
            factor = 0.5 ** (dt_days / FLOOR_DECAY_HALFLIFE_DAYS)
            level = market_clearing + (float(prev_level) - market_clearing) * factor
        else:
            level = float(prev_level)            # active: hold (no decay, no snap-up)
        # Never below the clearing fee nor the absolute min; never above the floor.
        level = min(max(level, market_clearing, float(FLOOR_DECAY_MIN_PPM)), float(hard_floor))
        effective_floor = level
        new_level = int(round(level))
        decaying = new_level < hard_floor

    new_level_ts = now if new_level is not None else 0

    target = max(adjusted, effective_floor)
    target = min(target, float(FEE_HARD_CEILING_PPM))
    target_ppm = int(round(target))

    floor_ppm = int(round(effective_floor)) if hard_floor else 0
    if hard_floor and effective_floor >= adjusted:
        source = "floor-decaying" if decaying else "floor"
    elif abs(mult) > 0.01:
        source = "sigmoid+market"
    else:
        source = "sigmoid"

    floor_note = f"floor={floor_ppm}"
    if decaying:
        floor_note += f"(↓from {hard_floor})"
    reason = (
        f"sigmoid={base} mult={mult:+.2f} {floor_note} "
        f"local={local_ratio:.2f} → {target_ppm} [{source}]"
    )

    return {
        "target_ppm": target_ppm,
        "base_ppm": base,
        "mult": mult,
        "floor_ppm": floor_ppm,
        "hard_floor_ppm": hard_floor,
        "source": source,
        "reason": reason,
        "floor_decay_started_ts": new_level_ts,
        "floor_decay_anchor_ppm": new_level,
        "floor_armed_refill_ts": new_armed_ts,
    }


def _should_broadcast(target_ppm, current_ppm, signals, local_ratio, now):
    """Hysteresis gate. Returns (broadcast: bool, why: str)."""
    delta = target_ppm - current_ppm
    abs_delta = abs(delta)

    # Tolerance — must clear both the absolute and the relative thresholds.
    pct = abs_delta / max(current_ppm, 1)
    if abs_delta < FEE_HYSTERESIS_TOLERANCE_PPM and pct < FEE_HYSTERESIS_TOLERANCE_PCT:
        return False, f"within tolerance (Δ={delta:+d})"

    # Snap escapes — always broadcast big jumps.
    if abs_delta >= FEE_HYSTERESIS_SNAP_PPM:
        return True, f"snap (Δ={delta:+d})"

    # Edge-zone crossing escapes the cooldown.
    last_ratio = signals.get("last_local_ratio")
    if last_ratio is not None:
        if _edge_zone(local_ratio) != _edge_zone(last_ratio):
            return True, f"edge crossing ({_edge_zone(last_ratio)}→{_edge_zone(local_ratio)})"

    # Cooldown — otherwise rate-limit to once per FEE_HYSTERESIS_COOLDOWN_SEC.
    last_ts = int(signals.get("last_fee_update_ts") or 0)
    if last_ts and (now - last_ts) < FEE_HYSTERESIS_COOLDOWN_SEC:
        remaining = FEE_HYSTERESIS_COOLDOWN_SEC - (now - last_ts)
        return False, f"cooldown ({remaining // 60}m left)"

    return True, f"normal (Δ={delta:+d})"


def update_all_fees(dry_run=False):
    """Update fee policies on all channels.

    Pipeline per channel:
      1. Pin in fee_overrides? → use pin, done.
      2. base   = sigmoid(local_ratio)
      3. mult   = market_multiplier  (defense zone only allows positive)
      4. floor  = last_refill_ppm × REBALANCE_FEE_MARGIN (0 if never refilled)
      5. target = clamp( max(base*(1+mult), floor), 0, FEE_HARD_CEILING_PPM )
      6. Broadcast only if hysteresis permits.

    Pins below the rebalance floor are honoured but logged as a warning.
    Returns list of changes attempted.
    """
    now = int(time.time())
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    fee_report = lnd_client.get_fee_report()
    overrides = db.get_fee_overrides()

    current_fees = {}
    for item in fee_report.get("channel_fees", []):
        cp = item.get("channel_point", "")
        current_fees[cp] = {
            "base_fee_msat": int(item.get("base_fee_msat", 0)),
            "fee_rate_ppm": int(item.get("fee_per_mil", 0)),
        }

    # Whether any overfull channel exists to rebalance FROM — feeds the Layer-3
    # ladder's rebalance-vs-defend decision (computed once, not per channel).
    has_overfull_source = any(
        c.get("active") and c["local_ratio"] > REBALANCE_HIGH_THRESHOLD for c in channels
    )

    updates = []
    for ch in channels:
        chan_id = ch["chan_id"]
        cp = ch["channel_point"]
        signals = db.get_channel_signals(chan_id)
        old = current_fees.get(cp, {})
        old_ppm = old.get("fee_rate_ppm", 0)
        old_base = old.get("base_fee_msat", 0)
        # Inbound-fee decision (Layer 3). Defaults keep inbound untouched; only the
        # non-pin branch computes a real target, and only when the feature is on.
        inbound_target = None   # None ⇒ omit from the chanpolicy POST (no inbound change)
        inbound_changed = False

        pin = overrides.get(chan_id)
        if pin is not None:
            new_ppm = int(pin["pinned_ppm"])
            last_refill = db.get_last_refill_ppm(chan_id)
            floor = int(round(last_refill * REBALANCE_FEE_MARGIN)) if last_refill else 0
            if floor and new_ppm < floor:
                log.warning("fees: %s pin %d ppm is BELOW rebalance floor %d ppm — "
                            "you may be selling below refill cost",
                            ch["peer_alias"], new_ppm, floor)
            reason = f"manual pin: {new_ppm} ppm"
            target_info = {
                "target_ppm": new_ppm, "base_ppm": new_ppm, "mult": 0.0,
                "floor_ppm": floor, "source": "pin", "reason": reason,
            }
            broadcast = (new_ppm != old_ppm) or (old_base != FEE_BASE_MSAT)
            why = "pin enforced" if broadcast else "pin unchanged"
        else:
            last_forward_ts = db.get_last_forward_ts(chan_id)
            last_refill_ts = db.get_last_refill_ts(chan_id)
            # Fast-drain bump (up-only): a depleted channel dropping forwards for
            # lack of liquidity gets an immediate market-mult bump THIS cycle, so
            # its resting fee climbs after the first bad cycle instead of waiting
            # for the nightly ±STEP drift. The routine drift stays nightly.
            if ch["local_ratio"] < FEE_HYSTERESIS_EDGE_LOW:
                since_ts = int(signals.get("last_fee_update_ts") or 0) or (now - 7200)
                if db.count_forward_fails(chan_id, since_ts) > 0:
                    prev_mult = float(signals.get("market_multiplier", 0.0) or 0.0)
                    bumped = min(MARKET_MULT_MAX, prev_mult + MARKET_MULT_FASTDRAIN_STEP)
                    if bumped > prev_mult:
                        signals["market_multiplier"] = bumped
                        if not dry_run:
                            db.upsert_channel_signals(chan_id, market_multiplier=bumped)
                        log.info("fees: %s fast-drain bump mult %+.2f → %+.2f "
                                 "(dropped forwards, INSUFFICIENT_BALANCE)",
                                 ch["peer_alias"], prev_mult, bumped)

            target_info = compute_fee_target(ch, signals, now,
                                              last_forward_ts=last_forward_ts,
                                              last_refill_ts=last_refill_ts)
            new_ppm = target_info["target_ppm"]
            reason = target_info["reason"]
            broadcast, why = _should_broadcast(
                new_ppm, old_ppm, signals, ch["local_ratio"], now
            )

            # Persist soft-floor ratchet state every run (not just on broadcast) so
            # the level ratchets/holds/re-arms independent of broadcasts.
            if not dry_run:
                ratchet_state = {
                    "floor_decay_started_ts": int(target_info.get("floor_decay_started_ts") or 0),
                    "floor_armed_refill_ts": int(target_info.get("floor_armed_refill_ts") or 0),
                }
                level = target_info.get("floor_decay_anchor_ppm")
                if level is not None:
                    ratchet_state["floor_decay_anchor_ppm"] = level
                db.upsert_channel_signals(chan_id, **ratchet_state)

            # Layer 3: inbound-fee decision (only when the feature is enabled —
            # otherwise inbound stays untouched and this is fully inert).
            if INBOUND_FEE_ENABLED:
                from engine.rebalance_planner import get_channel_rebalance_budget
                budget_info = get_channel_rebalance_budget(chan_id)
                action_info = decide_channel_action(
                    ch, signals, budget_info, new_ppm, has_overfull_source, now)
                inbound_target = action_info["inbound_fee_ppm"]
                prev_inbound = int(signals.get("inbound_fee_ppm") or 0)
                inbound_changed = abs(inbound_target - prev_inbound) >= INBOUND_HYSTERESIS_PPM
                if inbound_changed:
                    log.info("fees: %s inbound %+d → %+d ppm [%s]",
                             ch["peer_alias"], prev_inbound, inbound_target,
                             action_info["action"])

        change = {
            "chan_id": chan_id,
            "channel_point": cp,
            "alias": ch["peer_alias"],
            "old_ppm": old_ppm,
            "new_ppm": new_ppm,
            "old_base": old_base,
            "new_base": FEE_BASE_MSAT,
            "local_ratio": ch["local_ratio"],
            "pinned": pin is not None,
            "source": target_info["source"],
            "base_ppm": target_info["base_ppm"],
            "mult": target_info["mult"],
            "floor_ppm": target_info["floor_ppm"],
            "broadcast": broadcast,
            "broadcast_reason": why,
            "inbound_ppm": inbound_target,
            "inbound_changed": inbound_changed,
        }

        # Outbound and inbound share one chanpolicy POST — broadcast if EITHER moved.
        if not (broadcast or inbound_changed):
            log.debug("fees: %s skip — %s (target %d, current %d)",
                      ch["peer_alias"], why, new_ppm, old_ppm)
            continue

        # When inbound fees are managed we must send the explicit inbound value on
        # every POST (LND 0.20 resets an omitted inbound_fee to 0). Pins keep
        # inbound untouched (operator-controlled outbound only).
        manage_inbound = INBOUND_FEE_ENABLED and pin is None
        inbound_arg = inbound_target if manage_inbound else None

        # INFO-level audit line for every broadcast so the log explains why
        # the fee moved (sigmoid / floor / pin) and how hysteresis allowed it.
        log.info("fees: %s %d → %d ppm%s — %s [hysteresis: %s]",
                 ch["peer_alias"], old_ppm, new_ppm,
                 f" inbound {inbound_arg:+d}" if inbound_arg is not None else "",
                 reason, why)

        if not dry_run:
            try:
                lnd_client.update_channel_policy(
                    cp, FEE_BASE_MSAT, new_ppm, inbound_fee_rate_ppm=inbound_arg)
                change["applied"] = True
            except Exception as e:
                change["applied"] = False
                change["error"] = str(e)

            db.save_fee_update(
                chan_id, ch["peer_alias"], old_ppm, new_ppm,
                old_base, FEE_BASE_MSAT, ch["local_ratio"], reason,
                new_inbound_ppm=inbound_arg,
            )
            # Stamp last_fee_update_ts + last_local_ratio for next-run hysteresis,
            # and the inbound state when we manage it.
            sig_update = {
                "last_fee_update_ts": now,
                "last_local_ratio": ch["local_ratio"],
            }
            if manage_inbound:
                sig_update["inbound_fee_ppm"] = inbound_target
                sig_update["inbound_fee_set_ts"] = now
            db.upsert_channel_signals(chan_id, **sig_update)
        else:
            change["applied"] = "dry_run"

        updates.append(change)

    if updates:
        applied = sum(1 for u in updates if u.get("applied") is True)
        log.info("fees: %d change(s) applied, %d failed",
                 applied, len(updates) - applied)
    else:
        log.info("fees: no broadcasts needed (all within hysteresis bands)")
    return updates


# ─── Market multiplier (nightly recompute) ──────────────────────

def compute_market_multiplier(chan_id, prev_mult):
    """Nudge the per-channel market multiplier based on observed forward activity.

    Slow-moving by design (±MARKET_MULT_STEP per recompute):
      - If forwarded in the last MARKET_MULT_BUSY_HOURS → nudge up.
      - If no forwards for MARKET_MULT_SILENT_DAYS → nudge down.
      - Otherwise unchanged.

    Clamped to [MARKET_MULT_MIN, MARKET_MULT_MAX]. The defense-zone
    asymmetry (multiplier can only RAISE the fee at low local) is enforced
    later, in compute_fee_target — the multiplier itself stays unconstrained.

    Returns (new_mult, reason) — reason is a short string for logging.
    """
    now = int(time.time())
    last_ts = db.get_last_forward_ts(chan_id)
    prev = float(prev_mult or 0.0)
    mult = prev

    if last_ts is not None:
        age_days = (now - last_ts) / 86400.0
        if (now - last_ts) <= MARKET_MULT_BUSY_HOURS * 3600:
            mult += MARKET_MULT_STEP
            reason = f"busy (forward {age_days*24:.1f}h ago)"
        elif (now - last_ts) >= MARKET_MULT_SILENT_DAYS * 86400:
            mult -= MARKET_MULT_STEP
            reason = f"silent ({age_days:.1f}d, ≥{MARKET_MULT_SILENT_DAYS}d)"
        else:
            reason = f"idle ({age_days:.1f}d, no nudge)"
    else:
        mult -= MARKET_MULT_STEP
        reason = "never forwarded"

    clamped = max(MARKET_MULT_MIN, min(MARKET_MULT_MAX, mult))
    delta = clamped - prev
    if abs(clamped - mult) > 1e-9:
        reason += " [clamped]"
    return clamped, f"{reason}, Δ={delta:+.2f}"


def recompute_all_signals():
    """Recompute slow signals for every active channel and write to channel_signals.

    Designed for a nightly cron. Cheap and idempotent. Doesn't touch fees
    or broadcast anything — just refreshes the cached inputs that the 2h
    pipeline reads.
    """
    now = int(time.time())
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    log.info("recompute_signals: processing %d channels", len(channels))

    results = []
    for ch in channels:
        chan_id = ch["chan_id"]
        prev = db.get_channel_signals(chan_id)
        prev_mult = float(prev.get("market_multiplier", 0.0) or 0.0)

        mult, mult_reason = compute_market_multiplier(chan_id, prev_mult)
        last_refill = db.get_last_refill_ppm(chan_id)
        failures = db.count_failures_since_last_success(chan_id)

        # Layer 1: stamp the structural-liquidity flag when a channel is judged
        # unprofitable to refill and keeps failing. First-stamp on entry, keep
        # the original timestamp thereafter, clear when it recovers. Local import
        # avoids a load-order cycle in engine/__init__.
        from engine.rebalance_planner import get_channel_rebalance_budget
        budget = get_channel_rebalance_budget(chan_id)
        prev_flag = int(prev.get("structural_flag_ts") or 0)
        structural_ts = (prev_flag or now) if budget["structural"] else 0
        # Alert once, on entry into the structural state — refilling this channel
        # is a losing trade; it needs a capital decision, not more rebalancing.
        if budget["structural"] and not prev_flag:
            alias = ch.get("peer_alias", chan_id[:12])
            ep = budget["earned_ppm"]
            db.save_alert(
                "structural_liquidity",
                f"{alias} structurally unprofitable to refill — earns "
                f"{ep:.0f} ppm, refill needs ≫ that ({budget['failures_since_success']} "
                f"fails). Consider more inbound / splice / close rather than rebalancing.",
                channel_id=chan_id,
            )

        db.upsert_channel_signals(
            chan_id,
            market_multiplier=mult,
            signals_updated_ts=now,
            structural_flag_ts=structural_ts,
        )
        results.append({
            "chan_id": chan_id,
            "alias": ch.get("peer_alias", ""),
            "last_refill_ppm": last_refill,
            "failures_since_success": failures,
            "earned_ppm": budget["earned_ppm"],
            "profit_capped": budget["profit_capped"],
            "structural": budget["structural"],
            "mult": mult,
            "mult_prev": prev_mult,
            "mult_reason": mult_reason,
        })
        log.info("recompute_signals: %s last_refill=%s ppm earned=%s ppm failures=%d "
                 "mult %+.2f → %+.2f (%s)%s",
                 ch.get("peer_alias", chan_id[:12]),
                 last_refill if last_refill is not None else "none",
                 f"{budget['earned_ppm']:.0f}" if budget["earned_ppm"] is not None else "unjudged",
                 failures, prev_mult, mult, mult_reason,
                 " [STRUCTURAL]" if budget["structural"] else "")

    return results
