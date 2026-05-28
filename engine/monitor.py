"""
LN Operator — Health monitoring.

get_channel_health_report walks every channel, snapshots its state into
the DB, enriches with 30d performance + current rebalance budget, and
emits alerts (depleted / saturated / peer offline / repeated rebalance
failures) for the pipeline to forward to Telegram and the dashboard.
"""

import time

from config import (
    REBALANCE_LOW_THRESHOLD, REBALANCE_HIGH_THRESHOLD,
    REBALANCE_BALANCED_RATIO, REBALANCE_BALANCED_RATIO_HIGH,
)
import lnd_client
import db
from logging_config import get_logger
from engine.rebalance_planner import get_channel_rebalance_budget

log = get_logger('engine.monitor')


def get_channel_health_report(channels=None):
    """Generate a health report for all channels.

    Returns a structured report with alerts for unhealthy channels.
    """
    if channels is None:
        channels = lnd_client.get_channels()
        channels = lnd_client.resolve_aliases(channels)

    report = {
        "timestamp": int(time.time()),
        "total_channels": len(channels),
        "active_channels": sum(1 for c in channels if c["active"]),
        "inactive_channels": sum(1 for c in channels if not c["active"]),
        "total_capacity": sum(c["capacity"] for c in channels),
        "total_local": sum(c["local_balance"] for c in channels),
        "total_remote": sum(c["remote_balance"] for c in channels),
        "overall_local_ratio": 0,
        "channels": [],
        "alerts": [],
    }

    total_cap = report["total_capacity"]
    if total_cap > 0:
        report["overall_local_ratio"] = round(report["total_local"] / total_cap, 4)

    for ch in channels:
        ch_report = {
            "chan_id": ch["chan_id"],
            "peer_alias": ch["peer_alias"],
            "capacity": ch["capacity"],
            "local_ratio": ch["local_ratio"],
            "active": ch["active"],
            "status": "healthy",
        }

        # Track channel maturity (balanced time accumulator)
        is_balanced = (
            REBALANCE_BALANCED_RATIO <= ch["local_ratio"] <= REBALANCE_BALANCED_RATIO_HIGH
        )
        db.update_channel_maturity(ch["chan_id"], is_balanced)

        # Enrich with historical performance and current rebalance budget
        try:
            perf = db.get_channel_performance(ch["chan_id"])
            ch_report["fee_revenue_30d"] = perf["fee_revenue"]
            ch_report["forwards_30d"] = perf["forwards"]
            ch_report["rebalance_cost_30d"] = perf["rebalance_cost"]
            ch_report["net_profit_30d"] = perf["net_profit"]
        except Exception:
            pass

        try:
            budget = get_channel_rebalance_budget(ch["chan_id"])
            ch_report["budget_ppm"] = budget["max_fee_ppm"]
            ch_report["budget_reason"] = budget["reason"]
        except Exception:
            pass

        # Generate alerts
        if not ch["active"]:
            ch_report["status"] = "offline"
            report["alerts"].append({
                "type": "peer_offline",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} is offline ({ch['capacity']:,} sats locked)",
            })
        elif ch["local_ratio"] < REBALANCE_LOW_THRESHOLD:
            ch_report["status"] = "depleted"
            report["alerts"].append({
                "type": "channel_depleted",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} depleted at {ch['local_ratio']:.0%} local",
            })
        elif ch["local_ratio"] > REBALANCE_HIGH_THRESHOLD:
            ch_report["status"] = "saturated"
            report["alerts"].append({
                "type": "channel_saturated",
                "chan_id": ch["chan_id"],
                "alias": ch["peer_alias"],
                "message": f"{ch['peer_alias']} saturated at {ch['local_ratio']:.0%} local",
            })

        # Check for repeated rebalance failures regardless of health status
        try:
            failure_count = db.get_repeated_rebalance_failures(ch["chan_id"], min_failures=3)
            if failure_count >= 3:
                report["alerts"].append({
                    "type": "rebalance_failing",
                    "chan_id": ch["chan_id"],
                    "alias": ch["peer_alias"],
                    "message": (
                        f"{ch['peer_alias']} has failed to rebalance {failure_count} times in a row "
                        f"— check route availability or raise fee budget"
                    ),
                })
                ch_report["rebalance_failing"] = True
                log.warning("repeated rebalance failures for %s: %d consecutive failures",
                            ch["peer_alias"], failure_count)
        except Exception:
            pass

        report["channels"].append(ch_report)

    # Save snapshot to database
    db.save_channel_snapshot(channels)

    log.info("healthcheck: %d channel(s) active, %d inactive, overall local %.0f%%",
             report["active_channels"], report["inactive_channels"],
             report["overall_local_ratio"] * 100)
    if report["alerts"]:
        for alert in report["alerts"]:
            log.warning("healthcheck alert [%s]: %s", alert["type"], alert["message"])
    else:
        log.info("healthcheck: all channels healthy")

    return report
