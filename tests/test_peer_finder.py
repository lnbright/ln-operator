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


class SuggestPeersTests(unittest.TestCase):
    @patch("peer_finder.lnd_client.query_routes")
    def test_ranks_by_validated_route_cost(self, mqr):
        # Y2 is the better hub, but Y1 has the cheaper live route → Y1 ranks first.
        def fake(target, amt, source_pubkey=None):
            return {"Y1": {"fee_ppm": 40, "hops": 1},
                    "Y2": {"fee_ppm": 250, "hops": 2}}[source_pubkey]
        mqr.side_effect = fake
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1", "Y2"])
        self.assertEqual(out[0]["route_ppm"], 40)

    @patch("peer_finder.lnd_client.query_routes")
    def test_no_route_candidates_dropped(self, mqr):
        # Y2 has no live route → dropped; only Y1 survives.
        mqr.side_effect = lambda t, a, source_pubkey=None: (
            {"fee_ppm": 40, "hops": 1} if source_pubkey == "Y1" else None)
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1"])

    @patch("peer_finder.lnd_client.query_routes")
    def test_no_viable_peer_returns_empty(self, mqr):
        mqr.return_value = None  # nothing routes → resize/close, not open
        self.assertEqual(peer_finder.suggest_peers_for("T", digest=DIGEST), [])

    @patch("peer_finder.lnd_client.query_routes")
    def test_probe_exception_is_skipped_not_fatal(self, mqr):
        mqr.side_effect = lambda t, a, source_pubkey=None: (
            (_ for _ in ()).throw(RuntimeError("x")) if source_pubkey == "Y2"
            else {"fee_ppm": 40, "hops": 1})
        out = peer_finder.suggest_peers_for("T", digest=DIGEST)
        self.assertEqual([c["pubkey"] for c in out], ["Y1"])

    @patch("peer_finder.lnd_client.query_routes")
    def test_validate_false_skips_lnd(self, mqr):
        out = peer_finder.suggest_peers_for("T", digest=DIGEST, validate=False)
        mqr.assert_not_called()
        self.assertEqual({c["pubkey"] for c in out}, {"Y1", "Y2"})
        self.assertNotIn("_score", out[0])  # internal score stripped from output


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
