"""The sockets: connection, books, spot, futures and recycling."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from dataclasses import replace

import btc_5m_predictor as m
import ws_feeds
from tests.support import ROOT, build_client, cfg, scalp_cfg

class TestWsConnection(unittest.TestCase):
    """Lifecycle only. Nothing here knows what the frames mean."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = cfg()

    def _conn(self, **kw):
        kw.setdefault("name", "test")
        kw.setdefault("url_factory", lambda: "wss://example.invalid/x")
        kw.setdefault("on_message", lambda _raw: None)
        kw.setdefault("cfg_source", self.cfg)
        return self.ws_feeds.WsConnection(**kw)

    def test_a_fresh_connection_is_not_healthy_until_a_frame_arrives(self):
        """
        Health is evidence, not optimism. A socket that opened and then said
        nothing has proved nothing, and treating it as healthy is how a dead
        feed gets to price an order.
        """
        c = self._conn()
        self.assertFalse(c.healthy)

    def test_a_recent_frame_makes_it_healthy(self):
        c = self._conn()
        c.mark_frame()
        self.assertTrue(c.healthy)

    def test_silence_past_the_budget_makes_it_unhealthy(self):
        c = self._conn()
        c.mark_frame()
        c.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertFalse(c.healthy)

    def test_a_disabled_connection_is_never_healthy(self):
        c = self._conn()
        c.mark_frame()
        c.disabled = True
        self.assertFalse(c.healthy)

    def test_backoff_grows_and_is_capped(self):
        c = self._conn()
        delays = [c.backoff_for(n) for n in range(1, 12)]
        self.assertEqual(delays, sorted(delays), "backoff must not shrink")
        self.assertLessEqual(max(delays), self.cfg.ws_reconnect_max_s)
        self.assertGreater(delays[-1], delays[0])

    def test_an_auth_refusal_disables_the_feed_and_does_not_retry(self):
        """
        A 401 means the key is wrong or the egress IP is not allowlisted.
        Neither heals on its own, and this venue rate-limits by IP, so
        retrying turns a configuration error into a second problem. On a
        Render starter the egress address is not knowable in advance, which
        is the whole reason AUTH_WAIT_S exists -- so this refusal has to be
        legible rather than buried under a reconnect loop.
        """
        c = self._conn()
        c.note_failure(self.ws_feeds.WsAuthRefused("HTTP 401"))
        self.assertTrue(c.disabled)
        self.assertFalse(c.should_retry)

    def test_a_transport_failure_does_retry(self):
        c = self._conn()
        c.note_failure(OSError("connection reset"))
        self.assertFalse(c.disabled)
        self.assertTrue(c.should_retry)

    def test_a_handshake_401_is_read_as_an_auth_refusal(self):
        self.assertTrue(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 401 Unauthorized")))
        self.assertTrue(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 403 Forbidden")))
        self.assertFalse(self.ws_feeds.is_auth_refusal(
            Exception("Connection to remote host was lost")))
        self.assertFalse(self.ws_feeds.is_auth_refusal(
            Exception("Handshake status 503 Service Unavailable")))

    def test_recycle_is_due_before_the_venue_would_close_it(self):
        c = self._conn()
        c.opened_ts = time.time()
        self.assertFalse(c.recycle_due)
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        self.assertTrue(c.recycle_due)

    def test_opening_restarts_the_recycle_clock(self):
        """
        Consumers are told about a reconnect through the on_open
        callback, not by watching a counter, so the only thing a reopen
        has to record is when this socket started -- which is what the
        23h handover is measured from.
        """
        c = self._conn()
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        self.assertTrue(c.recycle_due)
        c.note_open()
        self.assertFalse(c.recycle_due)

    def test_a_raising_handler_does_not_take_the_reader_down(self):
        """
        Mirrors the claim worker: a bug in one frame's handling must not
        strand the feed. It is logged and the next frame is read.
        """
        seen = []

        def boom(raw):
            seen.append(raw)
            raise ValueError("bad frame")

        c = self._conn(on_message=boom)
        c.dispatch("{}")
        c.dispatch("{}")
        self.assertEqual(len(seen), 2, "the second frame must still arrive")

    def test_a_frame_is_marked_even_when_the_handler_raises(self):
        """
        The frame proves the socket is alive regardless of whether we could
        make sense of its contents. Marking only on success would let a run
        of unparseable frames read as a dead connection.
        """
        c = self._conn(on_message=lambda _raw: (_ for _ in ()).throw(
            ValueError("bad")))
        c.last_frame_ts = 0.0
        c.dispatch("{}")
        self.assertGreater(c.last_frame_ts, 0.0)

    def test_send_on_a_closed_socket_reports_failure_rather_than_raising(self):
        c = self._conn()
        self.assertFalse(c.send('{"method":"SUBSCRIBE"}'))


class TestBookFeed(unittest.TestCase):
    """The signed order-book stream, and the mapping it has to prove."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = cfg()

    def _frame(self, market_id=8859231, ts=1717420800123,
               asks=None, bids=None):
        payload = {"msgType": "orderbook", "marketId": market_id,
                   "updateTimestampMs": ts,
                   "asks": asks if asks is not None else [["0.32", "500"],
                                                          ["0.33", "1200"]],
                   "bids": bids if bids is not None else [["0.31", "800"],
                                                          ["0.30", "2000"]]}
        return json.dumps({"type": "TOPIC",
                           "topic": f"web3_prediction_orderbook_{market_id}",
                           "data": json.dumps(payload)})

    # -- parsing ------------------------------------------------------------

    def test_the_payload_is_a_json_string_inside_the_envelope(self):
        """
        The envelope's data field is not a nested object, it is a string
        holding JSON. Parsing the envelope once and reading data["asks"]
        gets a TypeError on every frame.
        """
        parsed = self.ws_feeds.parse_book_frame(self._frame())
        self.assertIsNotNone(parsed)
        market_id, ts, asks, bids = parsed
        self.assertEqual(market_id, 8859231)
        self.assertEqual(ts, 1717420800123)
        self.assertEqual(asks[0], (0.32, 500.0))
        self.assertEqual(bids[0], (0.31, 800.0))

    def test_a_non_orderbook_frame_is_ignored(self):
        raw = json.dumps({"type": "PONG"})
        self.assertIsNone(self.ws_feeds.parse_book_frame(raw))

    def test_malformed_json_returns_none_rather_than_raising(self):
        self.assertIsNone(self.ws_feeds.parse_book_frame("not json{"))

    def test_unparseable_levels_are_dropped_not_fatal(self):
        parsed = self.ws_feeds.parse_book_frame(
            self._frame(asks=[["0.32", "500"], ["oops", "1"], ["1.5", "2"]]))
        _, _, asks, _ = parsed
        self.assertEqual(asks, [(0.32, 500.0)])

    # -- the mapping --------------------------------------------------------

    def test_the_near_side_asks_come_through_unchanged(self):
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        self.assertEqual(
            self.ws_feeds.derive_asks(asks, bids, m.Side.UP), asks)

    def test_the_far_side_asks_are_the_bids_mirrored(self):
        """
        A share paying 1 on UP and a share paying 1 on DOWN sum to 1, so a
        bid of 0.31 for UP is an offer of 0.69 for DOWN. Getting this
        backwards prices every DOWN trade at its complement, which is why
        the mapping is checked against REST before it is trusted.
        """
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        got = self.ws_feeds.derive_asks(asks, bids, m.Side.DOWN)
        self.assertEqual(got, [(0.69, 800.0), (0.70, 2000.0)])

    def test_the_far_side_is_sorted_ascending(self):
        bids = [(0.31, 800.0), (0.30, 2000.0), (0.29, 10.0)]
        got = self.ws_feeds.derive_asks([(0.32, 1.0)], bids, m.Side.DOWN)
        self.assertEqual(got, sorted(got))

    def test_deriving_twice_returns_the_original(self):
        asks = [(0.32, 500.0), (0.33, 1200.0)]
        bids = [(0.31, 800.0), (0.30, 2000.0)]
        once = self.ws_feeds.derive_asks(asks, bids, m.Side.DOWN)
        twice = self.ws_feeds.derive_asks(once, once, m.Side.DOWN)
        for (p1, s1), (p2, s2) in zip(sorted(bids), twice):
            self.assertAlmostEqual(p1, p2)
            self.assertAlmostEqual(s1, s2)

    # -- freshness and ordering --------------------------------------------

    def test_an_older_update_is_discarded(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.40", "1"]]))
        feed._conn.dispatch(self._frame(ts=1000, asks=[["0.90", "1"]]))
        self.assertEqual(feed._books[8859231].asks[0][0], 0.40)

    def test_an_equal_timestamp_is_discarded(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.40", "1"]]))
        feed._conn.dispatch(self._frame(ts=2000, asks=[["0.90", "1"]]))
        self.assertEqual(feed._books[8859231].asks[0][0], 0.40)

    def test_silence_beats_a_recent_per_market_timestamp(self):
        """
        THE trap. Every book in the cache can carry a timestamp from ten
        seconds ago and still be correct, because a quiet market sends
        nothing. Only the connection can say the feed is alive, and this
        test is what stops a future edit from "simplifying" the health check
        into a per-market one.
        """
        feed = self._feed()
        feed._conn.dispatch(self._frame(ts=int(time.time() * 1000)))
        self.assertTrue(feed.healthy)
        feed._conn.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertFalse(
            feed.healthy,
            "a silent connection is unhealthy no matter how recent the "
            "per-market timestamps look")

    def test_a_reconnect_purges_every_cached_book(self):
        """
        The venue forbids carrying a book across a reconnect and requires a
        fresh REST snapshot. A book that survived the gap is a book with an
        unknown number of missed updates in it.
        """
        feed = self._feed()
        feed._conn.dispatch(self._frame())
        self.assertIn(8859231, feed._books)
        feed._conn.note_open()
        feed.on_reconnect(feed._conn)
        self.assertEqual(feed._books, {})
        self.assertEqual(feed._validated, set())

    # -- validation ---------------------------------------------------------

    def test_a_market_is_not_served_until_it_is_validated(self):
        feed = self._feed()
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        feed._validated = set()
        feed._rejected = set()
        self.assertIsNone(feed.asks(rnd, m.Side.UP),
                          "an unvalidated market must fall through to REST")

    def test_a_matching_rest_ladder_validates_the_market(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.69, 800.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        self.assertTrue(feed.validate(rnd))
        self.assertIn(rnd.market_id, feed._validated)
        self.assertEqual(feed.asks(rnd, m.Side.UP)[0], (0.32, 500.0))

    def test_a_transposed_mapping_is_caught_and_pins_that_market_to_rest(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.31, 800.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        self.assertFalse(feed.validate(rnd))
        self.assertIn(rnd.market_id, feed._rejected)
        self.assertIsNone(feed.asks(rnd, m.Side.UP))

    def test_drift_inside_the_tolerance_still_validates(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.325, 500.0)],
                                     m.Side.DOWN: [(0.685, 800.0)]})
        feed._conn.dispatch(self._frame())
        self.assertTrue(feed.validate(self._round()))

    def test_one_rejected_market_does_not_pin_the_others(self):
        """
        Validation is per market, because the mapping could be right for one
        listing's shape and wrong for another's. A single disagreement must
        not cost every other market its stream.
        """
        feed = self._feed(rest_asks={m.Side.UP: [(0.32, 500.0)],
                                     m.Side.DOWN: [(0.69, 800.0)]})
        feed._conn.dispatch(self._frame(market_id=8859231))
        feed._conn.dispatch(self._frame(market_id=7000000))
        good = self._round()
        with feed._lock:
            feed._rejected.add(7000000)
        self.assertIsNotNone(feed.asks(good, m.Side.UP))
        self.assertIn(8859231, feed._validated)

    # -- the signed URL -----------------------------------------------------

    def test_the_socket_url_is_signed_by_the_same_code_as_every_request(self):
        """
        Binance recomputes the HMAC over the query string it RECEIVES, so
        the signed bytes and the sent bytes must be identical or it answers
        -1022. A second signing path for the socket is how that bug gets
        earned twice, so the URL has to come from _signed_query verbatim.
        """
        client = build_client(cfg())
        feed = self.ws_feeds.BookFeed(client, self.cfg)
        url = feed._url()
        base, _, query = url.partition("?")
        self.assertEqual(base, self.cfg.ws_book_url)
        self.assertIn(f"topic={self.ws_feeds.BookFeed.TOPIC}", query)
        self.assertIn("signature=", query)
        self.assertIn("random=", query)

    def test_the_socket_url_signature_verifies(self):
        """
        Recompute the HMAC over everything before the signature parameter
        and check it matches. This is the check that would have caught
        signing a sorted dict and then sending it in insertion order.
        """
        import hashlib as _hashlib
        import hmac as _hmac
        c = cfg()
        client = build_client(c)
        feed = self.ws_feeds.BookFeed(client, c)
        _, _, query = feed._url().partition("?")
        signed, _, signature = query.rpartition("&signature=")
        expected = _hmac.new(c.api_secret.encode(), signed.encode(),
                             _hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)

    def test_a_rejected_market_is_not_revalidated_every_tick(self):
        feed = self._feed(rest_asks={m.Side.UP: [(0.99, 1.0)],
                                     m.Side.DOWN: [(0.99, 1.0)]})
        feed._conn.dispatch(self._frame())
        rnd = self._round()
        feed.validate(rnd)
        calls = feed._client.calls
        feed.asks(rnd, m.Side.UP)
        feed.asks(rnd, m.Side.UP)
        self.assertEqual(feed._client.calls, calls,
                         "a rejected market must not re-probe REST on "
                         "every read")

    # -- helpers ------------------------------------------------------------

    def _round(self):
        return m.Round(
            topic_id=1, market_id=8859231, vendor="v", slug="btc-up-down",
            symbol="BTCUSDT", start_ms=0, end_ms=300_000,
            up_token_id="up", down_token_id="down", up_quote=0.5,
            down_quote=0.5, fee_bps=200, chain_id="1", collateral="USDT",
            venue_slippage_bps=100, decimal_precision=2, liquidity=1000.0,
            strike=50_000.0, feed_symbol="BTC/USD")

    def _feed(self, rest_asks=None):
        class FakeClient:
            calls = 0

            def __init__(self, table):
                self._table = table or {}

            def asks_for(self, rnd, side):
                type(self).calls += 1
                return self._table.get(side)

            def _signed_query(self, params):
                return "topic=x&signature=deadbeef"

        feed = self.ws_feeds.BookFeed(FakeClient(rest_asks), self.cfg)
        FakeClient.calls = 0
        return feed


class TestSpotFeed(unittest.TestCase):
    """Public price and kline streams."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = replace(cfg(), vol_lookback_min=10, sigma_window_min=5)

    def _trade(self, symbol="BTCUSDT", price="50123.45"):
        return json.dumps({"stream": f"{symbol.lower()}@trade",
                           "data": {"e": "trade", "s": symbol,
                                    "p": price, "T": 1717420800123}})

    def _kline(self, symbol="BTCUSDT", close="50123.45", closed=True,
               open_time=1717420800000):
        return json.dumps({"stream": f"{symbol.lower()}@kline_1m",
                           "data": {"e": "kline", "s": symbol,
                                    "k": {"t": open_time, "c": close,
                                          "x": closed}}})

    def _feed(self, seed=None):
        class FakeClient:
            def __init__(self, closes):
                self._closes = closes
                self.seed_calls = 0

            def kline_closes(self, symbol, limit):
                self.seed_calls += 1
                return list(self._closes)

            def spot_price(self, symbol=None):
                return 1.0

        return self.ws_feeds.SpotFeed(
            FakeClient(seed if seed is not None else
                       [100.0 + i for i in range(10)]), self.cfg)

    # -- parsing ------------------------------------------------------------

    def test_a_trade_frame_yields_the_last_price(self):
        kind, symbol, payload = self.ws_feeds.parse_spot_frame(self._trade())
        self.assertEqual(kind, "trade")
        self.assertEqual(symbol, "BTCUSDT")
        self.assertAlmostEqual(float(payload["p"]), 50123.45)

    def test_a_kline_frame_is_recognised(self):
        kind, symbol, _ = self.ws_feeds.parse_spot_frame(self._kline())
        self.assertEqual(kind, "kline")
        self.assertEqual(symbol, "BTCUSDT")

    def test_a_control_reply_is_ignored(self):
        self.assertIsNone(self.ws_feeds.parse_spot_frame(
            json.dumps({"result": None, "id": 1})))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self.ws_feeds.parse_spot_frame("{{"))

    # -- price --------------------------------------------------------------

    def test_a_trade_updates_the_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._trade(price="50123.45"))
        self.assertAlmostEqual(feed.price("BTCUSDT"), 50123.45)

    def test_a_symbol_never_seen_has_no_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        self.assertIsNone(feed.price("ETHUSDT"))

    def test_a_silent_connection_serves_no_price(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._trade())
        self.assertIsNotNone(feed.price("BTCUSDT"))
        feed._conn.last_frame_ts = time.time() - (self.cfg.ws_stale_s + 1.0)
        self.assertIsNone(feed.price("BTCUSDT"))

    # -- the kline window ---------------------------------------------------

    def test_the_window_is_seeded_from_rest_not_accumulated(self):
        """
        vol_lookback_min is 500 in production -- 8.3 hours of one-minute
        closes. A window built from the stream alone leaves the volatility
        estimate unusable for a third of a day after every start, which on a
        worker that redeploys on every push is most of the time.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        self.assertEqual(feed._client.seed_calls, 1)
        # The window is health-gated like every other read, so a frame has
        # to have arrived before it is served. In production the @trade
        # stream supplies one within milliseconds of the subscribe.
        feed._conn.mark_frame()
        self.assertEqual(len(feed.closes("BTCUSDT")), 10)

    def test_a_closed_candle_appends(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._kline(close="999.0", closed=True,
                                        open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT")[-1], 999.0)

    def test_an_unclosed_candle_does_not_append(self):
        """
        An in-progress candle's close is the current price, not a close. It
        would be re-appended on every tick of the same minute and turn one
        minute into a hundred samples of itself, collapsing the measured
        volatility.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        feed._conn.mark_frame()
        before = list(feed.closes("BTCUSDT"))
        feed._conn.dispatch(self._kline(close="999.0", closed=False,
                                        open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT"), before)

    def test_the_same_candle_twice_appends_once(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        feed._conn.dispatch(self._kline(close="999.0", open_time=2_000_000))
        feed._conn.dispatch(self._kline(close="999.0", open_time=2_000_000))
        self.assertEqual(feed.closes("BTCUSDT").count(999.0), 1)

    def test_the_window_is_bounded_by_the_lookback(self):
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        for n in range(20):
            feed._conn.dispatch(self._kline(close=str(1000.0 + n),
                                            open_time=2_000_000 + n * 60_000))
        self.assertEqual(len(feed.closes("BTCUSDT")),
                         self.cfg.vol_lookback_min)

    def test_a_reconnect_reseeds_rather_than_carrying_a_gap(self):
        """
        A drop of any length leaves a hole. An append-only window carries it
        forward invisibly, and every sigma computed off it is measuring a
        series with a jump in it that the market never made.
        """
        feed = self._feed(seed=[float(i) for i in range(10)])
        feed.track(["BTCUSDT"])
        self.assertEqual(feed._client.seed_calls, 1)
        feed.on_reconnect(feed._conn)
        self.assertEqual(feed._client.seed_calls, 2)

    # -- subscriptions ------------------------------------------------------

    def test_tracking_a_new_symbol_subscribes_to_both_streams(self):
        feed = self._feed()
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["ETHUSDT"])
        self.assertEqual(len(sent), 1)
        body = json.loads(sent[0])
        self.assertEqual(body["method"], "SUBSCRIBE")
        self.assertIn("ethusdt@trade", body["params"])
        self.assertIn("ethusdt@kline_1m", body["params"])

    def test_tracking_the_same_symbol_twice_does_not_resubscribe(self):
        feed = self._feed()
        feed.track(["BTCUSDT"])
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["BTCUSDT"])
        self.assertEqual(sent, [])

    def test_dropping_a_symbol_unsubscribes(self):
        feed = self._feed()
        feed.track(["BTCUSDT", "ETHUSDT"])
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.track(["BTCUSDT"])
        bodies = [json.loads(s) for s in sent]
        methods = {b["method"] for b in bodies}
        self.assertIn("UNSUBSCRIBE", methods)
        unsub = next(b for b in bodies if b["method"] == "UNSUBSCRIBE")
        self.assertIn("ethusdt@trade", unsub["params"])


class TestMarketData(unittest.TestCase):
    """The one seam the trading code sees."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds

    class FakeClient:
        def __init__(self):
            self.spot_calls = 0
            self.kline_calls = 0
            self.asks_calls = 0

        def spot_price(self, symbol=None):
            self.spot_calls += 1
            return 111.0

        def kline_closes(self, symbol, limit):
            self.kline_calls += 1
            return [1.0, 2.0, 3.0]

        def asks_for(self, rnd, side):
            self.asks_calls += 1
            return [(0.55, 10.0)]

        def _signed_query(self, params):
            return "topic=x&signature=deadbeef"

    def _md(self, enabled=True):
        client = self.FakeClient()
        md = self.ws_feeds.MarketData(client, replace(cfg(),
                                                      ws_enabled=enabled))
        return md, client

    def test_disabled_means_every_read_is_rest(self):
        """
        ws_enabled=false must reproduce the behaviour the bot had before any
        of this existed. It is the rollback path, so it has to be exact.
        """
        md, client = self._md(enabled=False)
        md.start()
        self.assertEqual(md.spot("BTCUSDT"), 111.0)
        self.assertEqual(md.closes("BTCUSDT"), [1.0, 2.0, 3.0])
        self.assertEqual(md.asks(self._round(), m.Side.UP), [(0.55, 10.0)])
        self.assertEqual(client.spot_calls, 1)
        self.assertEqual(client.asks_calls, 1)

    def test_disabled_starts_no_threads(self):
        md, _ = self._md(enabled=False)
        before = threading.active_count()
        md.start()
        self.assertEqual(threading.active_count(), before)

    def test_an_unhealthy_feed_falls_back_to_rest(self):
        md, client = self._md()
        self.assertEqual(md.spot("BTCUSDT"), 111.0)
        self.assertEqual(client.spot_calls, 1)

    def test_a_healthy_feed_is_preferred_over_rest(self):
        md, client = self._md()
        md._spot._conn.mark_frame()
        with md._spot._lock:
            md._spot._prices["BTCUSDT"] = 222.0
        self.assertEqual(md.spot("BTCUSDT"), 222.0)
        self.assertEqual(client.spot_calls, 0)

    def test_a_healthy_feed_missing_this_symbol_still_falls_back(self):
        """
        A live connection is not evidence about a symbol it has never
        carried. Serving nothing here, or raising, would both be worse than
        making the REST call the bot would have made anyway.
        """
        md, client = self._md()
        md._spot._conn.mark_frame()
        self.assertEqual(md.spot("ETHUSDT"), 111.0)
        self.assertEqual(client.spot_calls, 1)

    def test_closes_prefers_the_window_when_it_is_healthy(self):
        md, client = self._md()
        md._spot._conn.mark_frame()
        with md._spot._lock:
            md._spot._closes["BTCUSDT"] = [7.0, 8.0, 9.0]
        self.assertEqual(md.closes("BTCUSDT"), [7.0, 8.0, 9.0])
        self.assertEqual(client.kline_calls, 0)

    def test_asks_falls_back_when_the_book_feed_says_none(self):
        md, client = self._md()
        self.assertEqual(md.asks(self._round(), m.Side.UP), [(0.55, 10.0)])
        self.assertEqual(client.asks_calls, 1)

    def test_when_the_socket_is_stale_and_rest_fails_too_there_is_no_book(self):
        """
        The end of the fallback chain. Nothing invents a ladder here: asks
        returns None, _maybe_enter skips the round, and no trade is priced
        off a book nobody could fetch.
        """
        class BlindClient(self.FakeClient):
            def asks_for(self, rnd, side):
                self.asks_calls += 1
                return None

        client = BlindClient()
        md = self.ws_feeds.MarketData(client, cfg())
        self.assertIsNone(md.asks(self._round(), m.Side.UP))
        self.assertEqual(client.asks_calls, 1)

    def test_status_names_every_feed(self):
        md, _ = self._md()
        status = md.status()
        self.assertEqual(set(status), {"spot", "book", "futures"})
        for value in status.values():
            self.assertIn(value, ("live", "connecting", "disabled", "off"))

    def test_the_futures_feed_is_off_unless_something_reads_it(self):
        """Only scalp does, and a socket nobody reads is spent on nothing."""
        md, _ = self._md()
        self.assertEqual(md.status()["futures"], "off")
        self.assertIsNone(md.futures_mid("BTCUSDT"))
        self.assertIsNone(md.futures_move_bps("BTCUSDT", 1500.0))

    def test_status_says_off_when_disabled(self):
        md, _ = self._md(enabled=False)
        self.assertEqual(set(md.status().values()), {"off"})

    def _round(self):
        return m.Round(
            topic_id=1, market_id=8859231, vendor="v", slug="btc-up-down",
            symbol="BTCUSDT", start_ms=0, end_ms=300_000,
            up_token_id="up", down_token_id="down", up_quote=0.5,
            down_quote=0.5, fee_bps=200, chain_id="1", collateral="USDT",
            venue_slippage_bps=100, decimal_precision=2, liquidity=1000.0,
            strike=50_000.0, feed_symbol="BTC/USD")


class TestFuturesFeed(unittest.TestCase):
    """The one feed with no REST fallback, and why that is not a gap."""

    def _feed(self, **kw):
        c = scalp_cfg(**kw)
        return ws_feeds.FuturesFeed(client=None, cfg_source=c)

    def _live(self, feed):
        feed._conn.last_frame_ts = time.time()
        return feed

    @staticmethod
    def _frame(symbol, bid, ask):
        return json.dumps({"stream": f"{symbol.lower()}@bookTicker",
                           "data": {"s": symbol, "b": str(bid),
                                    "a": str(ask), "B": "1", "A": "1"}})

    def test_a_book_ticker_frame_parses_to_a_mid(self):
        parsed = ws_feeds.parse_futures_frame(
            self._frame("BTCUSDT", 100_000.0, 100_002.0))
        self.assertEqual(parsed, ("BTCUSDT", 100_000.0, 100_002.0))

    def test_a_control_reply_is_not_a_tick(self):
        self.assertIsNone(ws_feeds.parse_futures_frame(
            json.dumps({"result": None, "id": 1})))

    def test_a_crossed_or_empty_book_is_refused(self):
        for bid, ask in ((0.0, 100.0), (100.0, 0.0), (100.0, 99.0)):
            self.assertIsNone(ws_feeds.parse_futures_frame(
                self._frame("BTCUSDT", bid, ask)), f"{bid}/{ask}")

    def test_garbage_is_not_a_tick(self):
        for raw in ("", "not json", "[]", json.dumps({"data": "x"})):
            self.assertIsNone(ws_feeds.parse_futures_frame(raw))

    def test_an_unhealthy_socket_answers_nothing_rather_than_stale_data(self):
        feed = self._feed()
        feed._on_frame(self._frame("BTCUSDT", 100_000.0, 100_002.0))
        self.assertIsNone(feed.mid("BTCUSDT"))
        self.assertIsNone(feed.move_bps("BTCUSDT", 1500.0))
        self.assertIsNone(feed.tick_age_ms("BTCUSDT"))

    def test_the_mid_is_the_midpoint(self):
        feed = self._live(self._feed())
        feed._on_frame(self._frame("BTCUSDT", 100_000.0, 100_002.0))
        self.assertAlmostEqual(feed.mid("BTCUSDT"), 100_001.0)

    def test_a_window_that_is_not_covered_yet_is_None_not_zero(self):
        """
        "No move" and "no data" must not read alike: one is a market with
        nothing happening, the other is a feed that has just come up.
        """
        feed = self._live(self._feed())
        feed._on_frame(self._frame("BTCUSDT", 100_000.0, 100_002.0))
        self.assertIsNone(feed.move_bps("BTCUSDT", 1500.0))

    def test_a_covered_window_measures_the_move(self):
        feed = self._live(self._feed())
        with self._lock_free(feed):
            feed._ticks["BTCUSDT"] = ws_feeds.deque([
                (1_000.0, 100_000.0), (2_600.0, 100_050.0)])
        # 5 bps up over the window, and the older sample spans it.
        self.assertAlmostEqual(feed.move_bps("BTCUSDT", 1500.0), 5.0, places=6)

    def test_the_sign_is_the_direction(self):
        feed = self._live(self._feed())
        with self._lock_free(feed):
            feed._ticks["BTCUSDT"] = ws_feeds.deque([
                (1_000.0, 100_050.0), (2_600.0, 100_000.0)])
        self.assertLess(feed.move_bps("BTCUSDT", 1500.0), 0.0)

    def test_samples_older_than_the_window_are_dropped_but_one_is_kept(self):
        """
        The one behind the horizon is load-bearing: without it the oldest
        sample drifts inside the window and every measurement shortens.
        """
        feed = self._feed()
        ring = ws_feeds.deque([(0.0, 1.0), (1.0, 1.0), (2.0, 1.0),
                               (100_000.0, 1.0)])
        feed._trim(ring, 100_000.0)
        self.assertEqual(len(ring), 2)
        self.assertLess(ring[0][0], 100_000.0 - feed._window_ms())

    def test_a_reconnect_throws_the_history_away(self):
        """
        A gap is not a thing to measure across: a move spanning it describes
        a jump the perp never made in the time this thinks it did.
        """
        feed = self._live(self._feed())
        feed._on_frame(self._frame("BTCUSDT", 100_000.0, 100_002.0))
        feed._tracked = {"BTCUSDT"}
        sent = []
        feed._conn.send = lambda payload: sent.append(payload) or True
        feed.on_reconnect(feed._conn)
        self.assertEqual(feed._ticks, {})
        self.assertIn("btcusdt@bookTicker", sent[0])
        self.assertIn("SUBSCRIBE", sent[0])

    def test_dropping_a_symbol_forgets_its_history(self):
        feed = self._live(self._feed())
        feed._conn.send = lambda payload: True
        feed.track(["BTCUSDT"])
        feed._on_frame(self._frame("BTCUSDT", 100_000.0, 100_002.0))
        feed.track(["ETHUSDT"])
        self.assertNotIn("BTCUSDT", feed._ticks)

    @staticmethod
    def _lock_free(feed):
        """A no-op context manager: these tests mutate the ring directly."""
        import contextlib
        return contextlib.nullcontext()


class TestWsRecycle(unittest.TestCase):
    """The 23h handover, which has to actually happen and not merely be due."""

    def setUp(self):
        import ws_feeds
        self.ws_feeds = ws_feeds
        self.cfg = cfg()

    def _conn(self):
        closed = []

        class FakeWs:
            def close(self):
                closed.append(True)

        c = self.ws_feeds.WsConnection(
            name="test", url_factory=lambda: "wss://example.invalid/x",
            on_message=lambda _raw: None, cfg_source=self.cfg)
        c._ws = FakeWs()
        return c, closed

    def test_a_young_socket_is_left_alone(self):
        c, closed = self._conn()
        c.opened_ts = time.time()
        c.dispatch("{}")
        self.assertEqual(closed, [])

    def test_an_old_socket_is_closed_on_the_next_frame(self):
        """
        recycle_due on its own changes nothing -- run_forever blocks until
        the socket drops, so a frame is the only moment anything can act on
        it. A recycle that is computed but never performed is the 24h close
        arriving as a surprise anyway.
        """
        c, closed = self._conn()
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        c.dispatch("{}")
        self.assertEqual(closed, [True])

    def test_recycling_does_not_fire_twice_while_closing(self):
        c, closed = self._conn()
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        c.dispatch("{}")
        c.dispatch("{}")
        self.assertEqual(closed, [True])

    def test_a_frame_still_counts_when_the_socket_is_being_recycled(self):
        c, _ = self._conn()
        c.opened_ts = time.time() - (self.cfg.ws_recycle_s + 1.0)
        c.last_frame_ts = 0.0
        c.dispatch("{}")
        self.assertGreater(c.last_frame_ts, 0.0)


class TestScriptModeSharesOneSide(unittest.TestCase):
    """
    One Side class, however the file was started.

    `python btc_5m_predictor.py` runs this file as __main__, so ws_feeds' lazy
    `from btc_5m_predictor import Side` used to import a SECOND copy of the
    module and get a SECOND Side class. `side is Side.UP` was then False for
    the UP the trader passed in, and the UP ladder came back derived from the
    DOWN side of the book -- the bot pricing UP off DOWN's prices.
    """

    def test_ws_feeds_sees_the_scripts_own_side(self):
        import subprocess, os as _os
        here = ROOT
        code = (
            "import runpy, ws_feeds\n"
            "g = runpy.run_path('btc_5m_predictor.py', run_name='as_script')\n"
            "print(ws_feeds.derive_asks([(0.4, 5.0)], [(0.3, 5.0)], g['Side'].UP))\n"
        )
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
        r = subprocess.run([sys.executable, "-c", code], cwd=here, env=env,
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(r.stdout.strip(), "[(0.4, 5.0)]",
                         "ws_feeds derived the UP ladder from the DOWN side, "
                         "which means it resolved a different Side class")
