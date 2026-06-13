"""
LN Operator — Data-integrity reconciliation.

The §2 silent-failure checks the daily-check agent used to do BY HAND — arithmetic
over SQLite an LLM can get subtly, invisibly wrong. Moved here as deterministic
assertions: `run_checks()` returns a list of issue dicts the agent just reads and
reports, instead of recomputing.

DB-only and pure (no LND): every check is a query + comparison, unit-tested against
a throwaway DB.

What it checks is deliberately narrow: the failure modes that can ONLY surface at
runtime — LND ignoring a fee_limit (`fee_ppm` over the row's recorded `budget_ppm` ×1.1,
or over the absolute max), a payment double-logged across writers, a routing-fee spike
within a chunk cluster, a missing payment_hash from a partial write. These are emergent /
external conditions a unit test can't reproduce.

It does NOT check pure-logic invariants on our own code — those are caught earlier and
better by unit tests, so a runtime re-assertion adds nothing:
  - the budget never exceeding REBALANCE_MAX_BUDGET_PPM is a clamp inside
    `get_channel_rebalance_budget` (engine tests: test_capped_at_max_budget /
    test_accelerator_never_exceeds_max_budget).
  - a pinned channel broadcasting exactly its pin is a property of `update_all_fees`
    (engine tests: FeePinBroadcastTests).
We assert the budget INVARIANT against the stored `budget_ppm` (the value actually used),
never by re-deriving the multi-layer budget math — mirroring engine internals in a
"checker" false-positives the same way a table-only hysteresis check would.

The fee-update HYSTERESIS rule is also deliberately NOT checked — a naive deterministic
version is WORSE than none. Its cooldown escapes (snap Δ, edge-zone crossing) depend on
engine state that isn't captured in `fee_updates.reason`, so checking it from the table
alone false-positives on every legitimate floor-decay/snap broadcast.

Two LND-dependent §2 checks (self-payment ↔ rebalance_log matching, live new_fee_ppm vs
/v1/fees) were removed entirely, not moved here: both are real failure modes already
covered continuously, so neither ever fired. `sync_rebalances` reconciles every circular
self-payment into `rebalance_log` (the check just re-verified sync), and `update_all_fees`
reads live /v1/fees every 2h and re-broadcasts on divergence (an LND fee-reset self-heals
within 2h). A real LND-reset guard, if wanted, belongs as a post-broadcast assertion in
the pipeline, not here.

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
        # NOTE: "recorded budget itself > REBALANCE_MAX_BUDGET_PPM" is NOT checked here —
        # it's a pure-logic invariant on get_channel_rebalance_budget's own clamp, fully
        # covered by engine unit tests (test_capped_at_max_budget /
        # test_accelerator_never_exceeds_max_budget). A runtime checker would only
        # re-assert what the tests already guarantee.

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

# NOTE: fee_updates is no longer reconciled here. The pinned-channel broadcast
# invariant (a pinned channel must broadcast exactly its pin) is a pure-logic
# property of update_all_fees, now covered by engine unit tests
# (test_engine.FeePinBroadcastTests) — a runtime checker would only re-assert what
# the tests guarantee. The hysteresis rule remains deliberately unchecked (its
# cooldown escapes aren't reconstructable from fee_updates alone — see docstring).
