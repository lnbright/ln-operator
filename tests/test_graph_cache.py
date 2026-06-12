"""Unit tests for the B1 graph cache digest (graph_cache.build_digest).

Pure-function tests — no LND, no disk. Run from project root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_cache


def _pol(ppm):
    return {"fee_rate_milli_msat": str(ppm)}


# Topology:  U(us)─A, U─B, A─C, B─C, C─D   plus E (announced, no channels)
#   our_peers = [A, B]
#   2-hop reachable from A,B = {A, B} + neigh(A)={U,C} + neigh(B)={U,C} − U = {A,B,C}
GRAPH = {
    "nodes": [
        {"pub_key": "U", "alias": "us"},
        {"pub_key": "A", "alias": "alice"},
        {"pub_key": "B", "alias": "bob"},
        {"pub_key": "C", "alias": "carol"},
        {"pub_key": "D", "alias": "dave"},
        {"pub_key": "E", "alias": "eve"},  # no channels → dropped
    ],
    "edges": [
        {"node1_pub": "U", "node2_pub": "A", "capacity": "1000",
         "node1_policy": _pol(100), "node2_policy": _pol(200)},
        {"node1_pub": "U", "node2_pub": "B", "capacity": "2000",
         "node1_policy": _pol(0), "node2_policy": _pol(300)},
        {"node1_pub": "A", "node2_pub": "C", "capacity": "3000",
         "node1_policy": _pol(50), "node2_policy": _pol(50)},
        {"node1_pub": "B", "node2_pub": "C", "capacity": "4000",
         "node1_policy": _pol(60), "node2_policy": _pol(60)},
        {"node1_pub": "C", "node2_pub": "D", "capacity": "5000",
         "node1_policy": _pol(70), "node2_policy": None},
    ],
}


class BuildDigestTests(unittest.TestCase):
    def setUp(self):
        self.d = graph_cache.build_digest(GRAPH, "U", ["A", "B"], now=1000)

    def test_channelless_node_dropped(self):
        self.assertNotIn("E", self.d["nodes"])
        self.assertIn("D", self.d["nodes"])

    def test_channel_counts_and_capacity(self):
        n = self.d["nodes"]
        self.assertEqual(n["C"]["channels"], 3)          # A-C, B-C, C-D
        self.assertEqual(n["C"]["capacity"], 3000 + 4000 + 5000)
        self.assertEqual(n["A"]["channels"], 2)
        self.assertEqual(n["D"]["channels"], 1)

    def test_neighbors_adjacency(self):
        self.assertEqual(self.d["nodes"]["C"]["neighbors"], ["A", "B", "D"])
        self.assertEqual(self.d["nodes"]["A"]["neighbors"], ["C", "U"])

    def test_avg_fee_skips_zero_fee_sides(self):
        # U's sides: 100 (to A) and 0 (to B). Zero-fee side excluded → avg = 100.
        self.assertEqual(self.d["nodes"]["U"]["avg_fee_ppm"], 100)
        # C's sides: 50, 60, 70 → avg 60.
        self.assertEqual(self.d["nodes"]["C"]["avg_fee_ppm"], 60)

    def test_two_hop_reachable(self):
        self.assertEqual(set(self.d["reachable_2hop"]), {"A", "B", "C"})

    def test_our_pubkey_excluded_from_reachable(self):
        self.assertNotIn("U", self.d["reachable_2hop"])

    def test_stats(self):
        self.assertEqual(self.d["stats"]["total_nodes"], 5)      # U,A,B,C,D (E dropped)
        self.assertEqual(self.d["stats"]["total_channels"], 5)   # 5 edges
        self.assertEqual(self.d["stats"]["total_capacity"], 1000 + 2000 + 3000 + 4000 + 5000)

    def test_metadata(self):
        self.assertEqual(self.d["ts"], 1000)
        self.assertEqual(self.d["our_pubkey"], "U")
        self.assertEqual(self.d["our_peers"], ["A", "B"])

    def test_empty_graph_is_safe(self):
        d = graph_cache.build_digest({}, "U", [], now=1)
        self.assertEqual(d["nodes"], {})
        self.assertEqual(d["reachable_2hop"], [])
        self.assertEqual(d["stats"]["total_nodes"], 0)


if __name__ == "__main__":
    unittest.main()
