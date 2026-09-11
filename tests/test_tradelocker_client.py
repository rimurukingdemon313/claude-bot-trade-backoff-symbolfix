"""Unit tests for tradelocker_client.py's transport-layer behavior:
exponential backoff on HTTP 429, and instrument symbol matching.

These mock urllib.request.urlopen directly (no real network calls), so
they run anywhere including CI:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import unittest
import urllib.error
from unittest.mock import patch

import tradelocker_client as tl


def _http_error(code: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://demo.tradelocker.com/backend-api/test",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's response object."""

    def __init__(self, body: io.BytesIO):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body.read()


class BackoffRetryTests(unittest.TestCase):
    """Locks in the exponential-backoff-on-429 behavior: retries must only
    happen when explicitly enabled, must stop after a bounded number of
    attempts, and must never apply to writes."""

    def setUp(self):
        # time.sleep is patched so tests run in milliseconds instead of
        # actually waiting 1s + 2s + 4s.
        self.sleep_patcher = patch("tradelocker_client.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_429_retries_and_eventually_succeeds(self):
        """Two 429s followed by a real success must return the success,
        having slept with increasing (exponential) durations."""

        success_body = io.BytesIO(b'{"ok": true}')
        call_count = {"n": 0}

        def fake_urlopen(req, timeout):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise _http_error(429, b'{"error_code":1015,"error_name":"rate_limited"}')
            return _FakeResponse(success_body)

        with patch("tradelocker_client.urllib.request.urlopen", side_effect=fake_urlopen):
            result = tl._http(
                "GET", "https://demo.tradelocker.com/backend-api/test", retry_on_rate_limit=True
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(call_count["n"], 3)
        self.mock_sleep.assert_any_call(1)
        self.mock_sleep.assert_any_call(2)

    def test_429_without_retry_flag_raises_immediately(self):
        """retry_on_rate_limit=False (the default, and mandatory for
        writes) must raise on the very first 429 with zero sleeps."""

        def fake_urlopen(req, timeout):
            raise _http_error(429, b'{"error_code":1015}')

        with patch("tradelocker_client.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tl.TradeLockerError):
                tl._http("POST", "https://demo.tradelocker.com/backend-api/orders")

        self.mock_sleep.assert_not_called()

    def test_429_retry_gives_up_after_max_attempts(self):
        """Persistent 429s must eventually raise, not retry forever."""

        def fake_urlopen(req, timeout):
            raise _http_error(429, b'{"error_code":1015}')

        with patch("tradelocker_client.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tl.TradeLockerError):
                tl._http(
                    "GET",
                    "https://demo.tradelocker.com/backend-api/test",
                    retry_on_rate_limit=True,
                )
        # 4 total attempts (1 initial + 3 retries) means 3 sleeps.
        self.assertEqual(self.mock_sleep.call_count, 3)

    def test_non_429_error_never_retries_even_with_flag_enabled(self):
        """A 500, 401, etc. must raise immediately even when
        retry_on_rate_limit=True — backoff is specific to 429, not a
        general retry-on-any-error mechanism."""

        def fake_urlopen(req, timeout):
            raise _http_error(500, b'{"error":"server error"}')

        with patch("tradelocker_client.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tl.TradeLockerError):
                tl._http(
                    "GET",
                    "https://demo.tradelocker.com/backend-api/test",
                    retry_on_rate_limit=True,
                )
        self.mock_sleep.assert_not_called()


class InstrumentMatchingTests(unittest.TestCase):
    """Locks in the fix for brokers that suffix instrument names
    differently than the bot's plain symbol list (e.g. GATESFX lists
    'EURUSD.R' rather than a bare 'EURUSD')."""

    def _instruments(self, names: list[str]) -> list[dict]:
        return [
            {
                "tradableInstrumentId": str(100 + i),
                "id": str(100 + i),
                "name": name,
                "routes": [{"id": str(200 + i), "type": "TRADE"}],
            }
            for i, name in enumerate(names)
        ]

    def test_exact_match(self):
        instruments = self._instruments(["EURUSD", "USDJPY"])
        with patch.object(tl, "get_instruments", return_value=instruments):
            result = tl.find_instrument(config=None, symbol="EURUSD")
        self.assertEqual(result["tradableInstrumentId"], "100")

    def test_suffixed_broker_name_matches_via_prefix(self):
        """The exact bug from production: GATESFX lists 'EURUSD.R', and
        the bot's symbol list says 'EURUSD'."""

        instruments = self._instruments(["EURUSD.R", "USDJPY.R"])
        with patch.object(tl, "get_instruments", return_value=instruments):
            result = tl.find_instrument(config=None, symbol="EURUSD")
        self.assertEqual(result["tradableInstrumentId"], "100")

    def test_no_match_raises_with_available_names_listed(self):
        instruments = self._instruments(["USDJPY.R", "USDCHF.R"])
        with patch.object(tl, "get_instruments", return_value=instruments):
            with self.assertRaises(tl.TradeLockerError) as ctx:
                tl.find_instrument(config=None, symbol="EURUSD")
        # The error must name the real available instruments, not just
        # say "not found" — that's the whole point of the fix.
        self.assertIn("USDJPY.R", str(ctx.exception))
        self.assertIn("USDCHF.R", str(ctx.exception))

    def test_ambiguous_prefix_match_raises_rather_than_guessing(self):
        """If a symbol prefix-matches more than one distinct instrument,
        this must refuse rather than silently picking one — a wrong pick
        here means placing a real order on the wrong instrument."""

        instruments = self._instruments(["EURUSD.R", "EURUSDGBP.R"])
        with patch.object(tl, "get_instruments", return_value=instruments):
            with self.assertRaises(tl.TradeLockerError) as ctx:
                tl.find_instrument(config=None, symbol="EURUSD")
        self.assertIn("multiple instruments", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
