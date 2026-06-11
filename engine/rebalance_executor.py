"""
LN Operator — Rebalance executor.

Per-plan execution: take one source→target plan from the planner and try to
move the requested amount via a circular self-payment. If the full chunk
fails (no route, fee exceeded), halve and retry down to a 100k floor. Every
successful chunk is its own row in rebalance_log at its actual ppm so the
fee-floor logic in engine.fees has accurate refill history.

The dual-ledger orchestration that sits ABOVE this — deciding which plans
to attempt and skipping ones whose target is already satisfied — lives in
main.execute_rebalance_plans. See CLAUDE.md for the contract.
"""

import time
import lnd_client
import db
from logging_config import get_logger

log = get_logger('engine.rebalance_executor')


def _attempt_single_rebalance(plan, amount, max_fee_sats, on_probe=None):
    """Attempt one circular rebalance payment at a specific amount.

    on_probe(event) is forwarded to send_payment_v2 so an interactive caller can
    render a progress indicator as routes are tested. Returns dict with:
    success, fee_paid, fee_ppm, failure_reason
    """
    try:
        # Create invoice
        invoice = lnd_client.add_invoice(
            amount,
            memo=f"rebal:{plan['source_alias'][:10]}→{plan['target_alias'][:10]}"
        )
        payment_request = invoice.get("payment_request", "")

        if not payment_request:
            return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                    "failure_reason": "failed to create invoice"}

        log.debug("  attempt %s sats (fee limit %d sats) via /v2/router/send",
                  f"{amount:,}", max_fee_sats)

        pay_result = lnd_client.send_payment_v2(
            payment_request=payment_request,
            outgoing_chan_id=plan["source_chan_id"],
            last_hop_pubkey=plan["target_peer_pubkey"],
            fee_limit_sat=max_fee_sats,
            timeout_seconds=120,
            on_probe=on_probe,
        )

        if pay_result["status"] == "SUCCEEDED":
            fee = pay_result["fee_sat"]
            ppm = fee / amount * 1_000_000 if amount > 0 else 0
            return {"success": True, "fee_paid": fee, "fee_ppm": ppm,
                    "failure_reason": "",
                    "payment_hash": pay_result.get("payment_hash", "")}
        else:
            return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                    "failure_reason": pay_result.get("failure_reason", "unknown"),
                    "payment_hash": pay_result.get("payment_hash", "")}

    except Exception as e:
        return {"success": False, "fee_paid": 0, "fee_ppm": 0,
                "failure_reason": str(e), "payment_hash": ""}


def execute_rebalance(plan, dry_run=False, on_progress=None, on_probe=None):
    """Execute a circular rebalance using Router SendPaymentV2.

    If the full amount fails (e.g. no route with enough liquidity), automatically
    splits into smaller chunks and retries. Halves the amount on each failure,
    down to a minimum of 100,000 sats. Successful chunks accumulate — the goal
    is to move as much as possible toward the target, not all-or-nothing.

    Forces the payment:
    - OUT through plan["source_chan_id"]  (the overfull channel)
    - BACK IN through plan["target_peer_pubkey"] (the depleted channel peer)

    on_progress, if given, is called with short status strings at each chunk
    boundary so an interactive caller can show live progress during the
    (potentially minute-long) SendPaymentV2 attempts. on_probe(event) is
    forwarded to the payment stream to drive a per-route progress indicator
    ("start"/"tick"/"end"). Both are cosmetic — the pipeline/cron path leaves
    them None.
    """
    def _emit(msg):
        if on_progress:
            on_progress(msg)
    result = {
        "source_chan_id": plan["source_chan_id"],
        "target_chan_id": plan["target_chan_id"],
        "source_alias": plan["source_alias"],
        "target_alias": plan["target_alias"],
        "amount": plan["amount_sats"],
        "max_fee": plan["max_fee_sats"],
        "success": False,
        "fee_paid": 0,
        "fee_ppm": 0,
        "failure_reason": "",
    }

    if dry_run:
        log.info("dry run: would rebalance %s→%s %s sats [%d ppm cap]",
                 plan["source_alias"], plan["target_alias"],
                 f"{plan['amount_sats']:,}", plan["max_fee_ppm"])
        result["failure_reason"] = "dry_run"
        return result

    log.info("executing rebalance: %s→%s %s sats (max fee %d ppm)",
             plan["source_alias"], plan["target_alias"],
             f"{plan['amount_sats']:,}", plan["max_fee_ppm"])
    start = time.time()

    total_moved = 0
    total_fees = 0
    moved_by_target = {}     # chan_id -> sats that actually landed there
    remaining = plan["amount_sats"]
    chunk_amount = remaining  # start with full amount
    min_chunk = 100_000       # never try less than 100k sats
    max_chunks = 10           # safety limit to prevent infinite splitting
    last_failure_reason = ""
    succeeded_chunks = 0

    for chunk_num in range(1, max_chunks + 1):
        if remaining < min_chunk:
            log.info("remaining %s sats is below minimum chunk %s — stopping",
                     f"{remaining:,}", f"{min_chunk:,}")
            break

        # Calculate fee limit for this chunk based on the budget ppm
        chunk_fee_limit = int(chunk_amount * plan["max_fee_ppm"] / 1_000_000 * 1.1)

        log.info("rebalance chunk %d: trying %s of %s remaining sats",
                 chunk_num, f"{chunk_amount:,}", f"{remaining:,}")
        _emit(f"chunk {chunk_num}: trying {chunk_amount:,} sats "
              f"(≤{chunk_fee_limit:,} sat fee, up to 120s)…")

        chunk_start = time.time()
        attempt = _attempt_single_rebalance(plan, chunk_amount, chunk_fee_limit,
                                            on_probe=on_probe)
        chunk_duration = time.time() - chunk_start

        if attempt["success"]:
            total_moved += chunk_amount
            total_fees += attempt["fee_paid"]
            remaining -= chunk_amount
            succeeded_chunks += 1
            log.info("  chunk %d succeeded: %s sats moved, fee %d sats (%.0f ppm)",
                     chunk_num, f"{chunk_amount:,}", attempt["fee_paid"], attempt["fee_ppm"])
            _emit(f"chunk {chunk_num}: ✓ moved {chunk_amount:,} sats in "
                  f"{chunk_duration:.0f}s, fee {attempt['fee_paid']:,} sats "
                  f"({attempt['fee_ppm']:.0f} ppm)")

            # Resolve where the chunk actually landed. last_hop_pubkey pins
            # the PEER, not the channel — with sibling channels to the same
            # peer LND may deliver into either one, and the books
            # (last_refill_ppm, fee floor, earned attribution) must follow
            # the sats, not the plan.
            landed_chan = lnd_client.get_invoice_landing_chan(
                attempt.get("payment_hash", "")) or plan["target_chan_id"]
            if landed_chan != plan["target_chan_id"]:
                log.info("  chunk landed on sibling channel %s (planned %s)",
                         landed_chan, plan["target_chan_id"])
            moved_by_target[landed_chan] = (
                moved_by_target.get(landed_chan, 0) + chunk_amount)

            # Persist this chunk as its own row so sync_rebalances can dedup by
            # payment_hash instead of misattributing it to a "manual" send.
            db.save_rebalance_attempt(
                plan["source_chan_id"], landed_chan,
                plan["source_alias"], plan["target_alias"],
                chunk_amount, attempt["fee_paid"],
                True, "", chunk_duration,
                payment_hash=attempt.get("payment_hash") or None,
                budget_ppm=plan["max_fee_ppm"],
                run_id=plan.get("run_id"),
                triggered_by=plan.get("triggered_by", "auto"),
            )

            # Try another chunk at same size if there's remaining
            if remaining < min_chunk:
                break
            chunk_amount = min(chunk_amount, remaining)

        else:
            log.info("  chunk %d failed: %s — halving amount",
                     chunk_num, attempt["failure_reason"])
            last_failure_reason = attempt["failure_reason"]
            # Halve the amount and retry
            chunk_amount = chunk_amount // 2
            if chunk_amount < min_chunk:
                log.info("  chunk size %s below minimum %s — giving up",
                         f"{chunk_amount:,}", f"{min_chunk:,}")
                _emit(f"chunk {chunk_num}: ✗ {attempt['failure_reason']} — "
                      f"next size {chunk_amount:,} below {min_chunk:,} floor, giving up")
                result["failure_reason"] = last_failure_reason
                break
            _emit(f"chunk {chunk_num}: ✗ {attempt['failure_reason']} — "
                  f"halving to {chunk_amount:,} sats and retrying")

    duration = time.time() - start

    if total_moved > 0:
        result["success"] = True
        result["amount"] = total_moved
        result["moved_by_target"] = moved_by_target
        result["fee_paid"] = total_fees
        result["fee_ppm"] = total_fees / total_moved * 1_000_000 if total_moved > 0 else 0
        log.info("rebalance complete: %s→%s moved %s of %s sats in %.1fs across %d chunk(s), "
                 "total fee %d sats (%.0f ppm)",
                 plan["source_alias"], plan["target_alias"],
                 f"{total_moved:,}", f"{plan['amount_sats']:,}",
                 duration, succeeded_chunks, total_fees, result["fee_ppm"])
    else:
        log.warning("rebalance failed completely: %s→%s — no sats moved after %d attempts in %.1fs",
                    plan["source_alias"], plan["target_alias"], chunk_num, duration)
        result["failure_reason"] = result["failure_reason"] or last_failure_reason
        # Only log a row for total failures — successful chunks were already saved above.
        db.save_rebalance_attempt(
            plan["source_chan_id"], plan["target_chan_id"],
            plan["source_alias"], plan["target_alias"],
            plan["amount_sats"], 0,
            False, result["failure_reason"], duration,
            budget_ppm=plan["max_fee_ppm"],
            run_id=plan.get("run_id"),
            triggered_by=plan.get("triggered_by", "auto"),
        )

    return result
