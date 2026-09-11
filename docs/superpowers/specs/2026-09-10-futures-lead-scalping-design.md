# Futures-lead scalping: the `scalp` profile

Design for replacing the example `maker` profile with a real strategy: watch
the perpetual futures book, buy the prediction side the perp is moving toward,
bracket it at +/-5% of stake, and repeat until the last minute of the round.

Status: approved, not yet implemented.

## Why

`maker` was added so that `LIMIT` entry had a configuration that could be run,
not because anyone believed in it. Its own comment says so: "whether this
actually earns more than it misses is an open question". It is a code path
wearing the costume of a strategy, and it has never been the thing anybody
wanted to trade.

What is wanted is this. Spot follows the perpetual future with a lag measured
in milliseconds. The prediction market resolves on spot. So the perp is a
short-horizon forecast of the quantity the contract settles against, and while
the prediction book has not repriced to a perp move, the side the move points
at is underpriced.

That edge is small and it decays in about a second, so it is not a bet to hold
to settlement. It is a bet to take and give back: buy, offer the shares out
5% higher, and cut at 5% lower if the move was wrong. Many small round trips
inside one round, rather than one all-or-nothing position per round.

Every other profile in this file asks "which side wins this round?". This one
never asks that. It asks "which way is this token's PRICE about to move?" --
a different question with a different horizon, and the first strategy here
whose P&L does not depend on the oracle at all.

## Ground truth

| Fact | Source | Consequence |
|---|---|---|
| Binance USD-M perps stream on `wss://fstream.binance.com/stream` | Binance futures WS docs | a second socket, not a second subscription on the spot one |
| `<sym>@bookTicker` pushes best bid/ask on every change | same | the fastest mid available without a depth stream |
| The perp and the spot ticker share a symbol string (`BTCUSDT`) | same | `market_symbol()` already resolves the only mapping needed |
| `timeInForce` is `FOK` for `MARKET`, `GTC` for `LIMIT`. There is no IOC, and no stop or conditional order type | `@binance/w3w-prediction@2.1.2` | **a stop-loss cannot be a resting order** -- see below |
| `feeRateBps` is published per market; 200 is only this bot's fallback | `Round.fee_bps` | the bracket's economics are per market, not global |
| `QueryOrderBookResponse` carries `bids`, and `MarketData.bids()` exposes them | `ws_feeds` | the stop trigger has a live bid to watch |

## The trap this design is built around

**A limit order cannot be a stop-loss.**

The instruction is "2 limit orders that let us either get 5% of the stake or
cap our loss at 5% of the stake". Only one of those two can be a resting
order. A `SELL` limit priced *below* the best bid is marketable: it crosses
and fills at once, at the bid, for whatever the book holds. Posting the stop
leg as a limit order does not arm a stop -- it sells the position immediately,
at a loss, every single time.

So the bracket is asymmetric, and has to be:

* **Take-profit** is a genuine resting `LIMIT` `SELL` above the market. The
  venue holds it and it earns the spread when it fills.
* **Stop** is a local trigger. Each pass reads the best bid; when the bid
  falls to the stop price the take-profit is cancelled and the remainder is
  sold `MARKET`. Nothing rests on the venue for this leg.

"Once one of the orders gets executed, we cancel the other one" therefore
means: when the take-profit fills, the stop is simply disarmed (it was never
an order); when the stop triggers, the take-profit is cancelled for real --
and, because a cancel races a fill, its state is **re-read afterwards and
whatever came back is booked**, exactly as `_reap_pending` already does.

This is not a compromise forced by laziness. `exit_trigger: POLLED` exists in
this codebase precisely because the venue has no conditional orders, and the
scalp bracket is that mechanism applied to a price the position sets rather
than one the model computes.

## The arithmetic, stated before anything is built

A position of stake `S` filled at price `p` holds `n = S/p` shares. Selling
`n` shares at price `q` nets `n*q*(1-f)`. So

```
pnl / S  =  q(1-f)/p - 1
```

and the two bracket prices invert exactly:

```
tp   = p*(1 + r_tp) / (1 - f)
stop = p*(1 - r_sl) / (1 - f)
```

**The fee is paid on the way out of both legs, so it shifts both prices up.**
At the 200 bps fallback and `r_tp = r_sl = 0.05`:

| | price move needed | |
|---|---|---|
| +5% of stake | **+7.14%** | `1.05/0.98 - 1` |
| -5% of stake | **-3.06%** | `1 - 0.95/0.98` |

The target is more than twice as far away as the stop. On a driftless walk the
chance of touching the target first is `d/(u+d)` = **30%**, and 5% won three
times in ten against 5% lost seven times in ten is **-2% of stake per round
trip**. The strategy is not "more right than wrong"; at a 2% fee it is "right
about 70% of the time or it bleeds".

At `feeRateBps = 0` the same bracket is `+5%` / `-5%` in price, `d/(u+d)` is
0.50, and "more right than wrong" is exactly the bar. **The whole profile
lives or dies on the market's published fee**, so that is made a gate rather
than a footnote:

```
u = (1 + r_tp)/(1 - f) - 1          # price rise the target needs
d = 1 - (1 - r_sl)/(1 - f)          # price fall the stop allows
edge_required = r_sl/(r_tp + r_sl)  -  d/(u + d)
```

`edge_required` is how far above a no-information coin flip the futures signal
has to be for this bracket to break even, in hit-rate points. It is 0.00 at a
zero fee and about 0.20 at 200 bps. A market whose fee pushes it above
`scalp_max_edge_required` is refused, loudly, with the number printed -- the
bot does not trade a bracket whose own arithmetic it cannot defend.

`d <= 0` is the degenerate case: the fee alone exceeds the stop distance, so
the stop price sits at or above the entry and the position is stopped out on
the tick it opens. Refused unconditionally, not by threshold.

## The signal

Two conditions, both required, both measured on the perp feed:

1. **The perp has moved.** Mid return over `scalp_lookback_ms` is at least
   `scalp_min_move_bps` in magnitude. Its sign is the direction.
2. **Spot has not caught up.** The perp/spot basis, in bps, has moved *away
   from its own recent mean* by at least `scalp_min_basis_bps`, in the same
   direction.

The second is the premise stated as a testable condition, and it is measured
as a dislocation rather than a level because the basis has a persistent
non-zero mean -- funding, not information. An absolute basis threshold would
fire constantly on one side and never on the other, which is a funding-rate
detector wearing a lead-lag costume.

`UP` when the direction is positive, `DOWN` when negative. There is no model
probability anywhere in this path, and none is invented: the journal records
the market's own implied probability, as `lastminute` already does, so that
the row says what was paid rather than a forecast nobody made.

**No REST fallback.** Every other feed in `ws_feeds` falls back to REST, so
the REST path stays exercised. This one does not, and the difference is
deliberate: a lead measured in milliseconds, sampled by a REST call inside a
poll loop, is not a degraded signal but a different and imaginary one. When
the futures socket is unhealthy, or its newest tick is older than
`scalp_max_tick_age_ms`, **the profile does not trade** and says so.

## Repeating inside one round

Every existing strategy enters a round once: `_maybe_enter_model` writes
`self._seen[topic_id]` and never looks again. This one must enter, exit, and
enter again, so it keeps its own per-round counter and never writes `_seen`.

Bounded three ways, because an unbounded re-entry loop is a way to pay the fee
twenty times in a minute:

* `scalp_max_entries_per_round` -- a hard ceiling per round.
* `scalp_cooldown_s` -- minimum seconds between entries on one symbol, so one
  signal that persists for three passes does not open three positions.
* A new entry requires the symbol to be **flat**: no position, and no pending
  order on that round. Stacking would blur two brackets into one and the stop
  would then be computed off a blended price that neither entry chose.

## The window, and why two numbers

"The first 4 minutes of the round, stop in the last minute" is two boundaries,
not one, because a scalp opened at the boundary would have to be closed on the
other side of it:

* `entry_window_end_s = 75` -- the last moment a scalp may be **opened**.
* `scalp_flatten_s = 60` -- the moment every scalp order is cancelled and
  every scalp position is sold at market.

Fifteen seconds between them is the runway a fresh bracket gets. Config
rejects `entry_window_end_s <= scalp_flatten_s`, which would open positions
into a flatten that fires the same instant.

**Nothing is placed inside the last minute.** That is the whole point of the
rule -- the book is thin there and orders fail -- so the flatten happens at
the 60s mark, not after it.

If the flatten fails (a thin book, a rejected order, an unreachable venue) the
position is **left to settle** through the ordinary `_settle_open` path and
the failure is logged at ERROR. Retrying into a book that is not there is how
a 5% loss becomes a 100% one; letting the oracle decide is the smaller of the
two bad outcomes, and it is the one the rest of this bot already knows how to
finish.

## Risk accounting: a gap this profile cannot tolerate

`RiskManager.record_result` is called from `_settle_one`, and a **sold**
position never reaches `_settle_one` -- it leaves through `_book_sale`. So a
profile that closes every position by selling currently reports *no* results
to the risk manager: `daily_loss_limit_pct` sees nothing, the consecutive-loss
counter never moves, and the breaker that is supposed to stop a bad day is
wired to a path this profile never takes.

That is survivable for `maker`, which sells rarely. It is not survivable for a
profile that sells twenty times a round. `_book_sale` therefore reports every
closed sale to the risk manager, with the realised P&L and `model_prob=None`
so the calibration statistics -- which answer "did the price predict the
outcome", a question a sold position has no answer to -- stay untouched.

`record_result` also increments `rounds_today`, which for this profile counts
round trips rather than rounds. `max_rounds_per_day` is set accordingly.

## Configuration

Every field is new, and every one is overridden by the `scalp` profile so no
default silently governs (`coherence.py` checks exactly this).

| Field | Default | Meaning |
|---|---|---|
| `scalp` | `False` | selects this entry strategy, like `straddle` and `last_minute` |
| `scalp_stake_pct` | `0.05` | stake per round trip, as a fraction of bankroll |
| `scalp_take_profit_pct` | `0.05` | net gain per unit staked that closes a winner |
| `scalp_stop_loss_pct` | `0.05` | net loss per unit staked that closes a loser |
| `scalp_lookback_ms` | `1500` | window the perp move is measured over |
| `scalp_min_move_bps` | `2.0` | how far the perp must have moved in it |
| `scalp_min_basis_bps` | `0.5` | how far the basis must have dislocated. `0` disables the second condition |
| `scalp_max_tick_age_ms` | `2000` | a perp tick older than this is not a signal |
| `scalp_cooldown_s` | `2.0` | minimum gap between entries on one symbol |
| `scalp_max_entries_per_round` | `20` | hard ceiling per round |
| `scalp_flatten_s` | `60.0` | cancel and sell everything at this many seconds left |
| `scalp_max_edge_required` | `0.25` | refuse a market whose fee demands more signal than this |
| `ws_futures_url` | `wss://fstream.binance.com/stream` | the perp stream |

`scalp` is mutually exclusive with `straddle`, `last_minute` and `scale_in`,
for the reasons those three are already mutually exclusive with each other:
one dispatch, and nothing for a top-up to aim at.

## Components

**`ws_feeds.FuturesFeed`** -- mirrors `SpotFeed`: one supervised
`WsConnection`, dynamic `SUBSCRIBE`/`UNSUBSCRIBE` following the traded set, a
per-symbol ring of `(ts_ms, mid)` samples bounded by `scalp_lookback_ms`.
Exposes `mid(symbol)` and `move_bps(symbol, lookback_ms)`. `MarketData` gains
`futures_mid`, `futures_move_bps` and a `futures` entry in `status()`.

**`btc_5m_predictor`**

| Addition | Purpose |
|---|---|
| `bracket_prices(fill_price, fee_bps, tp_pct, sl_pct)` | the two prices, derived from the arithmetic above. One place, so they cannot disagree |
| `signal_edge_required(fee_bps, tp_pct, sl_pct)` | the gate number, computed not written |
| `Trader._maybe_enter_scalp` | the entry strategy, dispatched from `_maybe_enter` |
| `Trader._arm_bracket` | posts the resting take-profit after a fill |
| `Trader._check_stops` | the polled stop leg, run each pass before entry |
| `Trader._flatten_scalps` | the 60-second deadline |
| `Trader._scalp_entries: dict[int, int]` | entries per round, pruned with `_seen` |
| `Trader._scalp_last_entry: dict[str, float]` | cooldown, per symbol |

`_book_sale` gains the `record_result` call described above. Nothing else in
the existing exit path changes: a scalp closes through the same
`resolve_sold` / `settle_source='sold'` route that `maker` already used, so
`diagnose()` keeps these rows out of the calibration buckets for free.

## What is deliberately not built

* **Latency.** The signal is read inside a `poll_interval_s` loop. At the 1.0s
  the profile sets, a millisecond lead is long gone, and what is actually
  being traded is a one-to-two-second momentum signal. That is stated here
  rather than implied by the word "futures", and the profile is honest about
  being the shape of the strategy rather than the speed of it. Making it fast
  is separate work.
* **Repricing a resting take-profit.** Posted once at the price the fill
  implies, and left alone. Chasing it is the next thing to try, not this one.
* **Hedging with the opposite token** instead of selling the one held.
* **Sizing on signal strength.** Flat stake. There is no model, so there is
  nothing to size against, and a multiplier on a bps number would be a
  parameter fitted to nothing.

## Testing

No network, ever.

| Class | Covers |
|---|---|
| `TestBracketArithmetic` | the two prices round-trip to exactly +/-5% of stake; the fee shifts both up; `d <= 0` is refused |
| `TestSignalEdgeRequired` | 0.00 at a zero fee, about 0.20 at 200 bps, monotone in the fee |
| `TestScalpConfig` | the new fields, the mutual exclusions, the window ordering |
| `TestScalpProfile` | the profile's declared values, and that no other profile gained an exit |
| `TestFuturesFeed` | frame parsing, the sample ring, staleness, resubscribe on reconnect |
| `TestScalpSignal` | both conditions required; a stale tick is not a signal; an unhealthy socket refuses to trade |
| `TestScalpEntry` | entry, bracket armed at the right prices, cooldown, per-round ceiling, flat-only re-entry |
| `TestScalpStops` | the stop cancels the take-profit and re-reads it; a cancel that raced a fill is booked |
| `TestScalpFlatten` | everything is closed at 60s; a failed flatten leaves the position to settle and says so |
| `TestSoldPositionsReachTheRiskManager` | the accounting gap above, closed |
