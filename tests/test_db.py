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


if __name__ == "__main__":
    unittest.main()
