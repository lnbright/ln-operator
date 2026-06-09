"""Unit tests for db.py against a throwaway SQLite file.

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


DAY = 86400


class EarnedPpmWideningTests(unittest.TestCase):
    """Evidence widening in get_channel_earned_ppm — the unjudged-cliff fix.

    A channel whose volume sits just outside the standard window must stay
    judged on that older evidence; only a channel with too little volume in
    the whole max lookback is unjudged."""

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

    def _forward(self, chan_out, age_days, amount_out, fee):
        with db.get_conn() as conn:
            conn.execute("""
                INSERT INTO forwarding_log
                (ts, chan_in, chan_out, amount_in_sats, amount_out_sats, fee_earned_sats)
                VALUES (?, '999', ?, ?, ?, ?)
            """, (self.now - age_days * DAY, chan_out, amount_out, amount_out, fee))

    def test_recent_volume_uses_standard_window(self):
        # Enough volume inside 21d → judged on it; older traffic not pulled in.
        self._forward("chan", 5, config.EARNED_PPM_MIN_VOLUME_SATS, 2_000)
        self._forward("chan", config.EARNED_PPM_WINDOW_DAYS + 5,
                      config.EARNED_PPM_MIN_VOLUME_SATS, 0)  # would dilute ppm to 1000
        ppm, vol = db.get_channel_earned_ppm("chan")
        self.assertAlmostEqual(ppm, 2_000 / config.EARNED_PPM_MIN_VOLUME_SATS * 1e6, delta=1)
        self.assertEqual(vol, config.EARNED_PPM_MIN_VOLUME_SATS)

    def test_quiet_channel_stays_judged_on_older_evidence(self):
        # All volume just past the 21d window — pre-widening this returned None
        # (the cliff); now the window doubles until the evidence is captured.
        self._forward("chan", config.EARNED_PPM_WINDOW_DAYS + 7,
                      config.EARNED_PPM_MIN_VOLUME_SATS, 1_119)
        ppm, vol = db.get_channel_earned_ppm("chan")
        self.assertIsNotNone(ppm)
        self.assertAlmostEqual(ppm, 1_119 / config.EARNED_PPM_MIN_VOLUME_SATS * 1e6, delta=1)
        self.assertEqual(vol, config.EARNED_PPM_MIN_VOLUME_SATS)

    def test_evidence_near_max_lookback_still_judges(self):
        self._forward("chan", config.EARNED_PPM_MAX_LOOKBACK_DAYS - 1,
                      config.EARNED_PPM_MIN_VOLUME_SATS, 500)
        ppm, _ = db.get_channel_earned_ppm("chan")
        self.assertIsNotNone(ppm)

    def test_evidence_beyond_max_lookback_is_unjudged(self):
        self._forward("chan", config.EARNED_PPM_MAX_LOOKBACK_DAYS + 1,
                      config.EARNED_PPM_MIN_VOLUME_SATS * 10, 500)
        ppm, vol = db.get_channel_earned_ppm("chan")
        self.assertIsNone(ppm)
        self.assertEqual(vol, 0)

    def test_insufficient_volume_everywhere_is_unjudged(self):
        self._forward("chan", 5, config.EARNED_PPM_MIN_VOLUME_SATS // 4, 100)
        self._forward("chan", 50, config.EARNED_PPM_MIN_VOLUME_SATS // 4, 100)
        ppm, vol = db.get_channel_earned_ppm("chan")
        self.assertIsNone(ppm)
        self.assertEqual(vol, config.EARNED_PPM_MIN_VOLUME_SATS // 2)

    def test_widened_window_aggregates_partial_volumes(self):
        # Half the volume recent, half older — neither alone judges, together they do.
        half = config.EARNED_PPM_MIN_VOLUME_SATS // 2
        self._forward("chan", 5, half, 1_000)
        self._forward("chan", config.EARNED_PPM_WINDOW_DAYS + 10, half, 3_000)
        ppm, vol = db.get_channel_earned_ppm("chan")
        self.assertIsNotNone(ppm)
        self.assertEqual(vol, half * 2)
        self.assertAlmostEqual(ppm, 4_000 / (half * 2) * 1e6, delta=1)


class FailureExpiryTests(unittest.TestCase):
    """Failure evidence expires on the same clock as earned-ppm evidence.

    Refusals older than EARNED_PPM_MAX_LOOKBACK_DAYS stop counting toward
    escalation/structural — a channel re-entering planning after a long quiet
    resumes at last_refill × 1.0 instead of a bid inflated by failures that
    only ever tested the (now-expired) profit cap's price."""

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

    def _attempt(self, age_days, success):
        with db.get_conn() as conn:
            conn.execute("""
                INSERT INTO rebalance_log
                    (ts, source_chan_id, target_chan_id, amount_sats, success)
                VALUES (?, 'src', 'chan', 500000, ?)
            """, (self.now - int(age_days * DAY), 1 if success else 0))

    def test_recent_failures_count(self):
        for d in (1, 2, 3):
            self._attempt(d, success=False)
        self.assertEqual(db.count_failures_since_last_success("chan"), 3)

    def test_stale_failures_expire(self):
        # 9 refusals from ~a quarter ago — all older than the max lookback.
        for d in range(0, 9):
            self._attempt(config.EARNED_PPM_MAX_LOOKBACK_DAYS + 1 + d, success=False)
        self.assertEqual(db.count_failures_since_last_success("chan"), 0)

    def test_mixed_ages_count_only_fresh(self):
        self._attempt(config.EARNED_PPM_MAX_LOOKBACK_DAYS + 5, success=False)  # expired
        self._attempt(10, success=False)                                       # fresh
        self._attempt(2, success=False)                                        # fresh
        self.assertEqual(db.count_failures_since_last_success("chan"), 2)

    def test_success_still_resets_regardless_of_age(self):
        self._attempt(5, success=False)
        self._attempt(3, success=True)   # success after the failure
        self._attempt(1, success=False)  # one fresh failure since
        self.assertEqual(db.count_failures_since_last_success("chan"), 1)


class FailureRunDedupTests(unittest.TestCase):
    """Failure unit is the refill RUN, not the attempt.

    One pipeline run fans out a primary plan plus fallbacks at the same channel,
    all sharing a run_id; the budget escalation / structural threshold must
    advance once per run, not once per fallback. A run that landed any sats is
    not a failed cycle."""

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

    def _attempt(self, age_days, success, run_id):
        with db.get_conn() as conn:
            conn.execute("""
                INSERT INTO rebalance_log
                    (ts, source_chan_id, target_chan_id, amount_sats, success, run_id)
                VALUES (?, 'src', 'chan', 500000, ?, ?)
            """, (self.now - int(age_days * DAY), 1 if success else 0, run_id))

    def test_same_run_counts_once(self):
        # Primary + 5 fallbacks in one run (bfx's actual failure shape).
        for _ in range(6):
            self._attempt(1, success=False, run_id=42)
        self.assertEqual(db.count_failures_since_last_success("chan"), 1)

    def test_distinct_runs_count_separately(self):
        self._attempt(3, success=False, run_id=1)
        self._attempt(2, success=False, run_id=2)
        self._attempt(1, success=False, run_id=3)
        self.assertEqual(db.count_failures_since_last_success("chan"), 3)

    def test_run_that_landed_sats_is_not_a_failure(self):
        # A fallback in the run succeeded → partial refill, not a failed cycle.
        self._attempt(1, success=False, run_id=7)
        self._attempt(1, success=True, run_id=7)
        self._attempt(1, success=False, run_id=7)
        self.assertEqual(db.count_failures_since_last_success("chan"), 0)

    def test_null_run_id_counts_per_row(self):
        # Legacy / manual rows with no run_id keep the old per-attempt behaviour.
        self._attempt(2, success=False, run_id=None)
        self._attempt(1, success=False, run_id=None)
        self.assertEqual(db.count_failures_since_last_success("chan"), 2)

    def test_backfill_clusters_by_time(self):
        # Six NULL-run_id failures within ~an hour backfill to one run.
        base = self.now - 3 * DAY
        for mins in (0, 11, 23, 34, 35, 46):
            with db.get_conn() as conn:
                conn.execute("""
                    INSERT INTO rebalance_log
                        (ts, source_chan_id, target_chan_id, amount_sats, success)
                    VALUES (?, 'src', 'chan', 500000, 0)
                """, (base + mins * 60,))
        db._migrate_rebalance_run_id()
        self.assertEqual(db.count_failures_since_last_success("chan"), 1)


if __name__ == "__main__":
    unittest.main()
