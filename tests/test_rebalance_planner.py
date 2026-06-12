"""Unit tests for B8 — QueryRoutes budget acceleration in the rebalance planner.

Run from project root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from engine import rebalance_planner as rp


def _target(local_ratio=0.04, cap=5_000_000):
    """A depleted target channel dict (needs inbound)."""
    return {
        "chan_id": "111",
        "peer_alias": "bfx-lnd0",
        "peer_pubkey": "ab" * 33,
        "capacity": cap,
        "local_balance": int(cap * local_ratio),
        "local_ratio": local_ratio,
    }


def _source(local_ratio=0.90, cap=5_000_000):
    """An overfull source channel dict (can donate)."""
    return {
        "chan_id": "222",
        "peer_alias": "Boltz",
        "peer_pubkey": "cd" * 33,
        "capacity": cap,
        "local_balance": int(cap * local_ratio),
        "local_ratio": local_ratio,
    }


def _budget(max_fee_ppm=14, ceiling=721, earned_ppm=576):
    return {
        "max_fee_ppm": max_fee_ppm,
        "affordable_ceiling_ppm": ceiling,
        "earned_ppm": earned_ppm,
        "reason": f"last_refill 7 ppm → {max_fee_ppm} ppm",
    }


class AccelerateBudgetTests(unittest.TestCase):
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_case2_jump_to_live_cost(self, mqr):
        # Route exists at 300 ppm, current bid 14, ceiling 721 → jump to 300.
        mqr.return_value = {"fee_ppm": 300, "hops": 3}
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 300)
        self.assertIn("QueryRoutes raised to 300", out["reason"])

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_case1_already_affordable_no_change(self, mqr):
        # Route cheaper than the current bid → the existing attempt already covers
        # it; don't touch the budget.
        mqr.return_value = {"fee_ppm": 10, "hops": 2}
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 14)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_route_no_change(self, mqr):
        # case 3/4: no affordable route → unchanged, normal attempt-and-fail proceeds.
        mqr.return_value = None
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 14)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_never_exceeds_ceiling(self, mqr):
        # Rounding could land a hair above the ceiling — clamp by leaving unchanged.
        mqr.return_value = {"fee_ppm": 722, "hops": 4}
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 14)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_headroom_skips_probe(self, mqr):
        # Already at the ceiling (profit-capped) → no probe, no change.
        out = rp._accelerate_budget_with_queryroutes(_budget(721, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 721)
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_source_skips_probe(self, mqr):
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [])
        self.assertEqual(out["max_fee_ppm"], 14)
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.REBALANCE_QUERYROUTES_ENABLED", False)
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_disabled_flag_skips_probe(self, mqr):
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 14)
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_probe_exception_never_breaks_planning(self, mqr):
        mqr.side_effect = RuntimeError("boom")
        out = rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        self.assertEqual(out["max_fee_ppm"], 14)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_probe_amount_and_fee_cap(self, mqr):
        # Target needs 2.3M to reach 50%, source can give 2M → probe at min = 2M,
        # fee cap = 2M × 721 ppm = 1442 sat, pinned to the source's outgoing chan.
        mqr.return_value = None
        rp._accelerate_budget_with_queryroutes(_budget(14, 721), _target(), [_source()])
        _, kwargs = mqr.call_args
        args, _ = mqr.call_args
        self.assertEqual(args[0], "ab" * 33)          # dest = target peer
        self.assertEqual(args[1], 2_000_000)          # probe amount = min(deficit, surplus)
        self.assertEqual(kwargs["fee_limit_sat"], 1442)
        self.assertEqual(kwargs["outgoing_chan_id"], "222")


class EarlyOutTests(unittest.TestCase):
    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_route_records_and_skips(self, mqr, msave):
        mqr.return_value = None  # confirmed no affordable route
        skip = rp._queryroutes_early_out(_target(), _budget(), _source(), 999, record=True)
        self.assertTrue(skip)
        msave.assert_called_once()
        # synthetic row: the depleted channel, a failure, distinctive reason, run_id
        _, kw = msave.call_args
        self.assertEqual(kw["success"], False)
        self.assertEqual(kw["failure_reason"], "QR_NO_AFFORDABLE_ROUTE")
        self.assertEqual(kw["run_id"], 999)

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_dry_run_skips_without_recording(self, mqr, msave):
        mqr.return_value = None
        skip = rp._queryroutes_early_out(_target(), _budget(), _source(), 999, record=False)
        self.assertTrue(skip)
        msave.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_affordable_route_does_not_skip(self, mqr, msave):
        mqr.return_value = {"fee_ppm": 300, "hops": 3}  # a route exists ≤ ceiling
        skip = rp._queryroutes_early_out(_target(), _budget(), _source(), 999, record=True)
        self.assertFalse(skip)
        msave.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_unjudged_never_early_outs(self, mqr, msave):
        # earned_ppm None → keep full price discovery via real attempts; no probe.
        skip = rp._queryroutes_early_out(_target(), _budget(earned_ppm=None), _source(), 999, True)
        self.assertFalse(skip)
        mqr.assert_not_called()
        msave.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_probe_unavailable_never_strands(self, mqr, msave):
        # A transport blip (raise) must NOT strand — fall through to normal attempt.
        mqr.side_effect = RuntimeError("LND down")
        skip = rp._queryroutes_early_out(_target(), _budget(), _source(), 999, record=True)
        self.assertFalse(skip)
        msave.assert_not_called()

    @patch("engine.rebalance_planner.REBALANCE_QUERYROUTES_EARLYOUT_ENABLED", False)
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_disabled_flag_skips_probe(self, mqr):
        skip = rp._queryroutes_early_out(_target(), _budget(), _source(), 999, record=True)
        self.assertFalse(skip)
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_source_does_not_early_out(self, mqr):
        skip = rp._queryroutes_early_out(_target(), _budget(), None, 999, record=True)
        self.assertFalse(skip)
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_probes_min_chunk_at_ceiling(self, mqr, _msave):
        mqr.return_value = None
        rp._queryroutes_early_out(_target(), _budget(ceiling=721), _source(), 999, record=False)
        args, kw = mqr.call_args
        self.assertEqual(args[0], "ab" * 33)                 # dest = target peer
        self.assertEqual(args[1], 100_000)                   # min-chunk probe size
        self.assertEqual(kw["fee_limit_sat"], 72)            # 100k × 721 ppm / 1e6
        self.assertEqual(kw["outgoing_chan_id"], "222")
        self.assertTrue(kw["raise_on_error"])                # must distinguish no-route


class AffordableCeilingTests(unittest.TestCase):
    @patch("engine.rebalance_planner.db")
    def test_ceiling_is_profit_cap_when_judged(self, mdb):
        mdb.get_last_refill_ppm.return_value = 7
        mdb.count_failures_since_last_success.return_value = 0
        mdb.get_channel_earned_ppm.return_value = (576, 10_000_000)
        b = rp.get_channel_rebalance_budget("111")
        # earned 576 × horizon 1.25 = 720
        self.assertEqual(b["affordable_ceiling_ppm"], round(576 * config.REBALANCE_PROFIT_HORIZON))

    @patch("engine.rebalance_planner.db")
    def test_ceiling_is_max_when_unjudged(self, mdb):
        mdb.get_last_refill_ppm.return_value = 7
        mdb.count_failures_since_last_success.return_value = 0
        mdb.get_channel_earned_ppm.return_value = (None, 0)
        b = rp.get_channel_rebalance_budget("111")
        self.assertEqual(b["affordable_ceiling_ppm"], config.REBALANCE_MAX_BUDGET_PPM)


if __name__ == "__main__":
    unittest.main()
