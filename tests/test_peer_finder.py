"""Unit tests for the B2 targeted peer-finder (peer_finder).

Stage-1 selection is pure (fake digest); stage-2 mocks query_routes. Run from root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import peer_finder


def _node(alias, channels, capacity, neighbors, fee=50):
    return {"alias": alias, "channels": channels, "capacity": capacity,
            "avg_fee_ppm": fee, "neighbors": neighbors}


# US (peer P1). Target T's neighbours: Y2(hub), Y1, US(self), P1(existing peer),
# SMALL(too few channels). Only Y1 + Y2 are valid candidates.
DIGEST = {
    "our_pubkey": "US",
    "our_peers": ["P1"],
    "reachable_2hop": ["P1", "N_known", "T"],  # T reachable via our peer P1
    "nodes": {
        "T":     _node("target", 300, 9_000_000, ["Y1", "Y2", "US", "P1", "SMALL"]),
        "Y1":    _node("alice",   20, 5_000_000, ["T", "Z1", "Z2"]),       # Z* new reach
        "Y2":    _node("bob-hub", 80, 50_000_000, ["T", "P1", "N_known"]), # all known reach
        "P1":    _node("ourpeer", 40, 4_000_000, ["T", "US"]),
        "SMALL": _node("tiny",     3,   100_000, ["T"]),
        "US":    _node("us",       9,  3_000_000, ["P1"]),
    },
}


class Stage1Tests(unittest.TestCase):
    def test_filters_self_peers_target_and_noise(self):
        cands = peer_finder._stage1_candidates(DIGEST, "T", limit=10)
        keys = {c["pubkey"] for c in cands}
        self.assertEqual(keys, {"Y1", "Y2"})  # US/P1/SMALL/T all excluded

    def test_hub_ranks_first(self):
        cands = peer_finder._stage1_candidates(DIGEST, "T", limit=10)
        self.assertEqual(cands[0]["pubkey"], "Y2")  # 80ch/50M outscores 20ch/5M

    def test_diversity_reflects_new_reach(self):
        c = {x["pubkey"]: x for x in peer_finder._stage1_candidates(DIGEST, "T", 10)}
        # Y1 neighbours T,Z1,Z2 — T known, Z1/Z2 outside 2-hop → 2/3 new
        self.assertAlmostEqual(c["Y1"]["diversity"], 2 / 3, places=2)
        # Y2 neighbours T,P1,N_known — all already in our 2-hop → 0 new
        self.assertAlmostEqual(c["Y2"]["diversity"], 0.0, places=2)

    def test_unknown_target_returns_empty(self):
        self.assertEqual(peer_finder._stage1_candidates(DIGEST, "ZZZ", 10), [])


def _probe(fee_ppm, hops=2, chan_id=None):
    """A query_routes return with the raw `routes` the first-hop lookup reads. No
    chan_id → first-hop fee resolves to 0 (get_channel_edge never called)."""
    hop0 = {"pub_key": "X"}
    if chan_id:
        hop0["chan_id"] = chan_id
    return {"fee_ppm": fee_ppm, "hops": hops, "routes": [{"hops": [hop0]}]}


class SuggestPeersTests(unittest.TestCase):
    @patch("peer_finder.lnd_client.query_routes")
    def test_ranks_by_validated_route_cost(self, mqr):
        # Y2 is the better hub, but Y1 has the cheaper live route → Y1 ranks first.
        def fake(dest, amt, source_pubkey=None, last_hop_pubkey=None):
            return {"Y1": _probe(40), "Y2": _probe(250)}[source_pubkey]
        mqr.side_effect = fake
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1", "Y2"])
        self.assertEqual(out[0]["route_ppm"], 40)

    @patch("peer_finder.lnd_client.query_routes")
    def test_no_route_candidates_dropped(self, mqr):
        # Y2 has no live route → dropped; only Y1 survives.
        mqr.side_effect = lambda d, a, source_pubkey=None, last_hop_pubkey=None: (
            _probe(40) if source_pubkey == "Y1" else None)
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1"])

    @patch("peer_finder.lnd_client.query_routes")
    def test_no_viable_peer_returns_empty(self, mqr):
        mqr.return_value = None  # nothing routes → resize/close, not open
        self.assertEqual(peer_finder.suggest_peers_for("T", digest=DIGEST), [])

    @patch("peer_finder.lnd_client.query_routes")
    def test_probe_exception_is_skipped_not_fatal(self, mqr):
        mqr.side_effect = lambda d, a, source_pubkey=None, last_hop_pubkey=None: (
            (_ for _ in ()).throw(RuntimeError("x")) if source_pubkey == "Y2"
            else _probe(40))
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1"])

    @patch("peer_finder.lnd_client.query_routes")
    def test_validate_false_skips_lnd(self, mqr):
        out = peer_finder.suggest_peers_for("T", digest=DIGEST, validate=False)
        mqr.assert_not_called()
        self.assertEqual({c["pubkey"] for c in out}, {"Y1", "Y2"})
        self.assertNotIn("_score", out[0])  # internal score stripped from output


class FirstHopFeeTests(unittest.TestCase):
    """The probe omits the candidate's own first-hop (source) fee; it's added back
    from the channel edge so route_ppm is the true end-to-end refill cost."""

    @patch("peer_finder.lnd_client.get_channel_edge")
    @patch("peer_finder.lnd_client.query_routes")
    def test_first_hop_fee_added_and_reranks(self, mqr, mge):
        # Y2 is cheaper DOWNSTREAM (probe 10 vs 100) but charges a huge first-hop fee;
        # Y1 pricier downstream but near-free first hop → true cost flips the ranking.
        mqr.side_effect = lambda d, a, source_pubkey=None, last_hop_pubkey=None: {
            "Y1": _probe(100, chan_id="e1"),
            "Y2": _probe(10, chan_id="e2")}[source_pubkey]
        # candidate's OWN policy = node1_policy if it is node1, else node2_policy
        edges = {
            "e1": {"node1_pub": "Y1", "node2_pub": "T",
                   "node1_policy": {"fee_base_msat": "0", "fee_rate_milli_msat": "1"},
                   "node2_policy": {"fee_base_msat": "0", "fee_rate_milli_msat": "9999"}},
            "e2": {"node1_pub": "T", "node2_pub": "Y2",
                   "node1_policy": {"fee_base_msat": "0", "fee_rate_milli_msat": "9999"},
                   "node2_policy": {"fee_base_msat": "0", "fee_rate_milli_msat": "2000"}},
        }
        mge.side_effect = lambda cid: edges[cid]
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1", "Y2"])  # 101 < 2010
        self.assertEqual(out[0]["route_ppm"], 101)      # 100 probe + 1 first-hop
        self.assertEqual(out[0]["first_hop_ppm"], 1)
        self.assertEqual(out[1]["first_hop_ppm"], 2000)

    @patch("peer_finder.lnd_client.get_channel_edge")
    @patch("peer_finder.lnd_client.query_routes")
    def test_disabled_first_hop_adds_nothing(self, mqr, mge):
        mqr.side_effect = lambda d, a, source_pubkey=None, last_hop_pubkey=None: (
            _probe(40, chan_id="e1") if source_pubkey == "Y1" else _probe(40))
        mge.return_value = {"node1_pub": "Y1", "node2_pub": "T",
                            "node1_policy": {"fee_base_msat": "0",
                                             "fee_rate_milli_msat": "5000",
                                             "disabled": True},
                            "node2_policy": {}}
        out = {c["pubkey"]: c for c in peer_finder.suggest_peers_for("T", digest=DIGEST)}
        self.assertEqual(out["Y1"]["first_hop_ppm"], 0)   # disabled → not added
        self.assertEqual(out["Y1"]["route_ppm"], 40)

    def test_policy_ppm_amortises_base_fee(self):
        # base 1 sat (1000 msat) + 1 ppm over 500k sats = 2 + 1 = 3 ppm
        ppm = peer_finder._policy_ppm(
            {"fee_base_msat": "1000", "fee_rate_milli_msat": "1"}, 500_000)
        self.assertEqual(round(ppm), 3)


class ResolveTargetTests(unittest.TestCase):
    def test_resolves_hex_pubkey_passthrough(self):
        pk = "ab" * 33
        self.assertEqual(peer_finder.resolve_target(pk, DIGEST), pk)

    def test_resolves_alias_substring_biggest_match(self):
        self.assertEqual(peer_finder.resolve_target("hub", DIGEST), "Y2")

    def test_no_match_returns_none(self):
        self.assertIsNone(peer_finder.resolve_target("nonesuch", DIGEST))


if __name__ == "__main__":
    unittest.main()
