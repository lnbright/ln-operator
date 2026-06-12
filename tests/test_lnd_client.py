"""Unit tests for lnd_client.query_routes (HTTP mocked).

Run from project root:
    python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lnd_client

PUB = "ab" * 33  # 33-byte hex pubkey


def _resp(status_code=200, payload=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    return r


class QueryRoutesTests(unittest.TestCase):
    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_parses_best_route(self, mget, _h):
        # 1000 sat routed, 5000 msat (=5 sat) fee, 3 hops → 5000 ppm
        mget.return_value = _resp(payload={
            "routes": [{
                "total_fees_msat": "5000",
                "total_amt_msat": "1005000",
                "hops": [{}, {}, {}],
            }],
            "success_prob": 0.42,
        })
        out = lnd_client.query_routes(PUB, 1000, fee_limit_sat=50)
        self.assertIsNotNone(out)
        self.assertEqual(out["fee_sat"], 5)
        self.assertEqual(out["fee_ppm"], 5000)
        self.assertEqual(out["hops"], 3)
        self.assertEqual(out["amt_sat"], 1000)
        self.assertAlmostEqual(out["success_prob"], 0.42)

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_ppm_is_amount_dependent(self, mget, _h):
        # Same 5 sat fee over 1,000,000 sat → 5 ppm (vs 5000 ppm at 1k sat).
        mget.return_value = _resp(payload={
            "routes": [{"total_fees_msat": "5000", "hops": [{}, {}]}],
        })
        out = lnd_client.query_routes(PUB, 1_000_000)
        self.assertEqual(out["fee_ppm"], 5)

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_no_route_returns_none(self, mget, _h):
        mget.return_value = _resp(status_code=404,
                                  payload={"message": "unable to find a path to destination"})
        self.assertIsNone(lnd_client.query_routes(PUB, 1000))

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_empty_routes_returns_none(self, mget, _h):
        mget.return_value = _resp(payload={"routes": []})
        self.assertIsNone(lnd_client.query_routes(PUB, 1000))

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_unexpected_error_returns_none(self, mget, _h):
        # A non-"no path" failure must not be mistaken for "no route"; returns
        # None but logs a warning rather than swallowing silently.
        mget.return_value = _resp(status_code=500, payload={"message": "boom"})
        self.assertIsNone(lnd_client.query_routes(PUB, 1000))

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_request_exception_returns_none(self, mget, _h):
        mget.side_effect = lnd_client.requests.RequestException("conn refused")
        self.assertIsNone(lnd_client.query_routes(PUB, 1000))

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_passes_optional_constraints_as_params(self, mget, _h):
        mget.return_value = _resp(payload={"routes": []})
        lnd_client.query_routes(
            "cd" * 33, 250_000,
            fee_limit_sat=120,
            source_pubkey="ee" * 33,
            outgoing_chan_id=123456789,
            last_hop_pubkey="ff" * 33,
            use_mission_control=False,
        )
        _, kwargs = mget.call_args
        params = kwargs["params"]
        self.assertEqual(params["fee_limit.fixed"], "120")
        self.assertEqual(params["source_pub_key"], "ee" * 33)
        self.assertEqual(params["outgoing_chan_id"], "123456789")
        self.assertEqual(params["use_mission_control"], "false")
        self.assertIn("last_hop_pubkey", params)  # base64-encoded bytes

    @patch("lnd_client._headers", return_value={})
    @patch("lnd_client.requests.get")
    def test_omits_unset_constraints(self, mget, _h):
        mget.return_value = _resp(payload={"routes": []})
        lnd_client.query_routes(PUB, 1000)
        _, kwargs = mget.call_args
        params = kwargs["params"]
        self.assertNotIn("fee_limit.fixed", params)
        self.assertNotIn("source_pub_key", params)
        self.assertNotIn("outgoing_chan_id", params)
        self.assertNotIn("last_hop_pubkey", params)
        self.assertEqual(params["use_mission_control"], "true")  # default on


if __name__ == "__main__":
    unittest.main()
