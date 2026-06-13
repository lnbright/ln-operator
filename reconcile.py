"""
LN Operator — Data-integrity reconciliation.

The §2 silent-failure checks the daily-check agent used to do BY HAND — arithmetic
over SQLite an LLM can get subtly, invisibly wrong. Moved here as deterministic
assertions: `run_checks()` returns a list of issue dicts the agent just reads and
reports, instead of recomputing.

DB-only and pure (no LND): every check is a query + comparison, unit-tested against
a throwaway DB.

Budgets are verified as an INVARIANT, not a reconstruction: each row records the
`budget_ppm` the planner/executor actually used, so we assert `fee_ppm ≤ budget_ppm ×
1.1` and `budget_ppm ≤ REBALANCE_MAX_BUDGET_PPM` against that stored value. We do NOT
re-derive `get_channel_rebalance_budget`'s multi-layer math (escalation × profit cap ×
earn-ceiling accelerator × QueryRoutes pricing) here — mirroring engine internals in a
"checker" false-positives the same way a table-only hysteresis check would. Subtle
budget-logic bugs are the job of unit tests on the engine, not this reconciliation.

Some §2 checks deliberately stay agent-side, because a naive deterministic version is
WORSE than none (it looks authoritative while being wrong):
  - self-payment ↔ rebalance_log matching + live new_fee_ppm vs /v1/fees — need LND.
  - the fee-update HYSTERESIS rule — its cooldown escapes (snap Δ, edge-zone crossing)
    depend on engine state that isn't captured in `fee_updates.reason`, so checking it
    from the table alone false-positives on every legitimate floor-decay/snap broadcast.
    Verifying it correctly means mirroring engine internals — left to the agent /
    a future engine-faithful check.

Each issue: {"check", "severity" ('fail'|'warn'), "message"}. Empty list = clean.
"""

import time
from collections import defaultdict

import db
from config import REBALANCE_MAX_BUDGET_PPM
from logging_config import get_logger

log = get_logger("reconcile")

CHUNK_CLUSTER_GAP_SEC = 120      # rows of one attempt's chunks land within ~seconds
CHUNK_SPIKE_FACTOR = 2.0         # a chunk ≥ this × the cluster median = routing spike
BUDGET_OVERSHOOT_FACTOR = 1.1    # chunk wrapper adds a 10% search buffer over the budget


def _issue(check, severity, message):
    return {"check": check, "severity": severity, "message": message}


def run_checks(window_days=1, now=None):
    """Run all deterministic data-integrity checks over the last `window_days`."""
    now = now or int(time.time())
    cutoff = now - window_days * 86400
    issues = []
    with db.get_conn() as conn:
        issues += _check_rebalance_log(conn, cutoff)
        issues += _check_fee_updates(conn, cutoff)
    return issues


def _check_rebalance_log(conn, cutoff):
    issues = []
    rows = conn.execute(
        "SELECT id, ts, source_chan_id, target_chan_id, source_alias, target_alias, "
        "       fee_ppm, budget_ppm, payment_hash, triggered_by "
        "FROM rebalance_log WHERE success = 1 AND ts > ? ORDER BY ts",
        (cutoff,)).fetchall()

    for r in rows:
        pair = f"{r['source_alias'] or r['source_chan_id']}→{r['target_alias'] or r['target_chan_id']}"
        fee = r["fee_ppm"] or 0
        budget = r["budget_ppm"] or 0
        # 1. A recent AUTO success with no payment_hash — execute_rebalance has saved
        #    hashes since the chunking change, so a missing one is a sync/save bug.
        if r["triggered_by"] == "auto" and not (r["payment_hash"] or "").strip():
            issues.append(_issue(
                "rebalance_missing_hash", "warn",
                f"auto rebalance id={r['id']} ({pair}) succeeded with no payment_hash"))
        # 2. fee_ppm above the absolute ceiling — LND ignored fee_limit, or a bug.
        if r["triggered_by"] == "auto" and fee > REBALANCE_MAX_BUDGET_PPM:
            issues.append(_issue(
                "rebalance_over_max_budget", "fail",
                f"auto rebalance id={r['id']} ({pair}) paid {fee:.0f} ppm > "
                f"REBALANCE_MAX_BUDGET_PPM {REBALANCE_MAX_BUDGET_PPM}"))
        # 2a. Paid more than the budget RECORDED for this row (+10% chunk buffer) — LND
        #     ignored the fee_limit or a stale budget reached the wire. We assert the
        #     INVARIANT against the stored budget_ppm rather than re-deriving the
        #     multi-layer budget formula here (mirroring the engine would false-positive
        #     the same way the hysteresis check does — see module docstring).
        if budget > 0 and fee > budget * BUDGET_OVERSHOOT_FACTOR:
            issues.append(_issue(
                "rebalance_over_budget", "fail",
                f"rebalance id={r['id']} ({pair}) paid {fee:.0f} ppm > recorded budget "
                f"{budget:.0f} ppm ×{BUDGET_OVERSHOOT_FACTOR:g} ({budget * BUDGET_OVERSHOOT_FACTOR:.0f})"))
        # 2b. The budget the row recorded itself exceeds the absolute ceiling — a bug in
        #     budget computation (the planner should clamp to REBALANCE_MAX_BUDGET_PPM).
        if r["triggered_by"] == "auto" and budget > REBALANCE_MAX_BUDGET_PPM:
            issues.append(_issue(
                "rebalance_budget_over_max", "fail",
                f"auto rebalance id={r['id']} ({pair}) recorded budget {budget:.0f} ppm > "
                f"REBALANCE_MAX_BUDGET_PPM {REBALANCE_MAX_BUDGET_PPM}"))

    # 3. The same payment_hash on >1 row = the same payment double-logged (e.g. the
    #    executor AND sync both recording it) → revenue/cost double-count.
    for d in conn.execute(
            "SELECT payment_hash, COUNT(*) n FROM rebalance_log "
            "WHERE payment_hash IS NOT NULL AND payment_hash != '' "
            "GROUP BY payment_hash HAVING n > 1").fetchall():
        issues.append(_issue(
            "rebalance_dup_hash", "fail",
            f"payment_hash {d['payment_hash'][:16]}… logged {d['n']}× — double-counted"))

    # 4. Within one attempt's chunk cluster (same pair, rows within ~seconds), a chunk
    #    paying ≥2× the cluster median is a routing-fee spike worth flagging.
    pairs = defaultdict(list)
    for r in rows:
        pairs[(r["source_chan_id"], r["target_chan_id"])].append(r)
    for prs in pairs.values():
        prev_ts, cluster = None, []
        for r in prs:
            if prev_ts is not None and r["ts"] - prev_ts > CHUNK_CLUSTER_GAP_SEC:
                issues += _chunk_outliers(cluster)
                cluster = []
            cluster.append(r)
            prev_ts = r["ts"]
        issues += _chunk_outliers(cluster)
    return issues


def _chunk_outliers(cluster):
    if len(cluster) < 3:
        return []
    ppms = sorted((c["fee_ppm"] or 0) for c in cluster)
    median = ppms[len(ppms) // 2]
    if median <= 0:
        return []
    out = []
    for c in cluster:
        if (c["fee_ppm"] or 0) >= CHUNK_SPIKE_FACTOR * median:
            pair = f"{c['source_alias'] or c['source_chan_id']}→{c['target_alias'] or c['target_chan_id']}"
            out.append(_issue(
                "rebalance_chunk_spike", "warn",
                f"chunk id={c['id']} ({pair}) paid {c['fee_ppm']:.0f} ppm ≥ "
                f"{CHUNK_SPIKE_FACTOR:g}× cluster median {median:.0f} — routing spike"))
    return out


def _check_fee_updates(conn, cutoff):
    issues = []
    pins = {r["chan_id"]: r["pinned_ppm"]
            for r in conn.execute("SELECT chan_id, pinned_ppm FROM fee_overrides").fetchall()}
    rows = conn.execute(
        "SELECT id, ts, chan_id, peer_alias, new_fee_ppm, reason "
        "FROM fee_updates WHERE ts > ? ORDER BY chan_id, ts", (cutoff,)).fetchall()

    # 5. A pinned channel broadcasting anything other than its pinned ppm (and not
    #    via a pin-reason update) means the override was bypassed — a bug.
    for r in rows:
        pin = pins.get(r["chan_id"])
        if pin is not None and r["new_fee_ppm"] != pin and "pin" not in (r["reason"] or "").lower():
            issues.append(_issue(
                "fee_pin_violation", "fail",
                f"pinned channel {r['peer_alias'] or r['chan_id']} broadcast "
                f"{r['new_fee_ppm']} ppm ≠ pinned {pin} (reason: {r['reason']})"))

    # (Hysteresis cooldown is intentionally NOT checked here — see module docstring:
    # its escape conditions aren't reconstructable from fee_updates alone.)
    return issues
