"""Unit tests for B3 deterministic reconciliation (reconcile.run_checks).

Run from project root:
    python3 -m unittest discover tests
"""
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import reconcile


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._patch = patch("db.DB_PATH", self.db_path)
        self._patch.start()
        db.init_db()
        self.now = int(time.time())

    def tearDown(self):
        self._patch.stop()
        os.unlink(self.db_path)

    def _reb(self, ts_off=-3600, success=1, fee_ppm=100, payment_hash="h",
             triggered_by="auto", src="S", tgt="T", amount=200_000, budget_ppm=None):
        with db.get_conn() as c:
            c.execute(
                "INSERT INTO rebalance_log (ts, source_chan_id, target_chan_id, "
                "source_alias, target_alias, amount_sats, fee_paid_sats, fee_ppm, "
                "success, payment_hash, triggered_by, budget_ppm) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.now + ts_off, src, tgt, "src", "tgt", amount,
                 int(amount * fee_ppm / 1e6), fee_ppm, success, payment_hash,
                 triggered_by, budget_ppm))

    def _fee(self, ts_off, chan="C", new_ppm=100, reason="sigmoid"):
        with db.get_conn() as c:
            c.execute(
                "INSERT INTO fee_updates (ts, chan_id, peer_alias, new_fee_ppm, "
                "local_ratio, reason) VALUES (?,?,?,?,?,?)",
                (self.now + ts_off, chan, "peer", new_ppm, 0.5, reason))

    def _codes(self, issues):
        return sorted(i["check"] for i in issues)

    def test_clean_db_no_issues(self):
        self._reb(fee_ppm=100, payment_hash="h1")
        self.assertEqual(reconcile.run_checks(), [])

    def test_missing_payment_hash_on_auto_success(self):
        self._reb(payment_hash="", triggered_by="auto")
        self.assertIn("rebalance_missing_hash", self._codes(reconcile.run_checks()))

    def test_manual_missing_hash_is_ok(self):
        # manual rows legitimately may lack a hash → not flagged.
        self._reb(payment_hash="", triggered_by="manual")
        self.assertNotIn("rebalance_missing_hash", self._codes(reconcile.run_checks()))

    def test_over_max_budget_is_fail(self):
        self._reb(fee_ppm=config.REBALANCE_MAX_BUDGET_PPM + 1, payment_hash="h1")
        issues = reconcile.run_checks()
        self.assertIn("rebalance_over_max_budget", self._codes(issues))
        self.assertEqual([i["severity"] for i in issues
                          if i["check"] == "rebalance_over_max_budget"], ["fail"])

    def test_fee_within_budget_is_ok(self):
        # paid 100 ppm under a 500 ppm recorded budget → clean
        self._reb(fee_ppm=100, budget_ppm=500, payment_hash="b1")
        self.assertNotIn("rebalance_over_budget", self._codes(reconcile.run_checks()))

    def test_fee_over_recorded_budget_is_fail(self):
        # paid 600 ppm against a 500 ppm budget (>500×1.1=550) → fail
        self._reb(fee_ppm=600, budget_ppm=500, payment_hash="b2")
        issues = reconcile.run_checks()
        self.assertIn("rebalance_over_budget", self._codes(issues))
        self.assertEqual([i["severity"] for i in issues
                          if i["check"] == "rebalance_over_budget"], ["fail"])

    def test_fee_within_10pct_buffer_is_ok(self):
        # paid 540 ppm against a 500 budget — inside the 10% chunk buffer (550) → clean
        self._reb(fee_ppm=540, budget_ppm=500, payment_hash="b3")
        self.assertNotIn("rebalance_over_budget", self._codes(reconcile.run_checks()))

    def test_null_budget_skips_budget_check(self):
        # legacy row with no recorded budget → no budget invariant to assert
        self._reb(fee_ppm=900, budget_ppm=None, payment_hash="b4")
        self.assertNotIn("rebalance_over_budget", self._codes(reconcile.run_checks()))

    def test_recorded_budget_over_max_is_fail(self):
        self._reb(fee_ppm=100, budget_ppm=config.REBALANCE_MAX_BUDGET_PPM + 50,
                  payment_hash="b5")
        self.assertIn("rebalance_budget_over_max", self._codes(reconcile.run_checks()))

    def test_manual_fee_over_budget_still_flagged(self):
        # honoring a fee limit is universal — a manual row that overshot its budget
        # is just as much a fee_limit-ignored bug as an auto one.
        self._reb(fee_ppm=600, budget_ppm=500, triggered_by="manual", payment_hash="b6")
        self.assertIn("rebalance_over_budget", self._codes(reconcile.run_checks()))

    def test_duplicate_payment_hash(self):
        self._reb(payment_hash="dup", src="S")
        self._reb(payment_hash="dup", src="S2")
        self.assertIn("rebalance_dup_hash", self._codes(reconcile.run_checks()))

    def test_chunk_spike_outlier(self):
        # cluster of 4 chunks at ~100 ppm + one at 400 (≥2× median) within 120s
        base = -3600
        for i, ppm in enumerate([100, 100, 100, 400]):
            self._reb(ts_off=base + i * 10, fee_ppm=ppm, payment_hash=f"c{i}")
        self.assertIn("rebalance_chunk_spike", self._codes(reconcile.run_checks()))

    def test_no_chunk_spike_when_clustered_far_apart(self):
        # same ppms but spread >120s apart → not one attempt, no median to compare
        for i, ppm in enumerate([100, 400]):
            self._reb(ts_off=-3600 + i * 600, fee_ppm=ppm, payment_hash=f"d{i}")
        self.assertNotIn("rebalance_chunk_spike", self._codes(reconcile.run_checks()))

    def test_pin_violation(self):
        with db.get_conn() as c:
            c.execute("INSERT INTO fee_overrides (chan_id, pinned_ppm) VALUES ('C', 250)")
        self._fee(-3600, chan="C", new_ppm=300, reason="sigmoid")  # not the pinned 250
        self.assertIn("fee_pin_violation", self._codes(reconcile.run_checks()))

    def test_pinned_value_broadcast_is_ok(self):
        with db.get_conn() as c:
            c.execute("INSERT INTO fee_overrides (chan_id, pinned_ppm) VALUES ('C', 250)")
        self._fee(-3600, chan="C", new_ppm=250, reason="manual pin: 250 ppm")
        self.assertNotIn("fee_pin_violation", self._codes(reconcile.run_checks()))

    def test_window_excludes_old_rows(self):
        self._reb(ts_off=-5 * 86400, payment_hash="", triggered_by="auto")  # 5d old
        self.assertEqual(reconcile.run_checks(window_days=1), [])


if __name__ == "__main__":
    unittest.main()
