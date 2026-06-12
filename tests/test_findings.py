"""Unit tests for the B6 daily-finding dedup store (db.reconcile_findings).

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

import db


def _f(key, state, summary="s", kind="stranded", entity="e"):
    return {"key": key, "kind": kind, "entity": entity, "state": state, "summary": summary}


class ReconcileFindingsTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._patch = patch("db.DB_PATH", self.db_path)
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        os.unlink(self.db_path)

    def test_first_run_everything_is_new(self):
        out = db.reconcile_findings([_f("stranded:1", "earn=600"), _f("conc:boltz", "80%")])
        self.assertEqual({f["key"] for f in out["new"]}, {"stranded:1", "conc:boltz"})
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["unchanged"], [])
        self.assertEqual(out["resolved"], [])

    def test_identical_rerun_is_unchanged(self):
        db.reconcile_findings([_f("stranded:1", "earn=600")])
        out = db.reconcile_findings([_f("stranded:1", "earn=600")])
        self.assertEqual(out["new"], [])
        self.assertEqual([f["key"] for f in out["unchanged"]], ["stranded:1"])

    def test_state_change_is_changed_with_prev(self):
        db.reconcile_findings([_f("stranded:1", "earn=600")])
        out = db.reconcile_findings([_f("stranded:1", "earn=900")])
        self.assertEqual([f["key"] for f in out["changed"]], ["stranded:1"])
        self.assertEqual(out["changed"][0]["prev_state"], "earn=600")

    def test_absent_finding_is_resolved_once(self):
        db.reconcile_findings([_f("stranded:1", "x"), _f("conc:boltz", "y")])
        out = db.reconcile_findings([_f("conc:boltz", "y")])  # stranded:1 gone
        self.assertEqual([f["key"] for f in out["resolved"]], ["stranded:1"])
        # resolved is one-time: a third run no longer surfaces it
        out2 = db.reconcile_findings([_f("conc:boltz", "y")])
        self.assertEqual(out2["resolved"], [])

    def test_first_seen_is_preserved_across_runs(self):
        t0 = int(time.time()) - 5 * 86400
        db.reconcile_findings([_f("stranded:1", "a")], now=t0)
        out = db.reconcile_findings([_f("stranded:1", "b")])  # changed, later
        self.assertEqual(out["changed"][0]["first_seen"], t0)

    def test_get_open_findings_excludes_resolved(self):
        db.reconcile_findings([_f("a", "1"), _f("b", "2")])
        db.reconcile_findings([_f("a", "1")])  # b resolved
        keys = {r["key"] for r in db.get_open_findings()}
        self.assertEqual(keys, {"a"})

    def test_resolved_key_reopens_as_new(self):
        db.reconcile_findings([_f("stranded:1", "x")])
        db.reconcile_findings([])                       # resolved
        out = db.reconcile_findings([_f("stranded:1", "x")])  # reappears
        self.assertEqual([f["key"] for f in out["new"]], ["stranded:1"])


if __name__ == "__main__":
    unittest.main()
