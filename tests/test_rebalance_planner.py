"""Unit tests for B8 — the unified QueryRoutes probe in the rebalance planner.

One min-chunk probe per source drives BOTH the infeasibility early-out (drop only
when ALL sources fail) and the bid (price off the cheapest feasible source).

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
    return {"chan_id": "111", "peer_alias": "bfx-lnd0", "peer_pubkey": "ab" * 33,
            "capacity": cap, "local_balance": int(cap * local_ratio),
            "local_ratio": local_ratio}


def _source(chan_id="222", alias="Boltz", local_ratio=0.90, cap=5_000_000):
    return {"chan_id": chan_id, "peer_alias": alias, "peer_pubkey": "cd" * 33,
            "capacity": cap, "local_balance": int(cap * local_ratio),
            "local_ratio": local_ratio}


def _budget(max_fee_ppm=14, ceiling=721, earned_ppm=576):
    return {"max_fee_ppm": max_fee_ppm, "affordable_ceiling_ppm": ceiling,
            "earned_ppm": earned_ppm, "reason": f"last_refill 7 ppm → {max_fee_ppm} ppm"}


# query_routes mock that returns a per-source cost keyed by outgoing_chan_id.
def _by_source(costs, raises=()):
    def fake(peer, amt, fee_limit_sat=None, outgoing_chan_id=None, raise_on_error=False):
        if outgoing_chan_id in raises:
            raise RuntimeError("LND down")
        c = costs.get(outgoing_chan_id)
        return {"fee_ppm": c, "hops": 1} if c is not None else None
    return fake


class _ProbeTestBase(unittest.TestCase):
    """Stub get_channel_edge so the last-hop lookup adds 0 ppm (no live LND call);
    last-hop accounting itself is covered by ProbeLastHopTests."""
    def setUp(self):
        p = patch("engine.rebalance_planner.lnd_client.get_channel_edge",
                  return_value=None)
        p.start()
        self.addCleanup(p.stop)


class ProbeFeasibilityTests(_ProbeTestBase):
    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_all_sources_no_route_drops_and_records(self, mqr, msave):
        mqr.return_value = None  # every source: definite no-route
        v = rp._queryroutes_probe(_target(), _budget(), [_source("S1"), _source("S2")],
                                  999, record=True)
        self.assertTrue(v["drop"])
        msave.assert_called_once()
        _, kw = msave.call_args
        self.assertEqual(kw["failure_reason"], "QR_NO_AFFORDABLE_ROUTE")
        self.assertEqual(kw["run_id"], 999)

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_one_source_routes_does_not_drop(self, mqr, msave):
        # S1 no route, S2 routes → feasible, must NOT drop (the key multi-source fix)
        mqr.side_effect = _by_source({"S2": 200})
        v = rp._queryroutes_probe(_target(), _budget(), [_source("S1"), _source("S2")],
                                  999, record=True)
        self.assertFalse(v["drop"])
        msave.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_dry_run_drops_without_recording(self, mqr, msave):
        mqr.return_value = None
        v = rp._queryroutes_probe(_target(), _budget(), [_source("S1")], 999, record=False)
        self.assertTrue(v["drop"])
        msave.assert_not_called()

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_no_route_but_a_probe_unavailable_never_strands(self, mqr, msave):
        # S1 no route, S2 unavailable (raises) → can't prove ALL fail → keep
        mqr.side_effect = _by_source({}, raises=("S2",))
        v = rp._queryroutes_probe(_target(), _budget(), [_source("S1"), _source("S2")],
                                  999, record=True)
        self.assertFalse(v["drop"])
        msave.assert_not_called()

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_unjudged_never_probes(self, mqr):
        v = rp._queryroutes_probe(_target(), _budget(earned_ppm=None), [_source()], 999, True)
        self.assertFalse(v["drop"])
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.REBALANCE_QUERYROUTES_ENABLED", False)
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_master_flag_off_skips_probe(self, mqr):
        v = rp._queryroutes_probe(_target(), _budget(), [_source()], 999, True)
        self.assertFalse(v["drop"])
        mqr.assert_not_called()

    @patch("engine.rebalance_planner.REBALANCE_QUERYROUTES_EARLYOUT_ENABLED", False)
    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_earlyout_flag_off_prices_but_never_strands(self, mqr, msave):
        mqr.return_value = None  # all no-route, but early-out disabled
        v = rp._queryroutes_probe(_target(), _budget(), [_source()], 999, True)
        self.assertFalse(v["drop"])
        msave.assert_not_called()


class ProbeForceModeTests(_ProbeTestBase):
    """force=True: diagnose + price + rank, but NEVER strand and NEVER record."""

    @patch("engine.rebalance_planner.db.save_rebalance_attempt")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_force_all_no_route_never_drops_or_records(self, mqr, msave):
        mqr.return_value = None  # every source: definite no-route
        v = rp._queryroutes_probe(_target(), _budget(), [_source("S1"), _source("S2")],
                                  999, record=True, force=True)
        self.assertFalse(v["drop"])          # force never strands
        msave.assert_not_called()            # force never records a synthetic cycle
        # per-source results still surfaced for the operator
        self.assertEqual([r["status"] for r in v["probe_results"]],
                         ["no_route", "no_route"])

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_force_probes_unjudged_for_diagnostics(self, mqr):
        # calibrating → auto skips entirely, but force probes anyway (ceiling exists)
        mqr.side_effect = _by_source({"S1": 300})
        b = _budget(earned_ppm=None)
        b["affordable_ceiling_ppm"] = 1000
        v = rp._queryroutes_probe(_target(), b, [_source("S1")], 999,
                                  record=False, force=True)
        self.assertFalse(v["drop"])
        self.assertEqual(v["probe_results"][0]["cost_ppm"], 300)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_force_still_prices_and_ranks(self, mqr):
        mqr.side_effect = _by_source({"S1": 300, "S2": 150})
        v = rp._queryroutes_probe(_target(), _budget(14, 721),
                                  [_source("S1"), _source("S2")], 999,
                                  record=True, force=True)
        self.assertEqual(v["budget"]["max_fee_ppm"], 150)
        self.assertEqual(v["source_order"], ["S2", "S1"])


class ProbeLastHopTests(_ProbeTestBase):
    """The target peer's final-hop fee into our channel is added to each source's
    cost (the probe routes to dest=target_peer, so LND omits it)."""

    def _edge(self, target_pub, base_msat=0, rate_ppm=0, disabled=False):
        return {"node1_pub": target_pub, "node2_pub": "ff" * 33,
                "node1_policy": {"fee_base_msat": str(base_msat),
                                 "fee_rate_milli_msat": str(rate_ppm),
                                 "disabled": disabled},
                "node2_policy": {"fee_base_msat": "0", "fee_rate_milli_msat": "0"}}

    @patch("engine.rebalance_planner.lnd_client.get_channel_edge")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_last_hop_rate_added_to_cost(self, mqr, medge):
        # target peer charges 200 ppm outbound into us; probe route costs 50 ppm
        # → reported cost = 250 ppm (and the bid jumps to 250, ≤ 721 ceiling).
        medge.return_value = self._edge("ab" * 33, rate_ppm=200)
        mqr.side_effect = _by_source({"S1": 50})
        v = rp._queryroutes_probe(_target(), _budget(14, 721), [_source("S1")],
                                  999, record=True)
        self.assertEqual(v["probe_results"][0]["cost_ppm"], 250)
        self.assertEqual(v["budget"]["max_fee_ppm"], 250)

    @patch("engine.rebalance_planner.lnd_client.get_channel_edge")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_last_hop_base_fee_amortised(self, mqr, medge):
        # 1000 msat base over a 100k probe = 10 ppm, added on top of a 0 ppm route.
        medge.return_value = self._edge("ab" * 33, base_msat=1000)
        mqr.side_effect = _by_source({"S1": 0})
        v = rp._queryroutes_probe(_target(), _budget(14, 721), [_source("S1")],
                                  999, record=True)
        self.assertEqual(v["probe_results"][0]["cost_ppm"], 10)

    @patch("engine.rebalance_planner.lnd_client.get_channel_edge")
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_edge_lookup_failure_degrades_to_probe_cost(self, mqr, medge):
        medge.side_effect = RuntimeError("LND down")
        mqr.side_effect = _by_source({"S1": 50})
        v = rp._queryroutes_probe(_target(), _budget(14, 721), [_source("S1")],
                                  999, record=True)
        self.assertEqual(v["probe_results"][0]["cost_ppm"], 50)


class ProbePricingTests(_ProbeTestBase):
    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_prices_off_cheapest_and_ranks_cheapest_first(self, mqr):
        # S1=300, S2=150 → bid jumps to 150, order = [S2, S1]
        mqr.side_effect = _by_source({"S1": 300, "S2": 150})
        v = rp._queryroutes_probe(_target(), _budget(14, 721),
                                  [_source("S1"), _source("S2")], 999, record=True)
        self.assertEqual(v["budget"]["max_fee_ppm"], 150)
        self.assertEqual(v["source_order"], ["S2", "S1"])
        self.assertIn("cheapest of 2", v["budget"]["reason"])

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_cheapest_already_affordable_no_jump(self, mqr):
        # cheapest route 10 ≤ current bid 14 → no jump, but still ranked
        mqr.side_effect = _by_source({"S1": 10})
        v = rp._queryroutes_probe(_target(), _budget(14, 721), [_source("S1")], 999, True)
        self.assertEqual(v["budget"]["max_fee_ppm"], 14)
        self.assertEqual(v["source_order"], ["S1"])

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_never_jumps_above_ceiling(self, mqr):
        mqr.side_effect = _by_source({"S1": 722})  # rounding noise above 721
        v = rp._queryroutes_probe(_target(), _budget(14, 721), [_source("S1")], 999, True)
        self.assertEqual(v["budget"]["max_fee_ppm"], 14)

    @patch("engine.rebalance_planner.lnd_client.query_routes")
    def test_probes_min_chunk_at_ceiling(self, mqr):
        mqr.return_value = None
        rp._queryroutes_probe(_target(), _budget(ceiling=721), [_source("S1")], 999, False)
        args, kw = mqr.call_args
        self.assertEqual(args[0], "ab" * 33)            # dest = target peer
        self.assertEqual(args[1], 100_000)              # min-chunk probe size
        self.assertEqual(kw["fee_limit_sat"], 72)       # 100k × 721 / 1e6
        self.assertEqual(kw["outgoing_chan_id"], "S1")
        self.assertTrue(kw["raise_on_error"])           # must distinguish no-route


class AffordableCeilingTests(unittest.TestCase):
    @patch("engine.rebalance_planner.db")
    def test_ceiling_is_profit_cap_when_judged(self, mdb):
        mdb.get_last_refill_ppm.return_value = 7
        mdb.count_failures_since_last_success.return_value = 0
        mdb.get_channel_earned_ppm.return_value = (576, 10_000_000)
        b = rp.get_channel_rebalance_budget("111")
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
