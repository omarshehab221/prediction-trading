"""Everything said to the venue and every payload read back from it."""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest

import requests

import btc_5m_predictor as m
import ws_feeds
from btc_5m_predictor import Config, PredictionClient, Side, assess, settle_pnl
from tests.support import (
    FakeClient,
    _close_journals,
    build_client,
    build_trader,
    cfg,
    make_round,
)

class TestParseRound(unittest.TestCase):

    @staticmethod
    def topic(**over) -> dict:
        base = {
            "marketTopicId": 4229564, "chartType": "CRYPTO_UP_DOWN",
            "symbol": "BTCUSDT", "status": "REGISTERED",
            "startDate": 1748131200000, "endDate": 1748131500000,
            "slug": "btc-5m", "feeRateBps": 200, "variantData": None,
            "vendor": "PREDICT_FUN", "chainId": "56", "collateral": "USDT",
            "slippageBps": 1200,
            "markets": [{"marketId": 5567895, "tradingStatus": "OPEN",
                         "decimalPrecision": 4, "liquidity": "100000",
                         "outcomes": [
                             {"name": "YES", "price": "0.52", "tokenId": "1"},
                             {"name": "NO", "price": "0.48", "tokenId": "2"}]}],
        }
        base.update(over)
        return base

    def test_parses_documented_payload_without_strike(self):
        """Regression: null variantData must not reject the round."""
        rnd = PredictionClient._parse_round(self.topic())
        self.assertIsNotNone(rnd)
        self.assertIsNone(rnd.strike)
        self.assertEqual(rnd.up_quote, 0.52)
        self.assertEqual(rnd.up_token_id, "1")

    def test_uses_strike_when_present(self):
        rnd = PredictionClient._parse_round(
            self.topic(variantData={"startPrice": "100000.5",
                                    "priceFeedSymbol": "BTCUSDT"}))
        self.assertAlmostEqual(rnd.strike, 100000.5)
        self.assertEqual(rnd.feed_symbol, "BTCUSDT")

    def test_accepts_up_down_outcome_names(self):
        t = self.topic()
        t["markets"][0]["outcomes"] = [
            {"name": "UP", "price": "0.52", "tokenId": "1"},
            {"name": "DOWN", "price": "0.48", "tokenId": "2"}]
        self.assertIsNotNone(PredictionClient._parse_round(t))

    def test_default_accepts_any_symbol(self):
        """No `symbols` configured -> nothing is filtered by ticker."""
        PredictionClient(cfg())
        try:
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                self.assertIsNotNone(PredictionClient._parse_round(
                    self.topic(symbol=sym)), sym)
        finally:
            PredictionClient(cfg())

    def test_rejects_wrong_symbol_when_restricted(self):
        PredictionClient(cfg(symbols=("BTCUSDT",)))
        try:
            self.assertIsNone(PredictionClient._parse_round(
                self.topic(symbol="ETHUSDT")))
        finally:
            PredictionClient(cfg())

    def test_rejects_wrong_chart_type(self):
        self.assertIsNone(PredictionClient._parse_round(
            self.topic(chartType="FLAT")))

    def test_rejects_wrong_duration(self):
        self.assertIsNone(PredictionClient._parse_round(
            self.topic(endDate=1748131200000 + 3_600_000)))

    def test_rejects_closed_trading(self):
        t = self.topic()
        t["markets"][0]["tradingStatus"] = "CLOSED"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_rejects_out_of_range_prices(self):
        t = self.topic()
        t["markets"][0]["outcomes"][0]["price"] = "1.5"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_survives_garbage(self):
        for bad in [{}, {"chartType": "CRYPTO_UP_DOWN"},
                    self.topic(markets=[]), self.topic(startDate="abc"),
                    self.topic(markets=[{"marketId": 1,
                                         "tradingStatus": "OPEN"}])]:
            self.assertIsNone(PredictionClient._parse_round(bad))


class TestParseAsks(unittest.TestCase):

    def test_array_shape(self):
        got = PredictionClient._parse_asks(
            {"asks": [["0.55", "100"], ["0.50", "50"]]})
        self.assertEqual(got, [(0.50, 50.0), (0.55, 100.0)])

    def test_dict_shape(self):
        got = PredictionClient._parse_asks(
            {"asks": [{"price": "0.6", "size": "10"}]})
        self.assertEqual(got, [(0.6, 10.0)])

    def test_nested_shape(self):
        got = PredictionClient._parse_asks(
            {"data": {"asks": [{"price": "0.4", "quantity": "5"}]}})
        self.assertEqual(got, [(0.4, 5.0)])

    def test_returns_sorted_ascending(self):
        got = PredictionClient._parse_asks(
            {"asks": [["0.9", "1"], ["0.1", "1"], ["0.5", "1"]]})
        self.assertEqual([p for p, _ in got], [0.1, 0.5, 0.9])

    def test_missing_or_empty(self):
        self.assertIsNone(PredictionClient._parse_asks({}))
        self.assertIsNone(PredictionClient._parse_asks({"asks": []}))
        self.assertIsNone(PredictionClient._parse_asks({"asks": [["x", "y"]]}))


class TestParseBids(unittest.TestCase):
    """Selling needs the price a sale would get, which is not the ask."""

    def test_bids_parse_with_the_same_rules_as_asks(self):
        payload = {"bids": [{"price": "0.40", "size": "10"},
                            {"price": "0.35", "size": "5"}]}
        levels = m.PredictionClient._parse_levels(payload, "bids")
        self.assertEqual(levels[0], (0.40, 10.0))     # best bid first

    def test_bids_skip_the_same_malformed_levels(self):
        payload = {"bids": [{"price": "0.40", "size": "10"},
                            {"price": "1.5", "size": "5"},
                            {"price": "nope", "size": "5"}]}
        levels = m.PredictionClient._parse_levels(payload, "bids")
        self.assertEqual(levels, [(0.40, 10.0)])

    def test_asks_still_come_back_ascending(self):
        payload = {"asks": [{"price": "0.45", "size": "5"},
                            {"price": "0.40", "size": "10"}]}
        self.assertEqual(m.PredictionClient._parse_asks(payload)[0],
                         (0.40, 10.0))

    def test_derive_bids_mirrors_derive_asks(self):
        """A DOWN bid of 0.31 is an UP ask of 0.69, so DOWN bids come from asks."""
        asks = [(0.69, 4.0), (0.72, 1.0)]
        bids = [(0.60, 3.0), (0.55, 2.0)]
        up = ws_feeds.derive_bids(asks, bids, Side.UP)
        self.assertEqual(up[0], (0.60, 3.0))
        down = ws_feeds.derive_bids(asks, bids, Side.DOWN)
        self.assertAlmostEqual(down[0][0], 0.31)
        self.assertEqual(down[0][1], 4.0)

    def test_bids_are_best_first(self):
        bids = [(0.55, 2.0), (0.60, 3.0)]
        out = ws_feeds.derive_bids([], bids, Side.UP)
        self.assertEqual([p for p, _ in out], [0.60, 0.55])


class TestVariantParsing(unittest.TestCase):
    """The strike is startPrice, and the feed is the venue's own oracle."""

    def test_reads_start_price_and_feed(self):
        strike, sym = PredictionClient._parse_variant(
            {"startPrice": "104250.5", "priceFeedSymbol": "BTCUSD",
             "priceFeedProvider": "PYTH"})
        self.assertAlmostEqual(strike, 104250.5)
        self.assertEqual(sym, "BTCUSD")

    def test_missing_start_price(self):
        self.assertEqual(PredictionClient._parse_variant({}), (None, None))

    def test_rejects_garbage(self):
        self.assertIsNone(
            PredictionClient._parse_variant({"startPrice": "abc"})[0])
        self.assertIsNone(
            PredictionClient._parse_variant({"startPrice": "0"})[0])


class TestSymbolMapping(unittest.TestCase):
    """Oracle feed symbols are not Binance tickers."""

    def _client(self, valid):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c._symbol_cache = {}
        calls = []

        class S:
            def get(self, url, params=None, timeout=None):
                calls.append(params["symbol"])
                return types.SimpleNamespace(
                    status_code=200 if params["symbol"] in valid else 400)

        c._session = S()
        return c, calls

    def test_none_defaults_to_btcusdt(self):
        c, _ = self._client({"BTCUSDT"})
        self.assertEqual(c.market_symbol(None), "BTCUSDT")

    def test_valid_symbol_passes_through(self):
        c, _ = self._client({"BTCUSDT", "BTCUSDC"})
        self.assertEqual(c.market_symbol("BTCUSDC"), "BTCUSDC")

    def test_pyth_style_symbol_is_normalised(self):
        c, calls = self._client({"BTCUSD"})
        self.assertEqual(c.market_symbol("BTC/USD"), "BTCUSD")
        self.assertIn("BTCUSD", calls)

    def test_unknown_feed_falls_back_not_crashes(self):
        c, _ = self._client({"BTCUSDT"})
        self.assertEqual(c.market_symbol("Crypto.BTC/USD"), "BTCUSDT")

    def test_result_is_cached(self):
        c, calls = self._client({"BTCUSD"})
        c.market_symbol("BTC/USD")
        c.market_symbol("BTC/USD")
        self.assertEqual(len(calls), 1)


class TestRequestSigning(unittest.TestCase):
    """The signed bytes and the sent bytes must be byte-identical."""

    def _client(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c._clock_offset_ms = 0
        c._symbol_cache = {}
        c._wallet = None
        return c

    def test_signature_covers_exactly_the_sent_string(self):
        import hmac, hashlib
        c = self._client()
        q = c._signed_query({"b": "2", "a": "1", "limit": 50})
        sent, sig = q.rsplit("&signature=", 1)
        expected = hmac.new(b"s", sent.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_a_list_of_objects_is_sent_as_json(self):
        """
        doseq would send each element's Python repr, which the venue rejects.

        @binance/common serialises every array and object parameter as JSON,
        and cancelInfoList is an array of objects, so this is the connector's
        format rather than a preference.
        """
        import json as _json, urllib.parse as _up
        c = self._client()
        query = c._signed_query({"cancelInfoList": [{"orderId": "a"},
                                                    {"orderId": "b"}]})
        parsed = _up.parse_qs(query)
        self.assertEqual(len(parsed["cancelInfoList"]), 1,
                         "sent as repeated parameters, not one JSON value")
        self.assertEqual(_json.loads(parsed["cancelInfoList"][0]),
                         [{"orderId": "a"}, {"orderId": "b"}])

    def test_an_indexed_key_is_signed_and_sent_with_literal_brackets(self):
        """
        The indexed cancel list failed -1022, signature invalid, with the
        key signed as cancelInfoList%5B0%5D.orderId. requests sends the
        brackets percent-encoded either way, so the venue must decode the
        query before verifying -- and the decoded form is the one to sign.
        """
        import hashlib as _hashlib, hmac as _hmac
        c = self._client()
        query = c._signed_query({"cancelInfoList[0].orderId": "o1"})
        self.assertIn("cancelInfoList[0].orderId=o1", query)
        signed, _, signature = query.rpartition("&signature=")
        self.assertEqual(signature, _hmac.new(
            c._cfg.api_secret.encode(), signed.encode(),
            _hashlib.sha256).hexdigest())
        sent = requests.Request(
            "POST", "https://api.binance.com/x?" + query).prepare().url
        self.assertIn("cancelInfoList%5B0%5D.orderId=o1", sent,
                      "the wire form this signing assumes has changed")

    def test_a_flat_list_is_sent_as_json(self):
        """
        tokenIds is a JSON array, not repeated parameters.

        Verified by running the connector's own serialiser: @binance/common
        puts every array parameter through JSON.stringify, and signs the
        string it built, so the venue both receives and HMACs the JSON form.
        Repeated parameters are not a Binance array convention anywhere.

        This was a guess from the initial commit that no live call ever
        tested -- batch_redeem cannot run until a real position exists, and
        place-order-bundle has never been executed against a funded account.
        """
        import json as _json, urllib.parse as _up
        c = self._client()
        query = c._signed_query({"tokenIds": ["a", "b"]})
        self.assertNotIn("tokenIds=a&tokenIds=b", query)
        parsed = _up.parse_qs(query)
        self.assertEqual(len(parsed["tokenIds"]), 1,
                         "sent as repeated parameters, not one JSON value")
        self.assertEqual(_json.loads(parsed["tokenIds"][0]), ["a", "b"])

    def test_the_signature_covers_the_json_form(self):
        """
        The signed bytes and the sent bytes must still be identical once a
        list has become a JSON string, or every redemption returns -1022.
        """
        import hmac as _h, hashlib as _hl
        c = self._client()
        q = c._signed_query({"tokenIds": ["a", "b"], "chainId": "56"})
        sent, sig = q.rsplit("&signature=", 1)
        self.assertEqual(
            sig, _h.new(b"s", sent.encode(), _hl.sha256).hexdigest())

    def test_a_dict_value_is_sent_as_json(self):
        import json as _json, urllib.parse as _up
        c = self._client()
        query = c._signed_query({"meta": {"k": "v"}})
        self.assertEqual(
            _json.loads(_up.parse_qs(query)["meta"][0]), {"k": "v"})

    def test_parameters_are_sorted_in_the_sent_string(self):
        c = self._client()
        sent = c._signed_query({"zeta": 1, "alpha": 2}).rsplit("&signature=", 1)[0]
        keys = [kv.split("=")[0] for kv in sent.split("&")]
        self.assertEqual(keys, sorted(keys))

    def test_timestamp_and_recvwindow_are_included(self):
        c = self._client()
        q = c._signed_query({})
        self.assertIn("timestamp=", q)
        self.assertIn("recvWindow=", q)

    def test_none_values_are_dropped(self):
        self.assertNotIn("skipme",
                         self._client()._signed_query({"skipme": None}))

    def test_list_params_never_serialise_as_a_python_repr(self):
        """
        Regression: tokenIds must not serialise as a Python repr.

        That is what this test has always been for, and it still is. What it
        used to ALSO assert -- repeated parameters, and no "[" in the query --
        pinned a shape the venue never asked for, so a correct change to the
        connector's JSON form read as a regression. The repr is the bug; the
        JSON array is the format.
        """
        c = self._client()
        q = c._signed_query({"tokenIds": ["111", "222"]})
        self.assertNotIn("%27", q)      # no "'" -- a Python repr
        self.assertIn("111", q)
        self.assertIn("222", q)

    def test_signature_changes_when_a_parameter_changes(self):
        c = self._client()
        a = c._signed_query({"x": "1"}).rsplit("&signature=", 1)[1]
        b = c._signed_query({"x": "2"}).rsplit("&signature=", 1)[1]
        self.assertNotEqual(a, b)


class TestErrorSurfacing(unittest.TestCase):
    """Binance puts the diagnosis in the body; it must reach the user."""

    def _client_returning(self, status, body, text="{}"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=status, text=text,
                    json=lambda: body if body is not None else (_ for _ in ()).throw(ValueError()))
        c._session = S()
        return c

    def test_signature_error_is_explained(self):
        c = self._client_returning(400, {"code": -1022, "msg": "Signature invalid"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        msg = str(ctx.exception)
        self.assertIn("-1022", msg)
        self.assertIn("signed and sent", msg)

    def test_permission_error_is_explained(self):
        c = self._client_returning(401, {"code": -2015, "msg": "Invalid API-key"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("permissions", str(ctx.exception))

    def test_unknown_code_still_surfaces_the_message(self):
        c = self._client_returning(400, {"code": -9999, "msg": "Weird failure"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("Weird failure", str(ctx.exception))

    def test_non_json_error_body_is_not_swallowed(self):
        c = self._client_returning(500, None, text="upstream exploded")
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("upstream exploded", str(ctx.exception))

    def test_quota_failure_propagates_rather_than_returning_none(self):
        """Regression: preflight printed 'OK None' for a failing endpoint."""
        c = self._client_returning(400, {"code": -1102, "msg": "bad param"})
        with self.assertRaises(m.ApiError):
            c.remaining_quota_usdt()


class TestEndpointMethods(unittest.TestCase):
    """The verb must travel with the path."""

    def test_trading_endpoints_are_post(self):
        for name in ("get_quote", "place_order", "batch_redeem"):
            self.assertEqual(m.DEFAULT_ENDPOINTS[name][0], "POST", name)

    def test_read_endpoints_are_get(self):
        for name in ("market_list", "market_detail", "order_book",
                     "wallet_list", "balances", "quota_status",
                     "settled_history", "redeem_status", "positions"):
            self.assertEqual(m.DEFAULT_ENDPOINTS[name][0], "GET", name)

    def test_every_endpoint_declares_a_valid_verb(self):
        for name, (verb, path) in m.DEFAULT_ENDPOINTS.items():
            self.assertIn(verb, ("GET", "POST"), name)
            self.assertTrue(path.startswith("/sapi/"), name)

    def _built(self, endpoints):
        doc = m.default_config_document()
        doc["endpoints"].update(endpoints)
        return m.build_config(doc, api_key="k", api_secret="s", live=False,
                              db_path="d")

    def test_override_accepts_bare_path_and_keeps_method(self):
        self.assertEqual(self._built({"get_quote": "/new/path"}).ep("get_quote"),
                         ("POST", "/new/path"))

    def test_override_accepts_method_path_pair(self):
        self.assertEqual(self._built({"order_book": ["POST", "/x"]})
                         .ep("order_book"), ("POST", "/x"))

    def test_override_rejects_bad_method(self):
        with self.assertRaises(ValueError):
            self._built({"order_book": ["FETCH", "/x"]})

    def test_override_rejects_unknown_endpoint(self):
        with self.assertRaises(ValueError):
            self._built({"not_an_endpoint": "/x"})


class TestBalanceLookup(unittest.TestCase):
    """Regression: a funded account reported 0.00 because of a hard filter."""

    def _client(self, items, account_type="AUTO"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(account_type=account_type)
        c._clock_offset_ms = 0; c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=200, text="", json=lambda: {"items": items})
        c._session = S()
        return c

    def test_finds_funds_under_an_unexpected_account_type(self):
        """The actual failure: money in FUNDING, code only looked at SPOT."""
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "6.64", "enabled": True}])
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_breakdown_lists_every_option(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "6.64", "enabled": False}])
        opts = c.payment_options()
        self.assertEqual(len(opts), 2)
        self.assertIn(("FUNDING", 6.64, False), opts)

    def test_disabled_options_are_not_used(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "1.0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "99", "enabled": False}])
        self.assertAlmostEqual(c.balance_usdt(), 1.0)

    def test_explicit_account_type_is_respected(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "1.0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "99", "enabled": True}],
            account_type="SPOT")
        self.assertAlmostEqual(c.balance_usdt(), 1.0)

    def test_explicit_missing_type_names_what_is_available(self):
        c = self._client([
            {"accountType": "FUNDING", "availableBalanceDisplay": "9", "enabled": True}],
            account_type="SPOT")
        with self.assertRaises(m.ApiError) as ctx:
            c.balance_usdt()
        self.assertIn("FUNDING=9.00", str(ctx.exception))

    def test_malformed_balance_is_treated_as_zero_not_a_crash(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "abc", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "5", "enabled": True}])
        self.assertAlmostEqual(c.balance_usdt(), 5.0)

    def test_empty_response_raises(self):
        with self.assertRaises(m.ApiError):
            self._client([]).balance_usdt()


class TestPredictionWalletBalance(unittest.TestCase):
    """
    payment-options covers the CEX accounts only. A prediction wallet funded
    directly does not appear there, which reported 0.00 for a funded account.
    """

    def _client(self, options, wallet_value):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(account_type="AUTO")
        c.payment_options = lambda: options
        c.prediction_wallet_value = lambda: wallet_value
        return c

    def test_prediction_wallet_balance_is_found(self):
        c = self._client([("SPOT", 0.0, True)], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_larger_cex_balance_wins(self):
        c = self._client([("SPOT", 500.0, True)], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 500.0)

    def test_unreadable_portfolio_falls_back_to_cex(self):
        c = self._client([("SPOT", 12.0, True)], None)
        self.assertAlmostEqual(c.balance_usdt(), 12.0)

    def test_nothing_anywhere_raises(self):
        c = self._client([], None)
        with self.assertRaises(m.ApiError):
            c.balance_usdt()

    def test_empty_options_with_funded_wallet_still_works(self):
        c = self._client([], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_portfolio_parses_total_current_value(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"totalCurrentValue": "6.64"}
        self.assertAlmostEqual(c.prediction_wallet_value(), 6.64)

    def test_portfolio_rejects_non_finite_value(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"totalCurrentValue": "1e999"}
        self.assertIsNone(c.prediction_wallet_value())

    def test_portfolio_missing_field_is_none(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"walletAddress": "0xa"}
        self.assertIsNone(c.prediction_wallet_value())


class TestFundingSourceDerivation(unittest.TestCase):
    """
    Regression for -3026.

    Three places can hold collateral. `accountType` on place-order accepts
    only SPOT|FUNDING; the prediction wallet is not a legal value there and
    passing it through is what the venue rejected.
    """

    def _client(self, options, cfg_funding="AUTO", cfg_account="AUTO"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(funding_source=cfg_funding, account_type=cfg_account)
        c.payment_options = lambda: options
        return c

    def test_spot_account_funds_from_cex(self):
        c = self._client([("SPOT", 50.0, True)])
        self.assertEqual(c.funding_plan()[:2], ("SPOT", "CEX"))

    def test_funding_account_funds_from_cex(self):
        c = self._client([("FUNDING", 50.0, True)])
        self.assertEqual(c.funding_plan()[:2], ("FUNDING", "CEX"))

    def test_prediction_wallet_funds_from_mpc(self):
        """The reported bug: funds in the prediction account."""
        c = self._client([("SPOT", 0.0, True),
                          ("PREDICTION", 6.64, True)])
        account, funding, holder = c.funding_plan()
        self.assertEqual(funding, "MPC")
        self.assertEqual(holder, "PREDICTION")
        self.assertIn(account, m.CEX_ACCOUNT_TYPES)   # never the raw holder

    def test_prediction_holder_never_leaks_into_account_type(self):
        for name in ("PREDICTION", "MPC", "PREDICTION_WALLET", "WEB3"):
            c = self._client([(name, 9.0, True)])
            self.assertIn(c.funding_plan()[0], m.CEX_ACCOUNT_TYPES, name)

    def test_no_options_at_all_still_yields_a_legal_account(self):
        c = self._client([])
        account, funding, holder = c.funding_plan()
        self.assertIn(account, m.CEX_ACCOUNT_TYPES)
        self.assertEqual(funding, "MPC")
        self.assertIsNone(holder)

    def test_explicit_override_is_respected(self):
        c = self._client([("SPOT", 50.0, True)], cfg_funding="MPC")
        self.assertEqual(c.funding_plan()[1], "MPC")

    def test_explicit_invalid_account_type_is_rejected(self):
        c = self._client([("SPOT", 50.0, True)], cfg_account="FUNDING")
        self.assertEqual(c.funding_plan()[0], "FUNDING")

    def test_disabled_options_are_ignored_when_planning(self):
        c = self._client([("SPOT", 100.0, False), ("FUNDING", 5.0, True)])
        self.assertEqual(c.funding_plan()[0], "FUNDING")

    def test_default_config_is_auto(self):
        self.assertEqual(cfg().funding_source, "AUTO")

    def test_invalid_funding_source_rejected(self):
        with self.assertRaises(ValueError):
            cfg(funding_source="WALLET")

    def _order_client(self, options):
        sent = {}
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c.payment_options = lambda: options
        c._request = lambda name, params=None: (sent.update(params or {}),
                                                {"orderId": "1"})[1]
        return c, sent

    def test_cex_order_includes_a_fund_transfer(self):
        c, sent = self._order_client([("SPOT", 50.0, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.5)
        self.assertEqual(sent["fundingSource"], "CEX")
        self.assertEqual(sent["fundTransferAmount"], m.to_wei(1.5))

    def test_prediction_wallet_order_omits_the_transfer(self):
        """Funds already in place: nothing to move."""
        c, sent = self._order_client([("PREDICTION", 6.64, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.5)
        self.assertEqual(sent["fundingSource"], "MPC")
        self.assertNotIn("fundTransferAmount", sent)
        self.assertIn(sent["accountType"], m.CEX_ACCOUNT_TYPES)

    def test_order_never_sends_an_illegal_account_type(self):
        for holder in ("PREDICTION", "MPC", "WEB3", "SPOT", "FUNDING"):
            c, sent = self._order_client([(holder, 9.0, True)])
            c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.0)
            self.assertIn(sent["accountType"], m.CEX_ACCOUNT_TYPES, holder)

    def test_time_in_force_matches_market_order_type(self):
        c, sent = self._order_client([("SPOT", 50.0, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.0)
        self.assertEqual((sent["orderType"], sent["timeInForce"]),
                         ("MARKET", "FOK"))


class TestMinimumDiscovery(unittest.TestCase):
    """The minimum is not a published field, so it is measured."""

    def _client(self, true_min, balance=50.0):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c.calls = []
        c.balance_usdt = lambda: balance

        def fake_quote(rnd, plan):
            amount = plan.amount
            c.calls.append(amount)
            if amount > balance:
                raise m.ApiError("Please ensure your account has enough USDT.",
                                 code=-9000)
            if amount < true_min:
                raise m.ApiError("amount below minimum order size", code=None)
            return m.Quote("q", 0.5, amount / 0.5, 0.0, 0.0)

        c.get_quote = fake_quote
        return c

    def test_finds_the_threshold(self):
        c = self._client(1.5)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.5, delta=0.05)

    def test_finds_a_one_dollar_minimum(self):
        c = self._client(1.0)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.0, delta=0.05)

    def test_returns_low_when_everything_is_quotable(self):
        c = self._client(0.0)
        self.assertEqual(c.discover_min_stake(make_round(), Side.UP, low=0.25), 0.25)

    def test_returns_none_when_no_size_is_quotable(self):
        """Every size in range is rejected as too small -> no answer."""
        c = self._client(999.0, balance=100.0)
        self.assertIsNone(c.discover_min_stake(make_round(), Side.UP))

    def test_finds_a_high_floor_when_balance_allows(self):
        """With enough balance the real floor is found, however high."""
        c = self._client(999.0, balance=5000.0)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.5)
        self.assertAlmostEqual(found, 999.0, delta=1.0)

    def test_upper_bound_derives_from_balance(self):
        """Regression: a fixed 10.0 ceiling crashed on a 6.64 balance."""
        c = self._client(1.0, balance=6.64)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.0, delta=0.05)
        self.assertTrue(all(a <= 6.64 + 1e-9 for a in c.calls),
                        f"probed above balance: {c.calls}")

    def test_funds_error_rebounds_instead_of_crashing(self):
        c = self._client(1.0, balance=6.64)
        found = c.discover_min_stake(make_round(), Side.UP, low=0.25,
                                     high=25.0, tolerance=0.01)
        self.assertIsNotNone(found)
        self.assertLessEqual(found, 6.64)

    def test_probing_is_logarithmic_not_linear(self):
        """Probe count must scale with log(range/tolerance), not the range."""
        import math as _math
        low, tol, balance = 0.25, 0.05, 50.0
        c = self._client(1.5, balance=balance)
        c.discover_min_stake(make_round(), Side.UP, low=low, tolerance=tol)
        bound = _math.ceil(_math.log2((balance - low) / tol)) + 2
        self.assertLessEqual(len(c.calls), bound)
        self.assertLess(len(c.calls), (balance - low) / tol)   # not linear

    def test_rejects_invalid_bounds(self):
        c = self._client(1.5)
        with self.assertRaises(ValueError):
            c.discover_min_stake(make_round(), Side.UP, low=5.0, high=1.0)


class TestQuoteErrorClassification(unittest.TestCase):
    """A broken request must not be reported as 'book too thin'."""

    def _client(self, raiser, balance=50.0):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c.get_quote = raiser
        c.balance_usdt = lambda: balance
        return c

    def test_size_rejection_is_treated_as_below_minimum(self):
        def q(rnd, plan):
            amount = plan.amount
            if amount < 2.0:
                raise m.ApiError("amount below minimum order size")
            return m.Quote("q", 0.5, 1.0, 0.0, 0.0)
        found = self._client(q).discover_min_stake(make_round(), Side.UP,
                                                   tolerance=0.01)
        self.assertAlmostEqual(found, 2.0, delta=0.05)

    def test_signature_error_is_raised_not_swallowed(self):
        """Regression: any error used to look like 'amount too small'."""
        def q(rnd, plan):
            raise m.ApiError("HTTP 400: Signature invalid", code=-1022)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_method_not_supported_is_raised(self):
        def q(rnd, plan):
            raise m.ApiError("Request method 'GET' is not supported",
                             code=-1104)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_permission_error_is_raised(self):
        def q(rnd, plan):
            raise m.ApiError("invalid API key", code=-2015)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_liquidity_error_counts_as_a_size_problem(self):
        def q(rnd, plan):
            amount = plan.amount
            if amount < 3.0:
                raise m.ApiError("insufficient liquidity for this amount")
            return m.Quote("q", 0.5, 1.0, 0.0, 0.0)
        found = self._client(q).discover_min_stake(make_round(), Side.UP,
                                                   tolerance=0.01)
        self.assertAlmostEqual(found, 3.0, delta=0.05)


class TestStrictFieldParsing(unittest.TestCase):
    """Missing venue fields reject the market instead of being invented."""

    def test_missing_decimal_precision_rejects(self):
        t = TestParseRound.topic()
        del t["markets"][0]["decimalPrecision"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_unknown_liquidity_is_none_not_zero(self):
        t = TestParseRound.topic()
        del t["markets"][0]["liquidity"]
        t.pop("liquidity", None)
        r = PredictionClient._parse_round(t)
        self.assertIsNotNone(r)
        self.assertIsNone(r.liquidity)

    def test_real_zero_liquidity_is_preserved(self):
        t = TestParseRound.topic()
        t["markets"][0]["liquidity"] = "0"
        self.assertEqual(PredictionClient._parse_round(t).liquidity, 0.0)

    def test_precision_zero_is_honoured(self):
        t = TestParseRound.topic()
        t["markets"][0]["decimalPrecision"] = 0
        self.assertEqual(PredictionClient._parse_round(t).decimal_precision, 0)


class TestQuoteValidation(unittest.TestCase):
    """A malformed quote must fail loudly, not be coerced into plausibility."""

    def _client(self, payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = m.WalletRef("0xabc", "w1")
        c._request = lambda name, params=None: payload
        c.resolved_funding_source = lambda: "MPC"
        return c

    def _quote(self, payload):
        return self._client(payload).get_quote(
            make_round(), m._market_buy(Side.UP, 5.0))

    def test_valid_quote_parses(self):
        q = self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "8333333333333333333",
                         "priceImpact": "0.01", "feeAmount": "0"})
        self.assertAlmostEqual(q.average_price, 0.6)
        self.assertGreater(q.amount_out, 8.0)

    def test_missing_amount_out_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"quoteId": "q", "averagePrice": "0.6"})

    def test_zero_shares_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "0"})

    def test_missing_price_impact_is_infinite_not_zero(self):
        """Unknown impact must not silently pass the impact guard."""
        q = self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "8333333333333333333"})
        self.assertEqual(q.price_impact, float("inf"))
        self.assertGreater(q.price_impact, cfg().max_price_impact)

    def test_implausible_price_raises(self):
        for bad in ("0", "1", "1.5", "-0.2"):
            with self.assertRaises(m.ApiError):
                self._quote({"quoteId": "q", "averagePrice": bad,
                             "amountOut": "1000000000000000000"})

    def test_missing_quote_id_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"averagePrice": "0.6", "amountOut": "1"})


class TestErrorClassification(unittest.TestCase):
    """Errors are classified by the venue's numeric code, not by wording."""

    def test_observed_insufficient_funds_code(self):
        e = m.ApiError("Please ensure your account has enough USDT.", code=-9000)
        self.assertIs(e.kind, m.ErrorKind.INSUFFICIENT_FUNDS)

    def test_auth_codes(self):
        for c in (-1022, -2014, -2015, -1002):
            self.assertIs(m.ApiError("x", code=c).kind, m.ErrorKind.AUTH, c)

    def test_timing_code(self):
        self.assertIs(m.ApiError("x", code=-1021).kind, m.ErrorKind.TIMING)

    def test_parameter_codes(self):
        for c in (-1102, -1104, -1121):
            self.assertIs(m.ApiError("x", code=c).kind, m.ErrorKind.PARAMETER, c)

    def test_unknown_code_is_not_guessed_from_text(self):
        """A code we do not know must not be inferred from wording."""
        e = m.ApiError("minimum order size not met", code=-4321)
        self.assertIs(e.kind, m.ErrorKind.UNKNOWN)

    def test_message_fallback_only_without_a_code(self):
        self.assertIs(m.ApiError("order below the minimum").kind,
                      m.ErrorKind.SIZE)
        self.assertIs(m.ApiError("account has enough USDT? no").kind,
                      m.ErrorKind.INSUFFICIENT_FUNDS)
        self.assertIs(m.ApiError("Signature for this request").kind,
                      m.ErrorKind.AUTH)

    def test_unrecognised_text_is_unknown(self):
        self.assertIs(m.ApiError("something odd happened").kind,
                      m.ErrorKind.UNKNOWN)

    def test_code_and_status_are_retained(self):
        e = m.ApiError("x", code=-9000, status=400)
        self.assertEqual((e.code, e.status), (-9000, 400))

    def test_request_attaches_the_code(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=400, text="",
                    json=lambda: {"code": -9000, "msg": "not enough USDT"})
        c._session = S()
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertEqual(ctx.exception.code, -9000)
        self.assertIs(ctx.exception.kind, m.ErrorKind.INSUFFICIENT_FUNDS)


class TestOrderResponseHandling(unittest.TestCase):
    """place_order paths, which mutation testing showed were unexercised."""

    def _client(self, payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = m.WalletRef("0xabc", "w1")
        c._request = lambda name, params=None: payload
        c._resolved_account_type = lambda: "SPOT"
        c.resolved_funding_source = lambda: "CEX"
        return c

    def test_successful_order_returns_the_id(self):
        c = self._client({"orderId": "12345"})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        self.assertEqual(c.place_order(make_round(), q), "12345")

    def test_missing_order_id_raises(self):
        c = self._client({"status": "ACCEPTED"})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        with self.assertRaises(m.ApiError):
            c.place_order(make_round(), q)

    def test_empty_order_id_raises(self):
        c = self._client({"orderId": ""})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        with self.assertRaises(m.ApiError):
            c.place_order(make_round(), q)


class TestHostilePayloads(unittest.TestCase):
    """
    Regressions from property-based fuzzing. Every case here was a real crash
    found by random input, not by anyone imagining it.
    """

    def test_asks_as_a_scalar_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": 4477054861}))

    def test_asks_as_a_string_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": "0.5"}))

    def test_asks_as_a_dict_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": {"a": 1}}))

    def test_infinite_numeric_does_not_crash_parsing(self):
        """OverflowError is not a ValueError; int(inf) raises it."""
        t = TestParseRound.topic()
        t["markets"][0]["marketId"] = float("inf")
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_scientific_overflow_string_is_rejected(self):
        t = TestParseRound.topic()
        t["markets"][0]["outcomes"][0]["price"] = "1e999"   # parses to inf
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_nan_price_is_rejected(self):
        t = TestParseRound.topic()
        t["markets"][0]["outcomes"][0]["price"] = "nan"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_infinite_liquidity_becomes_unknown(self):
        t = TestParseRound.topic()
        t["markets"][0]["liquidity"] = "1e999"
        r = PredictionClient._parse_round(t)
        self.assertIsNotNone(r)
        self.assertIsNone(r.liquidity)

    def test_infinite_start_price_is_rejected(self):
        strike, _ = PredictionClient._parse_variant({"startPrice": "1e999"})
        self.assertIsNone(strike)

    def test_nan_start_price_is_rejected(self):
        strike, _ = PredictionClient._parse_variant({"startPrice": "nan"})
        self.assertIsNone(strike)

    def test_non_finite_book_levels_are_skipped(self):
        got = PredictionClient._parse_asks(
            {"asks": [["nan", "10"], ["1e999", "5"], ["0.4", "100"]]})
        self.assertEqual(got, [(0.4, 100.0)])

    def test_null_bytes_in_strings_do_not_crash(self):
        t = TestParseRound.topic()
        t["slug"] = "btc\x00updown"
        self.assertIsNotNone(PredictionClient._parse_round(t))

    def test_deeply_nested_garbage_does_not_crash(self):
        for payload in ({"markets": [{"outcomes": [{"price": {"a": [1]}}]}]},
                        {"markets": "not-a-list"},
                        {"markets": [None]},
                        {"variantData": [1, 2, 3]}):
            self.assertIsNone(PredictionClient._parse_round(payload))


class TestBatchRedeemResponses(unittest.TestCase):
    """Telling 'accepted, in flight' apart from 'there was nothing to claim'."""

    @staticmethod
    def _client(payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None
        c._static_cfg = cfg()
        c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: payload
        return c

    def test_tx_hashes_are_returned(self):
        c = self._client({"results": [{"txHash": "0xdead"}]})
        self.assertEqual(c.batch_redeem(["1"], "56"), ["0xdead"])

    def test_accepted_batch_without_a_hash_is_in_flight(self):
        c = self._client({"results": [], "batchId": "b1"})
        self.assertEqual(c.batch_redeem(["1"], "56"), [])

    def test_empty_response_means_nothing_was_redeemable(self):
        c = self._client({"results": []})
        with self.assertRaises(m.NothingToRedeem):
            c.batch_redeem(["1"], "56")


class TestVenueDerivedParameters(unittest.TestCase):
    """Nothing routing-related may be assumed when the venue publishes it."""

    def test_chain_collateral_precision_come_from_payload(self):
        t = TestParseRound.topic()
        t["chainId"] = "97"; t["collateral"] = "USDC"
        t["markets"][0]["decimalPrecision"] = 6
        r = PredictionClient._parse_round(t)
        self.assertEqual((r.chain_id, r.collateral, r.decimal_precision),
                         ("97", "USDC", 6))

    def test_missing_chain_id_rejects_the_market(self):
        t = TestParseRound.topic(); del t["chainId"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_collateral_rejects_the_market(self):
        t = TestParseRound.topic(); del t["collateral"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_vendor_rejects_the_market(self):
        t = TestParseRound.topic(); del t["vendor"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_fee_rejects_rather_than_defaulting(self):
        """Regression: a missing fee used to silently become 200 bps."""
        t = TestParseRound.topic(); del t["feeRateBps"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_zero_fee_is_honoured_not_overridden(self):
        t = TestParseRound.topic(); t["feeRateBps"] = 0
        self.assertEqual(PredictionClient._parse_round(t).fee_bps, 0)

    def test_slippage_takes_the_tighter_of_ours_and_the_venue(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(max_slippage_bps=300)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=1200)), 300)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=100)), 100)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=0)), 300)

    def test_price_snaps_to_published_precision(self):
        self.assertEqual(make_round(decimal_precision=2).round_price(0.123456), 0.12)
        self.assertEqual(make_round(decimal_precision=4).round_price(0.123456), 0.1235)

    def test_round_duration_is_derived_not_assumed(self):
        r = make_round()
        self.assertEqual(r.duration_ms, r.end_ms - r.start_ms)

    def test_duration_tolerance_scales_with_the_target(self):
        base = 1_700_000_000_000
        t = TestParseRound.topic(startDate=base, endDate=base + 300_000)
        self.assertIsNotNone(PredictionClient._parse_round(t))
        t2 = TestParseRound.topic(startDate=base, endDate=base + 360_000)
        self.assertIsNone(PredictionClient._parse_round(t2))   # 20% off target


class TestLimitQuoting(unittest.TestCase):
    """The wire format for a limit order, checked against the connector."""

    def _client(self, payload=None):
        c = build_client(_wallet=m.WalletRef("0xabc", "w1"))
        c.sent = []

        def fake_request(name, params=None):
            c.sent.append((name, dict(params or {})))
            if name == "get_quote":
                return payload or {"quoteId": "q1", "averagePrice": "0.40",
                                   "amountOut": m.to_wei(12.5),
                                   "priceImpact": 0.0,
                                   "feeAmount": "0"}
            return {"orderId": "o1"}

        c._request = fake_request
        c.funding_plan = lambda: ("SPOT", "MPC", None)
        c.resolved_funding_source = lambda: "MPC"
        return c

    def test_limit_quote_sends_price_as_a_plain_decimal(self):
        """
        priceLimit is NOT wei. amountIn's doc comment says wei explicitly and
        priceLimit's says only "must be > 0"; sending 18 decimals here is a
        price of 1e18 on a market whose prices live in (0, 1).
        """
        c = self._client()
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        c.get_quote(make_round(), plan)
        sent = dict(c.sent)["get_quote"]
        self.assertEqual(sent["orderType"], "LIMIT")
        self.assertEqual(sent["side"], "BUY")
        self.assertEqual(float(sent["priceLimit"]), 0.40)
        self.assertLess(len(sent["priceLimit"]), 8)     # not 18 decimals
        self.assertEqual(sent["amountIn"], m.to_wei(5.0))

    def test_market_quote_sends_no_price_limit(self):
        c = self._client()
        c.get_quote(make_round(), m._market_buy(m.Side.UP, 5.0))
        sent = dict(c.sent)["get_quote"]
        self.assertEqual(sent["orderType"], "MARKET")
        self.assertNotIn("priceLimit", sent)

    def test_quote_carries_what_was_quoted(self):
        c = self._client()
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        q = c.get_quote(make_round(), plan)
        self.assertIs(q.order_type, m.OrderType.LIMIT)
        self.assertIs(q.action, m.Action.BUY)
        self.assertEqual(q.price_limit, 0.40)

    def test_place_order_pairs_gtc_with_limit(self):
        c = self._client()
        q = m.Quote("q1", 0.40, 12.5, 0.0, 0.0, action=m.Action.BUY,
                    order_type=m.OrderType.LIMIT, price_limit=0.40)
        c.place_order(make_round(), q, 5.0)
        sent = dict(c.sent)["place_order"]
        self.assertEqual((sent["orderType"], sent["timeInForce"]),
                         ("LIMIT", "GTC"))
        self.assertEqual(float(sent["priceLimit"]), 0.40)

    def test_place_order_still_pairs_fok_with_market(self):
        c = self._client()
        c.place_order(make_round(), m.Quote("q1", 0.40, 12.5, 0.0, 0.0), 5.0)
        sent = dict(c.sent)["place_order"]
        self.assertEqual((sent["orderType"], sent["timeInForce"]),
                         ("MARKET", "FOK"))
        self.assertNotIn("priceLimit", sent)

    def test_sell_consistency_check_inverts(self):
        """
        On a SELL the input is shares and the output is USDT. Checking
        amountOut x price against amountIn -- the BUY reconciliation -- fails
        for every well-formed sell quote.
        """
        # 10 shares in at 0.50 => 5 USDT out. Internally consistent.
        c = self._client({"quoteId": "q1", "averagePrice": "0.50",
                          "amountOut": m.to_wei(5.0), "priceImpact": 0.0,
                          "feeAmount": "0"})
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.50)
        q = c.get_quote(make_round(), plan)
        self.assertEqual(q.amount_out, 5.0)

    def test_an_inconsistent_sell_quote_is_still_rejected(self):
        # 10 shares at 0.50 cannot produce 40 USDT.
        c = self._client({"quoteId": "q1", "averagePrice": "0.50",
                          "amountOut": m.to_wei(40.0), "priceImpact": 0.0,
                          "feeAmount": "0"})
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.50)
        with self.assertRaises(m.ApiError):
            c.get_quote(make_round(), plan)


class TestOrderStateAndCancel(unittest.TestCase):
    """A GTC order's ordinary answer is 'still resting', not an exception."""

    def _client(self, active=(), history=()):
        c = build_client(_wallet=m.WalletRef("0xabc", "w1"))
        c.sent = []

        def fake_request(name, params=None):
            c.sent.append((name, dict(params or {})))
            if name == "order_list":
                return {"orders": list(active)}
            if name == "order_history":
                return {"orders": list(history)}
            if name == "batch_cancel":
                return {"canceled": ["o1"],
                        "failed": [{"orderId": "o2",
                                    "reason": "already filled"}]}
            return {}

        c._request = fake_request
        return c

    def test_a_resting_order_reports_resting(self):
        c = self._client(active=[{"orderId": "o1", "status": "OPEN",
                                  "filledUsdtAmount": "0"}])
        state = c.order_state("o1")
        self.assertEqual(state.status, "RESTING")
        self.assertEqual(state.filled_usdt, 0.0)

    def test_a_partly_filled_order_reports_partial_with_its_fill(self):
        c = self._client(active=[{"orderId": "o1", "status": "OPEN",
                                  "filledUsdtAmount": "2.5",
                                  "filledShareQty": "6.25",
                                  "price": "0.40"}])
        state = c.order_state("o1")
        self.assertEqual(state.status, "PARTIAL")
        self.assertEqual(state.filled_usdt, 2.5)
        self.assertEqual(state.filled_shares, 6.25)
        self.assertEqual(state.price, 0.40)

    def test_a_filled_order_reports_filled(self):
        c = self._client(history=[{"orderId": "o1", "status": "FILLED",
                                   "filledUsdtAmount": "5.0"}])
        self.assertEqual(c.order_state("o1").status, "FILLED")

    def test_a_cancelled_order_keeps_the_fill_it_got(self):
        """
        A cancel after a partial fill is DEAD with money in it. Dropping the
        fill because the status is terminal strands a real position.
        """
        c = self._client(history=[{"orderId": "o1", "status": "CANCELLED",
                                   "filledUsdtAmount": "1.5",
                                   "filledShareQty": "3.75"}])
        state = c.order_state("o1")
        self.assertEqual(state.status, "DEAD")
        self.assertEqual(state.filled_usdt, 1.5)

    def test_an_unknown_order_is_none_not_dead(self):
        """
        The history lagging the placement is the absence of knowledge. Read
        as DEAD it abandons an order that is still out there.
        """
        self.assertIsNone(self._client().order_state("o9"))

    def test_cancel_reports_failures_without_interpreting_them(self):
        c = self._client()
        cancelled, failed = c.cancel_orders(["o1", "o2"])
        self.assertEqual(cancelled, ["o1"])
        self.assertEqual(failed, {"o2": "already filled"})

    def test_cancel_sends_the_connector_s_shape(self):
        c = self._client()
        c.cancel_orders(["o1", "o2"])
        sent = dict(c.sent)["batch_cancel"]
        self.assertEqual(sent["cancelInfoList"],
                         [{"orderId": "o1"}, {"orderId": "o2"}])
        self.assertEqual(sent["walletAddress"], "0xabc")
        self.assertEqual(sent["walletId"], "w1")

    def test_a_malformed_list_is_resent_in_indexed_form(self):
        """
        The venue answered the JSON list with -1102, "cancelInfoList was not
        sent, was empty/null, or malformed", on every cancel. The indexed
        form is the other encoding a list of objects has, so it is tried
        before the cancel is reported as failed.
        """
        c = self._client()
        sent = []

        def request(name, params=None):
            sent.append(dict(params))
            if "cancelInfoList" in params:
                raise m.ApiError("Mandatory parameter 'cancelInfoList' was "
                                 "not sent", code=-1102)
            return {"canceled": ["o1"]}

        c._request = request
        cancelled, _ = c.cancel_orders(["o1", "o2"])
        self.assertEqual(cancelled, ["o1"])
        self.assertEqual(sent[-1]["cancelInfoList[0].orderId"], "o1")
        self.assertEqual(sent[-1]["cancelInfoList[1].orderId"], "o2")
        self.assertEqual(sent[-1]["walletId"], "w1")

    def test_cancelling_nothing_makes_no_request(self):
        c = self._client()
        self.assertEqual(c.cancel_orders([]), ([], {}))
        self.assertEqual(c.sent, [])


class TestOrderPlan(unittest.TestCase):
    """An order that cannot be described correctly cannot be constructed."""

    def test_time_in_force_is_derived_from_the_order_type(self):
        self.assertEqual(m.OrderType.MARKET.time_in_force, "FOK")
        self.assertEqual(m.OrderType.LIMIT.time_in_force, "GTC")

    def test_limit_without_a_price_is_unconstructible(self):
        with self.assertRaises(ValueError):
            m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                        order_type=m.OrderType.LIMIT, amount=5.0)

    def test_market_with_a_price_is_unconstructible(self):
        """A priceLimit on a MARKET order is a contradiction, not a hint."""
        with self.assertRaises(ValueError):
            m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                        order_type=m.OrderType.MARKET, amount=5.0,
                        price_limit=0.4)

    def test_limit_price_must_be_a_probability(self):
        for bad in (0.0, 1.0, -0.5, 1.5):
            with self.assertRaises(ValueError):
                m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                            order_type=m.OrderType.LIMIT, amount=5.0,
                            price_limit=bad)

    def test_amount_must_be_positive(self):
        with self.assertRaises(ValueError):
            m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                        order_type=m.OrderType.MARKET, amount=0.0)

    def test_a_valid_limit_plan_survives(self):
        plan = m.OrderPlan(side=m.Side.DOWN, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=12.0,
                           price_limit=0.62)
        self.assertEqual(plan.order_type.time_in_force, "GTC")
        self.assertEqual(plan.action.value, "SELL")

    def test_quote_keeps_its_positional_shape(self):
        """Nine test sites build a Quote positionally; do not reorder it."""
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        self.assertEqual(q.quote_id, "q")
        self.assertEqual(q.average_price, 0.6)
        self.assertEqual(q.amount_out, 8.0)
        self.assertIs(q.order_type, m.OrderType.MARKET)
        self.assertIs(q.action, m.Action.BUY)
        self.assertIsNone(q.price_limit)


class TestAuthWait(unittest.TestCase):
    """
    Preflight waits for a shared outbound IP to be allowlisted.

    The address is not knowable until the process runs and can change on any
    restart, so it cannot be added in advance. Failing on the first refusal
    kills the deploy a second after printing the one thing needed to fix it.
    """

    def setUp(self):
        self.slept = []
        self._sleep = time.sleep
        time.sleep = self.slept.append

    def tearDown(self):
        time.sleep = self._sleep

    def _cfg(self, **kw):
        base = dict(auth_wait_timeout_s=30.0, auth_wait_poll_s=5.0)
        base.update(kw)
        return cfg(**base)

    @staticmethod
    def _quiet(fn, *args):
        """wait_for_auth reports progress on stdout; tests do not need it."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args)

    def _client(self, outcomes):
        """A client whose wallet() yields `outcomes` in order."""
        seq = iter(outcomes)

        def wallet():
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        return types.SimpleNamespace(sync_clock=lambda: 0, wallet=wallet)

    @staticmethod
    def _auth_error():
        return m.ApiError("Invalid API-key, IP, or permissions", code=-2015)

    def test_accepts_immediately_when_the_ip_is_allowlisted(self):
        client = self._client([m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_retries_an_auth_refusal_until_it_is_accepted(self):
        client = self._client([self._auth_error(), self._auth_error(),
                               m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [5.0, 5.0])

    def test_retries_a_network_error_too(self):
        client = self._client([requests.RequestException("connection reset"),
                               m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))

    def test_gives_up_at_the_deadline(self):
        client = self._client([self._auth_error()] * 50)
        self.assertFalse(self._quiet(m.wait_for_auth,
                                    self._cfg(auth_wait_timeout_s=0.0),
                                    client))

    def test_a_blocked_region_fails_at_once(self):
        """
        451 is the server's REGION, not its address. Waiting cannot change
        which continent the worker is on, so retrying only delays the news.
        """
        blocked = m.ApiError("restricted location", status=451)
        client = self._client([blocked, m.WalletRef("0xabc", "1")])
        self.assertFalse(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_a_non_auth_failure_fails_at_once(self):
        other = m.ApiError("balance too low", code=-9000)
        client = self._client([other, m.WalletRef("0xabc", "1")])
        self.assertFalse(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_the_wait_is_off_by_default(self):
        """Existing deployments must not silently start hanging on boot."""
        self.assertEqual(Config(api_key="k", api_secret="s",
                                ).auth_wait_timeout_s, 0.0)
        for name, prof in m.PROFILES.items():
            self.assertNotIn("auth_wait_timeout_s", prof, name)

    def test_a_negative_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            cfg(auth_wait_timeout_s=-1.0)
        with self.assertRaises(ValueError):
            cfg(auth_wait_poll_s=0.0)

    def test_preflight_prints_the_address_before_waiting(self):
        """The address is useless after the process has already given up."""
        import inspect
        src = inspect.getsource(m.preflight)
        self.assertLess(src.index("Outbound IP"), src.index("wait_for_auth"))

    def test_one_ip_lookup_shared_by_preflight_and_whoami(self):
        import inspect
        self.assertIn("outbound_ip(", inspect.getsource(m.preflight))
        self.assertIn("outbound_ip(", inspect.getsource(m.whoami))


class TestPerMarketFee(unittest.TestCase):
    """The market's published fee wins over the config fallback."""

    def test_kelly_uses_the_supplied_fee(self):
        c = cfg(fee_bps=200)
        low = m.kelly_stake(1000, 0.65, 0.60, c, fee_bps=1)
        high = m.kelly_stake(1000, 0.65, 0.60, c, fee_bps=200)
        self.assertGreater(low, high)      # smaller fee -> larger edge -> bigger

    def test_clears_edge_uses_the_supplied_fee(self):
        c = cfg(min_edge=0.045, min_edge_ratio=0.0)
        self.assertTrue(m.clears_edge(0.6460, 0.60, c, fee_bps=1))
        self.assertFalse(m.clears_edge(0.6460, 0.60, c, fee_bps=200))

    def test_evaluate_reads_fee_from_the_round(self):
        rnd_free = make_round(fee_bps=1)
        rnd_paid = make_round(fee_bps=900)
        now = rnd_free.end_ms - 60_000
        book = {Side.UP: [(0.55, 10_000)]}
        c = cfg(min_edge=0.04, min_edge_ratio=0.05)
        s_free = assess(rnd_free, 100_150, 0.5, 1000, now, c, book).signal
        s_paid = assess(rnd_paid, 100_150, 0.5, 1000, now, c, book).signal
        self.assertIsNotNone(s_free)
        if s_paid is not None:
            self.assertGreater(s_free.edge, s_paid.edge)

    def test_settle_pnl_honours_a_zero_fee(self):
        self.assertAlmostEqual(settle_pnl(1.0, 0.5, True, 0), 1.0, places=9)
        self.assertAlmostEqual(settle_pnl(1.0, 0.5, True, 200), 0.98, places=9)

    def test_fallback_used_when_no_fee_supplied(self):
        c = cfg(fee_bps=500)
        self.assertAlmostEqual(m.kelly_stake(1000, 0.65, 0.60, c),
                               m.kelly_stake(1000, 0.65, 0.60, c, 500))


class TestRedemption(unittest.TestCase):
    """Winnings are not auto-credited; unclaimed wins must not look like loss."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def _live_trader(self, client, **over):
        c = cfg(db_path=self.db, live=True, **over)
        return build_trader(client, c, self.db)

    def _win_once(self, client, **over):
        over.setdefault("claim_poll_interval_s", 0.01)
        over.setdefault("claim_timeout_s", 0.5)
        t = self._live_trader(client, **over)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        client.t = 2
        client._winners[1] = Side.UP
        t._settle_open()
        return t

    @staticmethod
    def _client():
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 100_400.0)]
        return FakeClient([rnd], path, {(1, Side.UP): [(0.50, 10_000)]}, {})

    def test_a_win_triggers_redemption(self):
        client = self._client()
        client.redeem_state = "SUCCESS"
        t = self._win_once(client)
        t._claim_queue.join()          # wait for the background worker
        self.assertEqual(client.redeemed, ["1"])   # UP token
        self.assertEqual(t._unredeemed, {})        # confirmed and cleared

    def test_unredeemed_winnings_are_not_double_counted(self):
        """
        The API reading is authoritative by default.

        Adding the gross payout on top of a portfolio figure that already
        includes settled positions inflated the bankroll by the GROSS payout
        (stake + profit), not the profit.
        """
        client = self._client()
        client.balance = 95.0
        t = self._win_once(client)
        self.assertAlmostEqual(t._bankroll(), 95.0, places=6)

    def test_unredeemed_can_be_counted_when_explicitly_configured(self):
        client = self._client()
        client.balance = 95.0
        # _claim() records the pending payout synchronously, before handing
        # the actual redemption to the worker, so this is readable right
        # away without waiting on the background thread.
        t = self._win_once(client, count_unredeemed_in_bankroll=True)
        payout = t._unredeemed["1"][0]
        self.assertAlmostEqual(t._bankroll(), 95.0 + payout, places=6)

    def test_a_winning_streak_does_not_trip_the_loss_limit(self):
        client = self._client()
        client.balance = 82.0                      # three stakes out, none back
        t = self._win_once(client)
        t._account_risk.check(t._bankroll())               # must not raise

    def test_confirmed_redemption_clears_the_pending_entry(self):
        client = self._client()
        client.redeem_state = "PENDING"
        t = self._win_once(client)
        client.balance = 105.0
        # Flip to confirmed and let the worker's next poll pick it up --
        # this is the background equivalent of the old manual poll call.
        client.redeem_state = "SUCCESS"
        t._claim_queue.join()
        self.assertEqual(t._unredeemed, {})
        self.assertAlmostEqual(t._bankroll(), 105.0, places=6)

    def test_pending_redemption_is_not_cleared_early(self):
        client = self._client()
        client.redeem_state = "PENDING"
        t = self._win_once(client, claim_timeout_s=0.05,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()          # worker gives up after claim_timeout_s
        self.assertIn("1", t._unredeemed)   # still tracked, not silently dropped

    def test_failed_claim_is_still_tracked_and_retried(self):
        client = self._client()
        client.redeem_fails = True
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        self.assertIn("1", t._unredeemed)          # value not lost
        # The worker is retrying relentlessly in the background; let it
        # fail a few times, then allow it through and wait for the tx hash.
        time.sleep(0.05)
        client.redeem_fails = False
        client.redeem_state = "SUCCESS"
        t._claim_queue.join()
        self.assertTrue(client.redeemed)           # the retry got through
        self.assertEqual(t._unredeemed, {})        # and then confirmed

    def _timeout_warnings(self, logs):
        return [r.getMessage() for r in logs.records
                if "not confirmed within" in r.getMessage()]

    def test_a_timed_out_claim_says_what_the_venue_answered(self):
        """
        "Not confirmed within 90s" alone reads the same whether the chain is
        slow, the venue refuses the claim, or the payout was credited without
        one. The venue's own answer is the only thing that separates them,
        and it used to be logged at DEBUG, which the host never shows.
        """
        client = self._client()

        def refuse(token_ids, chain_id="56"):
            raise m.ApiError("the round is still settling")

        client.batch_redeem = refuse
        with self.assertLogs("btc5m", level="WARNING") as logs:
            t = self._win_once(client, claim_timeout_s=0.05,
                               claim_poll_interval_s=0.01)
            t._claim_queue.join()
        timeouts = self._timeout_warnings(logs)
        self.assertEqual(len(timeouts), 1, logs.output)
        self.assertIn("the round is still settling", timeouts[0])

    def test_a_status_that_never_confirms_is_named(self):
        client = self._client()
        client.redeem_state = "PENDING"
        with self.assertLogs("btc5m", level="WARNING") as logs:
            t = self._win_once(client, claim_timeout_s=0.05,
                               claim_poll_interval_s=0.01)
            t._claim_queue.join()
        timeouts = self._timeout_warnings(logs)
        self.assertEqual(len(timeouts), 1, logs.output)
        self.assertIn("PENDING", timeouts[0])

    def test_a_refused_claim_is_warned_once_not_every_retry(self):
        client = self._client()

        def refuse(token_ids, chain_id="56"):
            raise m.ApiError("the round is still settling")

        client.batch_redeem = refuse
        with self.assertLogs("btc5m", level="WARNING") as logs:
            t = self._win_once(client, claim_timeout_s=0.1,
                               claim_poll_interval_s=0.01)
            t._claim_queue.join()
        early = [r for r in logs.records
                 if "still settling" in r.getMessage()
                 and "not confirmed within" not in r.getMessage()]
        self.assertEqual(len(early), 1, logs.output)

    def test_a_loss_never_redeems(self):
        client = self._client()
        t = self._live_trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        client.t = 2
        client._winners[1] = Side.DOWN
        t._settle_open()
        self.assertEqual(client.redeemed, [])
        self.assertEqual(t._unredeemed, {})

    def test_paper_mode_never_redeems(self):
        client = self._client()
        c = cfg(db_path=self.db)
        t = build_trader(client, c, self.db)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        client.t = 2
        client._winners[1] = Side.UP
        t._settle_open()
        self.assertEqual(client.redeemed, [])

    # -- redeemed by hand, outside the bot ---------------------------------

    def test_a_hand_redeemed_win_stops_being_chased(self):
        """
        The operator claims the win in the Binance app. The bot must notice.

        Its only success signal used to be the status of a tx hash IT had
        submitted, so a redemption performed anywhere else was invisible: the
        worker re-submitted the claim every poll for the whole timeout and
        then kept the token in _unredeemed forever, where nothing retries it.
        """
        client = self._client()
        calls = []

        def nothing_left(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            raise m.NothingToRedeem("no redeemable balance for 1")

        client.batch_redeem = nothing_left
        client.balance = 105.0                 # the money is already there
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 1)        # asked once, took the answer
        self.assertEqual(t._unredeemed, {})    # no phantom left behind
        self.assertAlmostEqual(t._outstanding(), 0.0, places=6)

    def test_venue_saying_already_redeemed_ends_the_claim(self):
        """The same fact delivered as an error rather than an empty batch."""
        client = self._client()
        calls = []

        def already(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            raise m.ApiError("token has already been redeemed")

        client.batch_redeem = already
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(t._unredeemed, {})

    def test_a_dead_redemption_tx_is_resubmitted_then_stops(self):
        """
        A hand-redemption that races the bot's own transaction.

        The bot gets a tx hash, the operator claims in the app, and the bot's
        transaction reverts. Once a hash existed the worker only ever asked
        for its status, so a terminal failure kept it polling a transaction
        that would never confirm until the timeout ran out.
        """
        client = self._client()
        client.redeem_state = "REVERTED"
        calls = []

        def redeem(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            if len(calls) == 1:
                return ["0xtx1"]
            raise m.NothingToRedeem("nothing redeemable for 1")

        client.batch_redeem = redeem
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 2)        # resubmitted once, then stopped
        self.assertEqual(t._unredeemed, {})

    def test_a_genuine_redeem_failure_is_still_retried(self):
        """
        The escape hatch must not swallow real failures.

        A transient rejection is an absence of information, not proof the
        money landed, and dropping the token on one would strand a real win.
        """
        client = self._client()
        client.redeem_fails = True
        t = self._win_once(client, claim_timeout_s=0.2,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertIn("1", t._unredeemed)       # still tracked
