#!/usr/bin/env python3
"""
LN Operator — Main CLI
Channel management cron jobs and investment advisor.

Usage:
    python main.py pipeline [--dry-run]            — run full pipeline (for crontab)
    python main.py adjust_fees [--dry-run]         — adjust channel fee rates
    python main.py rebalance_channels [--dry-run]  — rebalance depleted/overfull channels
    python main.py sync_routing                    — sync routing events from LND
    python main.py healthcheck                     — check channel health + fire alerts
    python main.py invest <amount_sats>            — investment advisor
    python main.py status                          — quick node overview
    python main.py history [days]                  — recent activity from database
"""

import sys
import argparse
import json
import time
from datetime import datetime

import db
import engine
from config import ANTHROPIC_API_KEY
from logging_config import setup_logging, get_logger
import advisor
import agent
import telegram_bot
import lnd_client


def cmd_invest(args):
    """Investment advisor: given X sats, produce a full plan."""
    amount = args.amount
    if amount < 100_000:
        print(f"Error: {amount:,} sats is too small. Minimum useful investment is ~100,000 sats.")
        sys.exit(1)

    print(f"\n⚡ LN Operator — Investment Plan for {amount:,} sats")
    print("=" * 55)

    # 60% — Python engine builds the plan
    plan = advisor.build_investment_plan(amount)

    # 10% — Agent adds judgement
    log_main = get_logger("main")
    log_main.info("requesting agent analysis")
    print("\n[agent] Getting Claude's analysis...")
    summary = agent.get_investment_summary(plan)
    plan["agent_summary"] = summary

    # Display
    _display_plan(plan)

    # Save updated plan with agent summary
    db.save_investment_plan(
        plan["total_sats"], plan["treasury_reserve"],
        plan["deployable_sats"], plan, summary
    )

    # Interactive follow-up loop
    if ANTHROPIC_API_KEY:
        _followup_loop(plan)

    return plan


def _followup_loop(plan):
    """Interactive Q&A loop after an investment plan is displayed."""
    print("\n" + "─" * 55)
    print("💬 Ask a follow-up question or press Enter to exit.")
    print("─" * 55)

    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            break

        print("\n🤖 Agent: ", end="", flush=True)
        answer = agent.get_followup_answer(plan, question)
        print(answer)


def cmd_adjust_fees(args):
    """Update fee policies on all channels."""
    print("\n⚡ LN Operator — Fee Policy Update")
    print("=" * 40)

    updates = engine.update_all_fees(dry_run=args.dry_run)

    if not updates:
        print("All fees are up to date — no changes needed.")
    else:
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

        # Telegram
        if not args.dry_run and not args.no_telegram:
            msg = telegram_bot.format_fee_update_report(updates)
            telegram_bot.send_message(msg)

    return updates


def cmd_rebalance_channels(args):
    """Check for and execute rebalancing."""
    print("\n⚡ LN Operator — Rebalance Check")
    print("=" * 40)

    plans, reason = engine.plan_rebalances()

    if not plans:
        print(f"  {reason}")
        return []

    print(f"\nFound {len(plans)} rebalance candidate(s):\n")
    results = []

    for p in plans:
        tier_icon = {"proven": "📊", "discovery": "🔍", "deadweight": "💤"}.get(
            p.get("budget_tier", ""), "•"
        )
        print(
            f"  {p['source_alias']} ({p['source_local_ratio']:.0%}) → "
            f"{p['target_alias']} ({p['target_local_ratio']:.0%}): "
            f"{p['amount_sats']:,} sats (max fee: {p['max_fee_ppm']} ppm)"
        )
        print(f"    {tier_icon} [{p.get('budget_tier', '?')}] {p.get('budget_reason', '')}")

        result = engine.execute_rebalance(p, dry_run=args.dry_run)
        results.append(result)

        if args.dry_run:
            print(f"    [DRY RUN] Would attempt rebalance")
        elif result["success"]:
            print(f"    ✓ Success! Fee: {result['fee_paid']:,} sats ({result['fee_ppm']:.0f} ppm)")
        else:
            print(f"    ✗ Failed: {result['failure_reason']}")

    # Telegram
    if not args.dry_run and not args.no_telegram:
        msg = telegram_bot.format_rebalance_report(results)
        telegram_bot.send_message(msg)

    return results


def cmd_sync_routing(args):
    """Sync routing events from LND into the local database."""
    print("\n⚡ LN Operator — Sync Routing History")
    print("=" * 40)

    num_events = engine.sync_forwarding_history()
    print(f"  Synced {num_events} new forwarding events")
    return num_events


def cmd_healthcheck(args):
    """Snapshot channel states, check for problems, fire alerts."""
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
    """Full pipeline: adjust_fees → rebalance_channels → sync_routing → healthcheck."""
    log_main = get_logger("main")
    started = time.time()
    dry = " [DRY RUN]" if args.dry_run else ""
    log_main.info("pipeline starting%s", dry)
    print(f"\n⚡ LN Operator — Pipeline Run ({datetime.now().strftime('%Y-%m-%d %H:%M')}){dry}")
    print("=" * 55)

    # Step 1: Adjust fees FIRST — protects reputation on depleted channels
    print("\n── Step 1: Adjust Fees ──")
    fee_updates = engine.update_all_fees(dry_run=args.dry_run)
    if fee_updates:
        for u in fee_updates:
            d = "↑" if u["new_ppm"] > u["old_ppm"] else "↓"
            print(f"  {d} {u['alias']}: {u['old_ppm']}→{u['new_ppm']} ppm (local {u['local_ratio']:.0%})")
    else:
        print("  No fee changes needed.")

    # Step 2: Rebalance channels
    print("\n── Step 2: Rebalance Channels ──")
    plans, reason = engine.plan_rebalances()
    rebalance_results = []
    if plans:
        for p in plans:
            result = engine.execute_rebalance(p, dry_run=args.dry_run)
            rebalance_results.append(result)
            if result["success"]:
                print(f"  ✓ {p['source_alias']} → {p['target_alias']}: "
                      f"{p['amount_sats']:,} sats, fee {result['fee_paid']:,} sats "
                      f"({result['fee_ppm']:.0f} ppm) [{p.get('budget_tier','?')}]")
            else:
                print(f"  ✗ {p['source_alias']} → {p['target_alias']}: "
                      f"{result['failure_reason']}")
    else:
        print(f"  {reason}")

    # Step 3: Sync routing history from LND
    print("\n── Step 3: Sync Routing ──")
    num_events = engine.sync_forwarding_history()
    print(f"  {num_events} new routing event(s) synced")

    # Step 4: Health check + alerts
    print("\n── Step 4: Health Check ──")
    report = engine.get_channel_health_report()
    print(f"  {report['active_channels']} active, {report['inactive_channels']} inactive — "
          f"overall local {report['overall_local_ratio']:.0%}")
    if report["alerts"]:
        for a in report["alerts"]:
            print(f"  ⚠️  [{a['type']}] {a['message']}")
    else:
        print("  ✅ All channels healthy.")

    elapsed = time.time() - started
    log_main.info("pipeline complete in %.1fs — fees:%d rebalances:%d events:%d alerts:%d",
                  elapsed, len(fee_updates), len(rebalance_results),
                  num_events, len(report["alerts"]))
    print(f"\n✅ Pipeline complete in {elapsed:.1f}s")

    # Telegram summary
    if not args.dry_run and not args.no_telegram:
        lines = [f"⚡ *Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')}*", ""]

        if fee_updates:
            applied = sum(1 for u in fee_updates if u.get("applied") is True)
            lines.append(f"📊 *Fees:* {applied} updated")
            for u in fee_updates:
                d = "↑" if u["new_ppm"] > u["old_ppm"] else "↓"
                lines.append(f"  {d} {u['alias']} {u['old_ppm']}→{u['new_ppm']} ppm")
        else:
            lines.append("📊 *Fees:* no changes needed")

        if rebalance_results:
            ok = sum(1 for r in rebalance_results if r["success"])
            lines.append(f"🔄 *Rebalance:* {ok}/{len(rebalance_results)} succeeded")
        else:
            lines.append(f"🔄 *Rebalance:* {reason}")

        lines.append(f"🔗 *Sync:* {num_events} new event(s)")

        if report["alerts"]:
            lines.append(f"⚠️ *Alerts: {len(report['alerts'])}*")
            for a in report["alerts"][:5]:
                lines.append(f"  • [{a['type']}] {a['message']}")
        else:
            lines.append("✅ *Health:* all channels OK")

        telegram_bot.send_message("\n".join(lines))


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

        print(f"\n  Per-channel breakdown:")
        for ch in sorted(channels, key=lambda c: c["local_ratio"]):
            bar = _balance_bar(ch["local_ratio"], 20)
            status = "●" if ch["active"] else "○"
            print(f"    {status} {ch['peer_alias'][:20]:20s} {bar} "
                  f"{ch['local_ratio']:.0%} ({ch['capacity']:,})")

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


def _display_plan(plan):
    """Pretty-print an investment plan to terminal."""
    state = plan.get("current_state", {})
    print(f"\n  Current node: {state.get('num_channels', 0)} channels, "
          f"{state.get('total_capacity', 0):,} sats, "
          f"{state.get('overall_ratio', 0):.0%} local ratio")

    print(f"\n  💰 Total: {plan['total_sats']:,} sats")
    print(f"  🏦 Treasury: {plan['treasury_reserve']:,} sats ({plan['treasury_pct']:.0%})")
    print(f"  🚀 Deployable: {plan['deployable_sats']:,} sats")

    fee_env = plan.get("fee_environment", {})
    if fee_env:
        print(f"  ⛓  Fees: {fee_env.get('note', 'unknown')}")

    actions = plan.get("actions", [])
    if actions:
        print(f"\n  Recommended actions:")
        for i, a in enumerate(actions, 1):
            print(f"    {i}. {a['type'].upper()} → {a['peer_alias']}: {a['amount_sats']:,} sats")
            if a.get("reason"):
                print(f"       {a['reason']}")
    else:
        print("\n  No actions recommended at this time.")

    nrec = plan.get("not_recommended", [])
    if nrec:
        print(f"\n  Notes:")
        for note in nrec:
            print(f"    • {note}")

    if plan.get("agent_summary"):
        print(f"\n  🤖 Agent says:")
        print(f"    {plan['agent_summary']}")


def _balance_bar(ratio, width=20):
    """Visual balance bar like the dashboard."""
    filled = int(ratio * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def main():
    parser = argparse.ArgumentParser(
        description="LN Operator — Lightning Node Channel Management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram notifications")

    subparsers = parser.add_subparsers(dest="command",
        metavar="command",
        help="See 'main.py <command> --help' for details")

    # ── AUTOMATED — full pipeline, designed for crontab ──────────
    p_pipeline = subparsers.add_parser("pipeline",
        help="[automated] Run full pipeline: adjust_fees → rebalance_channels → sync_routing → healthcheck")
    p_pipeline.add_argument("--dry-run", action="store_true",
        help="Preview all changes without applying")

    # ── FEATURES — interactive tools for the operator ────────────
    p_invest = subparsers.add_parser("invest",
        help="[feature]   Investment advisor — given X sats, what channels to open?")
    p_invest.add_argument("amount", type=int,
        help="Amount in sats you want to deploy")

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
        help="[debug]     Move sats from overfull to depleted channels")
    p_rebal.add_argument("--dry-run", action="store_true",
        help="Show plan without executing payments")

    p_sync = subparsers.add_parser("sync_routing",
        help="[debug]     Sync routing events from LND into the local database")

    p_health = subparsers.add_parser("healthcheck",
        help="[debug]     Snapshot channel states, check for problems, fire alerts")

    args = parser.parse_args()

    # Initialise logging
    setup_logging()
    log = get_logger('main')
    log.info("ln-operator starting: %s", args.command)

    # Initialise database
    db.init_db()

    if args.command == "invest":
        cmd_invest(args)
    elif args.command == "adjust_fees":
        cmd_adjust_fees(args)
    elif args.command == "rebalance_channels":
        cmd_rebalance_channels(args)
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
