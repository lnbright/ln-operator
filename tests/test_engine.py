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
import lnd_client


# ─── sigmoid_fee_ppm ─────────────────────────────────────────────

class SigmoidFeePpmTests(unittest.TestCase):
    def test_endpoints_approach_min_and_max(self):
        # At K=8 / midpoint=0.5 the unit endpoints sit at sigmoid(±4) ≈ 1.8%
        # inside each asymptote, so the tolerance must scale with the curve span
        # (not a fixed 10 ppm — that only held for the old narrow 25..250 range).
        span = config.SIGMOID_MAX_PPM - config.SIGMOID_MIN_PPM
        tol = max(10, round(span * 0.03))
        # Drained channel (local_ratio=0) → near SIGMOID_MAX_PPM
        self.assertGreaterEqual(engine.sigmoid_fee_ppm(0.0), config.SIGMOID_MAX_PPM - tol)
        self.assertLessEqual(engine.sigmoid_fee_ppm(0.0), config.SIGMOID_MAX_PPM)
        # Full-local channel (local_ratio=1) → near SIGMOID_MIN_PPM
        self.assertGreaterEqual(engine.sigmoid_fee_ppm(1.0), config.SIGMOID_MIN_PPM)
        self.assertLessEqual(engine.sigmoid_fee_ppm(1.0), config.SIGMOID_MIN_PPM + tol)

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


# ─── Soft-floor decay (Layer 2) ──────────────────────────────────

class SoftFloorDecayTests(unittest.TestCase):
    NOW = 1_000_000_000

    def _channel(self, local_ratio=0.5):
        return {"chan_id": "c", "local_ratio": local_ratio}

    def _signals(self, **kw):
        s = {"market_multiplier": 0.0}
        s.update(kw)
        return s

    def test_no_decay_while_active(self):
        # Forwarded recently → not idle → effective floor == hard floor.
        with patch("db.get_last_refill_ppm", return_value=1000):
            r = engine.compute_fee_target(
                self._channel(), self._signals(), now=self.NOW,
                last_forward_ts=self.NOW - 3600,  # 1h ago
            )
        expected = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        self.assertEqual(r["floor_ppm"], expected)
        self.assertEqual(r["source"], "floor")
        self.assertEqual(r["target_ppm"], expected)

    def test_decays_toward_market_when_idle(self):
        base = engine.sigmoid_fee_ppm(0.5)
        hard = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        started = self.NOW - int(config.FLOOR_DECAY_HALFLIFE_DAYS * 86400)  # one half-life ago
        with patch("db.get_last_refill_ppm", return_value=1000):
            r = engine.compute_fee_target(
                self._channel(), self._signals(
                    floor_decay_started_ts=started, floor_decay_anchor_ppm=hard),
                now=self.NOW, last_forward_ts=self.NOW - 10 * 86400,  # 10d idle
            )
        expected = base + (hard - base) * 0.5  # one half-life
        self.assertAlmostEqual(r["target_ppm"], round(expected), delta=1)
        self.assertLess(r["target_ppm"], hard)
        self.assertGreater(r["target_ppm"], base)
        self.assertEqual(r["source"], "floor-decaying")

    def test_decay_never_below_market_clearing(self):
        base = engine.sigmoid_fee_ppm(0.5)
        hard = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        started = self.NOW - 200 * 86400  # ages → decay ≈ 0
        with patch("db.get_last_refill_ppm", return_value=1000):
            r = engine.compute_fee_target(
                self._channel(), self._signals(
                    floor_decay_started_ts=started, floor_decay_anchor_ppm=hard),
                now=self.NOW, last_forward_ts=self.NOW - 200 * 86400,
            )
        self.assertEqual(r["target_ppm"], base)  # floor fully decayed to the clearing fee

    def test_first_idle_cycle_anchors_at_hard_floor(self):
        # No prior decay clock → starts now → age 0 → no give-back yet, and the
        # caller is told to persist started_ts = now.
        hard = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        with patch("db.get_last_refill_ppm", return_value=1000):
            r = engine.compute_fee_target(
                self._channel(), self._signals(floor_decay_started_ts=0),
                now=self.NOW, last_forward_ts=self.NOW - 10 * 86400,
            )
        self.assertEqual(r["target_ppm"], hard)
        self.assertEqual(r["floor_decay_started_ts"], self.NOW)
        self.assertEqual(r["floor_decay_anchor_ppm"], hard)

    def test_disabled_when_halflife_zero(self):
        hard = int(round(1000 * config.REBALANCE_FEE_MARGIN))
        with patch("db.get_last_refill_ppm", return_value=1000), \
             patch("engine.fees.FLOOR_DECAY_HALFLIFE_DAYS", 0):
            r = engine.compute_fee_target(
                self._channel(), self._signals(
                    floor_decay_started_ts=self.NOW - 50 * 86400, floor_decay_anchor_ppm=hard),
                now=self.NOW, last_forward_ts=self.NOW - 50 * 86400,
            )
        self.assertEqual(r["target_ppm"], hard)  # no decay
        self.assertEqual(r["source"], "floor")


# ─── get_channel_rebalance_budget ────────────────────────────────

class RebalanceBudgetTests(unittest.TestCase):
    # earned_ppm=None (unjudged) on the legacy cases → profit cap never binds,
    # so escalation behaves exactly as before.
    def test_bootstrap_default_when_no_history(self):
        with patch("db.get_last_refill_ppm", return_value=None), \
             patch("db.count_failures_since_last_success", return_value=0), \
             patch("db.get_channel_earned_ppm", return_value=(None, 0)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], config.REBALANCE_DEFAULT_BUDGET_PPM)
        self.assertEqual(r["failures_since_success"], 0)

    def test_anchors_on_last_refill_with_no_failures(self):
        with patch("db.get_last_refill_ppm", return_value=300), \
             patch("db.count_failures_since_last_success", return_value=0), \
             patch("db.get_channel_earned_ppm", return_value=(None, 0)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], 300)

    def test_escalates_with_failures(self):
        # 300 * (1 + 0.20 * 2) = 420
        with patch("db.get_last_refill_ppm", return_value=300), \
             patch("db.count_failures_since_last_success", return_value=2), \
             patch("db.get_channel_earned_ppm", return_value=(None, 0)):
            r = engine.get_channel_rebalance_budget("chan")
        expected = int(round(300 * (1.0 + config.REBALANCE_BUDGET_ESCALATION_STEP * 2)))
        self.assertEqual(r["max_fee_ppm"], expected)

    def test_capped_at_max_budget(self):
        # Huge last_refill + many failures still clamped at the ceiling
        with patch("db.get_last_refill_ppm", return_value=10_000), \
             patch("db.count_failures_since_last_success", return_value=10), \
             patch("db.get_channel_earned_ppm", return_value=(None, 0)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], config.REBALANCE_MAX_BUDGET_PPM)

    # ── Layer 1 profitability gate ──
    def test_unjudged_keeps_full_escalation(self):
        # No earned-ppm judgement → escalation runs free (this is the LNbits
        # bootstrap case the gate must NOT clamp to a useless low budget).
        with patch("db.get_last_refill_ppm", return_value=2000), \
             patch("db.count_failures_since_last_success", return_value=3), \
             patch("db.get_channel_earned_ppm", return_value=(None, 500_000)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], int(round(2000 * (1 + 0.20 * 3))))
        self.assertFalse(r["profit_capped"])

    def test_profit_cap_binds_for_judged_channel(self):
        # earns 400 ppm → cap = 400×3 = 1200; escalation wants 3200 → capped.
        with patch("db.get_last_refill_ppm", return_value=2000), \
             patch("db.count_failures_since_last_success", return_value=3), \
             patch("db.get_channel_earned_ppm", return_value=(400.0, 50_000_000)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertEqual(r["max_fee_ppm"], int(round(400.0 * config.REBALANCE_PROFIT_HORIZON)))
        self.assertTrue(r["profit_capped"])
        self.assertFalse(r["structural"])

    def test_profitable_channel_not_capped(self):
        # Earns well above the recoup price → cap (earned×horizon) exceeds the
        # escalated budget, so escalation wins and the channel isn't capped.
        earned = 4000.0
        with patch("db.get_last_refill_ppm", return_value=2000), \
             patch("db.count_failures_since_last_success", return_value=3), \
             patch("db.get_channel_earned_ppm", return_value=(earned, 50_000_000)):
            r = engine.get_channel_rebalance_budget("chan")
        escalated = int(round(2000 * (1 + 0.20 * 3)))
        self.assertGreater(earned * config.REBALANCE_PROFIT_HORIZON, escalated)  # guard the premise
        self.assertEqual(r["max_fee_ppm"], escalated)
        self.assertFalse(r["profit_capped"])

    def test_structural_when_capped_and_failing(self):
        # Profit-capped AND failures past the structural threshold → structural.
        fails = config.REBALANCE_STRUCTURAL_FAIL_THRESHOLD
        with patch("db.get_last_refill_ppm", return_value=2000), \
             patch("db.count_failures_since_last_success", return_value=fails), \
             patch("db.get_channel_earned_ppm", return_value=(300.0, 50_000_000)):
            r = engine.get_channel_rebalance_budget("chan")
        self.assertTrue(r["profit_capped"])
        self.assertTrue(r["structural"])


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


# ─── chan_open_ts_from_id ────────────────────────────────────────

class ChanOpenTsTests(unittest.TestCase):
    def _chan_id(self, block_height):
        # LND packs the funding-tx block in the high 24 bits (>> 40).
        return str(block_height << 40)

    def test_returns_reasonable_timestamp_for_valid_input(self):
        # Channel opened 100 blocks before tip; `now` is 2026-05-27 00:00 UTC.
        # Expected: now - 100*600 - 86400 (the 1-day safety margin).
        now = 1748390400
        tip = 950_000
        chan_id = self._chan_id(tip - 100)
        ts = engine.chan_open_ts_from_id(chan_id, tip, now)
        self.assertEqual(ts, now - 100 * 600 - 86400)

    def test_invalid_chan_id_returns_zero(self):
        self.assertEqual(engine.chan_open_ts_from_id("not-a-number", 950_000, 0), 0)
        self.assertEqual(engine.chan_open_ts_from_id(None, 950_000, 0), 0)

    def test_open_block_after_tip_returns_zero(self):
        # Garbage chan_id that decodes to a future block → can't be a real channel
        future = self._chan_id(1_000_000)
        self.assertEqual(engine.chan_open_ts_from_id(future, 950_000, 0), 0)

    def test_zero_tip_returns_zero(self):
        # Defensive: if we can't read chain tip, refuse to guess
        self.assertEqual(engine.chan_open_ts_from_id(self._chan_id(950_000), 0, 0), 0)

    def test_never_negative(self):
        # Very recent channel + tiny `now` → clamp to 0, not negative
        now = 100
        tip = 950_000
        chan_id = self._chan_id(tip)  # opened *at* tip
        self.assertEqual(engine.chan_open_ts_from_id(chan_id, tip, now), 0)


# ─── _edge_zone ──────────────────────────────────────────────────

class EdgeZoneTests(unittest.TestCase):
    def test_low_zone(self):
        self.assertEqual(engine._edge_zone(0.1), "low")
        self.assertEqual(engine._edge_zone(0.0), "low")

    def test_mid_zone(self):
        self.assertEqual(engine._edge_zone(0.5), "mid")

    def test_high_zone(self):
        self.assertEqual(engine._edge_zone(0.9), "high")
        self.assertEqual(engine._edge_zone(1.0), "high")

    def test_boundary_is_mid(self):
        # Comparisons are strict (`<` / `>`), so the boundary ratio itself is mid.
        self.assertEqual(engine._edge_zone(config.FEE_HYSTERESIS_EDGE_LOW), "mid")
        self.assertEqual(engine._edge_zone(config.FEE_HYSTERESIS_EDGE_HIGH), "mid")


# ─── _should_broadcast ───────────────────────────────────────────

class ShouldBroadcastTests(unittest.TestCase):
    def test_within_tolerance_skipped(self):
        # Need to fail BOTH abs and pct tolerance to skip. ±2 ppm on 150 is well
        # under both 10ppm absolute and 10% relative thresholds.
        ok, _ = engine._should_broadcast(152, 150, signals={}, local_ratio=0.5, now=1000)
        self.assertFalse(ok)

    def test_snap_overrides_cooldown(self):
        # Big delta (≥ SNAP_PPM) should fire even within cooldown
        signals = {"last_fee_update_ts": 1000}  # 0s ago
        ok, why = engine._should_broadcast(300, 100, signals=signals, local_ratio=0.5, now=1000)
        self.assertTrue(ok)
        self.assertIn("snap", why)

    def test_cooldown_blocks_normal_change(self):
        # Mid-magnitude change (clears tolerance, below snap) inside cooldown
        signals = {"last_fee_update_ts": 1000}
        ok, why = engine._should_broadcast(125, 100, signals=signals, local_ratio=0.5, now=1000 + 60)
        self.assertFalse(ok)
        self.assertIn("cooldown", why)

    def test_edge_crossing_overrides_cooldown(self):
        # Same change as the cooldown test but local_ratio crossed from mid into low → fire anyway
        signals = {"last_fee_update_ts": 1000, "last_local_ratio": 0.5}
        ok, why = engine._should_broadcast(125, 100, signals=signals, local_ratio=0.15, now=1000 + 60)
        self.assertTrue(ok)
        self.assertIn("edge crossing", why)

    def test_no_history_broadcasts(self):
        # First-ever update — no last_fee_update_ts, no last_local_ratio
        ok, _ = engine._should_broadcast(125, 100, signals={}, local_ratio=0.5, now=1000)
        self.assertTrue(ok)

    def test_after_cooldown_broadcasts(self):
        signals = {"last_fee_update_ts": 1000}
        later = 1000 + config.FEE_HYSTERESIS_COOLDOWN_SEC + 1
        ok, _ = engine._should_broadcast(125, 100, signals=signals, local_ratio=0.5, now=later)
        self.assertTrue(ok)


# ─── find_rebalance_candidates ───────────────────────────────────

class FindRebalanceCandidatesTests(unittest.TestCase):
    def _ch(self, chan_id, ratio, active=True):
        return {"chan_id": chan_id, "local_ratio": ratio, "active": active}

    def test_partitions_by_default_thresholds(self):
        chans = [
            self._ch("depleted", 0.05),
            self._ch("mid", 0.50),
            self._ch("overfull", 0.95),
        ]
        inbound, outbound = engine.find_rebalance_candidates(channels=chans)
        self.assertEqual([c["chan_id"] for c in inbound], ["depleted"])
        self.assertEqual([c["chan_id"] for c in outbound], ["overfull"])

    def test_inactive_channels_excluded(self):
        chans = [
            self._ch("offline-depleted", 0.05, active=False),
            self._ch("offline-overfull", 0.95, active=False),
        ]
        inbound, outbound = engine.find_rebalance_candidates(channels=chans)
        self.assertEqual(inbound, [])
        self.assertEqual(outbound, [])

    def test_inbound_sorted_most_depleted_first(self):
        chans = [
            self._ch("a", 0.18),
            self._ch("b", 0.05),
            self._ch("c", 0.12),
        ]
        inbound, _ = engine.find_rebalance_candidates(channels=chans)
        self.assertEqual([c["chan_id"] for c in inbound], ["b", "c", "a"])

    def test_outbound_sorted_most_overfull_first(self):
        chans = [
            self._ch("a", 0.82),
            self._ch("b", 0.99),
            self._ch("c", 0.91),
        ]
        _, outbound = engine.find_rebalance_candidates(channels=chans)
        self.assertEqual([c["chan_id"] for c in outbound], ["b", "c", "a"])

    def test_force_override_uses_custom_threshold(self):
        # With force=0.4, anything <0.4 is inbound, >0.4 is outbound — bypasses
        # the normal 0.20/0.80 thresholds entirely.
        chans = [
            self._ch("low", 0.30),    # would be 'mid' under defaults
            self._ch("high", 0.55),   # would be 'mid' under defaults
        ]
        inbound, outbound = engine.find_rebalance_candidates(channels=chans, force=0.4)
        self.assertEqual([c["chan_id"] for c in inbound], ["low"])
        self.assertEqual([c["chan_id"] for c in outbound], ["high"])


# ─── update_channel_policy inbound fees (Layer 3) ────────────────

class UpdateChannelPolicyInboundTests(unittest.TestCase):
    def test_omits_inbound_when_not_set(self):
        with patch("lnd_client._post", return_value={}) as p:
            lnd_client.update_channel_policy("txid:0", 0, 100)
        data = p.call_args[0][1]
        self.assertNotIn("inbound_fee", data)
        self.assertEqual(data["fee_rate_ppm"], "100")

    def test_includes_signed_inbound_when_set(self):
        with patch("lnd_client._post", return_value={}) as p:
            lnd_client.update_channel_policy("txid:0", 0, 100, inbound_fee_rate_ppm=-50)
        data = p.call_args[0][1]
        self.assertEqual(data["inbound_fee"], {"fee_rate_ppm": -50, "base_fee_msat": 0})

    def test_inbound_zero_is_sent_explicitly(self):
        # 0 must be sent (to clear), not omitted — the LND 0.20 reset footgun.
        with patch("lnd_client._post", return_value={}) as p:
            lnd_client.update_channel_policy("txid:0", 0, 100, inbound_fee_rate_ppm=0)
        data = p.call_args[0][1]
        self.assertIn("inbound_fee", data)
        self.assertEqual(data["inbound_fee"]["fee_rate_ppm"], 0)


# ─── decide_channel_action ladder (Layer 3) ──────────────────────

class DecisionLadderTests(unittest.TestCase):
    def _ch(self, lr):
        return {"chan_id": "c", "local_ratio": lr, "peer_alias": "C"}

    def test_depleted_profitable_rebalances(self):
        b = {"profit_capped": False, "structural": False}
        r = engine.decide_channel_action(self._ch(0.05), {}, b, 200, True, 0)
        self.assertEqual(r["action"], "rebalance")

    def test_inbound_disabled_collapses_to_none(self):
        # depleted + structural would be inbound_discount, but disabled → none, 0.
        b = {"profit_capped": True, "structural": True}
        r = engine.decide_channel_action(self._ch(0.05), {}, b, 200, True, 0)
        self.assertEqual(r["action"], "none")
        self.assertEqual(r["inbound_fee_ppm"], 0)

    def test_structural_discounts_when_enabled(self):
        b = {"profit_capped": True, "structural": True}
        with patch("engine.liquidity_policy.INBOUND_FEE_ENABLED", True):
            r = engine.decide_channel_action(
                self._ch(0.05), {"structural_flag_ts": 0}, b, 300, True, 0)
        self.assertEqual(r["action"], "inbound_discount")
        self.assertLess(r["inbound_fee_ppm"], 0)

    def test_defense_window_elapsed_flags_structural(self):
        b = {"profit_capped": True, "structural": True}
        now = 1 + config.INBOUND_DEFENSE_WINDOW_DAYS * 86400 + 10
        with patch("engine.liquidity_policy.INBOUND_FEE_ENABLED", True):
            r = engine.decide_channel_action(
                self._ch(0.05), {"structural_flag_ts": 1}, b, 300, True, now)
        self.assertEqual(r["action"], "flag_structural")
        self.assertEqual(r["inbound_fee_ppm"], 0)

    def test_no_source_does_not_rebalance(self):
        b = {"profit_capped": False, "structural": False}
        r = engine.decide_channel_action(self._ch(0.05), {}, b, 200, False, 0)
        self.assertNotEqual(r["action"], "rebalance")

    def test_healthy_channel_is_none(self):
        b = {"profit_capped": False, "structural": False}
        r = engine.decide_channel_action(self._ch(0.55), {}, b, 100, True, 0)
        self.assertEqual(r["action"], "none")
        self.assertEqual(r["inbound_fee_ppm"], 0)


class InboundDiscountPpmTests(unittest.TestCase):
    def test_zero_at_or_above_clear_ratio(self):
        self.assertEqual(
            engine.inbound_discount_ppm(config.INBOUND_DISCOUNT_CLEAR_RATIO, 1000), 0)

    def test_largest_when_most_depleted(self):
        deep = engine.inbound_discount_ppm(0.0, 10_000)
        shallow = engine.inbound_discount_ppm(0.15, 10_000)
        self.assertLess(deep, shallow)  # more negative when emptier
        self.assertLessEqual(abs(deep), config.INBOUND_DISCOUNT_MAX_PPM)

    def test_capped_by_outbound_minus_margin(self):
        # Low outbound (30 ppm) caps the discount at 30 − margin so the summed
        # same-channel fee stays positive.
        d = engine.inbound_discount_ppm(0.0, 30)
        self.assertGreaterEqual(d, -(30 - config.INBOUND_DISCOUNT_SAFETY_MARGIN_PPM))


if __name__ == "__main__":
    unittest.main()
