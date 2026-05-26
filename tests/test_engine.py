"""Unit tests for pure money-math in engine.py.

Run from project root:
    python3 -m unittest discover tests
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import engine


# ─── sigmoid_fee_ppm ─────────────────────────────────────────────

class SigmoidFeePpmTests(unittest.TestCase):
    def test_endpoints_approach_min_and_max(self):
        # Drained channel (local_ratio=0) → near SIGMOID_MAX_PPM
        self.assertGreaterEqual(engine.sigmoid_fee_ppm(0.0), config.SIGMOID_MAX_PPM - 10)
        self.assertLessEqual(engine.sigmoid_fee_ppm(0.0), config.SIGMOID_MAX_PPM)
        # Full-local channel (local_ratio=1) → near SIGMOID_MIN_PPM
        self.assertGreaterEqual(engine.sigmoid_fee_ppm(1.0), config.SIGMOID_MIN_PPM)
        self.assertLessEqual(engine.sigmoid_fee_ppm(1.0), config.SIGMOID_MIN_PPM + 10)

    def test_midpoint_is_halfway(self):
        mid = (config.SIGMOID_MIN_PPM + config.SIGMOID_MAX_PPM) / 2.0
        self.assertAlmostEqual(engine.sigmoid_fee_ppm(config.SIGMOID_MIDPOINT), mid, delta=2)

    def test_monotonically_decreasing(self):
        vals = [engine.sigmoid_fee_ppm(r / 20.0) for r in range(21)]
        for a, b in zip(vals, vals[1:]):
            self.assertGreaterEqual(a, b, f"non-monotonic at {a}→{b}: {vals}")

    def test_clamped_outside_unit_range(self):
        # Out-of-range inputs still produce a value inside [MIN, MAX]
        self.assertEqual(engine.sigmoid_fee_ppm(-5.0), config.SIGMOID_MAX_PPM)
        self.assertEqual(engine.sigmoid_fee_ppm(5.0), config.SIGMOID_MIN_PPM)


# ─── compute_fee_target ──────────────────────────────────────────

class ComputeFeeTargetTests(unittest.TestCase):
    def _channel(self, local_ratio):
        return {"chan_id": "test_chan", "local_ratio": local_ratio}

    def test_no_history_uses_sigmoid_only(self):
        with patch("db.get_last_refill_ppm", return_value=None):
            r = engine.compute_fee_target(self._channel(0.5), {"market_multiplier": 0.0}, now=0)
        self.assertEqual(r["floor_ppm"], 0)
        self.assertEqual(r["source"], "sigmoid")
        self.assertEqual(r["target_ppm"], engine.sigmoid_fee_ppm(0.5))

    def test_floor_dominates_when_last_refill_high(self):
        # last_refill * REBALANCE_FEE_MARGIN should beat sigmoid in mid-zone
        with patch("db.get_last_refill_ppm", return_value=1000):
            r = engine.compute_fee_target(self._channel(0.5), {"market_multiplier": 0.0}, now=0)
        expected_floor = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        self.assertEqual(r["floor_ppm"], expected_floor)
        self.assertEqual(r["source"], "floor")
        self.assertEqual(r["target_ppm"], expected_floor)

    def test_defense_zone_blocks_negative_market_multiplier(self):
        # In the low-local defense zone, a negative mult must not lower the fee
        # below the sigmoid base — otherwise we'd invite further drain.
        with patch("db.get_last_refill_ppm", return_value=None):
            base = engine.sigmoid_fee_ppm(0.1)
            r = engine.compute_fee_target(self._channel(0.1), {"market_multiplier": -0.5}, now=0)
        # 0.1 is below FEE_HYSTERESIS_EDGE_LOW (0.20), so defense kicks in
        self.assertLess(0.1, config.FEE_HYSTERESIS_EDGE_LOW)
        self.assertEqual(r["target_ppm"], base)

    def test_hard_ceiling_clamps_runaway_floor(self):
        # Even a huge last_refill cannot push target above FEE_HARD_CEILING_PPM
        with patch("db.get_last_refill_ppm", return_value=999_999):
            r = engine.compute_fee_target(self._channel(0.5), {"market_multiplier": 0.0}, now=0)
        self.assertEqual(r["target_ppm"], config.FEE_HARD_CEILING_PPM)


# ─── get_channel_rebalance_budget ────────────────────────────────

class RebalanceBudgetTests(unittest.TestCase):
    def test_bootstrap_default_when_no_history(self):
        with patch("db.get_last_refill_ppm", return_value=None), \
             patch("db.count_failures_since_last_success", return_value=0):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], config.REBALANCE_DEFAULT_BUDGET_PPM)
        self.assertEqual(r["failures_since_success"], 0)

    def test_anchors_on_last_refill_with_no_failures(self):
        with patch("db.get_last_refill_ppm", return_value=300), \
             patch("db.count_failures_since_last_success", return_value=0):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], 300)

    def test_escalates_with_failures(self):
        # 300 * (1 + 0.20 * 2) = 420
        with patch("db.get_last_refill_ppm", return_value=300), \
             patch("db.count_failures_since_last_success", return_value=2):
            r = engine.get_channel_rebalance_budget("chan")
        expected = int(round(300 * (1.0 + config.REBALANCE_BUDGET_ESCALATION_STEP * 2)))
        self.assertEqual(r["max_fee_ppm"], expected)

    def test_capped_at_max_budget(self):
        # Huge last_refill + many failures still clamped at the ceiling
        with patch("db.get_last_refill_ppm", return_value=10_000), \
             patch("db.count_failures_since_last_success", return_value=10):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], config.REBALANCE_MAX_BUDGET_PPM)


# ─── calculate_rebalance_amount ──────────────────────────────────

class CalculateRebalanceAmountTests(unittest.TestCase):
    def test_inbound_pulls_depleted_channel_toward_target(self):
        # 10M-sat channel at 10% local → want to get to 50% → move 4M sats
        ch = {"capacity": 10_000_000, "local_balance": 1_000_000}
        amount = engine.calculate_rebalance_amount(ch, direction="inbound")
        self.assertEqual(amount, 4_000_000)

    def test_outbound_drains_overfull_channel_toward_target(self):
        # 10M-sat channel at 90% local → want to get to 50% → move 4M sats
        ch = {"capacity": 10_000_000, "local_balance": 9_000_000}
        amount = engine.calculate_rebalance_amount(ch, direction="outbound")
        self.assertEqual(amount, 4_000_000)

    def test_capped_at_max_amount_ratio(self):
        # Severely depleted huge channel: gap > MAX_AMOUNT_RATIO * capacity → capped
        ch = {"capacity": 10_000_000, "local_balance": 0}
        amount = engine.calculate_rebalance_amount(ch, direction="inbound")
        self.assertEqual(amount, int(10_000_000 * config.REBALANCE_MAX_AMOUNT_RATIO))

    def test_below_min_returns_zero(self):
        # Tiny channel where the gap is under 50k sats → skip rebalance
        ch = {"capacity": 100_000, "local_balance": 49_000}  # gap = 1k
        self.assertEqual(engine.calculate_rebalance_amount(ch, direction="inbound"), 0)

    def test_target_ratio_override(self):
        # Force a 30% target instead of the default 50%
        ch = {"capacity": 10_000_000, "local_balance": 1_000_000}
        amount = engine.calculate_rebalance_amount(ch, direction="inbound", target_ratio=0.30)
        self.assertEqual(amount, 2_000_000)


# ─── compute_market_multiplier ───────────────────────────────────

class MarketMultiplierTests(unittest.TestCase):
    def test_busy_nudges_up(self):
        now = int(time.time())
        with patch("db.get_last_forward_ts", return_value=now - 3600):  # 1h ago
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=0.0)
        self.assertAlmostEqual(mult, config.MARKET_MULT_STEP, places=5)

    def test_silent_nudges_down(self):
        now = int(time.time())
        silent_secs = (config.MARKET_MULT_SILENT_DAYS + 1) * 86400
        with patch("db.get_last_forward_ts", return_value=now - silent_secs):
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=0.0)
        self.assertAlmostEqual(mult, -config.MARKET_MULT_STEP, places=5)

    def test_idle_window_leaves_multiplier_unchanged(self):
        # Between busy (24h) and silent (3d) → no nudge
        now = int(time.time())
        idle_secs = (config.MARKET_MULT_BUSY_HOURS * 3600) + 86400  # 1d past busy, still <3d
        self.assertLess(idle_secs, config.MARKET_MULT_SILENT_DAYS * 86400)
        with patch("db.get_last_forward_ts", return_value=now - idle_secs):
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=0.42)
        self.assertAlmostEqual(mult, 0.42, places=5)

    def test_never_forwarded_nudges_down(self):
        with patch("db.get_last_forward_ts", return_value=None):
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=0.0)
        self.assertAlmostEqual(mult, -config.MARKET_MULT_STEP, places=5)

    def test_clamped_at_max(self):
        now = int(time.time())
        with patch("db.get_last_forward_ts", return_value=now - 3600):
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=config.MARKET_MULT_MAX)
        self.assertEqual(mult, config.MARKET_MULT_MAX)

    def test_clamped_at_min(self):
        now = int(time.time())
        silent_secs = (config.MARKET_MULT_SILENT_DAYS + 1) * 86400
        with patch("db.get_last_forward_ts", return_value=now - silent_secs):
            mult, _ = engine.compute_market_multiplier("chan", prev_mult=config.MARKET_MULT_MIN)
        self.assertEqual(mult, config.MARKET_MULT_MIN)


if __name__ == "__main__":
    unittest.main()
