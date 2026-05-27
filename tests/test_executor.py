"""Unit tests for execute_rebalance_plans in main.py.

The executor carries two ledgers (target_deficits, source_remaining) across
plan attempts. These tests exercise the ledger arithmetic by stubbing the
single-rebalance executor with deterministic outcomes — no LND, no DB.

Run from project root:
    python3 -m unittest discover tests
"""
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


def _silent_log():
    log = logging.getLogger("test-executor")
    log.handlers = [logging.NullHandler()]
    log.propagate = False
    return log


def _plan(source, target, amount, *, source_surplus, target_deficit,
          is_fallback=False, max_fee_ppm=500):
    """Build a plan dict matching what engine.plan_rebalances produces."""
    return {
        "source_chan_id": f"src-{source}",
        "source_alias": source,
        "source_channel_point": f"{source}:0",
        "source_local_ratio": 0.9,
        "source_total_surplus": source_surplus,
        "target_chan_id": f"tgt-{target}",
        "target_alias": target,
        "target_channel_point": f"{target}:0",
        "target_peer_pubkey": f"pk-{target}",
        "target_local_ratio": 0.1,
        "target_total_deficit": target_deficit,
        "amount_sats": amount,
        "max_fee_sats": int(amount * max_fee_ppm / 1_000_000 * 1.1),
        "max_fee_ppm": max_fee_ppm,
        "budget_reason": "test",
        "is_fallback": is_fallback,
    }


def _success(amount, *, fee_paid=0, fee_ppm=0.0):
    return {"success": True, "amount": amount,
            "fee_paid": fee_paid, "fee_ppm": fee_ppm}


def _failure(reason="no_route"):
    return {"success": False, "amount": 0, "fee_paid": 0,
            "fee_ppm": 0, "failure_reason": reason}


class StubExecutor:
    """Records calls and returns scripted outcomes."""

    def __init__(self, outcomes):
        # outcomes: list of result dicts in the order calls happen
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, plan, dry_run=False):
        self.calls.append({
            "source": plan["source_alias"],
            "target": plan["target_alias"],
            "amount": plan["amount_sats"],
            "max_fee_sats": plan["max_fee_sats"],
        })
        if not self.outcomes:
            raise AssertionError(
                f"executor called more times than expected ({plan['source_alias']}→{plan['target_alias']})"
            )
        return self.outcomes.pop(0)


# Silence the print() statements inside the executor for clean test output.
def _run(plans, executor):
    with patch("sys.stdout", new=io.StringIO()):
        return main.execute_rebalance_plans(plans, _silent_log(), executor=executor)


class TargetDeficitTests(unittest.TestCase):

    def test_primary_fully_fills_target_skips_all_fallbacks(self):
        plans = [
            _plan("Boltz", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000),
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000,
                  is_fallback=True),
            _plan("LNBig", "ACINQ", 1_000_000,
                  source_surplus=1_000_000, target_deficit=3_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([_success(3_000_000)])

        results = _run(plans, stub)

        # Only the primary fires; fallbacks short-circuit on deficit=0.
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["source"], "Boltz")
        self.assertEqual(len(results), 1)

    def test_primary_partial_success_lets_fallback_top_up(self):
        # Primary moves 2M out of 3M deficit; fallback takes the remaining 1M.
        plans = [
            _plan("Boltz", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000),
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([
            _success(2_000_000),   # Boltz primary moves only 2M (chunked)
            _success(1_000_000),   # Kraken fallback fills the gap
        ])

        _run(plans, stub)

        self.assertEqual(len(stub.calls), 2)
        # Kraken's attempt is capped at the 1M still-needed, not its 3M plan amount.
        self.assertEqual(stub.calls[1]["source"], "Kraken")
        self.assertEqual(stub.calls[1]["amount"], 1_000_000)
        # max_fee_sats is recomputed from the capped amount.
        expected_fee = int(1_000_000 * 500 / 1_000_000 * 1.1)
        self.assertEqual(stub.calls[1]["max_fee_sats"], expected_fee)

    def test_fallback_caps_at_remaining_deficit_no_overshoot(self):
        # Target needs 1M, primary failed entirely. Fallback plan is 3M but
        # must shrink to 1M.
        plans = [
            _plan("Boltz", "ACINQ", 1_000_000,
                  source_surplus=1_000_000, target_deficit=1_000_000),
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=1_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([_failure(), _success(1_000_000)])

        _run(plans, stub)

        self.assertEqual(stub.calls[1]["amount"], 1_000_000)

    def test_deficit_below_minimum_skips_remaining_plans(self):
        # Primary leaves only 30k unfilled — below the 50k minimum chunk —
        # so the fallback should be skipped, not attempted.
        plans = [
            _plan("Boltz", "ACINQ", 1_000_000,
                  source_surplus=1_000_000, target_deficit=1_000_000),
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=1_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([_success(970_000)])

        _run(plans, stub)

        self.assertEqual(len(stub.calls), 1)


class SourceRemainingTests(unittest.TestCase):

    def test_source_drained_by_earlier_success_skips_later_fallback(self):
        # Two targets, two sources. Kraken's primary for LNBig succeeds in
        # full, draining Kraken. Boltz primary for ACINQ fails. Kraken's
        # fallback for ACINQ must NOT fire (source exhausted).
        plans = [
            # Primaries (planner emits in target-order)
            _plan("Boltz", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000),
            _plan("Kraken", "LNBig", 5_000_000,
                  source_surplus=5_000_000, target_deficit=5_000_000),
            # Fallbacks
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=5_000_000, target_deficit=3_000_000,
                  is_fallback=True),
            _plan("Boltz", "LNBig", 3_000_000,
                  source_surplus=3_000_000, target_deficit=5_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([
            _failure(),             # Boltz → ACINQ primary fails
            _success(5_000_000),    # Kraken → LNBig primary succeeds in full
            # Kraken → ACINQ fallback MUST be skipped (Kraken drained)
            # Boltz → LNBig fallback MUST be skipped (LNBig satisfied)
        ])

        _run(plans, stub)

        attempted = [(c["source"], c["target"]) for c in stub.calls]
        self.assertEqual(attempted, [("Boltz", "ACINQ"), ("Kraken", "LNBig")])

    def test_source_partial_drain_caps_next_attempt(self):
        # Kraken (3M surplus) is primary for ACINQ (1M deficit) — moves 1M.
        # Kraken should have 2M remaining, capping its next plan accordingly.
        plans = [
            _plan("Kraken", "ACINQ", 1_000_000,
                  source_surplus=3_000_000, target_deficit=1_000_000),
            _plan("Kraken", "LNBig", 3_000_000,
                  source_surplus=3_000_000, target_deficit=3_000_000),
        ]
        stub = StubExecutor([
            _success(1_000_000),    # Kraken → ACINQ moves 1M
            _success(2_000_000),    # Kraken → LNBig capped to 2M remaining
        ])

        _run(plans, stub)

        self.assertEqual(stub.calls[1]["amount"], 2_000_000)


class FallbackChainingTests(unittest.TestCase):

    def test_multiple_fallbacks_chain_until_deficit_drained(self):
        # ACINQ needs 5M. Primary Boltz (5M) fails. Fallbacks: Kraken (3M),
        # LNBig (1M), CoinPayments (5M). Expected: Kraken delivers 3M,
        # LNBig delivers 1M, CoinPayments is capped to 1M remainder.
        plans = [
            _plan("Boltz", "ACINQ", 5_000_000,
                  source_surplus=5_000_000, target_deficit=5_000_000),
            _plan("Kraken", "ACINQ", 3_000_000,
                  source_surplus=3_000_000, target_deficit=5_000_000,
                  is_fallback=True),
            _plan("LNBig", "ACINQ", 1_000_000,
                  source_surplus=1_000_000, target_deficit=5_000_000,
                  is_fallback=True),
            _plan("CoinPayments", "ACINQ", 5_000_000,
                  source_surplus=5_000_000, target_deficit=5_000_000,
                  is_fallback=True),
        ]
        stub = StubExecutor([
            _failure(),
            _success(3_000_000),
            _success(1_000_000),
            _success(1_000_000),
        ])

        _run(plans, stub)

        self.assertEqual(len(stub.calls), 4)
        # CoinPayments must shrink to the 1M still needed, not its 5M plan amount.
        self.assertEqual(stub.calls[3]["source"], "CoinPayments")
        self.assertEqual(stub.calls[3]["amount"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
