#!/usr/bin/env python3
"""
ws_feeds.py -- persistent WebSocket feeds for btc_5m_predictor.

Three reads sit inside the poll loop, and every one of them was a blocking
round trip: the prediction order book (2 x N candidate rounds per tick), spot
price (N per tick), and the kline window behind the volatility estimate. All
three have streaming equivalents. Everything signed and mutating -- quotes,
orders, redemptions -- stays on REST, where request/response is the right
shape for a call that moves money.

THE RULE THAT SHAPES EVERYTHING HERE
------------------------------------
A book that is not changing sends no messages. So a quiet market and a dead
socket are indistinguishable if freshness is measured per market: a socket
that died at 14:02 would keep serving 14:02 ladders to the sizing model,
reporting itself healthy the whole time.

Freshness is therefore measured on the CONNECTION -- time since any frame,
plus pong receipt -- and that is the only thing that decides WebSocket versus
REST. Per-market updateTimestampMs is used to discard out-of-order updates
and for nothing else.

See docs/superpowers/specs/2026-09-09-websocket-feeds-design.md.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable

import websocket

LOG = logging.getLogger("btc5m.ws")

# Handshake statuses that mean the credentials or the source address are
# wrong. 401 and 403 only: 429 and every 5xx are the venue having a moment,
# which is exactly what the backoff loop is for.
_AUTH_STATUS = re.compile(r"\b(401|403)\b")
_HANDSHAKE = re.compile(r"handshake status", re.I)


class WsAuthRefused(Exception):
    """The venue refused the credentials or the source address."""


def is_auth_refusal(exc: BaseException) -> bool:
    """
    Whether a failure means "your key is wrong", not "try again".

    websocket-client reports a rejected handshake as a generic exception
    carrying the status in its text, so the status has to be read back out.
    Narrow on purpose: a 503 in the same shape is transient, and treating it
    as an auth refusal would disable a working feed for the life of the
    process.
    """
    if isinstance(exc, WsAuthRefused):
        return True
    text = str(exc)
    return bool(_HANDSHAKE.search(text) and _AUTH_STATUS.search(text))


class WsConnection:
    """
    One supervised socket on one daemon thread.

    Deliberately ignorant of what it carries: it owns connect, keepalive,
    reconnect and recycle, and hands every frame to `on_message`. What the
    frames mean is the caller's problem, which is what lets the same class
    serve the public spot stream and the signed order-book stream.
    """

    def __init__(self, name: str,
                 url_factory: Callable[[], str],
                 on_message: Callable[[str], None],
                 cfg_source,
                 on_open: Callable[["WsConnection"], None] | None = None
                 ) -> None:
        self.name = name
        self._url_factory = url_factory
        self._on_message = on_message
        self._on_open = on_open
        # Either a Config or a ConfigStore, matching PredictionClient, so a
        # hot reload reaches the transport without rebuilding it. Which one
        # it is never needs asking: _cfg duck-types on .current.
        self._cfg_source = cfg_source
        self.last_frame_ts = 0.0
        self.opened_ts = 0.0
        self.disabled = False
        self.should_retry = True
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    # -- health -------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """
        Whether reads may trust this feed.

        Silence is the only signal, and it is measured on the connection.
        Per-topic timestamps cannot serve here: a book that is not changing
        sends nothing, so a quiet market and a dead socket look identical
        from inside a topic.
        """
        if self.disabled or self.last_frame_ts <= 0.0:
            return False
        return (time.time() - self.last_frame_ts) <= self._cfg.ws_stale_s

    @property
    def recycle_due(self) -> bool:
        """
        Whether this socket has been open long enough to hand over early.

        The venue closes at 24h. Reconnecting on our own schedule turns that
        into a handover we chose rather than a gap that arrives.
        """
        if self.opened_ts <= 0.0:
            return False
        return (time.time() - self.opened_ts) >= self._cfg.ws_recycle_s

    def backoff_for(self, attempt: int) -> float:
        return min(self._cfg.ws_reconnect_max_s, 2.0 ** min(attempt, 6))

    def mark_frame(self) -> None:
        self.last_frame_ts = time.time()

    def note_open(self) -> None:
        self.opened_ts = time.time()

    def note_failure(self, exc: BaseException) -> None:
        if is_auth_refusal(exc):
            self.disabled = True
            self.should_retry = False
            LOG.error("%s feed refused: %s. This does not heal on its own -- "
                      "the key is wrong or this worker's egress address is "
                      "not on the allowlist. Falling back to REST for the "
                      "life of the process; check the address preflight "
                      "printed at boot.", self.name, exc)
        else:
            self.should_retry = True
            LOG.warning("%s feed dropped (%s); reconnecting", self.name, exc)

    # -- frames -------------------------------------------------------------

    def dispatch(self, raw: str) -> None:
        """
        Hand one frame to the consumer, surviving whatever it does.

        The frame is marked BEFORE the handler runs, and marked regardless of
        the outcome: its arrival proves the socket is alive whether or not we
        could make sense of it. Marking only on success would let a run of
        unparseable frames read as a dead connection and send every price
        back to REST for no reason.
        """
        self.mark_frame()
        try:
            self._on_message(raw)
        except Exception:                    # noqa: BLE001
            # Same reasoning as the claim worker: a bug handling one frame
            # must not strand the feed. Loud, then carry on.
            LOG.exception("%s feed: handler failed on a frame", self.name)
        if self.recycle_due:
            # Checked here rather than on a timer thread because run_forever
            # blocks until the socket drops, so a frame is the only moment
            # this loop is reachable -- and frames arrive continuously on
            # both feeds. Closing returns control to _run_forever, which
            # reconnects through the normal path, so the handover reuses the
            # reseed and revalidate that a reconnect already performs.
            LOG.info("%s feed open %.1fh; recycling ahead of the venue's "
                     "24h close", self.name,
                     (time.time() - self.opened_ts) / 3600.0)
            self.opened_ts = 0.0             # do not ask twice while closing
            ws = self._ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:            # noqa: BLE001
                    LOG.debug("%s feed: recycle close failed", self.name)

    def send(self, payload: str) -> bool:
        """True if it went out. A closed socket is a False, never a raise."""
        ws = self._ws
        if ws is None:
            return False
        try:
            ws.send(payload)
            return True
        except Exception as exc:             # noqa: BLE001
            LOG.debug("%s feed: send failed (%s)", self.name, exc)
            return False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run_forever, name=f"ws-{self.name}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:                # noqa: BLE001
                LOG.debug("%s feed: close failed", self.name)

    def _run_forever(self) -> None:
        attempt = 0
        while not self._stopping:
            if self.disabled:
                return
            try:
                self._connect_once()
                attempt = 0
            except Exception as exc:         # noqa: BLE001
                self.note_failure(exc)
                if not self.should_retry:
                    return
                attempt += 1
            finally:
                # Never serve a price across a reconnect. The connection that
                # vouched for it is gone.
                self.last_frame_ts = 0.0
            if self._stopping or self.disabled:
                return
            time.sleep(self.backoff_for(max(attempt, 1)))

    def _connect_once(self) -> None:
        """One connection, from open to close. Returns when it drops."""
        opened = threading.Event()

        def _on_open(_ws):
            self.note_open()
            opened.set()
            if self._on_open is not None:
                try:
                    self._on_open(self)
                except Exception:            # noqa: BLE001
                    LOG.exception("%s feed: on_open failed", self.name)

        self._ws = websocket.WebSocketApp(
            self._url_factory(),
            on_open=_on_open,
            on_message=lambda _ws, raw: self.dispatch(raw),
            on_error=lambda _ws, exc: LOG.debug("%s feed error: %s",
                                                self.name, exc))
        # 30s ping is the venue's stated keepalive requirement on the signed
        # socket, and harmless on the public one.
        self._ws.run_forever(ping_interval=30, ping_timeout=10)
        if not opened.is_set():
            # run_forever returned without ever opening: the handshake was
            # refused, and its status is the only thing that says whether
            # retrying is pointless.
            raise WsAuthRefused(f"{self.name}: handshake never completed")


def _levels(raw, descending: bool = False) -> list[tuple[float, float]]:
    """
    Parse ["<price>", "<size>"] pairs, dropping whatever will not parse.

    Same posture as PredictionClient._parse_asks: a malformed level is
    dropped rather than allowed to raise, because one bad entry in a frame
    must not cost the whole book. Prices are strictly inside (0, 1) -- a
    prediction share outside that range is not a price, it is a parse error
    wearing one.

    `descending` exists so bids keep the venue's own ordering, best first.
    Sorting both sides ascending would parse correctly and still mislead
    every later reader about which end of a bid ladder is the top of book.
    """
    out: list[tuple[float, float]] = []
    for lvl in raw or ():
        try:
            price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError, OverflowError, KeyError):
            continue
        if not (math.isfinite(price) and math.isfinite(size)):
            continue
        if 0.0 < price < 1.0 and size > 0:
            out.append((price, size))
    return sorted(out, reverse=descending)


def parse_book_frame(raw: str) -> tuple[int, int, list, list] | None:
    """
    (market_id, update_ts_ms, asks, bids) from one push, or None.

    The envelope's data member is a JSON STRING, not a nested object, so it
    needs a second parse. Reading it as an object gets a TypeError on every
    single frame, which is a whole feed lost to one wrong assumption.
    """
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get("type") != "TOPIC":
            return None
        body = json.loads(envelope.get("data") or "null")
        if not isinstance(body, dict) or body.get("msgType") != "orderbook":
            return None
        market_id = int(body["marketId"])
        ts = int(body["updateTimestampMs"])
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return (market_id, ts,
            _levels(body.get("asks")),
            _levels(body.get("bids"), descending=True))


def derive_asks(asks: list[tuple[float, float]],
                bids: list[tuple[float, float]],
                side) -> list[tuple[float, float]]:
    """
    Per-side ask ladder from one market-level book.

    The push carries one book keyed on marketId with no tokenId, while
    asks_for is per-token, so one of the two sides has to be derived. UP and
    DOWN shares each pay 1 and are mutually exclusive, so their prices sum
    to 1: a bid of 0.31 for UP is an offer of 0.69 for DOWN.

    This is an inference. The venue documents the frame but not which token
    the book belongs to, which is exactly why BookFeed.validate checks it
    against REST once per market before any of it is allowed to price a
    trade.
    """
    from btc_5m_predictor import Side
    if side is Side.UP:
        return sorted(asks)
    return sorted((round(1.0 - price, 10), size) for price, size in bids)


class _Book:
    """One market's last known ladder, with the stamp that orders updates."""

    __slots__ = ("asks", "bids", "ts")

    def __init__(self, asks, bids, ts) -> None:
        self.asks = asks
        self.bids = bids
        self.ts = ts


class BookFeed:
    """
    The prediction order book, on one socket for every market.

    Subscribes to the aggregated topic rather than one per market: N markets
    on one connection instead of N connections, and it means a newly listed
    round is already covered before the bot has discovered it.
    """

    TOPIC = "web3_prediction_orderbook_data"

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._books: dict[int, _Book] = {}
        self._validated: set[int] = set()
        self._rejected: set[int] = set()
        self._lock = threading.Lock()
        self._conn = WsConnection(
            name="book", url_factory=self._url, on_message=self._on_frame,
            cfg_source=cfg_source, on_open=self.on_reconnect)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def _url(self) -> str:
        """
        Signed socket URL, signed by the same code that signs every request.

        Reusing _signed_query is not tidiness. It exists because the signed
        bytes and the sent bytes have to be identical or the venue answers
        -1022, and a second signing path is how that bug gets earned twice.
        The pipe between topics percent-encodes to %7C; the signature is
        computed over the encoded string and the encoded string is what is
        sent, so the two agree.
        """
        query = self._client._signed_query({
            "random": f"{time.time_ns():x}", "topic": self.TOPIC})
        return f"{self._cfg.ws_url('book')}?{query}"

    def start(self) -> None:
        self._conn.start()

    def stop(self) -> None:
        self._conn.stop()

    @property
    def healthy(self) -> bool:
        return self._conn.healthy

    def on_reconnect(self, _conn) -> None:
        """
        Throw away everything the previous connection knew.

        The venue forbids caching a book across a reconnect and requires a
        fresh REST snapshot, and it is right to: a book that survived the gap
        carries an unknown number of missed updates and looks exactly like a
        book that did not. Validation goes with it, because the socket that
        proved the mapping is not this socket.
        """
        with self._lock:
            self._books.clear()
            self._validated.clear()
            self._rejected.clear()

    def _on_frame(self, raw: str) -> None:
        parsed = parse_book_frame(raw)
        if parsed is None:
            return
        market_id, ts, asks, bids = parsed
        with self._lock:
            known = self._books.get(market_id)
            # Strictly newer. The venue's delivery is at-most-once and out of
            # order, and an equal stamp is a duplicate, not an update.
            if known is not None and ts <= known.ts:
                return
            self._books[market_id] = _Book(asks, bids, ts)

    def validate(self, rnd) -> bool:
        """
        Prove the side mapping for one market against REST, once.

        Costs one REST call per market per connection, against the 2N per
        tick it replaces. Top of book only, and price only: depth and size
        move faster than the round trip and would reject a correct mapping.
        """
        from btc_5m_predictor import LOG as _LOG, Side, WS_BOOK_VALIDATE_TOL
        with self._lock:
            book = self._books.get(rnd.market_id)
            if book is None:
                return False
        for side in Side:
            rest = self._client.asks_for(rnd, side)
            derived = derive_asks(book.asks, book.bids, side)
            if not rest or not derived:
                return False
            if abs(rest[0][0] - derived[0][0]) > WS_BOOK_VALIDATE_TOL:
                with self._lock:
                    self._rejected.add(rnd.market_id)
                _LOG.error(
                    "Order-book stream disagrees with REST on %s %s: stream "
                    "%.4f, REST %.4f. The market-level book is not mapping "
                    "to per-token ladders the way this code assumes, so %s "
                    "stays on REST.", rnd.slug, side.name,
                    derived[0][0], rest[0][0], rnd.slug)
                return False
        with self._lock:
            self._validated.add(rnd.market_id)
        return True

    def asks(self, rnd, side) -> list[tuple[float, float]] | None:
        """
        The ask ladder, or None to say "ask REST".

        None covers three different situations on purpose -- unhealthy
        connection, unvalidated market, rejected market -- because the caller
        does the same thing in all three, and distinguishing them here would
        only invite a caller to treat one of them as fatal.
        """
        if not self._conn.healthy:
            return None
        with self._lock:
            if rnd.market_id in self._rejected:
                return None
            validated = rnd.market_id in self._validated
            book = self._books.get(rnd.market_id)
        if book is None:
            return None
        if not validated and not self.validate(rnd):
            return None
        return derive_asks(book.asks, book.bids, side) or None


def parse_spot_frame(raw: str) -> tuple[str, str, dict] | None:
    """
    (kind, symbol, payload) from one combined-stream frame, or None.

    Combined streams wrap everything as {"stream": ..., "data": ...}, and
    control replies to SUBSCRIBE arrive on the same socket as
    {"result": ..., "id": ...} with no stream at all.
    """
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            return None
        data = envelope.get("data")
        if not isinstance(data, dict):
            return None
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return None
        event = data.get("e")
    except ValueError:
        return None
    if event == "trade":
        return "trade", symbol, data
    if event == "kline":
        return "kline", symbol, data
    return None


class SpotFeed:
    """
    Public last price and the 1m close window, over one combined stream.

    Subscriptions are dynamic because Config.symbols defaults to empty,
    meaning "discover and trade every 5m market" -- the traded set is not
    known until market_list answers. The alternative, an all-market ticker
    stream, needs no subscription management but delivers the whole
    exchange, which is most of a megabyte a second of JSON on a half-CPU
    worker, nearly all of it for symbols this bot will never trade.
    """

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._prices: dict[str, float] = {}
        self._closes: dict[str, list[float]] = {}
        self._last_candle: dict[str, int] = {}
        self._tracked: set[str] = set()
        self._lock = threading.Lock()
        self._conn = WsConnection(
            name="spot", url_factory=self._url, on_message=self._on_frame,
            cfg_source=cfg_source, on_open=self.on_reconnect)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def _url(self) -> str:
        return self._cfg.ws_url("spot")

    def start(self) -> None:
        self._conn.start()

    def stop(self) -> None:
        self._conn.stop()

    @property
    def healthy(self) -> bool:
        return self._conn.healthy

    @staticmethod
    def _streams(symbol: str) -> list[str]:
        low = symbol.lower()
        # @trade, not @bookTicker: ticker/price is the last traded price, and
        # best bid/ask is a different quantity. Substituting it would change
        # what the model is fed without changing any line that reads it.
        return [f"{low}@trade", f"{low}@kline_1m"]

    def track(self, symbols) -> None:
        """Bring the subscription set in line with what is being traded."""
        wanted = {s.upper() for s in symbols if s}
        with self._lock:
            added = wanted - self._tracked
            dropped = self._tracked - wanted
            self._tracked = set(wanted)
        for symbol in sorted(added):
            self._seed(symbol)
        if added:
            self._control("SUBSCRIBE", added)
        if dropped:
            self._control("UNSUBSCRIBE", dropped)
            with self._lock:
                for symbol in dropped:
                    self._prices.pop(symbol, None)
                    self._closes.pop(symbol, None)
                    self._last_candle.pop(symbol, None)

    def _control(self, method: str, symbols) -> None:
        params = [s for symbol in sorted(symbols)
                  for s in self._streams(symbol)]
        self._conn.send(json.dumps({"method": method, "params": params,
                                    "id": int(time.time() * 1000) % 1_000_000}))

    def _seed(self, symbol: str) -> None:
        """
        Fill the close window from REST before the stream contributes.

        vol_lookback_min is 500 -- 8.3 hours of one-minute closes. Waiting
        for the stream to supply that would leave the volatility estimate
        unusable for a third of a day after every start, on a worker that
        restarts on every push.
        """
        try:
            closes = self._client.kline_closes(
                symbol, self._cfg.vol_lookback_min)
        except Exception as exc:             # noqa: BLE001
            # Not fatal: closes() returns None and the estimator falls back
            # to its own REST fetch, which is what it did before this feed
            # existed.
            LOG.warning("Could not seed the kline window for %s (%s); "
                        "volatility stays on REST until the next reconnect",
                        symbol, exc)
            return
        with self._lock:
            self._closes[symbol] = list(closes)[-self._cfg.vol_lookback_min:]
            self._last_candle[symbol] = 0

    def on_reconnect(self, _conn) -> None:
        """
        Resubscribe and re-seed. A gap is not a thing to append across.

        A drop of any length leaves a hole in the close window, and an
        append-only window carries it forward invisibly -- every sigma
        computed off it then describes a series containing a jump the market
        never made.
        """
        with self._lock:
            tracked = sorted(self._tracked)
        for symbol in tracked:
            self._seed(symbol)
        if tracked:
            self._control("SUBSCRIBE", tracked)

    def _on_frame(self, raw: str) -> None:
        parsed = parse_spot_frame(raw)
        if parsed is None:
            return
        kind, symbol, data = parsed
        if kind == "trade":
            try:
                price = float(data["p"])
            except (KeyError, TypeError, ValueError):
                return
            if price > 0:
                with self._lock:
                    self._prices[symbol] = price
            return
        candle = data.get("k") or {}
        try:
            # An in-progress candle's close is the current price, not a
            # close. Appending it would re-append the same minute on every
            # tick and collapse the measured volatility toward zero.
            if not candle.get("x"):
                return
            open_time = int(candle["t"])
            close = float(candle["c"])
        except (KeyError, TypeError, ValueError):
            return
        if close <= 0:
            return
        with self._lock:
            if self._last_candle.get(symbol) == open_time:
                return
            window = self._closes.setdefault(symbol, [])
            window.append(close)
            del window[:-self._cfg.vol_lookback_min]
            self._last_candle[symbol] = open_time

    def price(self, symbol: str) -> float | None:
        if not self._conn.healthy:
            return None
        with self._lock:
            return self._prices.get(symbol.upper())

    def closes(self, symbol: str) -> list[float] | None:
        if not self._conn.healthy:
            return None
        with self._lock:
            window = self._closes.get(symbol.upper())
            return list(window) if window else None


class MarketData:
    """
    The only thing the trading code knows about any of this.

    Three methods, matching the three REST calls that used to sit in the
    poll loop, with the same signatures. Each prefers its socket and falls
    back to the call that was there before, which is what keeps the REST
    path continuously exercised instead of turning it into untested code
    that gets discovered broken at the moment it is first needed.
    """

    def __init__(self, client, cfg_source) -> None:
        self._client = client
        self._cfg_source = cfg_source
        self._spot = SpotFeed(client, cfg_source)
        self._book = BookFeed(client, cfg_source)

    @property
    def _cfg(self):
        current = getattr(self._cfg_source, "current", None)
        return self._cfg_source if current is None else current

    def start(self) -> None:
        """
        Bring the feeds up, if they are wanted.

        A failure here is never fatal. REST-only is exactly what this bot did
        before the sockets existed, and refusing to trade because an
        accelerator is unavailable is worse than trading at the old speed.
        """
        if not self._cfg.ws_enabled:
            LOG.info("WebSocket feeds are off; every read goes to REST")
            return
        self._spot.start()
        self._book.start()

    def stop(self) -> None:
        self._spot.stop()
        self._book.stop()

    def track(self, symbols) -> None:
        if self._cfg.ws_enabled:
            self._spot.track(symbols)

    def spot(self, symbol: str) -> float:
        if self._cfg.ws_enabled:
            price = self._spot.price(symbol)
            if price is not None:
                return price
        return self._client.spot_price(symbol)

    def closes(self, symbol: str) -> list[float]:
        if self._cfg.ws_enabled:
            window = self._spot.closes(symbol)
            if window:
                return window
        return self._client.kline_closes(symbol, self._cfg.vol_lookback_min)

    def asks(self, rnd, side) -> list[tuple[float, float]] | None:
        if self._cfg.ws_enabled:
            levels = self._book.asks(rnd, side)
            if levels:
                return levels
        return self._client.asks_for(rnd, side)

    def status(self) -> dict[str, str]:
        """One word per feed, for preflight and the log line at startup."""
        if not self._cfg.ws_enabled:
            return {"spot": "off", "book": "off"}
        out = {}
        for name, feed in (("spot", self._spot), ("book", self._book)):
            if feed._conn.disabled:
                out[name] = "disabled"
            elif feed.healthy:
                out[name] = "live"
            else:
                out[name] = "connecting"
        return out
