"""Unit tests for htlc_monitor.parse_link_failure — the pure event classifier.

parse_link_failure decides which raw LND htlcevents become forward_fail_log
rows. Only link failures (HTLCs we dropped at our own link) should survive;
everything else returns None. No LND, no DB, no stream.

Run from project root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import htlc_monitor


class ParseLinkFailureTests(unittest.TestCase):
    def _link_fail_event(self, detail="INSUFFICIENT_BALANCE"):
        return {
            "incoming_channel_id": "111",
            "outgoing_channel_id": "222",
            "incoming_htlc_id": "7",
            "outgoing_htlc_id": "0",
            "timestamp_ns": "1700000000000000000",
            "event_type": "FORWARD",
            "link_fail_event": {
                "info": {
                    "incoming_amt_msat": "1000000",
                    "outgoing_amt_msat": "999000",
                },
                "wire_failure": "TEMPORARY_CHANNEL_FAILURE",
                "failure_detail": detail,
                "failure_string": "insufficient bandwidth to route htlc",
            },
        }

    def test_link_failure_maps_to_row(self):
        row = htlc_monitor.parse_link_failure(self._link_fail_event())
        self.assertIsNotNone(row)
        self.assertEqual(row["chan_in"], "111")
        self.assertEqual(row["chan_out"], "222")
        self.assertEqual(row["amount_msat"], 999000)  # prefers outgoing
        self.assertEqual(row["wire_failure"], "TEMPORARY_CHANNEL_FAILURE")
        self.assertEqual(row["failure_detail"], "INSUFFICIENT_BALANCE")
        self.assertEqual(row["event_type"], "FORWARD")

    def test_amount_falls_back_to_incoming(self):
        ev = self._link_fail_event()
        ev["link_fail_event"]["info"].pop("outgoing_amt_msat")
        row = htlc_monitor.parse_link_failure(ev)
        self.assertEqual(row["amount_msat"], 1000000)

    def test_settle_event_ignored(self):
        ev = {"event_type": "FORWARD", "settle_event": {"preimage": "ab"}}
        self.assertIsNone(htlc_monitor.parse_link_failure(ev))

    def test_plain_forward_ignored(self):
        ev = {"event_type": "FORWARD", "forward_event": {"info": {}}}
        self.assertIsNone(htlc_monitor.parse_link_failure(ev))

    def test_downstream_forward_fail_ignored(self):
        # A failure that came back through us (not our link) — no detail to learn from.
        ev = {"event_type": "FORWARD", "forward_fail_event": {}}
        self.assertIsNone(htlc_monitor.parse_link_failure(ev))

    def test_subscribed_handshake_ignored(self):
        ev = {"event_type": "UNKNOWN", "subscribed_event": {}}
        self.assertIsNone(htlc_monitor.parse_link_failure(ev))

    def test_missing_fields_default_safely(self):
        ev = {"link_fail_event": {"failure_detail": "FEE_INSUFFICIENT"}}
        row = htlc_monitor.parse_link_failure(ev)
        self.assertEqual(row["amount_msat"], 0)
        self.assertEqual(row["chan_out"], "")
        self.assertEqual(row["failure_detail"], "FEE_INSUFFICIENT")
        self.assertEqual(row["wire_failure"], "")


if __name__ == "__main__":
    unittest.main()
