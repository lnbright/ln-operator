"""Unit tests for the `rebalance_channels --force` operator helpers in main.

Force mode runs the QueryRoutes probe diagnostically (never strands), shows the
per-source intel + calibration state, prompts before burning attempts when every
probe fails, and points at manual_rebalance for any target that didn't refill.

Run from project root:
    python3 -m unittest discover tests
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


def _plan(tid="111", alias="bfx", state="calibrated", probe=None, **kw):
    p = {"target_chan_id": tid, "target_alias": alias, "source_alias": "Boltz",
         "target_state": state, "target_total_deficit": 2_000_000,
         "amount_sats": 500_000, "max_fee_ppm": 300, "probe_results": probe or []}
    p.update(kw)
    return p


def _route(alias, ppm):
    return {"source_chan_id": alias, "source_alias": alias, "status": "route",
            "cost_ppm": ppm}


def _noroute(alias):
    return {"source_chan_id": alias, "source_alias": alias, "status": "no_route",
            "cost_ppm": None}


class FeasibilityTests(unittest.TestCase):
    def test_cheapest_picks_min_route(self):
        probe = [_route("A", 300), _route("B", 150), _noroute("C")]
        self.assertEqual(main._cheapest_probe_source(probe)["cost_ppm"], 150)

    def test_cheapest_none_when_no_route(self):
        self.assertIsNone(main._cheapest_probe_source([_noroute("A")]))

    def test_any_feasible_true_when_one_routes(self):
        plans = [_plan("1", probe=[_noroute("A")]),
                 _plan("2", probe=[_route("B", 200)])]
        self.assertTrue(main._any_feasible_route(plans))

    def test_any_feasible_false_when_all_fail(self):
        plans = [_plan("1", probe=[_noroute("A")]),
                 _plan("2", probe=[_noroute("B")])]
        self.assertFalse(main._any_feasible_route(plans))


class PromptTimeoutTests(unittest.TestCase):
    def test_non_tty_returns_default_no_without_blocking(self):
        # cron / piped stdin → never block, default No.
        with patch("main.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertFalse(
                main._prompt_proceed_with_timeout("x? ", timeout=99, default=False))

    def test_explicit_yes(self):
        with patch("main.sys.stdin") as stdin, \
             patch("main.select.select", return_value=([stdin], [], [])):
            stdin.isatty.return_value = True
            stdin.readline.return_value = "y\n"
            self.assertTrue(main._prompt_proceed_with_timeout("x? ", timeout=1))

    def test_timeout_fires_default(self):
        with patch("main.sys.stdin") as stdin, \
             patch("main.select.select", return_value=([], [], [])):
            stdin.isatty.return_value = True
            self.assertFalse(main._prompt_proceed_with_timeout("x? ", timeout=1,
                                                               default=False))

    def test_empty_line_returns_default(self):
        with patch("main.sys.stdin") as stdin, \
             patch("main.select.select", return_value=([stdin], [], [])):
            stdin.isatty.return_value = True
            stdin.readline.return_value = "\n"
            self.assertFalse(main._prompt_proceed_with_timeout("x? ", timeout=1,
                                                               default=False))


class ManualHintTests(unittest.TestCase):
    def test_hint_uses_cheapest_probed_source(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._print_manual_rebalance_hints(
                [_plan(probe=[_route("Kraken", 420), _route("WoS", 999)])])
        out = buf.getvalue()
        self.assertIn("manual_rebalance 'Kraken' 'bfx' 2000000 420", out)

    def test_hint_falls_back_to_plan_source_when_no_route(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._print_manual_rebalance_hints([_plan(probe=[_noroute("A")])])
        out = buf.getvalue()
        self.assertIn("manual_rebalance 'Boltz' 'bfx' 2000000 300", out)

    def test_no_output_for_empty(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._print_manual_rebalance_hints([])
        self.assertEqual(buf.getvalue(), "")

    def test_dedupes_by_target(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._print_manual_rebalance_hints(
                [_plan(probe=[_route("A", 100)]),
                 _plan(probe=[_route("A", 100)])])  # same target id
        self.assertEqual(buf.getvalue().count("ln-operator manual_rebalance"), 1)


class IntelDisplayTests(unittest.TestCase):
    def test_shows_state_label_per_target(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._show_queryroutes_intel([_plan(state="stranded",
                                                probe=[_route("A", 100)])])
        self.assertIn("bfx [stranded]", buf.getvalue())

    def test_silent_when_nothing_probed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._show_queryroutes_intel([_plan(probe=[])])
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
