"""Unit tests for sibling-channel handling in engine/sync.py.

With two channels open to the same peer, a circular self-payment's target
can no longer be resolved by peer pubkey alone — resolve_target_chan must
prefer the route's own last-hop chan_id and refuse to guess when ambiguous.

Run from project root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.sync import resolve_target_chan


OUR_CHANS = {"111": "PeerA", "222": "PeerB", "333": "PeerB"}  # 222/333 are siblings


class ResolveTargetChanTests(unittest.TestCase):

    def test_route_chan_id_wins_when_ours(self):
        # Route names sibling 333 explicitly — trust it over the peer map.
        self.assertEqual(
            resolve_target_chan("333", ["222", "333"], OUR_CHANS), "333")

    def test_single_channel_peer_falls_back_to_peer_map(self):
        # Legacy payments may lack a usable last-hop chan_id; with one
        # channel to the peer the answer is still unambiguous.
        self.assertEqual(
            resolve_target_chan("", ["111"], OUR_CHANS), "111")

    def test_unknown_route_chan_with_siblings_is_ambiguous(self):
        # No usable route chan_id and two siblings — must NOT guess.
        self.assertEqual(
            resolve_target_chan("999", ["222", "333"], OUR_CHANS), "")
        self.assertEqual(
            resolve_target_chan("", ["222", "333"], OUR_CHANS), "")

    def test_no_channels_to_peer(self):
        self.assertEqual(resolve_target_chan("", [], OUR_CHANS), "")


if __name__ == "__main__":
    unittest.main()
