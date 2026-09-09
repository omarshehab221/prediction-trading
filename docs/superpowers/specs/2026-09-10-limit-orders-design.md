# Limit orders alongside market orders

Design for adding `LIMIT` execution -- on both sides of the book -- to a bot
that has only ever sent `MARKET` buys.

Status: approved, not yet implemented.

## Why

Every order the bot has ever sent is a `MARKET` `FOK` buy that crosses the
spread. That has three costs, and only the first is obvious:

1. **It pays the spread on every entry.** `assumed_spread_pct` exists precisely
   because the bot knows it is a taker; the number is a haircut applied to a
   cost it never tries to avoid.
2. **It cannot trade small.** The venue's `MARKET` minimum is about 1.5 USDT.
   The connector states plainly that this floor **does not apply to `LIMIT`
   orders**, so a whole range of stakes is unreachable today for no reason
   other than order type.
3. **It cannot leave.** A position is held to settlement and redeemed. If the
   market moves to overpay for a share the bot holds, there is no way to take
   that money -- the only exit is the oracle.

`LIMIT` fixes all three. It also introduces the one thing this codebase has
never had to reason about: an order that is neither filled nor dead.

## Ground truth

Taken from `@binance/w3w-prediction@2.1.2`, the same connector `conformance.py`
checks against. Not from memory, and not from the doc pages that 404.

| Fact | Consequence |
|---|---|
| `orderType` is `MARKET \| LIMIT` on both `get-quote` and `place-order-bundle` | the two-phase flow is unchanged; only the parameters differ |
| `timeInForce` must be `FOK` for `MARKET` and **`GTC` for `LIMIT`** | there is no IOC. A limit order **rests**. It cannot be used as a fill-or-kill price guard |
| `priceLimit` is required when `orderType=LIMIT`, must be `> 0` | its doc comment says nothing about wei, unlike `amountIn` which says so explicitly. It is a plain decimal |
| `GET /trade/../order/list` returns active orders | the bot can ask what is still resting |
| `POST /trade/batch-cancel` takes `cancelInfoList`, returns `canceled` / `failed` | the bot can retract a resting order |
| `side` is `BUY \| SELL` on `get-quote` | selling is the same endpoint, not a new one |
| `QueryOrderBookResponse` carries `bids` in the same `{price,size}` shape as `asks` | `ws_feeds._Book` already stores bids; only `asks()` is exposed |

## Decisions taken

Settled before this document:

1. **Per profile, and mixable.** A profile selects its entry order type and its
   exit order type independently. Every existing profile stays on `MARKET`
   entry with no exit, which is exactly today's behaviour, stated rather than
   implied.
2. **Both sides, one execution layer.** `BUY` and `SELL` are built together.
   Adding `SELL` later would mean reopening every entry path a second time.
3. **The limit price is computed, never configured.** It is derived per round
   from that round's model probability and that market's own fee, by inverting
   the gates the bot already applies. There is no `limit_buy_price` setting.
4. **No repricing.** An order is posted once and left alone. No cancel-and-
   repost as the book moves.
5. **Cancellation has exactly three triggers**: the entry window that authorised
   the order closing, round end, and shutdown or halt. There is no
   configurable deadline.

## The trap this design is built around

**A cancel races a fill, and both can win.**

The bot decides to retract a resting order at the same moment the venue matches
it. `batch-cancel` returns that order under `failed`, and the naive reading of
`failed` is "the cancel did not work, the order is still out there". The truth
is usually the opposite: the cancel failed *because the order had already
filled*. A bot that reads `failed` as "still resting" walks away from a real
position -- which then settles, wins, and is never claimed, because nothing in
the journal knows it exists.

So `failed` is never interpreted. After every cancel the reaper **re-reads the
order's state** and books whatever came back. The cancel is a request, not an
answer; the order's state is the answer. This is the same discipline
`confirm_fill` already applies to `place_order` returning an id that proves
nothing.

The mirror-image trap is on the fill side. `min_fill_fraction` treats a fill
below 90% of the request as a failure, which is correct for `FOK` -- where a
partial fill means something went wrong -- and catastrophic for `GTC`, where a
partial fill is the ordinary outcome. **A partial fill cannot be refused: the
shares are already ours.** `min_fill_fraction` therefore stays the `FOK` guard
it was written as and is not consulted for limit orders. Any non-zero fill is
recorded, at its real size and its real price.

## Architecture

### The seam

One value object describes an order completely, and one method executes it.
Everything that decides *what* to trade produces an `OrderPlan`; everything
that talks to the venue consumes one.

```python
class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"

    @property
    def time_in_force(self) -> str:
        """FOK for MARKET, GTC for LIMIT -- the venue's rule, derived once."""
        return "FOK" if self is OrderType.MARKET else "GTC"


@dataclass(frozen=True)
class OrderPlan:
    side: Side                       # which outcome token
    action: Action
    order_type: OrderType
    amount: float                    # BUY: USDT in.  SELL: shares in.
    price_limit: float | None = None
```

`time_in_force` is derived rather than passed. Today `"FOK"` and `"MARKET"` are
two separate string literals in `place_order`, one edit away from disagreeing;
the venue rejects that pairing, and the failure arrives as a signed request the
bot believed was correct. Deriving one from the other makes the mismatch
unrepresentable -- the same reasoning that makes `_signed_query` return a
string rather than a dict.

`OrderPlan.__post_init__` enforces `LIMIT` if and only if `0 < price_limit < 1`,
so a limit order with no price cannot be constructed, let alone sent.

`Quote` gains `action`, `order_type`, `price_limit` and `amount_in`.
`place_order` reads the order type **off the quote** instead of taking it as an
argument, so the quote and the order it executes cannot describe different
trades.

### Pricing: the reservation price

The limit price is the answer to "what is the most I would pay / the least I
would accept, given what the model believes right now". Both come from
inverting gates that already exist.

`breakeven_probability(price, fee)` is strictly increasing in price, so it
inverts exactly. One new helper, in the style of `max_price_for_return`:

```python
def price_for_breakeven(b: float, fee_bps: int) -> float:
    """Exact inverse of breakeven_probability."""
    n = 1.0 - fee_bps / 10_000.0
    return b * n / (1.0 - b + b * n)
```

**Buy reservation price** -- the highest price at which every entry gate still
clears:

```
B  = min(model_prob - min_edge,  model_prob / (1 + min_edge_ratio))
p* = min(price_for_breakeven(B, fee),                    # clears_edge
         max_price_for_return(min_win_return, fee),      # clears_return
         max_entry_price)
```

Below `min_entry_price`, no order is posted.

**Sell reservation price** -- the mirror. Holding a share is worth `model_prob`;
selling at `p` nets `p(1-f)`. Sell only where the market overpays by the
profile's own edge bar:

```
p* = max(model_prob + min_edge,  model_prob * (1 + min_edge_ratio)) / (1 - f)
```

At `p* >= 1` no sale can clear the bar, and no exit order is posted.

Two properties follow, and both are the point of deriving rather than
configuring:

- **Passive and marketable are the same formula.** `p*` sits below the ask when
  the market is fairly priced and above it when the market is mispriced our
  way. Nothing selects between them.
- **Every profile is already tuned for this.** `min_edge`, `min_edge_ratio`,
  `min_win_return` and the entry band are per-profile numbers that exist today.
  A profile flipping to `LIMIT` inherits its own risk appetite automatically.

### Components

**`PredictionClient`**

| Method | Change |
|---|---|
| `get_quote(rnd, plan)` | replaces `(rnd, side, stake)`. Sends `orderType`, `side=plan.action`, and `priceLimit` as a plain decimal snapped to `rnd.decimal_precision` |
| quote consistency check | becomes direction-aware. On `BUY`, `shares x price ~ usdt_in`; on `SELL` it inverts to `usdt_out ~ shares_in x price`. The current check rejects every sell |
| `place_order(rnd, quote, stake_usdt=None)` | signature unchanged; derives `orderType`, `timeInForce`, `priceLimit` from the quote |
| `active_orders(market_id=None)` | new, `GET order/list` |
| `cancel_orders(order_ids)` | new, `POST trade/batch-cancel` |
| `order_state(order_id)` | new. Returns `RESTING / PARTIAL / FILLED / DEAD` plus the filled amount and price |
| `confirm_fill` | untouched. It raises when an order is not filled, which is right for `FOK` and wrong for `GTC` |
| `_parse_asks` -> `_parse_levels(payload, key)` | one parser, two keys |
| `bids_for(rnd, side)` | new, mirrors `asks_for` |
| `_signed_query` | JSON-encodes a value that is a list of dicts |

`_signed_query` needs care. `@binance/common` serialises **every** array and
object parameter as JSON before URL-encoding it; the bot serialises with
`doseq=True`, which sends `tokenIds=a&tokenIds=b`. `cancelInfoList` is an array
of objects, and `doseq` would send a Python dict repr for it -- a guaranteed
failure. So list-of-dict values are JSON-encoded to match the connector, and
flat lists keep `doseq`. **Redemption's wire format is deliberately unchanged**:
whether `tokenIds` should also be JSON is a real open question, but it is a
question about a working money path and does not belong in this change.

**`ws_feeds`**

`derive_bids(asks, bids, side)`, mirroring `derive_asks`. The pushed book is
market-level and UP-oriented, so UP bids are `bids` and DOWN bids are
`1 - asks`. `BookFeed.bids()` and `MarketData.bids()` follow the same
health-and-validation gating as `asks()`; nothing new is trusted.

**`Trader`**

```python
@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    rnd: Round
    plan: OrderPlan
    posted_ms: int
    expires_at_ms: int          # when the authorising window closes
    filled_usdt: float
    filled_shares: float
    trade_id: int | None
```

`expires_at_ms` is computed **at post time** from whichever window authorised
the order, not recomputed later from a global. The straddle and last-minute
strategies have their own windows (`straddle_entry_window_s`,
`last_minute_start_s`), and an order must expire against the rule that let it
exist. Exit orders are bounded by round end alone.

`self._pending: dict[str, PendingOrder]`, reaped by `_reap_pending()` at the
**top** of the loop, before `_settle_open`. A fill has to become a position
before its round is allowed to settle, or the position settles as though it
were never opened.

One pass of the reaper, per order:

1. Read `order_state`.
2. Book any fill not yet booked -- create the position, or extend it.
3. If `FILLED` or `DEAD`, drop it.
4. If still resting and `now_ms >= expires_at_ms` or `now_ms >= rnd.end_ms`,
   cancel, then **re-read state** and book whatever that shows.

### Entry window

`entry_window_end_s` binds limit orders. A resting entry order is cancelled once
the round passes the window that authorised it, so a bid posted at 150s left
cannot fill at 8s left -- an entry the profile's window was written to refuse.
This is the reason `expires_at_ms` is carried per order rather than derived.

## Configuration

Three fields, all enums, no prices:

| Field | Default | Values |
|---|---|---|
| `entry_order_type` | `"MARKET"` | `MARKET`, `LIMIT` |
| `exit_order_type` | `"NONE"` | `NONE`, `MARKET`, `LIMIT` |
| `exit_trigger` | `"RESTING"` | `RESTING`, `POLLED` -- inert while exit is `NONE` |

`exit_order_type: "NONE"` is today's behaviour: hold to settlement, redeem.
Every existing profile keeps it.

Validation rejects `RESTING` + `MARKET`. A market order cannot rest, and
coercing it silently would mean the config says one thing and the bot does
another.

A new `maker` profile uses `LIMIT` entry so the feature has a configuration
that can actually be run, rather than only a code path that can be tested.

## Exits

- **`RESTING`** -- the sell is posted the moment the entry fills, at `p*`
  computed then. The venue does the waiting.
- **`POLLED`** -- `p*` is recomputed each loop against the live bid, and the
  order is sent only once the bid crosses it.

A filled sell closes the journal row from **sale proceeds**, with
`settle_source="sold"`. The position leaves `_positions`, never reaches
`settled_outcome`, and is never redeemed -- there is no winning token to claim.
A partial sell reduces the position and leaves the remainder to settle
normally.

`diagnose()` excludes sold rows from its calibration buckets. Those buckets
answer one question -- did the price paid predict the outcome -- and a trade
closed before the outcome existed has no answer to contribute. Counting it as
one would corrupt the single number this bot exists to produce. Sold rows are
reported separately, as their own line.

## Journal

Four columns, added through the existing `ALTER TABLE` migration idiom so
journals written before this change stay readable:

`order_type TEXT`, `price_limit REAL`, `exit_price REAL`, `exit_order_id TEXT`.

`settle_source` gains the value `"sold"`.

## Paper mode

Paper and live share one lifecycle. A simulated limit `BUY` fills when the best
ask is at or below its price, a limit `SELL` when the best bid is at or above
it, sized by the depth available at that level -- so partial fills, resting and
cancellation all happen in paper too. A paper mode where limit orders fill
instantly would test nothing that matters.

## Failure behaviour

| Condition | Behaviour |
|---|---|
| `place_order` returns an id, order never appears | reaped as `RESTING` until its window closes, then cancelled. No position recorded |
| Cancel returns the order under `failed` | state is re-read; a fill found there is booked |
| `order_state` unreachable (network) | order stays pending, retried next loop. Never assumed dead |
| Shutdown or halt with orders resting | `_drain` cancels all pending orders first, then waits out positions |
| Partial fill, remainder cancelled | position recorded at the filled size and price |
| Sell fills for more than the position holds | rejected as impossible; logged loudly rather than netted |

## Testing

No network, ever. `FakeClient` grows a real resting-order book so limit
behaviour is exercised end to end.

| Class | Covers |
|---|---|
| `TestOrderPlan` | `LIMIT` without a price is unconstructible; `time_in_force` pairing |
| `TestLimitQuoting` | `priceLimit` is a plain decimal, not wei; `SELL` consistency check inverts |
| `TestPendingOrderLifecycle` | resting, expiry by window, expiry by round end, drain |
| `TestPartialFills` | non-zero fill always recorded; `min_fill_fraction` not consulted |
| `TestCancelRacesFill` | `failed` cancel whose order actually filled becomes a position |
| `TestLimitExits` | `RESTING` and `POLLED`; `settle_source="sold"`; sold rows excluded from calibration |
| `TestReservationPrice` | `price_for_breakeven` inverts exactly; `p*` clears every gate |
| `TestMakerProfile` | the profile's declared values, and that no other profile changed |

`conformance.py` gains `order_list` -> `QueryActiveOrders*` and `batch_cancel`
-> `BatchCancelOrders*`, so both new calls are schema-checked like every other.

`fuzz.py` gains invariants for reservation pricing and partial-fill accounting.

## Known risk

**Adverse selection.** A resting bid fills when someone is willing to sell to
it, which correlates with the market moving against it. The bot's edge is
measured against the model, not against the fill, so a maker strategy can show
a good edge and a bad P&L at the same time. The `maker` profile's journal rows
carry `order_type`, so `diagnose()` can be asked whether limit fills calibrate
differently from market fills. Until that data exists, this is an open question
and not a claim.

**Unfilled rounds.** A passive bid that never fills is a round not traded. That
is not free -- it is the cost of not paying the spread -- and the existing
missed-round tally already counts it.

## Not in scope

- Repricing or chasing a resting order.
- Changing `tokenIds` serialisation in `batch_redeem`.
- Selling into the opposite outcome (buying DOWN to flatten UP) rather than
  selling the token held.
- Making any existing profile use limit orders.
