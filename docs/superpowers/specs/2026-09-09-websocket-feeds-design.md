# Persistent WebSocket feeds

Design for replacing the bot's hot REST polls with supervised, long-lived
WebSocket connections.

Status: approved, not yet implemented.

## Why

The bot holds no persistent connection to anything. Every read is a blocking
`requests` round trip, and three of them sit inside the poll loop:

| Call | Site | Frequency |
|---|---|---|
| `asks_for` -> `order_book` | `btc_5m_predictor.py:3164` | 2 x N candidate rounds per tick, plus again per scale-in |
| `spot_price` -> `/api/v3/ticker/price` | `btc_5m_predictor.py:2818` | 1 x N candidate rounds per tick |
| `sigma_annual` -> `/api/v3/klines` | `btc_5m_predictor.py:2113` | once per `vol_cache_s` |

With `poll_interval_s = 2.0` and N discovered markets, the order book alone is
2N requests every two seconds. Each carries a full TLS round trip of latency
before the model sees a number. The active profile is `lastminute`, which
enters in the closing seconds of a round -- precisely where a digital option's
probability is most sensitive to spot, and where round-trip latency is most
expensive.

All three feeds have WebSocket equivalents. Everything else the bot calls does
not, and should not: `get_quote`, `place_order`, `batch_redeem`,
`redeem_status`, `market_list`, `balance/payment-options`, `quota/limit/status`
and `/api/v3/time` are signed or mutating, and request/response is the correct
shape for a call that moves money.

## Decisions taken

Three forks were settled before this document:

1. **WebSocket is authoritative, REST is the fallback on staleness.** Not a
   shadow-mode rollout. The cut-over happens on deploy.
2. **`websocket-client`** as the protocol implementation. One package, no
   transitive dependencies, synchronous and thread-shaped like the rest of
   this codebase. Hand-rolling RFC 6455 framing was rejected as a larger
   correctness risk than one audited dependency, notwithstanding the
   Dockerfile's standing preference against adding surface.
3. **New module `ws_feeds.py`, with `coherence.py` taught to analyse both
   files as one unit.** A second module that escapes the dead-code,
   dead-config and stale-prose gates would reintroduce exactly the residue
   `coherence.py` exists to catch.

## The trap this design is built around

**An order book that is not changing sends no messages.**

A quiet market and a dead socket are indistinguishable if freshness is
measured per market. Gating on `updateTimestampMs` for each `marketId` means a
socket that died at 14:02 keeps serving 14:02 ladders to the sizing model
indefinitely, reporting itself healthy the whole time. That is a silently
mispriced order, which is the failure mode this project is built to refuse.

Freshness is therefore measured at two levels, and they are never
interchangeable:

- **Connection health** -- time since any frame arrived on that socket, plus
  pong receipt. This, and only this, decides WebSocket versus REST. The
  aggregated book topic carries every market at once, so traffic is
  effectively continuous; silence means broken.
- **Per-market `updateTimestampMs`** -- used only to discard out-of-order
  updates. Never used to decide freshness.

The same rule governs spot. A thin market with no trades is not a dead feed.
Per-symbol staleness answers only "have we ever received this symbol", never
"is the feed alive".

## Architecture

### The seam

One new class, `MarketData`, is the only thing the trading code learns about.
It exposes the three calls that are hot today, keeping today's signatures:

```python
spot(symbol) -> float
closes(symbol) -> list[float]          # feeds VolatilityEstimator
asks(rnd, side) -> list[tuple[float, float]] | None
```

Each method reads the WebSocket slot when that feed is healthy, and otherwise
makes the REST call that exists today. `assess`, `Trader._maybe_enter`,
`Trader._maybe_scale_in` and `VolatilityEstimator` change only in where they
read from, not in shape.

Two consequences, both deliberate. The diff inside the 7,000-line module stays
small. And the REST path remains live and continuously exercised rather than
becoming untested fallback code that is discovered to be broken at the moment
it is first needed.

### Components in `ws_feeds.py`

**`WsConnection`** -- one supervised socket, one daemon thread. Mirrors the
`_claim_worker_loop` pattern at `btc_5m_predictor.py:4530`: broad exception
handler, log loudly, never let the thread die silently under the trading loop.

Owns the full lifecycle: connect, `ping_interval=30` per the venue's keepalive
rule, receive, and exponential-backoff reconnect capped at
`error_backoff_max_s`. Recycles proactively at 23 hours, because the venue
closes the connection at 24.

It distinguishes **authentication failure from transport failure**. A 401 or
403 on the signed socket means the key is wrong or the egress IP is not on the
allowlist. Neither is transient, and retrying hammers a venue that rate-limits
by IP. Auth failure disables that feed permanently with a loud log and falls
back to REST. Only transport errors enter the retry loop.

This matters more here than on most deployments: a Render starter worker has
no dedicated egress address, which is the entire reason `AUTH_WAIT_S` and the
preflight knock-and-wait loop exist. The WebSocket inherits that problem and
must report it in the same legible way rather than reconnecting forever.

**`SpotFeed`** -- public combined stream at
`wss://stream.binance.com:9443/stream`. Two streams per symbol:

- `<sym>@trade` for last price. This matches `/api/v3/ticker/price` semantics
  exactly. `@bookTicker` was rejected: best bid/ask is a different quantity
  from last trade price, and substituting it would silently change what the
  model is fed.
- `<sym>@kline_1m` for the volatility window.

`Config.symbols` defaults to empty, meaning "discover and trade every 5m
up/down market". The traded symbol set is therefore not known until
`market_list` returns, so subscriptions are managed dynamically with
SUBSCRIBE and UNSUBSCRIBE control frames as markets appear and expire.

The alternative, `!miniTicker@arr`, needs no subscription management but
delivers the entire exchange -- on the order of half a megabyte per second of
JSON to parse on a 0.5-CPU worker, almost all of it for symbols the bot will
never trade. Dynamic subscription keeps cost proportional to what is actually
traded.

**Kline seeding.** `vol_lookback_min` is 500, so the volatility window is 500
one-minute closes -- 8.3 hours. Building that from the stream alone would
leave the estimator unusable for a third of a day after every start. The
window is therefore seeded from one REST `/api/v3/klines` fetch on connect and
then appended from the stream, and **re-seeded on every reconnect**, because a
drop of any length leaves a hole that an append-only window would carry
forward invisibly.

**`BookFeed`** -- signed connection to `wss://api.binance.com/sapi/wss`,
subscribed to the aggregated topic `web3_prediction_orderbook_data`, which
covers every market. One socket replaces 2N REST calls per tick.

The URL is built by reusing `PredictionClient._signed_query`
(`btc_5m_predictor.py:2687`) verbatim, with `random` and `topic` added to the
parameter dict. That helper exists because the signed bytes and the sent bytes
must be byte-identical or the venue answers -1022; writing a second signing
path for the WebSocket is how that bug gets earned twice.

Note that `topic` values are separated by `|`, which `urlencode` percent-
encodes to `%7C`. The signature is computed over the encoded string and the
encoded string is what is sent, so the two agree. This is called out because
it looks like a discrepancy and is not one.

**Per-market validation, once per connection.** The push carries market-level
`asks` and `bids` keyed on `marketId`, with no `tokenId`. `asks_for` is
per-token. Deriving two per-side ask ladders from one market-level book is
therefore an inference -- almost certainly that the opposite side's asks are
the bids mirrored as `1 - price` -- and the venue's documentation does not
state it.

So: the first push for each `marketId` is cross-checked against one REST
`order_book` fetch for both sides. If the derived ladders agree, that market's
WebSocket book is trusted from then on. If they disagree, that market stays on
REST and logs loudly.

"Agree" needs a number, because the two fetches are not simultaneous and a
live book moves between them. The check is on top-of-book price only, within
`WS_BOOK_VALIDATE_TOL = 0.02` absolute, for both sides. That is loose enough
to survive normal drift over the few hundred milliseconds between the push and
the REST reply, and far tighter than the ~`1 - 2p` error a transposed side
mapping would produce at any price away from 0.50. Depth and size are not
compared: they move faster than price and would produce false rejections.

One REST call per market, once per connection. Against the 2N-per-tick it
replaces, it is free, and it converts the side-mapping from an assumption into
a checked fact.

**`MarketData`** -- the facade. Holds a `SpotFeed`, a `BookFeed` and the
existing `PredictionClient`, and implements the three-method interface above.
Nothing outside this class knows a WebSocket exists.

### Call sites that change

- `VolatilityEstimator.__init__` takes a `MarketData` in place of its
  `requests.Session`, and `sigma_annual` reads `MarketData.closes(symbol)`
  instead of fetching `/api/v3/klines` itself. `_measure_trend` and
  `_estimate_df` are unchanged: they already work off the closes list, and
  keeping both derived from one series is the property the existing comment at
  `btc_5m_predictor.py:2147` insists on.
- `Trader._maybe_enter` and `Trader._maybe_scale_in` read
  `MarketData.spot(...)` and `MarketData.asks(...)` in place of
  `client.spot_price(...)` and `client.asks_for(...)`.
- `Trader.__init__` constructs the `MarketData` and starts the feeds.
- `PredictionClient.spot_price` and `PredictionClient.asks_for` stay exactly as
  they are. They become the fallback path rather than the primary one, and are
  still called on every REST fallback and every per-market validation.
- `--preflight` gains a WebSocket check reporting each feed as connected,
  refused or unavailable. It does not fail preflight: REST-only is a
  supported running mode, and `PREFLIGHT_REQUIRED=1` must not start refusing
  boots over an accelerator.

## Data flow, one tick

```
Trader tick
  |
  +- MarketData.spot(sym)      -> SpotFeed healthy ? cached tick : client.spot_price()
  +- MarketData.closes(sym)    -> SpotFeed healthy ? window      : REST klines
  +- MarketData.asks(rnd, side)-> BookFeed healthy AND market validated
                                    ? derived ladder : client.asks_for()
```

## Failure behaviour

| Event | Response |
|---|---|
| WebSocket will not connect at boot | Warn, run REST-only. Never blocks startup: REST-only is today's proven behaviour, and a bot that refuses to trade because an accelerator is unavailable is worse than one that trades at today's speed |
| Disconnect | Health flag false immediately, all reads fall to REST, backoff reconnect begins |
| Reconnect | Purge every cached book (the venue's documentation mandates a fresh REST snapshot and forbids offline caching), re-seed the kline window, re-run per-market validation |
| Auth rejection | Disable that feed permanently, log loudly, REST from then on. No retry |
| Malformed frame | Log, drop the frame, thread survives |
| Out-of-order `updateTimestampMs` | Discard the update, keep the newer book |
| WebSocket stale and REST also fails | Existing behaviour: `asks_for` returns `None` and the round is skipped. No trade is ever priced off a stale book |

## Configuration

Six new fields on `Config`, each read at exactly one site and each given a
profile override so `coherence.py`'s "no profile overrides it" check stays
quiet:

| Field | Default | Purpose |
|---|---|---|
| `ws_enabled` | `True` | Kill switch |
| `ws_spot_url` | `wss://stream.binance.com:9443/stream` | Overridable like `endpoints` |
| `ws_book_url` | `wss://api.binance.com/sapi/wss` | Overridable like `endpoints` |
| `ws_stale_s` | `5.0` | Connection silence beyond this marks the feed unhealthy |
| `ws_reconnect_max_s` | `30.0` | Backoff ceiling |
| `ws_recycle_s` | `82800.0` | Proactive reconnect at 23h, ahead of the venue's 24h close |

`ws_enabled` resolves through `ConfigStore`, so setting it to `false` drops a
running bot to REST-only on the next hot reload without a redeploy. That is
the rollback path.

## Testing

Entirely offline, against a fake socket. No test touches the network.

Per the project's standing constraint, these run selectively during
development (`python -m unittest test_btc_5m.TestWsFeeds` and siblings), never
as a full-suite run. The Docker build remains the gate.

Cases:

1. Envelope parsing, including that `data` is a JSON string requiring a second
   parse.
2. Side-mapping derivation from a market-level book.
3. Out-of-order `updateTimestampMs` is discarded.
4. **Connection silence marks the feed unhealthy even when every per-market
   timestamp is recent.** This is the trap above; without this test the design
   has no teeth.
5. Reconnect purges the book cache and re-seeds the kline window.
6. Signed WebSocket URL is byte-identical to what `_signed_query` produces.
7. Kline window: seed, append, and re-seed after a simulated gap.
8. A malformed frame does not kill the reader thread.
9. Auth failure disables the feed and does not retry; transport failure does
   retry.
10. `ws_enabled = false` reproduces today's behaviour exactly.
11. Per-market validation mismatch pins that market to REST without affecting
    other markets.

`fuzz.py` gains invariants over the derived ladder, checked against randomly
generated market-level books: every price strictly in (0, 1), every size
positive, asks sorted ascending, and deriving the opposite side twice returns
the original ladder. That last one is what actually pins the mirror
transformation; the first three only say the output is well-formed.

## Toolchain changes

- `requirements.txt`: add `websocket-client`.
- `Dockerfile`: add `ws_feeds.py` to the COPY list.
- `verify.sh`: add a `py_compile` line for `ws_feeds.py`.
- `coherence.py`: extend to analyse `btc_5m_predictor.py` and `ws_feeds.py` as
  one unit, so dead code, unread config fields and stale prose are still
  caught in the new module.
- `render.yaml`: no change. The build command already runs `verify.sh`.

## Known risk

Cutting over rather than shadowing means the first live round after deploy is
also the first live test of this path. Per-market validation removes the
schema half of that risk -- a wrong side-mapping pins the market to REST
rather than mispricing an order. What it does not remove is everything else
that only appears under real traffic: reconnect behaviour at 23 hours, egress
IP changes mid-session, venue-side rate limiting on the 5-messages-per-second
ceiling.

The mitigation is that reverting is one config edit, `ws_enabled: false`, hot
reloaded without a redeploy. This risk was accepted knowingly when the
cut-over path was chosen over shadow mode.

## Not in scope

- No WebSocket for signed mutating calls. There is no venue support, and
  request/response is correct for orders.
- No WebSocket for `market_list` or `market_detail`. The venue documents an
  orderbook topic only; no market-lifecycle topic exists.
- No asyncio. This codebase is threads throughout, and mixing the two models
  would be a larger change than the one being made.
