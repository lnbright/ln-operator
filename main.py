#!/usr/bin/env python3
"""
LN Operator — Main CLI
Channel management cron jobs and investment advisor.

Usage:
    ln-operator pipeline [--dry-run]            — run full pipeline (for crontab)
    ln-operator adjust_fees [--dry-run]         — adjust channel fee rates
    ln-operator rebalance_channels [--dry-run]  — rebalance depleted/overfull channels
    ln-operator manual_rebalance <src> <tgt> <amount_sats> <max_ppm> [--dry-run] — pin one pair (recorded as manual)
    ln-operator sync_routing                    — sync routing events from LND
    ln-operator healthcheck                     — check channel health + fire alerts
    ln-operator backup [--trigger ...]          — push channel.backup off-site (called by systemd)
    ln-operator plan [--min-channel SATS] [--treasury RATIO] — channel plan
    ln-operator status                          — quick node overview
    ln-operator history [days]                  — recent activity from database
    ln-operator overwrite_fee <chan> <ppm> [--note]   — pin a channel's outbound fee
    ln-operator clear_fee <chan>                — remove a fee pin (pins are shown by `status`)
"""

import sys
import argparse
import time
from datetime import datetime

import db
import engine
from config import REBALANCE_LOW_THRESHOLD, REBALANCE_HIGH_THRESHOLD, REBALANCE_MAX_AMOUNT_RATIO
from logging_config import setup_logging, get_logger, set_console_level
import advisor
import telegram_bot
import lnd_client
import backup


def cmd_plan(args):
    """Channel investment planner — reads wallet balance from LND, proposes channel allocation."""
    import config as _cfg
    log = get_logger("main")

    # Apply CLI overrides
    if args.min_channel:
        _cfg.PREFERRED_CHANNEL_SIZE_SATS = args.min_channel
        log.info("min channel size overridden to %s sats", f"{args.min_channel:,}")
    if args.treasury is not None:
        if not 0.0 <= args.treasury <= 1.0:
            print(f"Error: --treasury must be between 0.0 and 1.0 (got {args.treasury})")
            sys.exit(1)
        _cfg.TREASURY_MIN_RATIO = args.treasury
        log.info("treasury ratio overridden to %.1f%%", args.treasury * 100)

    min_channel = _cfg.PREFERRED_CHANNEL_SIZE_SATS
    treasury_ratio = _cfg.TREASURY_MIN_RATIO

    print(f"\n⚡ LN Operator — Channel Plan")
    print("=" * 55)

    # ── Step 1: Read wallet balance from LND ─────────────────────
    onchain = lnd_client.get_onchain_balance()
    total_balance     = int(onchain.get("confirmed_balance", 0))
    anchor_reserved   = int(onchain.get("reserved_balance_anchor_chan", 0))


    # ── Step 2: Get fee rate from LND ────────────────────────────
    fee_rate = lnd_client.estimate_fee(conf_target=2) or 3
    log.info("fee rate: %d sat/vB", fee_rate)

    # ── Step 3: Calculate how many channels we can open ──────────
    # First pass: estimate 2 channels to get treasury + anchor costs
    # Then refine based on what actually fits
    max_channels = 10  # never suggest more than 10 at once

    best_num = 0
    best_channel_size = 0
    best_deployable = 0
    best_breakdown = {}

    for num_ch in range(1, max_channels + 1):
        # Costs for this number of channels
        new_anchor = min(
            num_ch * _cfg.ANCHOR_RESERVE_PER_CHANNEL,
            max(0, _cfg.ANCHOR_RESERVE_MAX - anchor_reserved)
        )
        open_fees = num_ch * fee_rate * 250  # 250 vBytes per channel open tx
        treasury  = int(total_balance * treasury_ratio)

        deployable = total_balance - anchor_reserved - new_anchor - open_fees - treasury
        channel_size = deployable // num_ch if num_ch > 0 else 0

        if channel_size >= min_channel:
            best_num         = num_ch
            best_channel_size = channel_size
            best_deployable  = deployable
            best_breakdown   = {
                "treasury":    treasury,
                "new_anchor":  new_anchor,
                "open_fees":   open_fees,
                "deployable":  deployable,
                "num_channels": num_ch,
                "channel_size": channel_size,
            }
        else:
            break  # adding more channels makes each one too small — stop here

    def _print_breakdown(num_ch, treasury, new_anchor, open_fees, deployable, fee_rate):
        """Print the full cost breakdown."""
        print(f"\n  {'─'*45}")
        print(f"  Wallet balance:           {total_balance:>12,} sats")
        print(f"  Existing anchor reserve:  {anchor_reserved:>12,} sats  (already locked by LND)")
        print(f"  {'─'*45}")
        print(f"  Available:                {total_balance - anchor_reserved:>12,} sats")
        print()
        print(f"  Treasury ({treasury_ratio:.1%}):          {treasury:>12,} sats  ({total_balance:,} × {treasury_ratio:.1%})")
        print(f"  New anchor reserve:       {new_anchor:>12,} sats  ({num_ch} × {_cfg.ANCHOR_RESERVE_PER_CHANNEL:,} per channel)")
        print(f"  Channel open fees:        {open_fees:>12,} sats  ({num_ch} × {fee_rate} sat/vB × 250 vB)")
        print(f"  {'─'*45}")
        print(f"  Deployable:               {deployable:>12,} sats")

    if best_num == 0:
        # Can't even afford one minimum-size channel — show full breakdown anyway
        treasury  = int(total_balance * treasury_ratio)
        new_anchor = min(_cfg.ANCHOR_RESERVE_PER_CHANNEL,
                         max(0, _cfg.ANCHOR_RESERVE_MAX - anchor_reserved))
        open_fees  = fee_rate * 250
        deployable = total_balance - anchor_reserved - new_anchor - open_fees - treasury
        _print_breakdown(1, treasury, new_anchor, open_fees, deployable, fee_rate)
        print(f"\n  ⚠️  Insufficient balance for even one {min_channel:,} sat channel.")
        print(f"  Need at least {min_channel + treasury + new_anchor + open_fees + anchor_reserved:,} sats in wallet.")
        # Don't return — still show candidates for future planning

    if best_num > 0:
        bd = best_breakdown
        _print_breakdown(best_num, bd["treasury"], bd["new_anchor"], bd["open_fees"], bd["deployable"], fee_rate)
        print(f"\n  → {best_num} channel(s) at {best_channel_size:,} sats each")

        log.info("plan: %d channel(s) at %s sats each (deployable %s)",
                 best_num, f"{best_channel_size:,}", f"{bd['deployable']:,}")

    # ── Step 4: Tier-segmented candidates (centrality prefilter → diversity rerank) ─
    # Two-stage ranking — see advisor._rerank_tiers_by_diversity. Each tier is
    # ranked independently because a small node's peers are obscure leaves
    # (high diversity) while a hub's peers overlap heavily with yours (low
    # diversity); a single global ranking would just surface backwater nodes.
    # Runs only on demand here — the live graph enrichment (one LND call per
    # candidate) is too slow for the 2h pipeline.
    print(f"\n  {'─'*40}")

    try:
        print("  1) Retrieving network graph from LND (multi-MB dump, it may take 30-60s)...", flush=True)
        state = advisor._gather_node_state()
        candidates = advisor._fetch_candidates_from_graph(state)
        candidates = advisor._enrich_with_1ml_aliases(candidates)

        n_hub = sum(1 for c in candidates if c.get("tier_hint") == "hub")
        n_mid = sum(1 for c in candidates if c.get("tier_hint") == "mid-tier")
        n_sml = sum(1 for c in candidates if c.get("tier_hint") == "small")
        print(f"     → {len(candidates)} candidates after tier filter "
              f"({n_hub} hub, {n_mid} mid-tier, {n_sml} small).", flush=True)

        print("  2) Ranking by centrality (channels + capacity, log-normalised)...", flush=True)
        scored = advisor._score_candidates(candidates, state)  # stage 1: centrality

        # Portfolio context — informational only, doesn't drive what's shown
        portfolio = advisor._classify_existing_portfolio(state["channels"], scored)
        print(f"     Existing portfolio: {portfolio['hub_count']} hub connection(s), "
              f"{portfolio['mid_tier_count']} mid-tier connection(s)")

        # Stage 2: enrich top N per tier, rerank by diversity. Served from the
        # graph cache when present (no round-trips), else live get_node_info.
        print(f"  3) Enriching top {advisor.ENRICH_PER_TIER}/tier and reranking by "
              f"diversity vs. 2-hop horizon...", flush=True)
        hubs, mid_tier, small = advisor._rerank_tiers_by_diversity(scored, state)

        print(f"  4) Presenting top {advisor.SHOW_PER_TIER}/tier...", flush=True)

        log.info("plan candidates: %d hub, %d mid-tier, %d small (after diversity rerank)",
                 len(hubs), len(mid_tier), len(small))

        def _print_candidates(pool, label):
            print(f"\n  {label}:\n")
            for i, c in enumerate(pool, 1):
                gd = c.get("graph_data") or {}
                div = gd.get("diversity_score")
                div_str = f"{div:.0%}" if div is not None else "  n/a"

                avg = c.get("avg_channel_size", 0)
                avg_str = f"{avg//1_000_000}M" if avg >= 1_000_000 else f"{avg//1_000}k" if avg >= 1_000 else str(avg)
                fee = c.get("avg_fee_ppm", 0)
                fee_str = f"{fee:>4}ppm" if fee < 100_000 else "  n/a"

                alias = c.get("alias", "")
                pubkey = c.get("pubkey", "unknown")
                no_alias = not alias or alias == pubkey[:len(alias)]
                display_name = alias[:26] if not no_alias else pubkey[:26]

                print(f"  {i:2}. {display_name:<26} | div {div_str:>4} "
                      f"| {c['channel_count']:>4} ch "
                      f"| avg {avg_str:>5} "
                      f"| fee {fee_str:>8} "
                      f"| rank #{c.get('network_rank','?')}")
                if no_alias:
                    print(f"      pk: {pubkey}")

        tier_labels = [
            (hubs,     f"Hubs (≥{advisor.HUB_MIN_CHANNELS} channels) — top {len(hubs)} by diversity"),
            (mid_tier, f"Mid-tier ({advisor.MID_MIN_CHANNELS}-{advisor.HUB_MIN_CHANNELS - 1} channels) — top {len(mid_tier)} by diversity"),
            (small,    f"Small ({advisor.SMALL_MIN_CHANNELS}-{advisor.MID_MIN_CHANNELS - 1} channels) — top {len(small)} by diversity"),
        ]
        for pool, label in tier_labels:
            if pool:
                _print_candidates(pool, label)
    except Exception as e:
        log.error("could not fetch candidates: %s", e)
        print(f"  Error fetching candidates: {e}")


    # ── Step 5: Save to DB ────────────────────────────────────────
    if best_num > 0:
        db.save_investment_plan(
            total_balance, best_breakdown["treasury"], best_breakdown["deployable"],
            {"num_channels": best_num, "channel_size": best_channel_size,
             "breakdown": best_breakdown, "fee_rate": fee_rate}
        )

    # ── Step 6: Offer to generate a deposit address ───────────────
    print(f"\n  {'─'*45}")
    try:
        answer = input("  Generate a deposit address to top up wallet? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer == "y":
        try:
            addr_type_input = input("  Address type: [1] Native segwit p2wkh  [2] Taproot p2tr  (default: 1) ").strip()
        except (EOFError, KeyboardInterrupt):
            addr_type_input = "1"

        if addr_type_input == "2":
            addr_type = "p2tr"
            addr_label = "taproot"
        else:
            addr_type = "p2wkh"
            addr_label = "native segwit"

        address = lnd_client.new_address(addr_type)
        if address:
            print(f"\n  Deposit address ({addr_label}):")
            print(f"  {address}\n")

            # Generate QR code using only stdlib — print as ASCII blocks in terminal
            try:
                import urllib.request
                import base64
                # Use a QR code terminal renderer via qrencode if available
                import subprocess
                result = subprocess.run(
                    ["qrencode", "-t", "UTF8", "-o", "-", address],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    print(result.stdout)
                else:
                    raise FileNotFoundError
            except (FileNotFoundError, subprocess.TimeoutExpired):
                # qrencode not available — show manual QR options
                print(f"  Scan on Amboss:  https://amboss.space/node/{address}")
                print(f"  Or install qrencode for terminal QR: sudo apt install qrencode")
        else:
            print("  Error: could not generate address from LND")



def cmd_monitor_htlcs(args):
    """Run the always-on HTLC failure monitor (blocks; for systemd)."""
    import htlc_monitor
    htlc_monitor.run()


def cmd_refresh_graph(args):
    """Pull the network graph and refresh the cache.

    describe_graph() is a multi-MB pull (up to ~300s on the Pi), so this runs from
    a daily cron (ahead of the daily-check) rather than inline. The daily agent and
    the peer-finder read the cached digest instead of re-pulling LND.
    """
    import graph_cache
    log = get_logger("main")
    log.info("refresh_graph: starting")
    print("\n⚡ LN Operator — Refresh Network Graph Cache")
    print("=" * 45)
    digest = graph_cache.refresh()
    s = digest["stats"]
    print(f"  nodes:          {s['total_nodes']:,}")
    print(f"  channels:       {s['total_channels']:,}")
    print(f"  total capacity: {s['total_capacity']:,} sats")
    print(f"  our peers:      {len(digest['our_peers'])}")
    print(f"  2-hop reach:    {len(digest['reachable_2hop']):,} nodes")
    print(f"  cached to:      {graph_cache.CACHE_PATH}")


def cmd_suggest_peers(args):
    """Suggest peers to open a channel to so refills toward a target get cheaper.

    Stage 1 reads the cached graph (the target's neighbours, scored by hub
    quality); stage 2 validates each finalist with a live QueryRoutes probe (cheapest
    route FROM the candidate TO the target, source_pubkey=candidate — the path a refill
    would take after we open to it). An empty result means resize/close, not open.
    """
    import graph_cache
    import peer_finder
    digest = graph_cache.load()
    if not digest:
        print("  No graph cache — run `ln-operator refresh_graph` first.")
        return
    target = peer_finder.resolve_target(args.target, digest)
    if not target:
        print(f"  No node matching '{args.target}' in the graph cache.")
        return
    tnode = digest["nodes"].get(target, {})
    age_h = (graph_cache.age_seconds() or 0) // 3600
    print(f"\n⚡ Peers to open toward {tnode.get('alias', target[:12])} "
          f"({target[:12]}…) — cheaper refills into this sink")
    print(f"   graph cache {age_h}h old, target has {tnode.get('channels', 0)} channels")
    print("=" * 66)
    results = peer_finder.suggest_peers_for(target, digest=digest,
                                            validate=not args.no_validate)
    if not results:
        print("  No viable peer with a cheap live route to this target.")
        print("  → capital answer is resize/close, not open (or refresh the graph).")
        return
    for r in results:
        route = (f"route {r['route_ppm']}ppm/{r['route_hops']}h"
                 if "route_ppm" in r else "(unvalidated)")
        print(f"  {r['alias'][:22]:22} {r['channels']:>5}ch "
              f"{r['capacity'] // 1_000_000:>5}M  fee~{r['avg_fee_ppm']:>4}  "
              f"reach+{r['diversity']:.0%}  {route}")
        print(f"      {r['pubkey']}")


def cmd_recompute_signals(args):
    """Refresh slow per-channel signals (market multiplier).

    Designed for a nightly cron. Reads from forwarding_log and writes
    market_multiplier to channel_signals. The rebalance budget and fee floor
    are derived live from rebalance_log on every 2h pipeline run — no caching.
    """
    log = get_logger("main")
    log.info("recompute_signals: starting")
    print("\n⚡ LN Operator — Refresh Channel Signals")
    print("=" * 45)

    results = engine.recompute_all_signals()

    if not results:
        print("No channels found.")
        return

    print(f"\n{'Peer':<24} {'Last Refill':>12} {'Failures':>9} {'Mult':>16}  {'Why':<36}")
    print("─" * 102)
    for r in results:
        refill = f"{r['last_refill_ppm']} ppm" if r['last_refill_ppm'] is not None else "—"
        mult_col = f"{r['mult_prev']:+.2f} → {r['mult']:+.2f}"
        print(f"  {r['alias'][:22]:<22} {refill:>12} {r['failures_since_success']:>9} "
              f"{mult_col:>16}  {r['mult_reason']:<36}")
    print()
    log.info("recompute_signals: updated %d channels", len(results))


def cmd_adjust_fees(args):
    """Update fee policies on all channels."""
    log = get_logger("main")
    log.info("adjust_fees: starting%s", " [dry-run]" if args.dry_run else "")
    print("\n⚡ LN Operator — Fee Policy Update")
    print("=" * 40)
    print("  Reading channels + current fees from LND "
          "(a few seconds)…", flush=True)

    updates = engine.update_all_fees(dry_run=args.dry_run)

    if not updates:
        log.info("adjust_fees: no changes needed")
        print("All fees are up to date — no changes needed.")
    else:
        log.info("adjust_fees: %d change(s)%s", len(updates), " (dry-run)" if args.dry_run else "")
        prefix = "[DRY RUN] " if args.dry_run else ""
        for u in updates:
            direction = "↑" if u["new_ppm"] > u["old_ppm"] else "↓"
            status = ""
            if not args.dry_run:
                status = " ✓" if u.get("applied") else f" ✗ {u.get('error', '')}"
            print(
                f"  {direction} {u['alias']}: {u['old_ppm']} → {u['new_ppm']} ppm "
                f"(local: {u['local_ratio']:.0%}){status}"
            )

    return updates


def _show_rebalance_scenarios(current_force=None):
    """Show what different --force target levels would achieve.

    Helps the operator decide which force level to use when the pipeline
    can't auto-rebalance (no overfull channels above 80%).
    Only shows if there are depleted channels that need help.
    """
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)
    active = [c for c in channels if c["active"]]
    depleted = [c for c in active if c["local_ratio"] < REBALANCE_LOW_THRESHOLD]

    if not depleted:
        return  # nothing to suggest

    # Don't show if already using force (user already picked a level)
    if current_force is not None:
        return

    print(f"\n  {'─'*45}")
    print(f"  Rebalance scenarios (use --force to enable):")
    print(f"  {'─'*45}")

    for target in [0.50, 0.45, 0.40, 0.35, 0.30]:
        sources = [c for c in active if c["local_ratio"] > target]
        targets = [c for c in active if c["local_ratio"] < target]

        if not sources:
            print(f"\n  --force {target:.0%}  No sources — all channels at or below target")
            continue

        total_can_give = sum(int(c["capacity"] * (c["local_ratio"] - target)) for c in sources)
        total_needed = sum(int(c["capacity"] * (target - c["local_ratio"])) for c in targets)
        total_moveable = min(total_can_give, total_needed)

        can_fix = total_moveable > 100_000
        fixes = "✓ fixes depleted" if can_fix else "⚠ not enough"
        print(f"\n  --force {target:.0%}  ({fixes})")

        for c in active:
            alias = c["peer_alias"]
            current = c["local_ratio"]
            capacity = c["capacity"]

            if current > target:
                give = min(int(capacity * (current - target)),
                           int(capacity * REBALANCE_MAX_AMOUNT_RATIO))
                end = current - (give / capacity)
                direction = "↓ gives sats"
            elif current < target:
                needs = int(capacity * (target - current))
                recv = min(needs, total_moveable)
                end = current + (recv / capacity)
                direction = "↑ receives sats"
            else:
                end = current
                direction = "— unchanged"

            end = min(end, 1.0)
            delta = end - current
            delta_str = f"({delta:+.0%})" if abs(delta) > 0.005 else ""
            print(f"    {alias:<25} {current:.0%} → {end:.0%} {delta_str:<8} {direction}")

    print(f"\n  Preview: ln-operator rebalance_channels --force 0.40 --dry-run")


def execute_rebalance_plans(plans, log, executor=None):
    """Execute the planner's plan list, carrying state across attempts.

    Two ledgers drive every gating decision:
      - target_deficits: sats each depleted target still needs.
      - source_remaining: sats each overfull source can still send.

    Each plan is capped at min(plan amount, target deficit, source remaining)
    before being executed. Successful chunks decrement both ledgers; when
    either side drops below the 50k minimum the plan is skipped. This means
    fallbacks fire naturally — they're just later entries in the plan list,
    tried only when earlier ones didn't drain the deficit.

    executor is engine.execute_rebalance by default; pass in a stub for tests.
    """
    if executor is None:
        executor = engine.execute_rebalance

    MIN_CHUNK = 50_000

    # One id for the whole run — every plan (primary + fallbacks) at any channel
    # shares it, so count_failures_since_last_success counts failed CYCLES, not
    # the fallback fan-out within a single run.
    run_id = int(time.time())

    target_deficits = {}
    source_remaining = {}
    for p in plans:
        tid = p["target_chan_id"]
        sid = p["source_chan_id"]
        if "target_total_deficit" in p:
            target_deficits.setdefault(tid, p["target_total_deficit"])
        else:
            # legacy plan dict: approximate from summed primary amounts
            if not p.get("is_fallback"):
                target_deficits[tid] = target_deficits.get(tid, 0) + p["amount_sats"]
        if "source_total_surplus" in p:
            source_remaining.setdefault(sid, p["source_total_surplus"])
        else:
            if not p.get("is_fallback"):
                source_remaining[sid] = source_remaining.get(sid, 0) + p["amount_sats"]

    results = []
    for p in plans:
        source_id = p["source_chan_id"]
        target_id = p["target_chan_id"]
        deficit = target_deficits.get(target_id, 0)
        available = source_remaining.get(source_id, 0)

        if deficit < MIN_CHUNK:
            log.debug("skipping %s→%s — target deficit %s below minimum",
                      p["source_alias"], p["target_alias"], f"{deficit:,}")
            continue
        if available < MIN_CHUNK:
            log.debug("skipping %s→%s — source surplus %s exhausted",
                      p["source_alias"], p["target_alias"], f"{available:,}")
            continue

        attempt_amount = min(p["amount_sats"], deficit, available)
        if attempt_amount < MIN_CHUNK:
            continue

        capped = dict(p)
        capped["run_id"] = run_id
        capped["amount_sats"] = attempt_amount
        capped["max_fee_sats"] = int(
            attempt_amount * p["max_fee_ppm"] / 1_000_000 * 1.1
        )

        kind = "fallback" if p.get("is_fallback") else "primary"
        log.info("executing %s: %s→%s capped at %s sats "
                 "(deficit %s, source %s available)",
                 kind, p["source_alias"], p["target_alias"],
                 f"{attempt_amount:,}", f"{deficit:,}", f"{available:,}")
        print(f"  → {p['source_alias']} → {p['target_alias']} ({kind}): "
              f"moving up to {attempt_amount:,} sats (cap {p['max_fee_ppm']} ppm)…",
              flush=True)

        # Stream chunk-level progress only through the real executor; test stubs
        # and any custom executor keep the original (plan, dry_run) signature.
        extra = {}
        if executor is engine.execute_rebalance:
            extra["on_progress"] = lambda m: print(f"      {m}", flush=True)
        result = executor(capped, dry_run=False, **extra)
        results.append(result)

        moved = result.get("amount", 0) if result.get("success") else 0
        if moved > 0:
            # Sats always left the planned source, but with sibling channels
            # to the same peer they may have landed on a different channel
            # than planned (last_hop_pubkey pins the peer, not the channel).
            # Credit each landed channel's deficit if we track it; sats that
            # landed on an untracked sibling leave the planned target's
            # deficit open so later plans keep topping it up.
            landed = result.get("moved_by_target") or {target_id: moved}
            for tid, amt in landed.items():
                if tid in target_deficits:
                    target_deficits[tid] = max(0, target_deficits[tid] - amt)
                elif tid != target_id:
                    log.info("rebalance landed %s sats on untracked sibling %s "
                             "(planned %s) — planned deficit left open",
                             f"{amt:,}", tid, target_id)
            source_remaining[source_id] = max(0, available - moved)
            log.info("rebalance succeeded: %s→%s moved %s sats (fee %d sats, %.0f ppm)",
                     p["source_alias"], p["target_alias"],
                     f"{moved:,}", result["fee_paid"], result["fee_ppm"])
            print(f"  ✓ {p['source_alias']} → {p['target_alias']}: "
                  f"{moved:,} sats moved, "
                  f"fee {result['fee_paid']:,} sats "
                  f"({result['fee_ppm']:.0f} ppm)")
            still_needed = target_deficits[target_id]
            if still_needed >= MIN_CHUNK:
                print(f"      {p['target_alias']} still needs {still_needed:,} sats — "
                      f"continuing with next plan")
        else:
            log.warning("rebalance failed %s→%s: %s",
                        p["source_alias"], p["target_alias"],
                        result.get("failure_reason", "unknown"))
            print(f"  ✗ {p['source_alias']} → {p['target_alias']}: "
                  f"{result.get('failure_reason', 'unknown')}")

    return results


def cmd_rebalance_channels(args):
    """Check for and execute rebalancing."""
    log = get_logger("main")
    log.info("rebalance_channels: starting%s", " [dry-run]" if args.dry_run else "")
    print("\n⚡ LN Operator — Rebalance Check")
    print("=" * 40)

    force = getattr(args, "force", None)
    if force is not None:
        if not 0.0 < force < 1.0:
            print(f"Error: --force target must be between 0.0 and 1.0 (got {force})")
            sys.exit(1)
        log.info("rebalance_channels: force mode — ignoring thresholds, targeting %.0f%% on all channels", force * 100)
    plans, reason = engine.plan_rebalances(force=force, record_early_outs=not args.dry_run)

    if not plans:
        log.info("rebalance_channels: %s", reason)
        print(f"  {reason}")

        # Show scenario analysis when no auto-rebalance is possible
        _show_rebalance_scenarios(force)
        return []

    # Split into primary and fallback for display
    primaries  = [p for p in plans if not p.get("is_fallback")]
    fallbacks  = [p for p in plans if p.get("is_fallback")]

    if args.dry_run:
        # Show full plan breakdown without executing

        # ── Channel status overview ───────────────────────────────────
        # Show ALL channels with their rebalance status so the operator
        # understands why certain channels are ignored
        channels_all = lnd_client.get_channels()
        channels_all = lnd_client.resolve_aliases(channels_all)
        print(f"\n  Channel status:")
        for ch in channels_all:
            ratio = ch["local_ratio"]
            alias = ch["peer_alias"]
            if not ch["active"]:
                status = "⚫ offline"
            elif ratio < REBALANCE_LOW_THRESHOLD:
                status = f"🔴 depleted ({ratio:.0%} local) — rebalance target"
            elif ratio > REBALANCE_HIGH_THRESHOLD:
                status = f"🔵 overfull ({ratio:.0%} local) — rebalance source"
            else:
                status = f"🟢 healthy ({ratio:.0%} local) — not rebalanced"
                if force is not None and ratio < force:
                    status = f"🟡 below target ({ratio:.0%} local) — force target"
                elif force is not None and ratio > force:
                    status = f"🟡 above target ({ratio:.0%} local) — force source"
            print(f"    {alias}: {status}")

        # Show all depleted/overfull channels from the full plan list (primaries + fallbacks)
        all_targets = {p["target_chan_id"]: p["target_alias"] for p in plans}
        all_sources = {p["source_chan_id"]: p["source_alias"] for p in plans}

        print(f"\n  Candidates:")
        print(f"    Depleted (need sats):  {', '.join(all_targets.values()) or 'none'}")
        print(f"    Overfull (can donate): {', '.join(dict.fromkeys(all_sources.values())) or 'none'}")

        num_targets = len(all_targets)
        num_sources  = len(set(all_sources.keys()))

        if not fallbacks:
            if num_targets == 1 and num_sources == 1:
                print(f"\n  Only one possible pair — no fallback available.")
                print(f"  A fallback would require either:")
                print(f"    • A second overfull channel (to try a different source)")
                print(f"    • A second depleted channel (to try a different target)")
                print(f"  If this pair fails, nothing else can be tried this run.")
        else:
            print(f"\n  {num_targets} depleted channel(s), {num_sources} overfull source(s).")
            print(f"  {len(fallbacks)} fallback(s) — tried in order until each target's deficit is filled.")
        print()

        print(f"  Primary plans ({len(primaries)}):")
        for p in primaries:
            max_fee_sats = int(p["amount_sats"] * p["max_fee_ppm"] / 1_000_000 * 1.1)
            print(f"    {p['source_alias']} ({p['source_local_ratio']:.0%}) "
                  f"→ {p['target_alias']} ({p['target_local_ratio']:.0%})")
            print(f"      Amount:   {p['amount_sats']:,} sats")
            print(f"      Fee cap:  {p['max_fee_ppm']} ppm = {max_fee_sats:,} sats max")
            print(f"      Budget:   {p.get('budget_reason','')}")

        if fallbacks:
            print(f"\n  Fallback plans (each tried only if its target still has a deficit):")
            for fp in fallbacks:
                print(f"    {fp['source_alias']} ({fp['source_local_ratio']:.0%}) "
                      f"→ {fp['target_alias']} ({fp['target_local_ratio']:.0%})")
                print(f"      Amount:   up to {fp['amount_sats']:,} sats "
                      f"(capped to remaining deficit at run time)")
                print(f"      Fee cap:  {fp['max_fee_ppm']} ppm")

        # Show scenarios if there are depleted channels without fallbacks
        _show_rebalance_scenarios(force)

        print(f"\n  [DRY RUN] No payments executed.")
        return []

    print(f"\nExecuting {len(primaries)} primary plan(s) (+ {len(fallbacks)} fallback(s)):\n")
    results = execute_rebalance_plans(plans, log)

    return results


def cmd_manual_rebalance(args):
    """Operator-driven one-off rebalance of a SPECIFIC source→target pair.

    Unlike `rebalance_channels` (which auto-selects pairs by balance ratio and
    is gated by the profit/structural ladder), this pins exactly the pair you
    name, bypasses the gate, and records the row as triggered_by='manual' so the
    dashboard tags it like any other manual rebalance. The executor still
    auto-chunks on failure (halving down to 100k) and resolves the real landing
    channel for sibling-safe attribution.
    """
    log = get_logger("main")
    print("\n⚡ LN Operator — Manual Rebalance")
    print("=" * 40)

    if args.amount_sats < 50_000:
        print(f"  ✗ amount must be >= 50,000 sats (got {args.amount_sats:,})")
        sys.exit(1)
    if args.max_ppm < 0:
        print(f"  ✗ max-ppm must be >= 0 (got {args.max_ppm})")
        sys.exit(1)

    source = _resolve_channel(args.source)
    target = _resolve_channel(args.target)
    if source["chan_id"] == target["chan_id"]:
        print("  ✗ source and target are the same channel")
        sys.exit(1)

    plan = {
        "source_chan_id": source["chan_id"],
        "source_alias": source["peer_alias"],
        "target_chan_id": target["chan_id"],
        "target_alias": target["peer_alias"],
        "target_peer_pubkey": target["peer_pubkey"],
        "amount_sats": args.amount_sats,
        "max_fee_ppm": args.max_ppm,
        "max_fee_sats": int(args.amount_sats * args.max_ppm / 1_000_000 * 1.1),
        "triggered_by": "manual",
        # run_id stays None — a manual one-off is its own episode, not part of a
        # pipeline cycle's primary+fallback fan-out.
    }

    print(f"  {source['peer_alias']} ({source['local_ratio']:.0%}) → "
          f"{target['peer_alias']} ({target['local_ratio']:.0%})")
    print(f"  amount: {args.amount_sats:,} sats   cap: {args.max_ppm} ppm "
          f"(≤{plan['max_fee_sats']:,} sat)")
    if args.dry_run:
        print("  [DRY RUN] no payment executed.")
        engine.execute_rebalance(plan, dry_run=True)
        return

    # Stream the engine's INFO logs live so the operator sees each chunk
    # attempt, the landing channel, and the final summary as it happens — the
    # default console handler is WARNING-only. (file always has full DEBUG.)
    import logging
    set_console_level(logging.INFO)

    # Lightweight per-route progress: print "testing paths " then a dot for each
    # route the router tests, closing the line when the attempt ends. Keeps the
    # noisy per-HTLC detail in the DEBUG file, not the terminal.
    def on_probe(event):
        if event == "start":
            sys.stdout.write("  testing paths ")
        elif event == "tick":
            sys.stdout.write(".")
        elif event == "end":
            sys.stdout.write("\n")
        sys.stdout.flush()

    log.info("manual_rebalance: %s→%s %s sats (cap %d ppm)",
             source["peer_alias"], target["peer_alias"],
             f"{args.amount_sats:,}", args.max_ppm)
    result = engine.execute_rebalance(plan, dry_run=False, on_probe=on_probe)
    if result["success"]:
        print(f"\n  ✓ moved {result['amount']:,} sats — fee {result['fee_paid']:,} sat "
              f"({result['fee_ppm']:.0f} ppm)")
    else:
        print(f"\n  ✗ failed: {result['failure_reason']}")
        sys.exit(1)


def cmd_sync_routing(args):
    """Sync forwarding events and manual rebalances from LND into the local database."""
    log = get_logger("main")
    log.info("sync_routing: starting")
    print("\n⚡ LN Operator — Sync Routing History")
    print("=" * 40)

    num_events = engine.sync_forwarding_history()
    print(f"  Synced {num_events} new forwarding events")

    num_rebal = engine.sync_rebalances()
    if num_rebal > 0:
        print(f"  Synced {num_rebal} manual rebalance(s) from LND")
    else:
        print(f"  No new manual rebalances found")
    return num_events


def cmd_healthcheck(args):
    """Snapshot channel states, check for problems, fire alerts."""
    log = get_logger("main")
    log.info("healthcheck: starting")
    print("\n⚡ LN Operator — Health Check")
    print("=" * 40)

    report = engine.get_channel_health_report()

    print(f"\n  Channels: {report['total_channels']} "
          f"({report['active_channels']} active, {report['inactive_channels']} inactive)")
    print(f"  Capacity: {report['total_capacity']:,} sats")
    print(f"  Local: {report['total_local']:,} sats ({report['overall_local_ratio']:.0%})")
    print(f"  Remote: {report['total_remote']:,} sats ({1 - report['overall_local_ratio']:.0%})")

    if report["alerts"]:
        print(f"\n  ⚠️  {len(report['alerts'])} alert(s):")
        for alert in report["alerts"]:
            print(f"    • [{alert['type']}] {alert['message']}")

            db.save_alert(alert["type"], alert["message"], alert.get("chan_id"))

            if not args.no_telegram:
                msg = telegram_bot.format_alert(alert["type"], alert["message"])
                telegram_bot.send_message(msg)
    else:
        print("\n  ✅ All channels healthy.")

    return report


def cmd_run(args):
    """Full pipeline: rebalance_channels → adjust_fees → sync_routing → healthcheck."""
    log_main = get_logger("main")
    started = time.time()
    dry = " [DRY RUN]" if args.dry_run else ""
    log_main.info("pipeline starting%s", dry)
    print(f"\n⚡ LN Operator — Pipeline Run ({datetime.now().strftime('%Y-%m-%d %H:%M')}){dry}")
    print("=" * 55)

    # Step 1: Rebalance FIRST — each successful chunk writes its rebalance_log row
    # (and thus last_refill_ppm) before fees are computed, so Step 2 prices every
    # refilled channel off the cost it ACTUALLY paid this run, not last run's anchor.
    print("\n── Step 1: Rebalance Channels ──")
    plans, reason = engine.plan_rebalances(record_early_outs=not args.dry_run)
    rebalance_results = []
    if not plans:
        print(f"  {reason}")
    elif args.dry_run:
        primaries = [p for p in plans if not p.get("is_fallback")]
        fallbacks = [p for p in plans if p.get("is_fallback")]
        print(f"  [DRY RUN] {len(primaries)} primary plan(s) "
              f"+ {len(fallbacks)} fallback(s):")
        for p in plans:
            kind = "fallback" if p.get("is_fallback") else "primary"
            print(f"    {p['source_alias']} → {p['target_alias']} ({kind}): "
                  f"up to {p['amount_sats']:,} sats (cap {p['max_fee_ppm']} ppm)")
        print("  [DRY RUN] No payments executed.")
    else:
        # Use the dual-ledger executor — caps each plan at the target's remaining
        # deficit and the source's remaining surplus, so fallbacks only fire when
        # their target still needs sats (no over-rebalancing). Same path as the
        # interactive rebalance_channels command.
        rebalance_results = execute_rebalance_plans(plans, log_main)

    # Step 2: Adjust fees — floors each channel at the fee just paid to refill it
    print("\n── Step 2: Adjust Fees ──")
    fee_updates = engine.update_all_fees(dry_run=args.dry_run)
    if fee_updates:
        for u in fee_updates:
            d = "↑" if u["new_ppm"] > u["old_ppm"] else "↓"
            print(f"  {d} {u['alias']}: {u['old_ppm']}→{u['new_ppm']} ppm (local {u['local_ratio']:.0%})")
    else:
        print("  No fee changes needed.")

    # Step 3: Sync routing history from LND
    print("\n── Step 3: Sync Routing ──")
    num_events = engine.sync_forwarding_history()
    print(f"  {num_events} new routing event(s) synced")
    num_rebal = engine.sync_rebalances()
    if num_rebal > 0:
        print(f"  {num_rebal} manual rebalance(s) synced from LND")

    # Step 4: Health check + alerts
    print("\n── Step 4: Health Check ──")
    report = engine.get_channel_health_report()
    print(f"  {report['active_channels']} active, {report['inactive_channels']} inactive — "
          f"overall local {report['overall_local_ratio']:.0%}")
    if report["alerts"]:
        for a in report["alerts"]:
            print(f"  ⚠️  [{a['type']}] {a['message']}")
            db.save_alert(a["type"], a["message"], a.get("chan_id"))
    else:
        print("  ✅ All channels healthy.")

    elapsed = time.time() - started
    log_main.info("pipeline complete in %.1fs — fees:%d rebalances:%d events:%d alerts:%d",
                  elapsed, len(fee_updates), len(rebalance_results),
                  num_events, len(report["alerts"]))
    for a in report["alerts"]:
        log_main.warning("alert [%s]: %s", a["type"], a["message"])
    print(f"\n✅ Pipeline complete in {elapsed:.1f}s")


def cmd_status(args):
    """Quick node status summary."""
    print("\n⚡ LN Operator — Node Status")
    print("=" * 40)

    try:
        info = lnd_client.get_info()
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)
        onchain = lnd_client.get_onchain_balance()

        print(f"  Node: {info.get('alias', 'unknown')}")
        print(f"  Synced: chain={info.get('synced_to_chain')}, graph={info.get('synced_to_graph')}")
        print(f"  Block height: {info.get('block_height', '?')}")
        print(f"  Channels: {len(channels)} ({sum(1 for c in channels if c['active'])} active)")

        total_cap = sum(c["capacity"] for c in channels)
        total_local = sum(c["local_balance"] for c in channels)
        ratio = total_local / total_cap if total_cap > 0 else 0

        print(f"  Capacity: {total_cap:,} sats")
        print(f"  Local: {total_local:,} ({ratio:.0%})")
        print(f"  On-chain: {int(onchain.get('confirmed_balance', 0)):,} sats")

        # Fetch fee policies for each channel from the graph
        my_pk = info.get("identity_pubkey", "")
        channel_fees = {}
        for ch in channels:
            try:
                edge = lnd_client.get_channel_edge(ch['chan_id'])
                if edge:
                    if edge.get("node1_pub") == my_pk:
                        my_pol   = edge.get("node1_policy", {})
                        their_pol = edge.get("node2_policy", {})
                    else:
                        my_pol   = edge.get("node2_policy", {})
                        their_pol = edge.get("node1_policy", {})

                    channel_fees[ch["chan_id"]] = {
                        "my_ppm":      int(my_pol.get("fee_rate_milli_msat", 0)),
                        "their_ppm":   int(their_pol.get("fee_rate_milli_msat", 0)),
                        "their_base":  int(their_pol.get("fee_base_msat", 0)) // 1000,
                        "their_inbound_ppm": int(their_pol.get("inbound_fee_rate_milli_msat", 0)),
                    }
            except Exception:
                pass

        pins = db.get_fee_overrides()

        # Siblings to the same peer share an alias — tag duplicates with a short
        # scid suffix so the rows can be told apart (matches the dashboard).
        alias_counts = {}
        for ch in channels:
            alias_counts[ch["peer_alias"]] = alias_counts.get(ch["peer_alias"], 0) + 1

        def _label(ch):
            tag = f" #{str(ch['chan_id'])[-5:]}" if alias_counts.get(ch["peer_alias"], 0) > 1 else ""
            return f"{ch['peer_alias']}{tag}"

        print(f"\n  Per-channel breakdown:")
        print(f"  {'─'*72}")
        print(f"  {'Channel':<26} {'Balance':<22} {'Our fee':>10} {'Their fee':>12} {'Their inbound':>14}")
        print(f"  {'─'*72}")
        for ch in sorted(channels, key=lambda c: c["local_ratio"]):
            bar = _balance_bar(ch["local_ratio"], 20)
            status = "●" if ch["active"] else "○"
            fees = channel_fees.get(ch["chan_id"], {})
            my_ppm    = fees.get("my_ppm", "?")
            their_ppm = fees.get("their_ppm", "?")
            their_base = fees.get("their_base", 0)
            their_inbound = fees.get("their_inbound_ppm", 0)

            their_str = f"{their_base}b+{their_ppm}ppm" if their_base else f"{their_ppm}ppm"
            inbound_str = f"{their_inbound}ppm" if their_inbound else "—"
            my_ppm_str = f"{my_ppm}📌" if ch["chan_id"] in pins else f"{my_ppm} "

            print(f"  {status} {_label(ch)[:26]:26s} {bar} {ch['local_ratio']:.0%} "
                  f"| {my_ppm_str:>7}ppm | {their_str:>11} | {inbound_str:>12}")
        print(f"  {'─'*72}")
        print(f"  Our fee = what we charge to route payments out. Their fee = what they charge others to route to us.")
        print(f"  Their inbound = extra fee they charge for receiving (— means 0 or not set in graph).")

        if pins:
            print(f"\n  Pinned fees (auto-fees skipped on these channels):")
            print(f"  {'─'*72}")
            print(f"  {'Channel':<22} {'Pinned':>9}  {'Set at':<18} Note")
            for chan_id, pin in pins.items():
                ch = next((c for c in channels if c["chan_id"] == chan_id), None)
                alias = _label(ch) if ch else "(channel closed?)"
                set_at = datetime.fromtimestamp(pin["set_at"]).strftime("%Y-%m-%d %H:%M")
                note = pin.get("note") or ""
                print(f"  {alias[:21]:<22} {pin['pinned_ppm']:>5} ppm  {set_at:<18} {note}")
            print(f"  Clear with: ln-operator clear_fee <alias-or-chan_id>")

    except Exception as e:
        print(f"  Error connecting to LND: {e}")
        sys.exit(1)


def cmd_history(args):
    """Show recent activity from database."""
    days = args.days
    print(f"\n⚡ LN Operator — Last {days} Days")
    print("=" * 40)

    # Rebalance stats
    stats = db.get_recent_rebalance_stats(days)
    print(f"\n  Rebalancing:")
    print(f"    Attempts: {stats['total_attempts']}")
    print(f"    Successes: {stats['successes']}")
    print(f"    Total fees: {stats['total_fees']:,} sats")
    print(f"    Avg fee: {stats['avg_fee_ppm']:.0f} ppm")
    print(f"    Total rebalanced: {stats['total_rebalanced']:,} sats")

    # Fee revenue
    avg_rev = db.get_avg_monthly_fee_revenue(months=max(1, days // 30))
    print(f"\n  Fee revenue (avg/month): {avg_rev:,.0f} sats")

    # Recent alerts
    with db.get_conn() as conn:
        alerts = conn.execute("""
            SELECT ts, alert_type, message FROM alerts
            WHERE ts > ? ORDER BY ts DESC LIMIT 10
        """, (int(time.time()) - days * 86400,)).fetchall()

    if alerts:
        print(f"\n  Recent alerts:")
        for a in alerts:
            dt = datetime.fromtimestamp(a["ts"]).strftime("%m/%d %H:%M")
            print(f"    [{dt}] {a['alert_type']}: {a['message']}")


def cmd_backup(args):
    """Push channel.backup to the remote host configured in backup.py."""
    ok = backup.run_backup(trigger=args.trigger)
    sys.exit(0 if ok else 1)


def _resolve_channel(needle):
    """Find a channel by chan_id or alias substring (case-insensitive).

    Returns the channel dict, or prints an error and exits.
    """
    channels = lnd_client.get_channels()
    channels = lnd_client.resolve_aliases(channels)

    # Exact chan_id match wins
    for ch in channels:
        if ch["chan_id"] == needle:
            return ch

    # Otherwise, case-insensitive alias substring match
    needle_lc = needle.lower()
    matches = [c for c in channels if needle_lc in (c["peer_alias"] or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"  ✗ No channel matches '{needle}'.")
        print(f"  Available channels:")
        for c in channels:
            print(f"    {c['chan_id']}  {c['peer_alias']}")
        sys.exit(1)
    print(f"  ✗ '{needle}' is ambiguous — matches {len(matches)} channels:")
    for c in matches:
        print(f"    {c['chan_id']}  {c['peer_alias']}")
    print(f"  Use the chan_id instead.")
    sys.exit(1)


def cmd_overwrite_fee(args):
    """Pin a channel's outbound fee rate so the pipeline leaves it alone."""
    log = get_logger("main")
    print("\n⚡ LN Operator — Pin Channel Fee")
    print("=" * 40)

    if args.ppm < 0:
        print(f"  ✗ ppm must be >= 0 (got {args.ppm})")
        sys.exit(1)

    ch = _resolve_channel(args.channel)
    cp = ch["channel_point"]

    # Current fee for diff display
    fee_report = lnd_client.get_fee_report()
    old_ppm = 0
    for item in fee_report.get("channel_fees", []):
        if item.get("channel_point") == cp:
            old_ppm = int(item.get("fee_per_mil", 0))
            break

    # Warn at pin time if the proposed ppm sits below the engine's rebalance
    # floor (last successful refill × REBALANCE_FEE_MARGIN). The engine still
    # honours the pin — this is just so the operator sees the trade-off at
    # the moment they make the decision, not buried in a 2h pipeline log.
    from config import FEE_BASE_MSAT, REBALANCE_FEE_MARGIN
    last_refill = db.get_last_refill_ppm(ch["chan_id"])
    floor = int(round(last_refill * REBALANCE_FEE_MARGIN)) if last_refill else 0
    if floor and args.ppm < floor:
        print(f"  ⚠️  pin {args.ppm} ppm is BELOW the rebalance floor of "
              f"{floor} ppm (last refill {last_refill:.0f} × "
              f"{REBALANCE_FEE_MARGIN:.2f}). You may be selling outbound "
              f"below refill cost.")

    # Apply on LND immediately so the pin takes effect now
    try:
        lnd_client.update_channel_policy(cp, FEE_BASE_MSAT, args.ppm)
    except Exception as e:
        print(f"  ✗ LND policy update failed: {e}")
        sys.exit(1)

    db.set_fee_override(ch["chan_id"], args.ppm, note=args.note)
    db.save_fee_update(
        ch["chan_id"], ch["peer_alias"], old_ppm, args.ppm,
        FEE_BASE_MSAT, FEE_BASE_MSAT, ch["local_ratio"],
        f"manual pin{f': {args.note}' if args.note else ''}",
    )
    log.info("overwrite_fee: pinned %s at %d ppm (was %d ppm)",
             ch["peer_alias"], args.ppm, old_ppm)

    print(f"  ✓ Pinned {ch['peer_alias']}: {old_ppm} → {args.ppm} ppm")
    print(f"    chan_id: {ch['chan_id']}")
    if args.note:
        print(f"    note:    {args.note}")
    print(f"  Pipeline will leave this channel alone until 'clear_fee'.")


def cmd_clear_fee(args):
    """Remove a fee pin so the pipeline can resume auto-adjusting."""
    log = get_logger("main")
    print("\n⚡ LN Operator — Clear Fee Pin")
    print("=" * 40)

    ch = _resolve_channel(args.channel)
    existed = db.clear_fee_override(ch["chan_id"])
    if not existed:
        print(f"  No pin on {ch['peer_alias']} — nothing to clear.")
        return

    log.info("clear_fee: removed pin on %s", ch["peer_alias"])
    print(f"  ✓ Pin removed from {ch['peer_alias']}.")
    print(f"  Run 'ln-operator adjust_fees' to recompute now, or wait for the next pipeline run.")


def _balance_bar(ratio, width=20):
    """Visual balance bar like the dashboard."""
    filled = int(ratio * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def main():
    parser = argparse.ArgumentParser(
        prog="ln-operator",
        description="LN Operator — Lightning Node Channel Management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram notifications")

    subparsers = parser.add_subparsers(dest="command",
        metavar="command",
        help="See 'ln-operator <command> --help' for details")

    # ── AUTOMATED — full pipeline, designed for crontab ──────────
    p_pipeline = subparsers.add_parser("pipeline",
        help="[automated] Run full pipeline: adjust_fees → rebalance_channels → sync_routing → healthcheck")
    p_pipeline.add_argument("--dry-run", action="store_true",
        help="Preview all changes without applying")

    # ── FEATURES — interactive tools for the operator ────────────
    p_plan = subparsers.add_parser("plan",
        help="[feature]   Channel investment plan — reads wallet balance, proposes allocation")
    p_plan.add_argument("--min-channel", type=int, default=None,
        metavar="SATS",
        help="Minimum channel size in sats (default: PREFERRED_CHANNEL_SIZE_SATS from config)")
    p_plan.add_argument("--treasury", type=float, default=None,
        metavar="RATIO",
        help="Treasury reserve ratio 0.0-1.0, e.g. 0.01 for 1%% (default: TREASURY_MIN_RATIO from config)")

    p_status = subparsers.add_parser("status",
        help="[feature]   Quick node overview with channel balance bars")

    p_hist = subparsers.add_parser("history",
        help="[feature]   Show fee revenue, rebalance stats, and alerts from database")
    p_hist.add_argument("days", type=int, nargs="?", default=30,
        help="Number of days to look back (default: 30)")

    # ── DEBUG — run individual pipeline steps in isolation ────────
    p_fees = subparsers.add_parser("adjust_fees",
        help="[debug]     Adjust channel fee rates based on current balance ratios")
    p_fees.add_argument("--dry-run", action="store_true",
        help="Show what would change without applying")

    p_rebal = subparsers.add_parser("rebalance_channels",
        help="[debug]     Move sats from overfull to depleted channels "
             "(auto-chunks: halves down to 100k on route failure)")
    p_rebal.add_argument("--dry-run", action="store_true",
        help="Show plan without executing payments")
    p_rebal.add_argument("--force", type=float, nargs="?", const=0.5, default=None,
        metavar="RATIO",
        help="Ignore thresholds — target RATIO on all channels (default: 0.5 if flag set without value)")

    p_manual = subparsers.add_parser("manual_rebalance",
        help="[feature]   Rebalance a SPECIFIC source→target pair (recorded as manual)")
    p_manual.add_argument("source", metavar="SOURCE",
        help="Source channel (overfull): chan_id or unique peer alias substring")
    p_manual.add_argument("target", metavar="TARGET",
        help="Target channel (depleted): chan_id or unique peer alias substring")
    p_manual.add_argument("amount_sats", type=int, metavar="AMOUNT_SATS",
        help="Sats to move (>= 50,000; auto-chunks down to 100k on failure)")
    p_manual.add_argument("max_ppm", type=int, metavar="MAX_PPM",
        help="Max routing fee in ppm of the amount (the fee cap)")
    p_manual.add_argument("--dry-run", action="store_true",
        help="Show the plan without executing the payment")

    p_sync = subparsers.add_parser("sync_routing",
        help="[debug]     Sync routing events from LND into the local database")

    p_health = subparsers.add_parser("healthcheck",
        help="[debug]     Snapshot channel states, check for problems, fire alerts")

    p_backup = subparsers.add_parser("backup",
        help="[automated] Push channel.backup to remote host (called by systemd)")
    p_backup.add_argument("--trigger", default="manual",
        choices=["path", "timer", "manual"],
        help="What triggered this backup run (recorded in DB)")

    p_overwritefee = subparsers.add_parser("overwrite_fee",
        help="[feature]   Pin a channel's outbound fee — pipeline will leave it alone")
    p_overwritefee.add_argument("channel",
        metavar="CHANNEL",
        help="Channel to pin: numeric chan_id (scid), or a peer alias "
             "substring (must match exactly one channel — use the chan_id "
             "when a peer has multiple channels)")
    p_overwritefee.add_argument("ppm", type=int,
        metavar="PPM",
        help="Outbound fee rate in ppm to pin (integer >= 0, e.g. 100)")
    p_overwritefee.add_argument("--note", default="",
        help="Optional note recorded with the pin")

    p_clearfee = subparsers.add_parser("clear_fee",
        help="[feature]   Remove a fee pin so the pipeline can auto-adjust again")
    p_clearfee.add_argument("channel",
        help="chan_id or peer alias (substring match)")

    p_signals = subparsers.add_parser("recompute_signals",
        help="[automated] Refresh slow per-channel signals (market multiplier). Designed for a nightly cron.")

    p_refresh_graph = subparsers.add_parser("refresh_graph",
        help="[automated] Pull the network graph into the local cache (multi-MB; daily cron ahead of daily-check).")

    p_suggest_peers = subparsers.add_parser("suggest_peers",
        help="[feature]   Suggest peers to open toward a target (alias or pubkey) for cheaper refills.")
    p_suggest_peers.add_argument("target", help="target node — alias substring or 66-hex pubkey")
    p_suggest_peers.add_argument("--no-validate", action="store_true",
        help="stage-1 graph shortlist only — skip the live QueryRoutes validation")

    p_monitor = subparsers.add_parser("monitor_htlcs",
        help="[automated] Long-running: record dropped forwards from LND's HTLC event stream (systemd)")

    args = parser.parse_args()

    # Initialise logging
    setup_logging()
    log = get_logger('main')
    log.info("ln-operator starting: %s", args.command)

    # Initialise database
    db.init_db()

    # Stamp the active knob set (writes only when a knob changed) so logged
    # fee/rebalance outcomes can be attributed to the config that produced them
    import config
    db.record_knob_snapshot(config.knob_snapshot())

    if args.command == "plan":
        cmd_plan(args)
    elif args.command == "adjust_fees":
        cmd_adjust_fees(args)
    elif args.command == "rebalance_channels":
        cmd_rebalance_channels(args)
    elif args.command == "manual_rebalance":
        cmd_manual_rebalance(args)
    elif args.command == "sync_routing":
        cmd_sync_routing(args)
    elif args.command == "healthcheck":
        cmd_healthcheck(args)
    elif args.command == "pipeline":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "backup":
        cmd_backup(args)
    elif args.command == "overwrite_fee":
        cmd_overwrite_fee(args)
    elif args.command == "clear_fee":
        cmd_clear_fee(args)
    elif args.command == "recompute_signals":
        cmd_recompute_signals(args)
    elif args.command == "refresh_graph":
        cmd_refresh_graph(args)
    elif args.command == "suggest_peers":
        cmd_suggest_peers(args)
    elif args.command == "monitor_htlcs":
        cmd_monitor_htlcs(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
