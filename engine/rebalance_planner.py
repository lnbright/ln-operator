"""
LN Operator — Rebalance planner.

Pure planning logic: turn current channel balances into a list of
source→target pairs the executor can attempt. The planner is the only
place that decides how much each channel needs and what we're willing
to pay for it; the executor never invents a new pair on its own.

Each plan dict carries `target_total_deficit` and `source_total_surplus`
so the executor's dual-ledger guard (see rebalance_executor / main) can
decide when a target is satisfied or a source is exhausted without any
additional LND calls.
"""

from config import (
    REBALANCE_LOW_THRESHOLD, REBALANCE_HIGH_THRESHOLD, REBALANCE_TARGET,
    REBALANCE_MAX_AMOUNT_RATIO,
    REBALANCE_DEFAULT_BUDGET_PPM, REBALANCE_MAX_BUDGET_PPM,
    REBALANCE_BUDGET_ESCALATION_STEP,
    REBALANCE_PROFIT_HORIZON, REBALANCE_STRUCTURAL_FAIL_THRESHOLD,
    REBALANCE_QUERYROUTES_ENABLED, REBALANCE_QUERYROUTES_EARLYOUT_ENABLED,
    REBALANCE_QUERYROUTES_MIN_CHUNK_SATS,
)
import time

import lnd_client
import db
from engine.liquidity_policy import decide_channel_action
from logging_config import get_logger

log = get_logger('engine.rebalance_planner')


def get_channel_rebalance_budget(chan_id, local_ratio=None):
    """Max fee ppm we'll pay to refill this channel.

    Escalation (unchanged): bootstrap at REBALANCE_DEFAULT_BUDGET_PPM (or the last
    refill ppm) and walk up by ESCALATION_STEP per consecutive failure, capped at
    REBALANCE_MAX_BUDGET_PPM — this discovers price.

    Profitability gate (Layer 1): for channels with enough trailing OUT-volume to
    JUDGE, cap the budget at earned_ppm × REBALANCE_PROFIT_HORIZON — never pay more
    to refill than the channel can earn back within ~horizon fill/drain cycles.
    Channels we can't judge (earned_ppm is None) keep full escalation untouched —
    capping them would kill the price-discovery the escalation exists for.
    A judged channel whose escalation exceeds the profit cap is `profit_capped`;
    if it has also failed REBALANCE_STRUCTURAL_FAIL_THRESHOLD times it is
    `structural` (rebalancing is the wrong tool — needs the Layer-3 ladder/capital).

    Earn-ceiling accelerator: for a JUDGED channel whose anchor sits well below
    what it can profitably afford (a single lucky-cheap refill can poison
    `last_refill` to a value far under `earned_ppm`), plain escalation crawls up
    at STEP-of-a-tiny-base per run and never rediscovers the clearing price. So
    each failed run instead closes STEP of the gap between the anchor and the
    affordable ceiling (`min(profit_cap, MAX)`), reaching it in 1/STEP (=5) runs.
    It reuses ESCALATION_STEP (no new knob), only ever RAISES the budget (a max),
    only fires for judged channels, and climbs only up TO the ceiling — so it
    never creates a `profit_capped`/`structural` state and leaves unjudged price
    discovery untouched. It is inert unless `earned_ppm × horizon > 2 × anchor`.

    Recovery escape: `structural` describes a depleted channel that can't be
    profitably refilled. If `local_ratio` is supplied and has climbed back to
    REBALANCE_TARGET (≥50%), the channel is no longer depleted, so the structural
    verdict is stale and is cleared regardless of earnings/failure history — the
    flag-stamp callers then retire the alarm. Strong hysteresis: it trips below
    REBALANCE_LOW_THRESHOLD (20%) and only clears at ≥50%, so it can't flap.
    Callers without a live ratio omit it and keep the pre-existing behaviour.
    """
    last_refill = db.get_last_refill_ppm(chan_id)
    failures = db.count_failures_since_last_success(chan_id)
    earned_ppm, out_volume = db.get_channel_earned_ppm(chan_id)

    if last_refill is None:
        base = REBALANCE_DEFAULT_BUDGET_PPM
        anchor = "default"
    else:
        base = last_refill
        anchor = "last_refill"

    plain_escalated = int(round(base * (1.0 + REBALANCE_BUDGET_ESCALATION_STEP * failures)))
    escalated = plain_escalated

    profit_cap = None
    if earned_ppm is not None:
        profit_cap = earned_ppm * REBALANCE_PROFIT_HORIZON

    # The most this channel could ever justify paying: the profit cap for a judged
    # channel, else the hard MAX. This is the headroom the escalation ladder climbs
    # toward; the QueryRoutes probe may set the bid up TO here but no further.
    affordable_ceiling = (min(profit_cap, REBALANCE_MAX_BUDGET_PPM)
                          if profit_cap is not None else REBALANCE_MAX_BUDGET_PPM)

    # Earn-ceiling accelerator: when a JUDGED channel's anchor sits well below
    # what it can profitably afford — e.g. one lucky-cheap refill poisoned
    # last_refill to 7 ppm on a channel earning 576 — plain escalation crawls
    # up at STEP-of-a-tiny-base per run and effectively never rediscovers the
    # clearing price. Instead let each failed run close STEP of the gap between
    # the anchor and the affordable ceiling, reaching it in 1/STEP (=5) runs.
    # Reuses STEP — no new knob. It only ever RAISES escalated (a max), only for
    # judged channels, and only up TO the ceiling (= the profit cap), so it can
    # never create a profit_capped/structural state and never touches unjudged
    # price discovery. Inert when earnings don't justify it: the gap is only
    # positive once ceiling > 2·base (earned×horizon > 2·last_refill).
    accelerated = False
    if profit_cap is not None and failures > 0:
        ceiling = min(profit_cap, REBALANCE_MAX_BUDGET_PPM)
        gap_climb = base + (ceiling - base) * min(
            1.0, REBALANCE_BUDGET_ESCALATION_STEP * failures)
        escalated = max(escalated, int(round(gap_climb)))
        accelerated = escalated > plain_escalated

    budget = escalated
    if profit_cap is not None:
        budget = min(budget, int(round(profit_cap)))
    budget = min(budget, REBALANCE_MAX_BUDGET_PPM)

    # `profit_capped` asks whether failure-escalation ALONE wants to bid past the
    # affordable cap — use the plain value, not the accelerated one. The
    # accelerator deliberately climbs UP TO the ceiling (= the cap), and rounding
    # `gap_climb` can land it a hair above the unrounded `profit_cap`; counting
    # that as "capped" would spuriously strand the very profitable channel the
    # accelerator exists to rescue. plain_escalated > profit_cap and the
    # accelerator firing are mutually exclusive (the accelerator's max() is a
    # no-op once plain escalation already exceeds the ceiling).
    profit_capped = profit_cap is not None and plain_escalated > profit_cap
    structural = profit_capped and failures >= REBALANCE_STRUCTURAL_FAIL_THRESHOLD
    recovered = (structural and local_ratio is not None
                 and local_ratio >= REBALANCE_TARGET)
    if recovered:
        structural = False   # liquidity recovered → structural alarm is stale

    if profit_capped:
        reason = (f"{anchor} {base} ppm escalated {escalated} capped to "
                  f"earn×{REBALANCE_PROFIT_HORIZON:g}={int(round(profit_cap))} ppm [profit gate]")
        if structural:
            reason += f" — STRANDED ({failures} failed runs)"
        elif recovered:
            reason += f" — stranded cleared (local {local_ratio:.0%} ≥ target)"
    elif accelerated:
        reason = (f"{anchor} {base} ppm accelerated toward earn-ceiling "
                  f"({REBALANCE_BUDGET_ESCALATION_STEP:.0%} of gap × {failures} "
                  f"failed runs) → {budget} ppm")
    elif failures > 0:
        reason = (f"{anchor} {base} ppm × (1 + {REBALANCE_BUDGET_ESCALATION_STEP:.0%}"
                  f" × {failures} failed runs) → {budget} ppm")
    else:
        reason = f"{anchor} {base} ppm → {budget} ppm"

    return {
        "max_fee_ppm": budget,
        "reason": reason,
        "last_refill_ppm": last_refill,
        "failures_since_success": failures,
        "earned_ppm": earned_ppm,
        "out_volume_sats": out_volume,
        "escalated_ppm": escalated,
        "affordable_ceiling_ppm": int(round(affordable_ceiling)),
        "profit_capped": profit_capped,
        "structural": structural,
        "accelerated": accelerated,
    }


def _queryroutes_probe(target_ch, budget, sources, run_id, record):
    """ONE min-chunk QueryRoutes dry-run per source — drives BOTH the
    infeasibility early-out AND the bid, from the same probes.

    For a depleted target it probes every overfull source at the MINIMUM chunk
    (`REBALANCE_QUERYROUTES_MIN_CHUNK_SATS`; smallest amount = strictly the easiest
    to route) capped at the affordable ceiling, then:

      - if ≥1 source has a live route → FEASIBLE. Price the bid off the CHEAPEST
        feasible source (raise the budget up to its live cost, bounded by the
        ceiling) and rank the sources cheapest-first so the executor pays the
        cheapest, not just the most-overfull.
      - if NO source routes AND every probe gave a DEFINITE no-route → INFEASIBLE.
        Refilling is a capital problem, not price discovery: drop the channel and,
        on a real run, record a synthetic failed cycle (QR_NO_AFFORDABLE_ROUTE) so
        the structural ladder still advances. The early-out replaces the wasted
        attempts, NOT the stranding they would eventually trigger.

    Why probe *every* source, not just the most-overfull: feasibility is
    existential (ONE working source proves it) but infeasibility is universal (ALL
    must fail). A single source's "no route" can't justify dropping a channel a
    different source could refill, and the drop is consequential (it advances
    stranding). Why the min chunk for the bid too: ppm is amount-dependent (a fixed
    base fee amortises over fewer sats), so the 100k price is the worst case — a
    safe upper bound for the cap that still lets larger/whole-amount routes settle
    under it, and one probe covers chunked refills a full-amount probe would miss.

    Returns {"drop": bool, "budget": <budget dict>, "source_order": [chan_id, …]}.

    Conservative by construction:
      - JUDGED only — unjudged keeps full price discovery via real attempts.
      - a probe that's UNAVAILABLE (LND down) is treated as UNKNOWN, never as
        no-route → a transport blip can never strand a channel.
      - records the synthetic cycle only on a real run (`record`).
      - never bids above the ceiling; the bid only ever RAISES (a max).
    """
    default_order = [s["chan_id"] for s in sources]
    keep = {"drop": False, "budget": budget, "source_order": default_order}

    # REBALANCE_QUERYROUTES_ENABLED gates the probe (pricing + cheapest-first
    # ranking — pure upside, never strands). The drop/strand on an all-no-route
    # verdict is separately gated below by REBALANCE_QUERYROUTES_EARLYOUT_ENABLED.
    if (not REBALANCE_QUERYROUTES_ENABLED or not sources
            or budget.get("earned_ppm") is None):
        return keep  # disabled, no source, or unjudged → plan normally
    ceiling = budget.get("affordable_ceiling_ppm")
    if not ceiling:
        return keep

    amount = REBALANCE_QUERYROUTES_MIN_CHUNK_SATS
    ceiling_sats = max(1, int(amount * ceiling / 1_000_000))
    routed = []      # (source, cost_ppm) for sources with a live route ≤ ceiling
    unavailable = 0  # probes that errored — UNKNOWN, never counted as no-route
    for s in sources:
        try:
            probe = lnd_client.query_routes(
                target_ch["peer_pubkey"], amount,
                fee_limit_sat=ceiling_sats,
                outgoing_chan_id=s["chan_id"],
                raise_on_error=True)
        except Exception as e:
            log.debug("probe unavailable for %s via %s: %s",
                      target_ch["peer_alias"], s["peer_alias"], e)
            unavailable += 1
            continue
        if probe is not None:
            routed.append((s, probe["fee_ppm"]))

    if routed:
        # Feasible. Rank sources cheapest-first (probed by cost, then any
        # unprobed/errored sources after, preserving the overfull order) and price
        # the bid off the cheapest.
        routed.sort(key=lambda r: r[1])
        ranked = [s["chan_id"] for s, _ in routed]
        ranked += [sid for sid in default_order if sid not in set(ranked)]
        cheapest = routed[0][1]
        current = budget["max_fee_ppm"]
        out = budget
        if current < cheapest <= ceiling:
            out = dict(budget)
            out["max_fee_ppm"] = cheapest
            out["reason"] = budget["reason"] + (
                f" → QueryRoutes set to {cheapest} ppm "
                f"(cheapest of {len(routed)} live source(s) ≤ ceiling)")
            log.info("rebalance: %s bid set to %d ppm via QueryRoutes "
                     "(cheapest of %d feasible source(s), ceiling %d)",
                     target_ch["peer_alias"], cheapest, len(routed), ceiling)
        return {"drop": False, "budget": out, "source_order": ranked}

    if unavailable:
        # No source routed, but some probe was unavailable → can't prove the
        # universal "no source can route". Never strand on a transport blip.
        log.debug("rebalance: %s — no route found but %d probe(s) unavailable; "
                  "planning normally (won't strand on a blip)",
                  target_ch["peer_alias"], unavailable)
        return keep

    # Every source returned a DEFINITE no-route → infeasible. Only strand if the
    # early-out is enabled; otherwise leave the channel to attempt normally.
    if not REBALANCE_QUERYROUTES_EARLYOUT_ENABLED:
        log.debug("rebalance: %s — no affordable route via any source, but early-out "
                  "disabled; planning normally", target_ch["peer_alias"])
        return keep
    log.info("rebalance: early-out %s — no route ≤ %d ppm ceiling via ANY of %d "
             "source(s) at min-chunk; skipping%s", target_ch["peer_alias"], ceiling,
             len(sources), ", recording infeasible cycle" if record else " (dry-run, not recorded)")
    if record:
        # Attribute the synthetic cycle to the most-overfull source (the one we'd
        # have tried first), as the per-cycle failure marker.
        s0 = sources[0]
        db.save_rebalance_attempt(
            s0["chan_id"], target_ch["chan_id"],
            s0["peer_alias"], target_ch["peer_alias"],
            amount=amount, fee_paid=0, success=False,
            failure_reason="QR_NO_AFFORDABLE_ROUTE",
            budget_ppm=ceiling, run_id=run_id, triggered_by="auto")
    return {"drop": True, "budget": budget, "source_order": default_order}


def find_rebalance_candidates(channels=None, force=None):
    """Identify channels that need rebalancing.

    Returns two lists:
    - needs_inbound: channels with local_ratio < LOW threshold (depleted, need sats back)
    - needs_outbound: channels with local_ratio > HIGH threshold (overfull, can donate sats)

    force: if set to a float (0.0-1.0), ignore thresholds and use that value as the target.
           Any channel below force% is inbound, above force% is outbound.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound = []
    needs_outbound = []

    for ch in channels:
        if not ch["active"]:
            continue
        if force is not None:
            # Ignore thresholds — use the specified target ratio
            if ch["local_ratio"] < force:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > force:
                needs_outbound.append(ch)
        else:
            if ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
                needs_inbound.append(ch)
            elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
                needs_outbound.append(ch)

    needs_inbound.sort(key=lambda c: c["local_ratio"])
    needs_outbound.sort(key=lambda c: c["local_ratio"], reverse=True)

    return needs_inbound, needs_outbound


def calculate_rebalance_amount(channel, direction="inbound", target_ratio=None):
    """Calculate how many sats to move for a channel.

    direction='inbound':  channel is depleted, we want to push sats to local side
    direction='outbound': channel is overfull, we want to push sats to remote side
    target_ratio: override the default REBALANCE_TARGET (e.g. from --force 0.4)
    """
    capacity = channel["capacity"]
    local = channel["local_balance"]
    target_local = int(capacity * (target_ratio if target_ratio is not None else REBALANCE_TARGET))
    max_amount = int(capacity * REBALANCE_MAX_AMOUNT_RATIO)

    if direction == "inbound":
        # Need to increase local balance
        amount = target_local - local
    else:
        # Need to decrease local balance
        amount = local - target_local

    # Cap at max ratio
    amount = min(amount, max_amount)
    # Minimum useful rebalance: 50k sats
    if amount < 50_000:
        return 0
    return amount


def plan_rebalances(channels=None, force=None, record_early_outs=False):
    """Create a rebalancing plan with primary and fallback pairs.

    Generates ALL possible source→target pairs, ordered by priority.
    Primary plans: most depleted target paired with most overfull source.
    Fallback plans: alternative pairings for each target, tried if the
    primary pair fails at execution time (e.g. no route between those nodes).

    This means if a primary source→target pair fails due to no route, the
    executor will try the same source against an alternative target before
    giving up on the run.

    force: if set to a float (0.0-1.0), ignore thresholds and use that ratio as target.
    Returns a (plans, reason) tuple.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    needs_inbound, needs_outbound = find_rebalance_candidates(channels, force=force)

    # Use force target ratio if specified, otherwise use config default
    rebalance_target = force if force is not None else REBALANCE_TARGET

    # Layer 1/3 gate: drop targets the liquidity ladder says we should NOT pay to
    # refill (judged structurally unprofitable, or being defended with an inbound
    # discount instead). This is where the profit gate actually stops the grind —
    # the channel falls out of planning. `force` is an explicit operator override,
    # so honour it and skip the gate. outbound_ppm doesn't affect the rebalance
    # verdict, so 0 is fine here.
    # target_budgets/target_source_order are filled by the QueryRoutes probe below
    # (or, in force mode, with the plain budget + the default overfull order).
    target_budgets = {}
    target_source_order = {}

    if force is None and needs_inbound:
        now = int(time.time())
        has_source = bool(needs_outbound)
        run_id = int(time.time())  # one cycle id for this run's early-outs
        kept = []
        for ch in needs_inbound:
            budget = get_channel_rebalance_budget(ch["chan_id"])
            signals = db.get_channel_signals(ch["chan_id"])
            act = decide_channel_action(ch, signals, budget, 0, has_source, now)
            if act["action"] != "rebalance":
                log.info("rebalance: skipping %s (%.0f%% local) — %s",
                         ch["peer_alias"], ch["local_ratio"] * 100, act["reason"])
                continue
            # ONE min-chunk QueryRoutes probe per source — drops the channel if
            # NO source has an affordable route (and records the cycle), otherwise
            # prices the bid off the cheapest feasible source and ranks the sources
            # cheapest-first for execution. See _queryroutes_probe.
            verdict = _queryroutes_probe(ch, budget, needs_outbound, run_id, record_early_outs)
            if verdict["drop"]:
                continue
            target_budgets[ch["chan_id"]] = verdict["budget"]
            target_source_order[ch["chan_id"]] = verdict["source_order"]
            kept.append(ch)
        needs_inbound = kept

    log.info("rebalance: %d depleted, %d overfull channel(s) found",
             len(needs_inbound), len(needs_outbound))
    for ch in needs_inbound:
        log.info("  depleted: %s at %.0f%% local", ch["peer_alias"], ch["local_ratio"] * 100)
    for ch in needs_outbound:
        log.info("  overfull: %s at %.0f%% local", ch["peer_alias"], ch["local_ratio"] * 100)

    if not needs_inbound and not needs_outbound:
        return [], "all channels balanced"
    if not needs_inbound:
        return [], (f"{len(needs_outbound)} channel(s) overfull but none depleted — "
                    f"no circular rebalance possible")
    if not needs_outbound:
        depleted = ", ".join(f"{c['peer_alias']} ({c['local_ratio']:.0%})" for c in needs_inbound)
        return [], (f"{len(needs_inbound)} channel(s) depleted ({depleted}) but no overfull "
                    f"channel to rebalance from — need more channels or top up on-chain")

    # Force mode skips the gate + QueryRoutes probe, so fill any target the probe
    # didn't price with the plain budget and the default (most-overfull) order.
    source_by_id = {s["chan_id"]: s for s in needs_outbound}
    for target_ch in needs_inbound:
        tid = target_ch["chan_id"]
        if tid not in target_budgets:
            target_budgets[tid] = get_channel_rebalance_budget(tid)
        target_source_order.setdefault(tid, [s["chan_id"] for s in needs_outbound])

    def _ordered_sources(target_ch):
        # Sources for this target, cheapest-feasible-first when the probe ranked
        # them (cheapest-first), else most-overfull-first. Skips any id no longer present.
        return [source_by_id[sid] for sid in target_source_order[target_ch["chan_id"]]
                if sid in source_by_id]

    # Generate ALL possible source→target pairs, ordered by priority:
    # most depleted targets first, each paired with all available sources.
    # The executor tries pairs in order — if one fails, the next pair is tried.
    # This ensures that if source→targetA fails, source→targetB is tried next.
    plans = []
    used_source_sats = {}  # track how much each source has committed

    for target_ch in needs_inbound:
        target_amount = calculate_rebalance_amount(target_ch, "inbound", target_ratio=rebalance_target)
        if target_amount <= 0:
            continue

        budget = target_budgets[target_ch["chan_id"]]
        max_fee_ppm = budget["max_fee_ppm"]

        # Try ALL sources for this target — a single source may not have
        # enough capacity to fully restore the target. Multiple sources can
        # each contribute their share. Cheapest feasible source first.
        remaining_target = target_amount
        for source_ch in _ordered_sources(target_ch):
            if remaining_target < 50_000:
                break  # target nearly satisfied

            source_total = calculate_rebalance_amount(source_ch, "outbound", target_ratio=rebalance_target)
            already_used = used_source_sats.get(source_ch["chan_id"], 0)
            source_available = source_total - already_used

            if source_available < 50_000:
                continue

            amount = min(remaining_target, source_available)
            max_fee = int(amount * max_fee_ppm / 1_000_000 * 1.1)

            log.info("rebalance plan: %s→%s %s sats [%d ppm cap — %s]",
                     source_ch["peer_alias"], target_ch["peer_alias"],
                     f"{amount:,}", max_fee_ppm, budget["reason"])

            plans.append({
                "source_chan_id": source_ch["chan_id"],
                "source_alias": source_ch["peer_alias"],
                "source_channel_point": source_ch["channel_point"],
                "source_local_ratio": source_ch["local_ratio"],
                "source_total_surplus": source_total,
                "target_chan_id": target_ch["chan_id"],
                "target_alias": target_ch["peer_alias"],
                "target_channel_point": target_ch["channel_point"],
                "target_peer_pubkey": target_ch["peer_pubkey"],
                "target_local_ratio": target_ch["local_ratio"],
                "target_total_deficit": target_amount,
                "amount_sats": amount,
                "max_fee_sats": max_fee,
                "max_fee_ppm": max_fee_ppm,
                "budget_reason": budget["reason"],
            })

            # Reserve this source capacity and reduce target remaining
            used_source_sats[source_ch["chan_id"]] = already_used + amount
            remaining_target -= amount

    # ── Fallback plans ───────────────────────────────────────────
    # For each target, add alternative source pairings.
    # These are only executed if the primary plan for that target fails.
    # This ensures the engine tries all available routes before giving up.
    primary_pairs = {(p["source_chan_id"], p["target_chan_id"]) for p in plans}
    for target_ch in needs_inbound:
        target_amount = calculate_rebalance_amount(target_ch, "inbound")
        if target_amount <= 0:
            continue
        budget = target_budgets[target_ch["chan_id"]]
        max_fee_ppm = budget["max_fee_ppm"]

        for source_ch in _ordered_sources(target_ch):
            pair_key = (source_ch["chan_id"], target_ch["chan_id"])
            if pair_key in primary_pairs:
                continue  # already in the primary plan

            source_available = calculate_rebalance_amount(source_ch, "outbound")
            if source_available < 50_000:
                continue

            amount = min(target_amount, source_available)
            max_fee = int(amount * max_fee_ppm / 1_000_000 * 1.1)

            log.info("rebalance fallback: %s→%s %s sats [%d ppm cap — %s]",
                     source_ch["peer_alias"], target_ch["peer_alias"],
                     f"{amount:,}", max_fee_ppm, budget["reason"])

            plans.append({
                "source_chan_id": source_ch["chan_id"],
                "source_alias": source_ch["peer_alias"],
                "source_channel_point": source_ch["channel_point"],
                "source_local_ratio": source_ch["local_ratio"],
                "source_total_surplus": source_available,
                "target_chan_id": target_ch["chan_id"],
                "target_alias": target_ch["peer_alias"],
                "target_channel_point": target_ch["channel_point"],
                "target_peer_pubkey": target_ch["peer_pubkey"],
                "target_local_ratio": target_ch["local_ratio"],
                "target_total_deficit": target_amount,
                "amount_sats": amount,
                "max_fee_sats": max_fee,
                "max_fee_ppm": max_fee_ppm,
                "budget_reason": budget["reason"],
                "is_fallback": True,
            })

    return plans, None
