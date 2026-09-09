# Limit Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `LIMIT` execution on both sides of the book, selectable per profile, alongside the `MARKET` `FOK` buys the bot sends today.

**Architecture:** One value object (`OrderPlan`) describes an order completely; strategy produces one, `PredictionClient` consumes one. The limit price is derived per round by inverting `breakeven_probability`, never configured. A resting order lives on `Trader._pending` and is reaped at the top of every loop, cancelled when the window that authorised it closes or the round ends.

**Tech Stack:** Python 3.12, `requests`, `websocket-client`, `unittest`, SQLite.

**Spec:** `docs/superpowers/specs/2026-09-10-limit-orders-design.md`

## Global Constraints

- Commit straight to `master`. No feature branches on this repo.
- **Never run the full test suite locally** -- 10+ minutes here versus 23s on Render. Run only the named test class in each task. The Docker build is the gate.
- `autoDeploy: true` in `render.yaml`. Every push to `master` restarts the live worker. Push between rounds if the bot is holding.
- Every new `Config` field must be read somewhere, or `coherence.py` fails the build with "declared but never read -- dead setting". Same for every new function in either module.
- Prose style in this repo: comments explain *why*, name the failure the code prevents, and use `--` rather than an em dash. Match it.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```
- Environment for any test run: `BINANCE_API_KEY=build BINANCE_API_SECRET=build`. `Config.__post_init__` raises without them. Use the Bash tool for this syntax, not PowerShell.
- No network in any test. Ever.
- Ground truth for every wire format is `@binance/w3w-prediction@2.1.2`. `priceLimit` is a **plain decimal**, not wei. `timeInForce` is `FOK` for `MARKET` and `GTC` for `LIMIT`, with no third option.

---

### Task 1: Reservation pricing

The limit price is derived, not configured. `breakeven_probability` is strictly increasing in price, so it inverts exactly -- one solve, no search. These three functions are pure arithmetic with no I/O, so they are first and they are cheap to get right.

**Files:**
- Modify: `btc_5m_predictor.py` -- insert after `max_price_for_return`, which ends at line 1974
- Test: `test_btc_5m.py` -- new class after `TestWinReturn` (ends line 5603)

**Interfaces:**
- Consumes: `breakeven_probability`, `max_price_for_return`, `Config`, `EPS` -- all existing.
- Produces:
  - `price_for_breakeven(breakeven: float, fee_bps: int) -> float`
  - `buy_reservation_price(model_prob: float, cfg: Config, fee_bps: int) -> float | None`
  - `sell_reservation_price(model_prob: float, cfg: Config, fee_bps: int) -> float | None`

  Both reservation functions return `None` to mean "no price in the band clears", which callers must not confuse with `0.0`.

- [ ] **Step 1: Write the failing test**

Add to `test_btc_5m.py`, after `TestWinReturn`:

```python
class TestReservationPrice(unittest.TestCase):
    """The limit price is derived from the gates, never written down."""

    def test_price_for_breakeven_inverts_breakeven_probability(self):
        for price in (0.05, 0.25, 0.5, 0.75, 0.95):
            for fee in (0, 50, 200, 1000):
                b = m.breakeven_probability(price, fee)
                self.assertAlmostEqual(m.price_for_breakeven(b, fee), price,
                                       places=9, msg=f"{price} @ {fee}bps")

    def test_price_for_breakeven_rejects_impossible_input(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                m.price_for_breakeven(bad, 200)

    def test_buy_reservation_price_clears_every_gate(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_win_return=0.0,
                max_entry_price=0.90, min_entry_price=0.05)
        p = m.buy_reservation_price(0.70, c, 200)
        self.assertIsNotNone(p)
        self.assertTrue(m.clears_edge(0.70, p - 1e-9, c, 200))
        self.assertLessEqual(p, c.max_entry_price)

    def test_buy_reservation_price_is_the_highest_such_price(self):
        """One tick above it must fail the gate, or it is not a ceiling."""
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_win_return=0.0,
                max_entry_price=0.99, min_entry_price=0.05)
        p = m.buy_reservation_price(0.70, c, 200)
        self.assertFalse(m.clears_edge(0.70, p + 1e-4, c, 200))

    def test_buy_reservation_price_respects_the_return_floor(self):
        c = cfg(min_edge=0.001, min_edge_ratio=0.0, min_win_return=0.50,
                max_entry_price=0.99, min_entry_price=0.05)
        p = m.buy_reservation_price(0.95, c, 200)
        self.assertTrue(m.clears_return(p, 200, c))

    def test_buy_reservation_price_is_none_below_the_band(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_entry_price=0.60,
                max_entry_price=0.90)
        self.assertIsNone(m.buy_reservation_price(0.20, c, 200))

    def test_sell_reservation_price_demands_the_market_overpays(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15)
        p = m.sell_reservation_price(0.50, c, 200)
        self.assertIsNotNone(p)
        # Proceeds net of fee must beat holding by the profile's own bar.
        self.assertGreaterEqual(p * 0.98 - 0.50, c.min_edge - 1e-9)

    def test_sell_reservation_price_is_none_when_no_price_can_clear(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15)
        self.assertIsNone(m.sell_reservation_price(0.97, c, 200))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestReservationPrice -v
```

Expected: FAIL, `AttributeError: module has no attribute 'price_for_breakeven'`.

- [ ] **Step 3: Write the implementation**

In `btc_5m_predictor.py`, immediately after `max_price_for_return` (which ends at line 1974) and before `kelly_stake`:

```python
def price_for_breakeven(breakeven: float, fee_bps: int) -> float:
    """
    The fill price whose breakeven probability is exactly `breakeven`.

    Exact inverse of breakeven_probability, which is strictly increasing in
    price, so this is a solve and not a search. It exists so the limit price
    can be DERIVED from the edge gates rather than written into a profile: a
    written-down price stops agreeing with min_edge the first time min_edge
    is tuned, and nothing reports the disagreement -- the bot simply starts
    bidding at a price its own gates would have refused.
    """
    if not 0.0 < breakeven < 1.0:
        raise ValueError("breakeven must be in (0, 1)")
    net = 1.0 - fee_bps / 10_000.0
    if net <= 0:
        return 0.0                      # fee eats the entire payout
    return breakeven * net / (1.0 - breakeven + breakeven * net)


def buy_reservation_price(model_prob: float, cfg: Config,
                          fee_bps: int) -> float | None:
    """
    Highest price at which every entry gate still clears. None if there is none.

    This is what a limit BUY posts at, and it is the whole reason the design
    needs no "passive or marketable" setting: p* sits below the ask when the
    market is priced fairly and above it when the market is priced wrong our
    way, so one formula produces both behaviours from the state of the book.

    None means no price in the entry band clears, which is the same answer as
    "do not trade this round". It is NOT 0.0, and a caller that treats it as a
    number posts a bid at zero.
    """
    # Invert both halves of clears_edge: model_prob - be >= min_edge, and
    # model_prob >= be * (1 + min_edge_ratio). Whichever binds first wins.
    ceiling = min(model_prob - cfg.min_edge,
                  model_prob / (1.0 + cfg.min_edge_ratio))
    if ceiling <= 0.0:
        return None
    if ceiling >= 1.0:
        # Every price in (0,1) clears the edge test, so only the band and the
        # return floor bind. Calling price_for_breakeven here would raise.
        price = cfg.max_entry_price
    else:
        price = price_for_breakeven(ceiling, fee_bps)
    price = min(price,
                max_price_for_return(cfg.min_win_return, fee_bps),
                cfg.max_entry_price)
    if price < cfg.min_entry_price:
        return None
    return price


def sell_reservation_price(model_prob: float, cfg: Config,
                           fee_bps: int) -> float | None:
    """
    Lowest price at which selling beats holding. None if no such price exists.

    The mirror of buy_reservation_price. Holding a share is worth model_prob,
    because it pays 1 with that probability; selling at p nets p(1-f). So the
    bar is "the market overpays by the same edge we demand when buying",
    which reuses min_edge and min_edge_ratio deliberately -- a second set of
    thresholds could be tuned apart from the first, and then the bot would
    buy on one definition of edge and sell on another.

    None when the bar lands at or above 1.0: no price can clear it, so no
    exit order is posted and the position runs to settlement as before.
    """
    net = 1.0 - fee_bps / 10_000.0
    if net <= 0:
        return None
    floor = max(model_prob + cfg.min_edge,
                model_prob * (1.0 + cfg.min_edge_ratio))
    price = floor / net
    if not 0.0 < price < 1.0:
        return None
    return price
```

- [ ] **Step 4: Run test to verify it passes**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestReservationPrice -v
```

Expected: 8 tests, all PASS.

- [ ] **Step 5: Add the fuzz invariant**

In `fuzz.py`, add after `fuzz_breakeven` (ends line 97):

```python
def fuzz_reservation(rng: random.Random, trials: int) -> None:
    """A reservation price that fails its own gate is worse than no price."""
    for _ in range(trials):
        prob = rng.uniform(0.02, 0.98)
        fee = rng.choice([0, 50, 200, 500, 1000])
        c = cfg(min_edge=rng.uniform(0.005, 0.10),
                min_edge_ratio=rng.uniform(0.0, 0.5),
                min_entry_price=0.02, max_entry_price=0.98,
                min_win_return=rng.choice([0.0, 0.10, 0.25]))
        buy = m.buy_reservation_price(prob, c, fee)
        if buy is not None:
            check("buy reservation in band",
                  c.min_entry_price <= buy <= c.max_entry_price,
                  f"{buy} outside [{c.min_entry_price}, {c.max_entry_price}]")
            check("buy reservation clears the return floor",
                  m.clears_return(buy, fee, c), f"{buy} at {fee}bps")
        sell = m.sell_reservation_price(prob, c, fee)
        if sell is not None:
            check("sell reservation beats holding",
                  sell * (1.0 - fee / 10_000.0) > prob,
                  f"{sell} nets less than holding {prob}")
```

Register it in `main()` alongside the other `fuzz_*` calls, following the pattern already there.

- [ ] **Step 6: Run fuzz and coherence**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: both exit 0. Coherence must not report the three new functions as dead -- `fuzz.py` is not in the corpus, so the *tests* are what prove they are called. If coherence complains, that means no production caller exists yet; that is expected until Task 9, so record the message and move on rather than inventing a caller.

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py fuzz.py
git commit -m "$(cat <<'EOF'
Derive the price to bid rather than writing one down

A limit order needs a price. Putting that price in the profile makes it a
number that stops agreeing with min_edge the first time min_edge is tuned,
and nothing reports the disagreement -- the bot just starts bidding at a
price its own gates would refuse.

breakeven_probability is strictly increasing in price, so it inverts
exactly. The highest price that still clears every entry gate is therefore
a solve, not a judgement, and it moves whenever the gates move.

The same formula covers both behaviours a limit order can have: the
reservation price sits below the ask in a fairly priced market and above it
in one priced wrong our way, so nothing has to choose between posting
passively and posting marketably.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Order value types

`OrderPlan` is the seam. Everything that decides *what* to trade produces one; everything that talks to the venue consumes one. `OrderType.time_in_force` is derived so the venue's pairing rule cannot be split across two call sites.

**Files:**
- Modify: `btc_5m_predictor.py:1498-1563` -- after `Side`, before `Round`; and `Quote` at 1554-1563
- Modify: `btc_5m_predictor.py:5716` -- the one reader of `amount_out_shares`
- Test: `test_btc_5m.py` -- new class after `TestReservationPrice`
- Modify: `test_btc_5m.py:2679` -- the other reader of `amount_out_shares`

**Interfaces:**
- Consumes: `Side` (existing).
- Produces:
  - `Action` -- `str, Enum` with `BUY`, `SELL`
  - `OrderType` -- `str, Enum` with `MARKET`, `LIMIT`, and property `time_in_force -> str`
  - `OrderPlan(side, action, order_type, amount, price_limit=None)` -- frozen
  - `Quote` gains `action`, `order_type`, `price_limit`, `amount_in`, all with defaults, and `amount_out_shares` is renamed to `amount_out`.

  The five existing positional fields of `Quote` keep their order, because `m.Quote("q", 0.6, 8.0, 0.0, 0.0)` appears positionally at nine test sites and must keep working.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestOrderPlan -v
```

Expected: FAIL, `AttributeError: module has no attribute 'OrderType'`.

- [ ] **Step 3: Add the enums and OrderPlan**

In `btc_5m_predictor.py`, immediately after the `Side` enum (ends line 1505) and before `class Round`:

```python
class Action(str, Enum):
    """What the order does to the book. The venue calls this `side`."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"

    @property
    def time_in_force(self) -> str:
        """
        The venue's required pairing, derived rather than written twice.

        place-order rejects MARKET with anything but FOK and LIMIT with
        anything but GTC. Those two strings used to sit as separate literals
        next to the order type, one edit away from disagreeing -- and the
        failure arrives as a signed request the bot believed was correct.
        Deriving one from the other makes the mismatch unrepresentable.
        """
        return "FOK" if self is OrderType.MARKET else "GTC"


@dataclass(frozen=True)
class OrderPlan:
    """
    One order, described completely. Built by strategy, consumed by I/O.

    Exists so the five things that define an order travel together. Passed as
    loose arguments they drift: a caller that computed a limit price and then
    called a MARKET path sends the price nowhere and crosses the spread, and
    nothing in the type system objects.
    """

    side: Side                       # which outcome token
    action: Action
    order_type: OrderType
    amount: float                    # BUY: USDT in.  SELL: shares in.
    price_limit: float | None = None

    def __post_init__(self) -> None:
        if not self.amount > 0:
            raise ValueError(f"amount must be positive, got {self.amount}")
        if self.order_type is OrderType.LIMIT:
            if self.price_limit is None:
                raise ValueError("a LIMIT order requires a price_limit")
            if not 0.0 < self.price_limit < 1.0:
                raise ValueError(
                    f"price_limit must be in (0, 1), got {self.price_limit}")
        elif self.price_limit is not None:
            # Not harmless: it reads as though the price was applied, and the
            # venue ignores it, so the order crosses the spread while the
            # caller believes it is resting.
            raise ValueError("a MARKET order must not carry a price_limit")
```

- [ ] **Step 4: Extend Quote**

Replace `Quote` at `btc_5m_predictor.py:1554-1563` with:

```python
@dataclass(frozen=True)
class Quote:
    """An executable quote from the venue."""

    quote_id: str
    average_price: float
    # Shares on a BUY, USDT on a SELL -- the venue's amountOut means whichever
    # asset the trade produces. Named for what it is rather than for the BUY
    # case, because a field called `shares` holding USDT is exactly the kind
    # of quiet wrongness that survives a green suite.
    amount_out: float
    price_impact: float
    fee_usdt: float
    # What was quoted. place_order reads the type from here rather than
    # taking it as an argument, so a quote and the order executing it cannot
    # describe two different trades.
    action: Action = Action.BUY
    order_type: OrderType = OrderType.MARKET
    price_limit: float | None = None
    amount_in: float = 0.0
```

- [ ] **Step 5: Update the two readers of the old name**

`btc_5m_predictor.py:5716` currently reads:

```python
                LOG.info("Order %s filled at %.4f for %.4f shares", order_id,
                         quote.average_price, quote.amount_out_shares)
```

Change `quote.amount_out_shares` to `quote.amount_out`.

`test_btc_5m.py:2679` currently reads:

```python
        self.assertGreater(q.amount_out_shares, 8.0)
```

Change to `q.amount_out`.

- [ ] **Step 6: Run the new and the affected tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestOrderPlan test_btc_5m.TestQuoteValidation -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Make an order describe itself, so it cannot describe itself wrongly

An order is five things: which token, buy or sell, market or limit, how
much, and at what price. Passed as loose arguments they drift apart -- a
caller that works out a limit price and then takes a MARKET path sends the
price nowhere and crosses the spread, and nothing objects.

OrderPlan carries all five, and refuses the two combinations that are
contradictions rather than mistakes: a LIMIT with no price, and a MARKET
with one.

timeInForce is now derived from the order type instead of sitting beside it
as a second literal. The venue accepts FOK only with MARKET and GTC only
with LIMIT, and a disagreement between those two strings arrives as a
signed request that looked correct on the way out.

Quote's amountOut is renamed for what it holds. It is shares on a buy and
USDT on a sell, and a field called shares holding USDT is the kind of
wrongness that survives a green suite.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Structured query parameters

`batch-cancel` takes `cancelInfoList`, an array of objects. `urlencode(doseq=True)` would send each element's Python `repr` -- `cancelInfoList={'orderId': 'x'}` -- which is a guaranteed rejection. `@binance/common` JSON-encodes every array and object parameter, so structured values go that way.

**Files:**
- Modify: `btc_5m_predictor.py:2735-2765` (`_signed_query`)
- Test: `test_btc_5m.py` -- extend `TestRequestSigning` (line 1802)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_signed_query` now JSON-encodes any parameter value that is a `dict`, or a `list`/`tuple` containing a `dict`. Flat lists are unchanged.

- [ ] **Step 1: Write the failing test**

Add these methods inside the existing `TestRequestSigning` class:

```python
    def test_a_list_of_objects_is_sent_as_json(self):
        """
        doseq would send each element's Python repr, which the venue rejects.

        @binance/common serialises every array and object parameter as JSON,
        and cancelInfoList is an array of objects, so this is the connector's
        format rather than a preference.
        """
        c = m.PredictionClient(cfg())
        c._clock_offset_ms = 0
        query = c._signed_query({"cancelInfoList": [{"orderId": "a"},
                                                    {"orderId": "b"}]})
        self.assertIn("cancelInfoList=%5B%7B%22orderId%22%3A%22a%22%7D%2C"
                      "%7B%22orderId%22%3A%22b%22%7D%5D", query)
        self.assertNotIn("orderId%27", query)      # no Python repr

    def test_a_flat_list_still_uses_doseq(self):
        """
        batch_redeem's tokenIds has always gone this way and is not changed
        here. Whether it SHOULD be JSON is a real question about a working
        money path, and it is not this change's question to answer.
        """
        c = m.PredictionClient(cfg())
        c._clock_offset_ms = 0
        query = c._signed_query({"tokenIds": ["a", "b"]})
        self.assertIn("tokenIds=a&tokenIds=b", query)

    def test_a_dict_value_is_sent_as_json(self):
        c = m.PredictionClient(cfg())
        c._clock_offset_ms = 0
        query = c._signed_query({"meta": {"k": "v"}})
        self.assertIn("meta=%7B%22k%22%3A%22v%22%7D", query)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestRequestSigning -v
```

Expected: FAIL on `test_a_list_of_objects_is_sent_as_json` -- the query contains a Python dict repr.

- [ ] **Step 3: Implement**

In `_signed_query`, after the `p = {k: v for ...}` line and before `p["timestamp"] = ...`, insert:

```python
        # A parameter that is an object, or a list of objects, has no
        # urlencode form: doseq sends each element's Python repr, and
        # "cancelInfoList={'orderId': 'x'}" is a request the venue will
        # always reject. @binance/common serialises every array and object
        # parameter as JSON, so structured values go that way.
        #
        # Flat lists keep doseq. tokenIds has always been sent that way by
        # batch_redeem, and whether it should also be JSON is a live question
        # about a working money path -- not one to answer as a side effect of
        # adding cancellation.
        p = {k: (json.dumps(v, separators=(",", ":"))
                 if isinstance(v, dict)
                 or (isinstance(v, (list, tuple))
                     and any(isinstance(x, dict) for x in v))
                 else v)
             for k, v in p.items()}
```

`json` is already imported at module level (line 66).

- [ ] **Step 4: Run test to verify it passes**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestRequestSigning -v
```

Expected: all PASS, including the pre-existing signing tests.

- [ ] **Step 5: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Send a list of objects as the connector sends one

Cancellation passes cancelInfoList, an array of objects. urlencode with
doseq has no form for that: it sends each element's Python repr, so the
query carries "cancelInfoList={'orderId': 'x'}" and the venue rejects every
cancel the bot ever attempts.

@binance/common serialises every array and object parameter as JSON before
encoding it, so structured values now go that way.

Flat lists deliberately keep doseq. tokenIds has always been sent as
repeated parameters by batch_redeem, and whether it should be JSON instead
is a real question about a path that moves money -- not one to answer as a
side effect of adding cancellation.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Quote and place an order of either type

`get_quote` takes an `OrderPlan` instead of `(side, stake)`. `place_order` reads the order type off the quote. The quote consistency check becomes direction-aware, because on a SELL the input is shares and the output is USDT, and the current check rejects every one of them.

**Files:**
- Modify: `btc_5m_predictor.py:3300-3352` (`get_quote`)
- Modify: `btc_5m_predictor.py:3432-3470` (`place_order`)
- Modify: `btc_5m_predictor.py:3397` (`discover_min_stake`'s internal call)
- Modify: `btc_5m_predictor.py:5064, 5169, 5277, 5480, 5658, 5904, 6075` (Trader call sites)
- Modify: `btc_5m_predictor.py:6615, 6718` (`preflight`, `discover_min`)
- Test: `test_btc_5m.py` -- new class; and update the call sites listed in Step 5

**Interfaces:**
- Consumes: `OrderPlan`, `Action`, `OrderType`, `Quote` from Task 2.
- Produces:
  - `PredictionClient.get_quote(rnd: Round, plan: OrderPlan) -> Quote`
  - `PredictionClient.place_order(rnd: Round, quote: Quote, stake_usdt: float | None = None) -> str` -- signature unchanged, behaviour extended
  - `PredictionClient._price_param(rnd: Round, price: float) -> str` -- formats a limit price for the wire

- [ ] **Step 1: Write the failing test**

```python
class TestLimitQuoting(unittest.TestCase):
    """The wire format for a limit order, checked against the connector."""

    def _client(self):
        c = m.PredictionClient(cfg())
        c._wallet = m.WalletRef("0xabc", "w1")
        c._clock_offset_ms = 0
        c.sent = []

        def fake_request(name, params=None):
            c.sent.append((name, dict(params or {})))
            if name == "get_quote":
                return {"quoteId": "q1", "averagePrice": "0.40",
                        "amountOut": m.to_wei(12.5), "priceImpact": 0.0,
                        "feeAmount": m.to_wei(0.0)}
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
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.MARKET, amount=5.0)
        c.get_quote(make_round(), plan)
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
        self.assertEqual(q.amount_in, 5.0)

    def test_place_order_pairs_gtc_with_limit(self):
        c = self._client()
        q = m.Quote("q1", 0.40, 12.5, 0.0, 0.0, action=m.Action.BUY,
                    order_type=m.OrderType.LIMIT, price_limit=0.40,
                    amount_in=5.0)
        c.place_order(make_round(), q, 5.0)
        sent = dict(c.sent)["place_order"]
        self.assertEqual((sent["orderType"], sent["timeInForce"]),
                         ("LIMIT", "GTC"))
        self.assertEqual(float(sent["priceLimit"]), 0.40)

    def test_place_order_still_pairs_fok_with_market(self):
        c = self._client()
        q = m.Quote("q1", 0.40, 12.5, 0.0, 0.0)
        c.place_order(make_round(), q, 5.0)
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
        c = self._client()

        def sell_request(name, params=None):
            c.sent.append((name, dict(params or {})))
            # 10 shares in at 0.50 => 5 USDT out. Internally consistent.
            return {"quoteId": "q1", "averagePrice": "0.50",
                    "amountOut": m.to_wei(5.0), "priceImpact": 0.0,
                    "feeAmount": m.to_wei(0.0)}

        c._request = sell_request
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.50)
        q = c.get_quote(make_round(), plan)
        self.assertEqual(q.amount_out, 5.0)

    def test_an_inconsistent_sell_quote_is_still_rejected(self):
        c = self._client()

        def bad_request(name, params=None):
            # 10 shares at 0.50 cannot produce 40 USDT.
            return {"quoteId": "q1", "averagePrice": "0.50",
                    "amountOut": m.to_wei(40.0), "priceImpact": 0.0,
                    "feeAmount": m.to_wei(0.0)}

        c._request = bad_request
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.50)
        with self.assertRaises(m.ApiError):
            c.get_quote(make_round(), plan)
```

`make_round()` is the existing module-level helper in `test_btc_5m.py`; do not redefine it.

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestLimitQuoting -v
```

Expected: FAIL -- `get_quote` takes three arguments.

- [ ] **Step 3: Rewrite get_quote**

Replace the body of `get_quote` (`btc_5m_predictor.py:3300-3352`). The docstring, the `quoteId`/`averagePrice` guards, the `0 < avg < 1` check and the `priceImpact`/`feeAmount` handling all stay; what changes is the parameters, the amountOut naming and the consistency check:

```python
    def _price_param(self, rnd: Round, price: float) -> str:
        """
        A limit price formatted for the wire.

        Plain decimal, snapped to the market's own precision. NOT wei:
        amountIn's doc comment says wei explicitly and priceLimit's says only
        "must be > 0", so passing it through to_wei sends a price of roughly
        1e18 on a market whose prices live in (0, 1).
        """
        return f"{rnd.round_price(price):.{rnd.decimal_precision}f}"

    def get_quote(self, rnd: Round, plan: OrderPlan) -> Quote:
        """
        Phase 1 of trading: ask the venue to price the trade.

        Returns the authoritative average fill price, so no local book-walking
        estimate is needed once we are live.

        Takes a plan rather than loose arguments because the order type, the
        side and the price have to agree, and only a value that carries all
        three can be checked for that.
        """
        params = {
            "walletAddress": self.wallet().address,
            "tokenId": rnd.token_for(plan.side),
            "side": plan.action.value,
            "amountIn": to_wei(plan.amount),
            "orderType": plan.order_type.value,
            "slippageBps": self.effective_slippage_bps(rnd),
            "chainId": rnd.chain_id,
            "feeRateBps": rnd.fee_bps,
            "fundingSource": self.resolved_funding_source()}
        if plan.price_limit is not None:
            params["priceLimit"] = self._price_param(rnd, plan.price_limit)
        payload = self._request("get_quote", params)

        quote_id = payload.get("quoteId")
        avg = payload.get("averagePrice")
        if not quote_id or avg is None:
            raise ApiError(f"malformed quote response: {payload}")

        avg_f = float(avg)
        if not 0.0 < avg_f < 1.0:
            raise ApiError(f"quote returned implausible price {avg_f}")

        out_raw = payload.get("amountOut")
        if out_raw is None:
            raise ApiError(f"quote omits amountOut: {payload}")
        amount_out = float(from_wei(out_raw))
        if amount_out <= 0:
            raise ApiError(f"quote returned {amount_out} out for "
                           f"{plan.amount:.2f} in")

        # Cross-check: the two amounts and the price must reconcile, or
        # averagePrice and amountOut describe different things and every
        # downstream calculation is wrong.
        #
        # The direction matters. A BUY puts USDT in and takes shares out, so
        # shares x price should equal the USDT. A SELL puts shares in and
        # takes USDT out, so the same product should equal the amount OUT.
        # Checking a sell the buy way rejects every well-formed sell quote.
        if plan.action is Action.BUY:
            implied, reference = amount_out * avg_f, plan.amount
        else:
            implied, reference = plan.amount * avg_f, amount_out
        tol = self._cfg.quote_consistency_tolerance
        if reference > 0 and abs(implied - reference) / reference > tol:
            raise ApiError(
                f"quote is internally inconsistent: {plan.action.value} of "
                f"{plan.amount:.4f} at {avg_f:.4f} implies {implied:.4f}, "
                f"not {reference:.4f}")

        impact_raw = payload.get("priceImpact")
        fee_raw = payload.get("feeAmount")
        return Quote(
            quote_id=str(quote_id),
            average_price=avg_f,
            amount_out=amount_out,
            # A missing impact is unknown, not zero; treat it as the worst
            # case so the caller's impact guard cannot be bypassed.
            price_impact=(float("inf") if impact_raw is None
                          else float(impact_raw)),
            fee_usdt=0.0 if fee_raw is None else float(from_wei(fee_raw)),
            action=plan.action,
            order_type=plan.order_type,
            price_limit=plan.price_limit,
            amount_in=plan.amount)
```

- [ ] **Step 4: Rewrite place_order's parameter block**

In `place_order`, replace the `params` dict and add the price. The docstring's first paragraph also needs correcting, because MARKET-is-FOK is no longer the whole story:

```python
        params = {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "quoteId": quote.quote_id,
            # Derived from the quote's own order type, so the pairing the
            # venue enforces cannot be split across two call sites.
            "timeInForce": quote.order_type.time_in_force,
            "accountType": account,
            "orderType": quote.order_type.value,
            "slippageBps": self.effective_slippage_bps(rnd),
            "fundingSource": funding,
        }
        if quote.price_limit is not None:
            params["priceLimit"] = self._price_param(rnd, quote.price_limit)
```

Replace the docstring's opening paragraph with:

```
        Phase 2: execute a quote.

        MARKET orders are FOK -- fill-or-kill, so there are no partial fills
        at prices the model never approved. LIMIT orders are GTC and REST:
        the id returned describes an order that may sit on the book for
        minutes, fill in pieces, or never fill at all. Confirming a limit
        order with confirm_fill would raise on the ordinary case; use
        order_state instead.
```

- [ ] **Step 5: Update every call site**

Each of these currently passes `(rnd, side, stake)`. Wrap in a plan. In production code the type comes from config, but that arrives in Task 7 -- for now every call site uses `OrderType.MARKET`, preserving today's behaviour exactly.

In `btc_5m_predictor.py`, replace each `self._client.get_quote(X, Y, Z)` and `self.get_quote(X, Y, Z)` with:

```python
_market_buy(Y, Z)
```

...where `_market_buy` is this new module-level helper, placed immediately after `OrderPlan`:

```python
def _market_buy(side: Side, amount: float) -> OrderPlan:
    """A plain market buy -- what every call site sent before limit orders."""
    return OrderPlan(side=side, action=Action.BUY,
                     order_type=OrderType.MARKET, amount=amount)
```

so, for example, line 5064 becomes:

```python
                quote = self._client.get_quote(raw, _market_buy(side, stake))
```

The complete list, with the argument that becomes `side` and the one that becomes `amount`:

| Line | Call | Becomes |
|---|---|---|
| 3397 | `self.get_quote(rnd, side, amount)` | `self.get_quote(rnd, _market_buy(side, amount))` |
| 5064 | `self._client.get_quote(raw, side, stake)` | `..., _market_buy(side, stake))` |
| 5169 | `self._client.get_quote(raw, side, stake)` | `..., _market_buy(side, stake))` |
| 5277 | `self._client.get_quote(raw, other, stake)` | `..., _market_buy(other, stake))` |
| 5480 | `self._client.get_quote(raw, side, ...)` | `..., _market_buy(side, ...))` |
| 5658 | `self._client.get_quote(rnd, sig.side, sig.stake_usdt)` | `..., _market_buy(sig.side, sig.stake_usdt))` |
| 5904 | `self._client.get_quote(raw, side, stake)` | `..., _market_buy(side, stake))` |
| 6075 | `self._client.get_quote(pos.rnd, pos.signal.side, topup)` | `..., _market_buy(pos.signal.side, topup))` |
| 6615 | `client.get_quote(hydrated[0], Side.UP, cfg.min_stake_usdt)` | `..., _market_buy(Side.UP, cfg.min_stake_usdt))` |
| 6718 | `client.get_quote(rnd, Side.UP, amount)` | `..., _market_buy(Side.UP, amount))` |

- [ ] **Step 6: Update the test doubles**

In `test_btc_5m.py`, these stubs take the old signature and must take the new one:

| Line | Change |
|---|---|
| 896 | `def get_quote(self, rnd, side, stake):` -> `def get_quote(self, rnd, plan):` and use `plan.amount` in place of `stake` |
| 1221 | `lambda r, s, st: m.Quote(...)` -> `lambda r, p: m.Quote(...)` |
| 1237 | same |
| 1251 | same |
| 2271 | `def get_quote(..., side, amount)` -> `(..., plan)`, use `plan.amount` |
| 2427 | same shape as 2271 |
| 2456 | same shape as 2271 |
| 6875 | `return m.Quote("q-" + side.value, price, stake / price, 0.001, 0.0)` -> take `plan`, use `plan.side.value` and `plan.amount` |

- [ ] **Step 7: Run the affected classes**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestLimitQuoting test_btc_5m.TestLiveQuoteGate \
  test_btc_5m.TestMinimumDiscovery test_btc_5m.TestQuoteErrorClassification \
  test_btc_5m.TestQuoteValidation test_btc_5m.TestFundingSourceDerivation \
  test_btc_5m.TestSimulatedSession test_btc_5m.TestStraddleEntry -v
```

Expected: all PASS.

- [ ] **Step 8: Run conformance**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python conformance.py \
  --connector "$HOME/probe/node_modules/@binance/w3w-prediction/dist/index.d.ts"
```

If the connector is not installed, `npm install @binance/w3w-prediction` in a scratch directory first and point `--connector` at it. Expected: exit 0. `get_quote` now builds its params in a variable, which `_resolve_params` already handles.

- [ ] **Step 9: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Quote and place an order of either type

get-quote and place-order-bundle both take an orderType, so the two-phase
flow does not change -- only what it carries. Both now take it from one
OrderPlan, and place-order reads the type off the quote rather than being
told again, so a quote and the order executing it cannot describe two
different trades.

The consistency check had to learn direction. A buy puts USDT in and takes
shares out; a sell does the reverse. Checking a sell the buy way rejects
every well-formed sell quote, which would have looked exactly like a broken
venue.

priceLimit goes on the wire as a plain decimal. amountIn's doc comment says
wei and priceLimit's does not, and passing it through to_wei sends a price
of about 1e18 on a market whose prices live between 0 and 1.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Read and cancel resting orders

`confirm_fill` raises unless an order filled. That is correct for FOK, where "not filled" means killed, and wrong for GTC, where "still resting" is the ordinary answer and raising on it abandons a live order. This adds a state reader that answers instead of raising, plus the two endpoints it needs.

**Files:**
- Modify: `btc_5m_predictor.py:116-135` (`DEFAULT_ENDPOINTS`)
- Modify: `btc_5m_predictor.py` -- new `OrderState` dataclass after `Quote`
- Modify: `btc_5m_predictor.py:3540-3560` -- new methods after `confirm_fill`
- Modify: `conformance.py:36-63` (`ENDPOINT_INTERFACES`)
- Test: `test_btc_5m.py` -- new class

**Interfaces:**
- Consumes: `_signed_query`'s JSON encoding from Task 3.
- Produces:
  - `OrderState(status, filled_usdt, filled_shares, price)` -- frozen; `status` is one of `"RESTING"`, `"PARTIAL"`, `"FILLED"`, `"DEAD"`
  - `PredictionClient.active_orders(market_id: int | None = None) -> list[dict]`
  - `PredictionClient.cancel_orders(order_ids: list[str]) -> tuple[list[str], dict[str, str]]`
  - `PredictionClient.order_state(order_id: str) -> OrderState | None` -- `None` means the venue has no record yet, which is **not** `DEAD`

- [ ] **Step 1: Write the failing test**

```python
class TestOrderStateAndCancel(unittest.TestCase):
    """A GTC order's ordinary answer is 'still resting', not an exception."""

    def _client(self, active=(), history=()):
        c = m.PredictionClient(cfg())
        c._wallet = m.WalletRef("0xabc", "w1")
        c._clock_offset_ms = 0
        c.sent = []

        def fake_request(name, params=None):
            c.sent.append((name, dict(params or {})))
            if name == "order_list":
                return {"orders": list(active)}
            if name == "order_history":
                return {"orders": list(history)}
            if name == "batch_cancel":
                return {"canceled": ["o1"],
                        "failed": [{"orderId": "o2", "reason": "already filled"}]}
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

    def test_cancelling_nothing_makes_no_request(self):
        c = self._client()
        self.assertEqual(c.cancel_orders([]), ([], {}))
        self.assertEqual(c.sent, [])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestOrderStateAndCancel -v
```

Expected: FAIL, `AttributeError: 'PredictionClient' object has no attribute 'order_state'`.

- [ ] **Step 3: Add the two endpoints**

In `DEFAULT_ENDPOINTS`, after the `order_history` line:

```python
    "order_list": ("GET", "/sapi/v1/w3w/wallet/prediction/order/list"),
    "batch_cancel": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/batch-cancel"),
```

- [ ] **Step 4: Add OrderState**

In `btc_5m_predictor.py`, immediately after the `Quote` dataclass:

```python
@dataclass(frozen=True)
class OrderState:
    """
    What the venue says about one order right now.

    Exists because confirm_fill cannot answer this. It raises unless the
    order filled, which is right for FOK -- where "did not fill" means
    killed -- and wrong for GTC, where "still resting" is the ordinary
    answer and raising on it abandons a live order.

    filled_usdt is meaningful in EVERY status, DEAD included: an order
    cancelled after a partial fill is terminal and still holds real shares,
    and dropping the fill because the status is terminal strands them.
    """

    status: str                      # RESTING | PARTIAL | FILLED | DEAD
    filled_usdt: float
    filled_shares: float
    price: float | None
```

- [ ] **Step 5: Add the three methods**

In `PredictionClient`, immediately after `confirm_fill` (ends line 3540):

```python
    def active_orders(self, market_id: int | None = None) -> list[dict]:
        """Orders the venue still considers live."""
        payload = self._request("order_list", {
            "walletAddress": self.wallet().address,
            "l1Category": "crypto",
            "marketId": market_id,
            "limit": self._cfg.settled_history_limit})
        orders = payload.get("orders")
        return list(orders) if isinstance(orders, list) else []

    def cancel_orders(self, order_ids: list[str]
                      ) -> tuple[list[str], dict[str, str]]:
        """
        Ask the venue to retract resting orders.

        Returns (cancelled ids, {id: reason} for the rest). The failures are
        reported and never interpreted here, because the usual reason a
        cancel fails is that the order filled first. A caller that reads
        `failed` as "still resting" walks away from a real position, which
        then settles, wins, and is never claimed because nothing knows it
        exists. The caller re-reads each order's state instead.
        """
        if not order_ids:
            return [], {}
        wallet = self.wallet()
        payload = self._request("batch_cancel", {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "cancelInfoList": [{"orderId": str(o)} for o in order_ids]})
        cancelled = [str(o) for o in (payload.get("canceled") or [])]
        failed = {}
        for entry in payload.get("failed") or []:
            if isinstance(entry, dict) and entry.get("orderId"):
                failed[str(entry["orderId"])] = str(entry.get("reason") or "")
        return cancelled, failed

    def order_state(self, order_id: str) -> OrderState | None:
        """
        Resting, partly filled, filled or dead -- an answer, not an exception.

        Active orders are consulted first: an order that is both live and
        partly filled appears there with its fill, and the history may not
        have caught up. Returns None when the venue has no record of the
        order at all, which is the absence of knowledge and must NOT be read
        as DEAD -- an order the history lags is still out there.
        """
        for order in self.active_orders():
            if str(order.get("orderId")) == str(order_id):
                return self._read_order(order, resting=True)
        found = self.order_fill(order_id)
        if found is None:
            return None
        return self._read_order(found, resting=False)

    def _read_order(self, order: dict, *, resting: bool) -> OrderState:
        """One venue order dict, read into an OrderState."""
        status = str(order.get("status") or "").upper()
        filled_usdt = _as_float_or_none(order.get("filledUsdtAmount")) or 0.0
        filled_shares = _as_float_or_none(order.get("filledShareQty")) or 0.0
        price = _as_float_or_none(order.get("price"))
        if status in self.DEAD_ORDER_STATUSES:
            state = "DEAD"
        elif status in self.FILLED_ORDER_STATUSES:
            state = "FILLED"
        elif filled_usdt > 0:
            state = "PARTIAL"
        else:
            state = "RESTING"
        if not resting and state == "RESTING" and filled_usdt <= 0:
            # In the history with no fill and no terminal status: the venue
            # has it, it has done nothing, and it is still live.
            state = "RESTING"
        return OrderState(state, filled_usdt, filled_shares, price)
```

- [ ] **Step 6: Map the endpoints for conformance**

In `conformance.py`, add to `ENDPOINT_INTERFACES` after the `order_history` entry:

```python
    "order_list": ("QueryActiveOrdersRequest", "QueryActiveOrdersResponse"),
    "batch_cancel": ("BatchCancelOrdersRequest", "BatchCancelOrdersResponse"),
```

- [ ] **Step 7: Run tests and conformance**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestOrderStateAndCancel test_btc_5m.TestEndpointMethods -v
BINANCE_API_KEY=build BINANCE_API_SECRET=build python conformance.py \
  --connector "$HOME/probe/node_modules/@binance/w3w-prediction/dist/index.d.ts"
```

Expected: tests PASS; conformance exit 0 with `order_list` and `batch_cancel` both reporting `OK`.

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py conformance.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Ask what an order is doing instead of demanding it be finished

confirm_fill raises unless the order filled. For a FOK order that is right:
not filled means killed. For a GTC order it is wrong, because "still
resting" is the ordinary answer, and an exception on the ordinary answer
abandons an order that is still live.

order_state answers instead. It reads active orders before history, because
an order that is live and partly filled appears there first, and it returns
None -- not DEAD -- when the venue has no record yet. The history lagging a
placement is the absence of knowledge, and reading it as death walks away
from real money.

A terminal status still carries its fill. An order cancelled after a
partial fill is DEAD and holds real shares, and dropping the fill because
the status is terminal strands them.

Cancellation reports its failures without interpreting them. The usual
reason a cancel fails is that the order filled first, so the caller re-reads
state rather than believing either list.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Bid ladders

Selling needs the price a sale would actually get, which is the bid, not the ask. The venue publishes `bids` in the same shape as `asks`, and `ws_feeds._Book` already stores them -- only `asks()` is exposed.

**Files:**
- Modify: `btc_5m_predictor.py:3237-3283` (`_parse_asks` -> `_parse_levels`) and `3225-3235` (`asks_for`)
- Modify: `ws_feeds.py:326-345` (`derive_asks`), `471-491` (`BookFeed.asks`), `744-749` (`MarketData.asks`)
- Test: `test_btc_5m.py` -- extend `TestParseAsks` (line 432) and `TestBookFeed` (line 8409)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `PredictionClient._parse_levels(payload: dict, key: str) -> list[tuple[float, float]] | None` -- `key` is `"asks"` or `"bids"`
  - `PredictionClient._parse_asks(payload)` -- kept as a thin wrapper, because `TestParseAsks` and `fuzz.py` both call it by name
  - `PredictionClient.bids_for(rnd, side) -> list[tuple[float, float]] | None`
  - `ws_feeds.derive_bids(asks, bids, side) -> list[tuple[float, float]]`
  - `ws_feeds.BookFeed.bids(rnd, side)`, `ws_feeds.MarketData.bids(rnd, side)`

  Bid ladders are sorted **best first**, i.e. descending by price. Ask ladders stay ascending. A caller reading `[0]` gets the touch on either side.

- [ ] **Step 1: Write the failing test**

Add to `TestParseAsks`:

```python
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
```

Add to `TestBookFeed`:

```python
    def test_derive_bids_mirrors_derive_asks(self):
        """A DOWN bid of 0.31 is an UP ask of 0.69, so DOWN bids come from asks."""
        asks = [(0.69, 4.0), (0.72, 1.0)]
        bids = [(0.60, 3.0), (0.55, 2.0)]
        up = ws_feeds.derive_bids(asks, bids, m.Side.UP)
        self.assertEqual(up[0], (0.60, 3.0))
        down = ws_feeds.derive_bids(asks, bids, m.Side.DOWN)
        self.assertAlmostEqual(down[0][0], 0.31)
        self.assertEqual(down[0][1], 4.0)

    def test_bids_are_best_first(self):
        bids = [(0.55, 2.0), (0.60, 3.0)]
        out = ws_feeds.derive_bids([], bids, m.Side.UP)
        self.assertEqual([p for p, _ in out], [0.60, 0.55])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestParseAsks test_btc_5m.TestBookFeed -v
```

Expected: FAIL on `_parse_levels` and `derive_bids` not existing.

- [ ] **Step 3: Generalise the parser**

Rename `_parse_asks` to `_parse_levels`, taking `key` as a second parameter. Inside, replace every `payload.get("asks")` with `payload.get(key)`, the nested lookup `nested.get("asks")` with `nested.get(key)`, and the two warnings' `"Order book 'asks' is..."` / `"Order book: skipped..."` with the key interpolated. Change the final line:

```python
        ordered = sorted(levels)
        # Asks read cheapest-first and bids read dearest-first, so [0] is the
        # touch on either side and no caller has to remember which is which.
        if key == "bids":
            ordered.reverse()
        return ordered or None
```

Then add the wrapper immediately after it:

```python
    @staticmethod
    def _parse_asks(payload: dict) -> list[tuple[float, float]] | None:
        """Ask ladder. Kept by name: TestParseAsks and fuzz.py both call it."""
        return PredictionClient._parse_levels(payload, "asks")
```

Add `bids_for` immediately after `asks_for`:

```python
    def bids_for(self, rnd: Round, side: Side
                 ) -> list[tuple[float, float]] | None:
        """
        Bid ladder for one outcome -- the price a SALE would actually get.

        Pricing an exit off the ask reads the price someone is asking, not
        the price anyone is offering, and on a thin book those are not close.
        """
        try:
            payload = self._request("order_book", {
                "vendor": rnd.vendor, "marketId": rnd.market_id,
                "tokenId": rnd.token_for(side)})
        except ApiError as exc:
            LOG.debug("order book unavailable: %s", exc)
            return None
        return self._parse_levels(payload, "bids")
```

- [ ] **Step 4: Add derive_bids and the feed accessors**

In `ws_feeds.py`, immediately after `derive_asks` (ends line 345):

```python
def derive_bids(asks: list[tuple[float, float]],
                bids: list[tuple[float, float]],
                side) -> list[tuple[float, float]]:
    """
    Per-side BID ladder from one market-level book.

    The mirror of derive_asks, and it inverts the OTHER half of the book. The
    push carries one book oriented on the UP token, so UP's bids are the bids
    as sent, while a DOWN bid of 0.31 is an UP ask of 0.69 -- DOWN's bids
    therefore come from the asks.

    Best first, so [0] is the touch. Reading a bid ladder cheapest-first
    would make the worst price in the book look like the best one available,
    which on an exit is money given away rather than an error raised.
    """
    from btc_5m_predictor import Side
    if side is Side.UP:
        return sorted(bids, reverse=True)
    return sorted(((round(1.0 - price, 10), size) for price, size in asks),
                  reverse=True)
```

In `BookFeed`, immediately after `asks` (ends line 491):

```python
    def bids(self, rnd, side) -> list[tuple[float, float]] | None:
        """
        The bid ladder, or None to say "ask REST".

        Gated on exactly what asks() is gated on -- connection health and the
        once-per-market side-mapping check -- because the bid side is derived
        from the same inference and is wrong in the same way if the mapping
        is transposed.
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
        return derive_bids(book.asks, book.bids, side) or None
```

In `MarketData`, immediately after `asks` (ends line 749):

```python
    def bids(self, rnd, side) -> list[tuple[float, float]] | None:
        if self._cfg.ws_enabled:
            levels = self._book.bids(rnd, side)
            if levels:
                return levels
        return self._client.bids_for(rnd, side)
```

- [ ] **Step 5: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestParseAsks test_btc_5m.TestBookFeed test_btc_5m.TestMarketData \
  test_btc_5m.TestHostilePayloads -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py ws_feeds.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Read the side of the book a sale would actually hit

Everything here has only ever bought, so only the ask ladder was ever
parsed. Pricing an exit off the ask reads the price someone is asking, not
the price anyone is offering, and on a thin book those are not close.

The venue publishes bids in the same shape as asks and the socket already
stores them, so this is one parser taught a second key rather than a second
parser.

Bids come back best-first while asks stay cheapest-first, so [0] is the
touch on either side. A bid ladder read cheapest-first makes the worst
price in the book look like the best one available, and on an exit that is
money given away rather than an error raised.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Configuration surface

Three enums, no prices. Every existing profile keeps today's behaviour, stated rather than implied.

**Files:**
- Modify: `btc_5m_predictor.py:211-216` -- new fields near the entry settings
- Modify: `btc_5m_predictor.py:627-700` (`__post_init__`)
- Modify: `btc_5m_predictor.py:863` (`PROFILES`) -- add `maker`
- Test: `test_btc_5m.py` -- new class

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Config.entry_order_type: str = "MARKET"` -- `MARKET` | `LIMIT`
  - `Config.exit_order_type: str = "NONE"` -- `NONE` | `MARKET` | `LIMIT`
  - `Config.exit_trigger: str = "RESTING"` -- `RESTING` | `POLLED`
  - `PROFILES["maker"]`

- [ ] **Step 1: Write the failing test**

```python
class TestLimitConfig(unittest.TestCase):
    """Three enums, and the one pairing that describes an impossible order."""

    def test_defaults_are_todays_behaviour(self):
        c = cfg()
        self.assertEqual(c.entry_order_type, "MARKET")
        self.assertEqual(c.exit_order_type, "NONE")

    def test_every_existing_profile_still_sends_market_and_never_exits(self):
        for name, prof in m.PROFILES.items():
            if name == "maker":
                continue
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertEqual(c.entry_order_type, "MARKET", name)
            self.assertEqual(c.exit_order_type, "NONE", name)

    def test_unknown_order_types_are_rejected(self):
        for field, bad in (("entry_order_type", "STOP"),
                           ("exit_order_type", "MAYBE"),
                           ("exit_trigger", "SOMETIMES")):
            with self.assertRaises(ValueError, msg=field):
                cfg(**{field: bad})

    def test_a_market_order_cannot_rest(self):
        """
        Coercing this pairing would mean the config says one thing and the
        bot does another, which is worse than refusing to start.
        """
        with self.assertRaises(ValueError):
            cfg(exit_order_type="MARKET", exit_trigger="RESTING")

    def test_a_polled_market_exit_is_allowed(self):
        c = cfg(exit_order_type="MARKET", exit_trigger="POLLED")
        self.assertEqual(c.exit_order_type, "MARKET")

    def test_a_resting_limit_exit_is_allowed(self):
        c = cfg(exit_order_type="LIMIT", exit_trigger="RESTING")
        self.assertEqual(c.exit_trigger, "RESTING")

    def test_the_generated_config_document_carries_the_new_fields(self):
        doc = m.default_config_document()
        for field in ("entry_order_type", "exit_order_type", "exit_trigger"):
            self.assertIn(field, doc["defaults"], field)


class TestMakerProfile(unittest.TestCase):
    """The profile that actually uses limit entry."""

    def test_maker_posts_limit_entries(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["maker"])
        self.assertEqual(c.entry_order_type, "LIMIT")

    def test_maker_leaves_room_for_a_resting_order_to_fill(self):
        """A window that closes immediately posts an order and cancels it."""
        c = Config(api_key="k", api_secret="s", **m.PROFILES["maker"])
        self.assertGreaterEqual(c.entry_window_start_s
                                - c.entry_window_end_s, 120)

    def test_maker_survives_the_shared_profile_checks(self):
        prof = m.PROFILES["maker"]
        for required in ("paper_start_bankroll", "daily_loss_limit_pct",
                         "assumed_spread_pct"):
            self.assertIn(required, prof, required)
        c = Config(api_key="k", api_secret="s", **prof)
        self.assertGreaterEqual(c.daily_loss_limit_pct / c.max_stake_pct, 2.5)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestLimitConfig test_btc_5m.TestMakerProfile -v
```

Expected: FAIL, `Config.__init__() got an unexpected keyword argument`.

- [ ] **Step 3: Add the fields**

In `Config`, immediately after `max_price_impact` (line 211) and before the `--- Timing ---` block:

```python
    # --- Execution ---------------------------------------------------------
    # How an entry reaches the book. MARKET crosses the spread and is
    # fill-or-kill; LIMIT rests at the model's own reservation price and may
    # fill in pieces or not at all.
    #
    # Per profile, because it is a strategy decision and not plumbing: a
    # profile that lives on thin longshots and one that buys favourites late
    # want opposite answers, and one global setting would be wrong for one of
    # them whichever way it was set.
    entry_order_type: str = "MARKET"
    # Whether to leave a position before it settles, and how.
    #
    # NONE is what this bot has always done: hold to settlement and redeem
    # the winning token. Stated rather than implied, so a profile that wants
    # exits has to say so and a profile that does not cannot acquire them by
    # a default changing underneath it.
    exit_order_type: str = "NONE"
    # RESTING posts the sell when the entry fills and lets the venue wait.
    # POLLED recomputes the reservation price each loop and sends only once
    # the bid crosses it. Inert while exit_order_type is NONE.
    exit_trigger: str = "RESTING"
```

- [ ] **Step 4: Add the validation**

In `__post_init__`, after the `assumed_spread_pct` check:

```python
        if self.entry_order_type not in ("MARKET", "LIMIT"):
            raise ValueError(
                f"entry_order_type must be MARKET or LIMIT, got "
                f"{self.entry_order_type!r}")
        if self.exit_order_type not in ("NONE", "MARKET", "LIMIT"):
            raise ValueError(
                f"exit_order_type must be NONE, MARKET or LIMIT, got "
                f"{self.exit_order_type!r}")
        if self.exit_trigger not in ("RESTING", "POLLED"):
            raise ValueError(
                f"exit_trigger must be RESTING or POLLED, got "
                f"{self.exit_trigger!r}")
        if self.exit_order_type == "MARKET" and self.exit_trigger == "RESTING":
            # A market order cannot rest on the book, so this pairing asks
            # for something the venue will not do. Coercing it to POLLED
            # would mean the config says one thing and the bot does another,
            # which is the failure that is found months later in a journal.
            raise ValueError(
                "exit_trigger RESTING requires exit_order_type LIMIT: a "
                "MARKET order cannot rest on the book")
```

- [ ] **Step 5: Add the maker profile**

In `PROFILES`, after the `favorite` entry:

```python
    # Post a resting bid instead of crossing the spread. Every risk number
    # here already exists on the other profiles; what differs is only how the
    # order reaches the book -- and therefore whether the spread is paid.
    #
    # The window is wide and closes early on purpose. A resting order needs
    # time to be hit, and it is cancelled when the window shuts, so a narrow
    # window posts an order and retracts it before anyone could take it.
    #
    # Whether this actually earns more than it misses is an open question.
    # The journal records order_type, so --calibration-report can be asked
    # whether limit fills calibrate differently from market fills, and until
    # that data exists this profile is an experiment rather than a claim.
    "maker": {"max_entry_price": 0.85, "min_entry_price": 0.10,
              "min_edge": 0.04, "min_edge_ratio": 0.15,
              "max_stake_pct": 0.05, "entry_window_start_s": 240,
              "entry_window_end_s": 45, "max_consecutive_losses": 15,
              "daily_loss_limit_pct": 0.20, "assumed_spread_pct": 0.04,
              "kelly_fraction": 0.25,
              "min_liquidity": 0.0, "max_rounds_per_day": 200,
              "paper_start_bankroll": 100.0,
              "min_win_return": 0.0,
              "max_blended_price": 0.80,
              "entry_order_type": "LIMIT",
              "exit_order_type": "NONE",
              "exit_trigger": "RESTING"},
```

- [ ] **Step 6: Run the tests, plus every class that iterates PROFILES**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestLimitConfig test_btc_5m.TestMakerProfile \
  test_btc_5m.TestProfileDefaults test_btc_5m.TestProfileRiskCoherence \
  test_btc_5m.TestConfigValidation test_btc_5m.TestConfigFile \
  test_btc_5m.TestDefaultProfileCoherence test_btc_5m.TestHotReload -v
```

Expected: all PASS. `TestProfileDefaults` and `TestProfileRiskCoherence` both loop over every profile; `maker` must satisfy them without their being changed.

- [ ] **Step 7: Run coherence**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py \
  --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: the three new fields are reported as **dead settings**, because nothing reads them until Task 9. That is correct at this point. Record the message; Task 9 clears it.

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Let a profile say how its orders reach the book

Order type is a strategy decision, not plumbing. A profile living on thin
longshots and one buying favourites in the last minute want opposite
answers, and a single global setting is wrong for one of them whichever way
it is set.

Three enums and no prices. The price a limit order posts at is derived per
round from that round's model probability, so there is nothing here to tune
into disagreement with min_edge.

exit_order_type NONE is what this bot has always done -- hold to settlement,
redeem the winner -- written down rather than assumed, so no profile can
acquire exits because a default moved underneath it.

RESTING with MARKET is refused rather than coerced. A market order cannot
rest, so the pairing asks for something the venue will not do, and quietly
substituting POLLED means the config says one thing while the bot does
another.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Journal columns and sold positions

**Files:**
- Modify: `btc_5m_predictor.py:3664-3717` (`Journal.__init__`, `record`, and a new `resolve_sold`)
- Modify: `btc_5m_predictor.py:3721-3760` (`diagnose`)
- Test: `test_btc_5m.py` -- extend `TestJournal` (line 5517) and `TestDiagnose` (line 3605)

**Interfaces:**
- Consumes: `OrderType` from Task 2.
- Produces:
  - `Journal.record(..., order_id=None, order_type="MARKET", price_limit=None)` -- two new keyword arguments, both defaulted so every existing call site is unchanged
  - `Journal.resolve_sold(trade_id: int, proceeds_usdt: float, exit_price: float, exit_order_id: str, stake: float) -> None`
  - Columns `order_type TEXT`, `price_limit REAL`, `exit_price REAL`, `exit_order_id TEXT`
  - `settle_source` value `"sold"`

- [ ] **Step 1: Write the failing test**

Add to `TestJournal`:

```python
    def test_new_columns_exist(self):
        j = m.Journal(self.path, "p")
        cols = {r[1] for r in
                j._conn.execute("PRAGMA table_info(trades)")}
        for col in ("order_type", "price_limit", "exit_price", "exit_order_id"):
            self.assertIn(col, cols, col)

    def test_a_journal_without_the_columns_is_still_readable(self):
        """A migration that drops old journals discards the calibration record."""
        import sqlite3 as _s
        conn = _s.connect(self.path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts INTEGER)")
        conn.commit()
        conn.close()
        j = m.Journal(self.path, "p")
        cols = {r[1] for r in j._conn.execute("PRAGMA table_info(trades)")}
        self.assertIn("order_type", cols)

    def test_resolve_sold_records_proceeds_and_its_own_source(self):
        j = m.Journal(self.path, "p")
        tid = j.record("LIVE", make_round(), make_signal(), 100.0, 0.5,
                       50.0, "o1")
        j.resolve_sold(tid, proceeds_usdt=6.0, exit_price=0.60,
                       exit_order_id="o2", stake=5.0)
        row = j._conn.execute(
            "SELECT resolved, pnl, settle_source, exit_price, exit_order_id"
            " FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], 1)
        self.assertAlmostEqual(row[1], 1.0)          # 6.0 proceeds - 5.0 stake
        self.assertEqual(row[2], "sold")
        self.assertAlmostEqual(row[3], 0.60)
        self.assertEqual(row[4], "o2")
```

Add to `TestDiagnose`:

```python
    def test_sold_trades_are_excluded_from_the_calibration_buckets(self):
        """
        The buckets ask whether the price paid predicted the outcome. A trade
        closed before the outcome existed has no answer, and counting it as
        one corrupts the only number this bot exists to produce.
        """
        j = m.Journal(self.path, "p")
        for _ in range(30):
            tid = j.record("LIVE", make_round(), make_signal(), 100.0, 0.5,
                           50.0, "o")
            j.resolve(tid, True, 1.0, "venue")
        sold = j.record("LIVE", make_round(), make_signal(), 100.0, 0.5,
                        50.0, "o")
        j.resolve_sold(sold, proceeds_usdt=99.0, exit_price=0.99,
                       exit_order_id="x", stake=1.0)
        report = j.diagnose()
        self.assertIn("Trades analysed : 30", report)
        self.assertIn("sold before settlement", report)
```

`make_signal()` is the existing helper in `test_btc_5m.py`; do not redefine it.

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestJournal test_btc_5m.TestDiagnose -v
```

Expected: FAIL on the missing columns.

- [ ] **Step 3: Add the columns**

In `Journal.__init__`, add the four columns to the `CREATE TABLE` statement -- after `settle_source TEXT` -- and add four migration lines beside the existing ones:

```python
        for col, decl in (("order_type", "TEXT"), ("price_limit", "REAL"),
                          ("exit_price", "REAL"), ("exit_order_id", "TEXT")):
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {decl}")
```

The `CREATE TABLE` gains `order_type TEXT, price_limit REAL, exit_price REAL, exit_order_id TEXT`.

- [ ] **Step 4: Record the order type**

Change `record`'s signature to:

```python
    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None,
               order_type: str = "MARKET",
               price_limit: float | None = None) -> int:
```

Add `order_type` and `price_limit` to the column list, the placeholder list and the values tuple. Both are defaulted, so no existing call site changes.

- [ ] **Step 5: Add resolve_sold**

Immediately after `resolve`:

```python
    def resolve_sold(self, trade_id: int, proceeds_usdt: float,
                     exit_price: float, exit_order_id: str,
                     stake: float) -> None:
        """
        Close a row that was sold rather than settled.

        P&L is proceeds minus stake, and `won` is deliberately left NULL: the
        round's outcome never applied to this position, and writing 0 or 1
        would answer a question the trade did not ask. settle_source records
        which kind of ending this was, so diagnose() can keep the two apart.
        """
        self._conn.execute(
            "UPDATE trades SET resolved=1, pnl=?, settle_source='sold',"
            " exit_price=?, exit_order_id=? WHERE id=?",
            (proceeds_usdt - stake, exit_price, exit_order_id, trade_id))
        self._conn.commit()
```

- [ ] **Step 6: Exclude sold rows from the buckets**

In `diagnose`, change the `where` clause and add the separate line. Replace:

```python
        where = "WHERE resolved=1"
```

with:

```python
        # Sold rows are resolved but have no outcome to be calibrated
        # against: the position was closed before the round decided
        # anything. Pooling them with settled rows would let an exit taken at
        # a good price look like a correct prediction, which is exactly the
        # inference this report exists to make impossible.
        where = ("WHERE resolved=1 AND COALESCE(settle_source, '') != 'sold'")
```

Then, immediately before the `if not rows:` early return, count them for the header:

```python
        sold = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades"
            " WHERE resolved=1 AND settle_source='sold'").fetchone()
```

and add to the `out` list, after the `Trades analysed` line:

```python
        if sold and sold[0]:
            out.insert(1, f"Excluded         : {sold[0]} sold before "
                          f"settlement ({sold[1]:+.2f} USDT)")
```

- [ ] **Step 7: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestJournal test_btc_5m.TestDiagnose test_btc_5m.TestBiasReport \
  test_btc_5m.TestPerProfileReport test_btc_5m.TestPerMarketFeeInDiagnostics -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Keep sold trades out of the calibration record

The price buckets answer one question: did the price paid predict the
outcome. A position sold before the round decided anything has no answer to
contribute, and counting it as one lets a well-timed exit read as a correct
prediction -- which is precisely the inference this report exists to make
impossible.

So sold rows resolve through their own path, carry settle_source 'sold',
and are reported as their own line rather than pooled. `won` stays NULL on
them, because writing 0 or 1 would answer a question the trade never asked.

The four new columns arrive through the same ALTER TABLE migration as every
column before them. A migration that dropped old journals would discard the
calibration record, which is the one artifact here worth more than the code.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: The resting-order lifecycle

The bot acquires a state it has never had: an order that is neither filled nor dead. This is the task the whole plan exists for, and the two traps from the spec live here.

**Files:**
- Modify: `btc_5m_predictor.py` -- new `PendingOrder` after `Position` (line 1671)
- Modify: `btc_5m_predictor.py:4413-4468` (`Trader.__init__`)
- Modify: `btc_5m_predictor.py:4869-4880` (the loop body, before `self._settle_open()`)
- Modify: `btc_5m_predictor.py:5040-5083` (`_place_leg`)
- Modify: `btc_5m_predictor.py:5640-5740` (`_maybe_enter_model`'s entry block)
- Modify: `btc_5m_predictor.py:6323-6332` (`_drain`)
- Test: `test_btc_5m.py` -- three new classes

**Interfaces:**
- Consumes: `OrderPlan`, `OrderType`, `Action` (Task 2); `order_state`, `cancel_orders`, `OrderState` (Task 5); `buy_reservation_price` (Task 1); `Config.entry_order_type` (Task 7); `Journal.record(order_type=..., price_limit=...)` (Task 8).
- Produces:
  - `PendingOrder(order_id, rnd, plan, posted_ms, expires_at_ms, filled_usdt, filled_shares, trade_id)` -- frozen
  - `Trader._pending: dict[str, PendingOrder]`
  - `Trader._reap_pending() -> None`
  - `Trader._post_limit_entry(rnd, sig, spot, sigma, bankroll, mode, expires_at_ms) -> bool`
  - `Trader._cancel_all_pending() -> None`

- [ ] **Step 1: Write the failing tests**

```python
class TestPendingOrderLifecycle(unittest.TestCase):
    """An order that is neither filled nor dead is a state, not an error."""

    def _trader(self, states, active=()):
        t = make_trader()                       # existing helper
        t._client.order_state = lambda oid: states.get(oid)
        t.cancelled = []

        def cancel(ids):
            t.cancelled.extend(ids)
            return list(ids), {}

        t._client.cancel_orders = cancel
        return t

    def test_a_resting_order_survives_a_reap(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertIn("o1", t._pending)
        self.assertEqual(t.cancelled, [])
        self.assertEqual(t._positions, {})

    def test_an_order_the_venue_has_never_heard_of_is_not_dropped(self):
        """None means the history is lagging, not that the order died."""
        t = self._trader({})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertIn("o1", t._pending)

    def test_a_filled_order_becomes_a_position_and_leaves_pending(self):
        t = self._trader({"o1": m.OrderState("FILLED", 5.0, 12.5, 0.40)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertNotIn("o1", t._pending)
        self.assertEqual(len(t._positions), 1)

    def test_the_window_closing_cancels_the_order(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=-1)
        t._reap_pending()
        self.assertEqual(t.cancelled, ["o1"])
        self.assertNotIn("o1", t._pending)

    def test_round_end_cancels_even_inside_the_window(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=999_999,
                                        round_ended=True)
        t._reap_pending()
        self.assertEqual(t.cancelled, ["o1"])

    def test_drain_cancels_everything_resting(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._drain(timeout_s=0.0)
        self.assertEqual(t.cancelled, ["o1"])


class TestPartialFills(unittest.TestCase):
    """A partial fill cannot be refused: the shares are already ours."""

    def test_a_partial_fill_is_recorded_at_its_real_size(self):
        t = make_trader()
        t._client.order_state = lambda oid: m.OrderState("PARTIAL", 2.0, 5.0,
                                                         0.40)
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        pos = next(iter(t._positions.values()))
        self.assertAlmostEqual(pos.committed_usdt, 2.0)
        self.assertAlmostEqual(pos.signal.fill_price, 0.40)

    def test_min_fill_fraction_is_not_consulted_for_a_limit_order(self):
        """
        It is the FOK guard, where a short fill means something went wrong.
        On a GTC order a short fill is the ordinary outcome, and refusing it
        strands shares the account already holds.
        """
        t = make_trader(min_fill_fraction=0.90)
        t._client.order_state = lambda oid: m.OrderState("PARTIAL", 0.5, 1.25,
                                                         0.40)
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        self.assertEqual(len(t._positions), 1)

    def test_a_further_fill_extends_rather_than_duplicates(self):
        t = make_trader()
        fills = iter([m.OrderState("PARTIAL", 2.0, 5.0, 0.40),
                      m.OrderState("FILLED", 5.0, 12.5, 0.40)])
        t._client.order_state = lambda oid: next(fills)
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        t._reap_pending()
        self.assertEqual(len(t._positions), 1)
        pos = next(iter(t._positions.values()))
        self.assertAlmostEqual(pos.committed_usdt, 5.0)


class TestCancelRacesFill(unittest.TestCase):
    """The usual reason a cancel fails is that the order filled first."""

    def test_a_failed_cancel_whose_order_filled_becomes_a_position(self):
        t = make_trader()
        states = iter([m.OrderState("RESTING", 0.0, 0.0, None),
                       m.OrderState("FILLED", 5.0, 12.5, 0.40)])
        t._client.order_state = lambda oid: next(states)
        t._client.cancel_orders = lambda ids: ([], {"o1": "already filled"})
        t._pending["o1"] = make_pending("o1", expires_in_ms=-1, amount=5.0)
        t._reap_pending()
        self.assertEqual(len(t._positions), 1,
                         "a filled order was abandoned because the cancel "
                         "reported failure")
        self.assertNotIn("o1", t._pending)
```

`make_trader(**overrides)` and `make_pending(order_id, *, expires_in_ms, amount=5.0, round_ended=False)` are new module-level helpers in `test_btc_5m.py`. Write them next to the existing `make_round` / `make_signal` helpers:

```python
def make_trader(**overrides):
    """A Trader wired to FakeClient, with no feeds and no network."""
    import tempfile, os as _os
    path = _os.path.join(tempfile.mkdtemp(), "j.db")
    c = cfg(live=True, db_path=path, ws_enabled=False, **overrides)
    t = m.Trader(c)
    t._client = FakeClient([make_round()], [(0, 100.0)], {}, {})
    t._client.cancel_orders = lambda ids: (list(ids), {})
    t._client.order_state = lambda oid: None
    return t


def make_pending(order_id, *, expires_in_ms, amount=5.0, round_ended=False):
    rnd = make_round()
    now = rnd.end_ms - (0 if round_ended else 120_000)
    return m.PendingOrder(
        order_id=order_id, rnd=rnd,
        plan=m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                         order_type=m.OrderType.LIMIT, amount=amount,
                         price_limit=0.40),
        signal=make_signal(),
        posted_ms=now, expires_at_ms=now + expires_in_ms,
        filled_usdt=0.0, filled_shares=0.0, trade_id=None)
```

`FakeClient.now_ms` must return `make_round().end_ms - 120_000` for these to line up; give `make_trader` a `t._client.now_ms = lambda: make_round().end_ms - 120_000` override, and for `round_ended` cases the test's own pending order carries the later stamp.

- [ ] **Step 2: Run tests to verify they fail**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestPendingOrderLifecycle test_btc_5m.TestPartialFills \
  test_btc_5m.TestCancelRacesFill -v
```

Expected: FAIL, `module has no attribute 'PendingOrder'`.

- [ ] **Step 3: Add PendingOrder**

In `btc_5m_predictor.py`, immediately after the `Position` dataclass:

```python
@dataclass(frozen=True)
class PendingOrder:
    """
    A limit order the venue has accepted and not yet finished with.

    The bot has never had to hold this state. A MARKET FOK order is resolved
    by the time place_order returns -- filled or killed -- so an order and a
    position were the same thing. A GTC order can sit on the book for minutes,
    fill in pieces, and still be live when the round ends.

    expires_at_ms is set when the order is POSTED, from whichever window
    authorised it. The straddle and last-minute strategies run their own
    windows, and an order must expire against the rule that let it exist --
    recomputing an expiry later from a global setting would apply the model
    profile's window to an order the last-minute rule placed.
    """

    order_id: str
    rnd: Round
    plan: OrderPlan
    # The signal this order was posted on. Carried rather than recomputed:
    # by the time a fill arrives the round has moved, and rebuilding the
    # signal then would journal the model's opinion at settlement rather
    # than the opinion the trade was actually taken on.
    signal: Signal
    posted_ms: int
    expires_at_ms: int
    filled_usdt: float
    filled_shares: float
    trade_id: int | None

    @property
    def fill_price(self) -> float:
        """Blended price actually paid so far, or the price we asked for."""
        if self.filled_shares > 0:
            return self.filled_usdt / self.filled_shares
        return self.plan.price_limit or 0.0
```

- [ ] **Step 4: Wire it into the Trader**

In `Trader.__init__`, beside `self._positions`:

```python
        # order_id -> the resting order behind it. Separate from _positions
        # because an order is not a position until something fills: counting
        # one as the other books a trade that may never happen, and not
        # tracking it at all abandons one that did.
        self._pending: dict[str, PendingOrder] = {}
```

In `run`'s loop body, immediately before `self._settle_open()`:

```python
                    # Before settlement, always. A fill has to become a
                    # position before its round is allowed to settle, or the
                    # position settles as though it had never been opened.
                    self._reap_pending()
```

In `_drain`, before the `while self._positions ...` loop:

```python
        # Cancel first, then wait. A resting order left behind on shutdown is
        # live money with nothing tracking it: the process that placed it is
        # gone, so nothing will settle it, claim it, or even record that it
        # exists. Render sends SIGTERM on every deploy, so this is the
        # ordinary path and not the exceptional one.
        self._cancel_all_pending()
```

- [ ] **Step 5: Implement the reaper**

Add to `Trader`, immediately after `_place_leg`:

```python
    def _cancel_all_pending(self) -> None:
        """Retract every resting order, then book whatever filled first."""
        if not self._pending:
            return
        ids = list(self._pending)
        LOG.info("Cancelling %d resting order(s)", len(ids))
        try:
            self._client.cancel_orders(ids)
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Cancel failed for %s: %s", ", ".join(ids), exc)
        # Never trust either list the cancel returned -- see _reap_pending.
        self._reap_pending(force_final=True)

    def _reap_pending(self, force_final: bool = False) -> None:
        """
        Advance every resting order: book fills, retract what has run out.

        Called at the top of the loop, before settlement, because a fill has
        to become a position before its round settles.

        The cancel is a request, not an answer. `batch-cancel` reports an
        order under `failed` most often because it FILLED first, so nothing
        here reads that list: after any cancel the order's state is read
        again and whatever came back is booked. Treating a failed cancel as
        "still resting" walks away from a real position, which then settles,
        wins, and is never claimed because no journal row knows it exists.
        """
        if not self._pending:
            return
        now_ms = self._client.now_ms()
        for order_id, pending in list(self._pending.items()):
            try:
                state = self._client.order_state(order_id)
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("Could not read order %s: %s", order_id, exc)
                continue
            if state is None:
                # The venue has no record yet. That is the absence of
                # knowledge, not death: the order may well be live, and
                # dropping it here strands it.
                continue

            pending = self._book_fill(order_id, pending, state)
            if state.status in ("FILLED", "DEAD"):
                self._pending.pop(order_id, None)
                continue

            expired = (now_ms >= pending.expires_at_ms
                       or now_ms >= pending.rnd.end_ms
                       or force_final)
            if not expired:
                continue

            LOG.info("%s: retracting the %s order (%s)", pending.rnd.slug,
                     pending.plan.side.value,
                     "round over" if now_ms >= pending.rnd.end_ms
                     else "entry window closed")
            try:
                self._client.cancel_orders([order_id])
            except (ApiError, requests.RequestException) as exc:
                LOG.error("Cancel failed for %s: %s", order_id, exc)
                continue
            try:
                final = self._client.order_state(order_id)
            except (ApiError, requests.RequestException) as exc:
                LOG.error("Could not re-read %s after cancel: %s",
                          order_id, exc)
                continue
            if final is not None:
                self._book_fill(order_id, pending, final)
            self._pending.pop(order_id, None)

    def _book_fill(self, order_id: str, pending: PendingOrder,
                   state: OrderState) -> PendingOrder:
        """
        Record whatever this order has filled that is not recorded yet.

        min_fill_fraction is deliberately not consulted. It is the FOK guard,
        where a short fill means something went wrong; on a GTC order a short
        fill is the ordinary outcome, and refusing it strands shares the
        account already holds. Any non-zero fill becomes a position.
        """
        new_usdt = state.filled_usdt - pending.filled_usdt
        if new_usdt <= EPS:
            return pending
        price = state.price or pending.fill_price or pending.plan.price_limit
        key = (pending.rnd.symbol, pending.plan.side)
        existing = self._positions.get(key)
        if existing is None:
            sig = replace(pending.signal,
                          stake_usdt=state.filled_usdt, fill_price=price)
            trade_id = pending.trade_id
            if trade_id is None:
                trade_id = self._journal.record(
                    "LIVE" if self._live else "PAPER", pending.rnd, sig,
                    self._market_data.spot(pending.rnd.symbol),
                    self._vol.sigma_annual(pending.rnd.symbol),
                    self._bankroll(), order_id,
                    order_type=pending.plan.order_type.value,
                    price_limit=pending.plan.price_limit)
            self._positions[key] = Position(trade_id, pending.rnd, sig,
                                            state.filled_usdt, 1)
        else:
            blended = existing.average_price(new_usdt, price)
            self._positions[key] = replace(
                existing,
                signal=replace(existing.signal, fill_price=blended,
                               stake_usdt=state.filled_usdt),
                committed_usdt=state.filled_usdt,
                tranches=existing.tranches + 1)
            trade_id = existing.trade_id
        LOG.info("%s: %s order filled %.4f USDT at %.4f (%.4f of %.4f)",
                 pending.rnd.slug, pending.plan.side.value, new_usdt, price,
                 state.filled_usdt, pending.plan.amount)
        updated = replace(pending, filled_usdt=state.filled_usdt,
                          filled_shares=state.filled_shares,
                          trade_id=trade_id)
        self._pending[order_id] = updated
        return updated
```

- [ ] **Step 6: Post a limit entry**

Add to `Trader`, after `_reap_pending`:

```python
    def _post_limit_entry(self, rnd: Round, sig: Signal, spot: float,
                          sigma: float, bankroll: float, mode: str,
                          expires_at_ms: int) -> bool:
        """
        Post a resting bid at the model's reservation price. True if accepted.

        No position is recorded here. The order is on the book and nothing has
        filled; recording one now books a trade that may never happen, which
        then "settles" and reports a result that was never real.
        """
        price = buy_reservation_price(sig.model_prob, self._cfg, rnd.fee_bps)
        if price is None:
            LOG.info("%s: no price in the band clears the gates; not posting",
                     rnd.slug)
            return False
        plan = OrderPlan(side=sig.side, action=Action.BUY,
                         order_type=OrderType.LIMIT, amount=sig.stake_usdt,
                         price_limit=price)
        try:
            quote = self._client.get_quote(rnd, plan)
            order_id = self._client.place_order(rnd, quote, sig.stake_usdt)
        except (OrderNotFilled, ApiError, requests.RequestException) as exc:
            LOG.warning("%s: limit entry rejected: %s", rnd.slug, exc)
            return False
        self._pending[str(order_id)] = PendingOrder(
            order_id=str(order_id), rnd=rnd, plan=plan,
            signal=replace(sig, fill_price=price),
            posted_ms=self._client.now_ms(), expires_at_ms=expires_at_ms,
            filled_usdt=0.0, filled_shares=0.0, trade_id=None)
        LOG.info("POST %s %s | limit %.4f model %.3f stake %.2f (%.0fs left)",
                 rnd.slug, sig.side.value, price, sig.model_prob,
                 sig.stake_usdt, sig.seconds_left)
        return True
```

In `_maybe_enter_model`, immediately after the `self._watching.pop(...)` line and the scale-in adjustment, before the `order_id = None` block:

```python
            if self._cfg.entry_order_type == "LIMIT":
                # Expiry is fixed here, from the window that authorised THIS
                # order, so a later config change or a different strategy's
                # window cannot retroactively extend or shorten it.
                window_end_ms = rnd.end_ms - int(
                    self._cfg.entry_window_end_s * 1000)
                if self._post_limit_entry(rnd, sig, spot, sigma, bankroll,
                                          mode, window_end_ms):
                    self._seen[rnd.topic_id] = rnd.end_ms
                continue
```

- [ ] **Step 7: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestPendingOrderLifecycle test_btc_5m.TestPartialFills \
  test_btc_5m.TestCancelRacesFill test_btc_5m.TestSimulatedSession \
  test_btc_5m.TestMultiMarket test_btc_5m.TestLoopSurvivesUnexpectedFailures -v
```

Expected: all PASS.

- [ ] **Step 8: Run coherence and fuzz**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py \
  --source btc_5m_predictor.py --source ws_feeds.py
BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Expected: both exit 0. `entry_order_type` is now read, so the dead-setting error from Task 7 clears. `exit_order_type` and `exit_trigger` remain dead until Task 11.

- [ ] **Step 9: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Hold an order that is neither filled nor dead

A MARKET FOK order is resolved by the time place_order returns, so an order
and a position were the same thing here. A GTC order is not: it can sit on
the book for minutes, fill in pieces, and still be live when the round ends.

Two failures this is shaped around.

A cancel races a fill, and batch-cancel reports an order as failed most
often because it filled first. So nothing reads that list. After any cancel
the order's state is read again and whatever came back is booked. Reading a
failed cancel as "still resting" walks away from a real position, which then
settles, wins, and is never claimed because no journal row knows it exists.

min_fill_fraction is not consulted. It is the FOK guard, where a short fill
means something went wrong; on a GTC order a short fill is the ordinary
outcome, and a partial fill cannot be refused anyway because the shares are
already ours. Refusing one strands them.

An order's expiry is fixed when it is posted, from the window that
authorised it. The straddle and last-minute strategies run their own
windows, and recomputing an expiry later would apply one strategy's rule to
another strategy's order.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Resting orders in paper mode

Task 9's `_post_limit_entry` calls the venue unconditionally. `_place_leg` guards every venue call with `if self._live:`; the limit path needs the same, and paper mode has to simulate the whole lifecycle rather than filling instantly. A paper mode where limit orders always fill immediately tests nothing that matters -- it is a market order wearing a different name, and it would report a fill rate the live bot will never achieve.

**Files:**
- Create: nothing
- Modify: `btc_5m_predictor.py` -- new `PaperBook` class before `Trader`, and `Trader.__init__`, `_post_limit_entry`, `_reap_pending`
- Test: `test_btc_5m.py` -- new class

**Interfaces:**
- Consumes: `OrderPlan`, `OrderType`, `Action`, `OrderState`, `PendingOrder` (Tasks 2, 5, 9); `MarketData.asks`/`bids` (Task 6).
- Produces:
  - `PaperBook(market_data)` with `place(plan, rnd) -> str`, `order_state(order_id) -> OrderState | None`, `cancel_orders(order_ids) -> tuple[list[str], dict[str, str]]`. The last two are named exactly as `PredictionClient`'s, which is what lets `_reap_pending` have one body.
  - `Trader._orders` -- the `PredictionClient` in live mode, a `PaperBook` in paper mode. Every order call in `_post_limit_entry`, `_post_exit` and `_reap_pending` goes through `self._orders`, never `self._client`, so the two modes share one lifecycle.

- [ ] **Step 1: Write the failing test**

```python
class TestPaperRestingOrders(unittest.TestCase):
    """Paper and live share one lifecycle, or paper proves nothing."""

    def _book(self, asks=(), bids=()):
        class FakeMarketData:
            def asks(self, rnd, side):
                return list(asks) or None

            def bids(self, rnd, side):
                return list(bids) or None

        return m.PaperBook(FakeMarketData())

    def test_a_bid_below_the_ask_does_not_fill(self):
        book = self._book(asks=[(0.50, 100.0)])
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        oid = book.place(plan, make_round())
        self.assertEqual(book.order_state(oid).status, "RESTING")

    def test_a_bid_at_or_above_the_ask_fills(self):
        book = self._book(asks=[(0.40, 100.0)])
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        oid = book.place(plan, make_round())
        state = book.order_state(oid)
        self.assertEqual(state.status, "FILLED")
        self.assertAlmostEqual(state.filled_usdt, 5.0)
        self.assertAlmostEqual(state.price, 0.40)

    def test_a_thin_book_fills_only_what_is_there(self):
        """Depth is the whole point: a partial fill has to be reachable."""
        book = self._book(asks=[(0.40, 5.0)])       # 5 shares = 2.0 USDT
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        oid = book.place(plan, make_round())
        state = book.order_state(oid)
        self.assertEqual(state.status, "PARTIAL")
        self.assertAlmostEqual(state.filled_usdt, 2.0)

    def test_a_sell_fills_against_the_bid_not_the_ask(self):
        book = self._book(asks=[(0.90, 100.0)], bids=[(0.60, 100.0)])
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.60)
        oid = book.place(plan, make_round())
        self.assertEqual(book.order_state(oid).status, "FILLED")

    def test_a_sell_above_the_bid_rests(self):
        book = self._book(bids=[(0.50, 100.0)])
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=10.0,
                           price_limit=0.60)
        oid = book.place(plan, make_round())
        self.assertEqual(book.order_state(oid).status, "RESTING")

    def test_a_cancelled_paper_order_is_dead_and_keeps_its_fill(self):
        book = self._book(asks=[(0.40, 5.0)])
        plan = m.OrderPlan(side=m.Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=5.0,
                           price_limit=0.40)
        oid = book.place(plan, make_round())
        book.cancel_orders([oid])
        state = book.order_state(oid)
        self.assertEqual(state.status, "DEAD")
        self.assertAlmostEqual(state.filled_usdt, 2.0)

    def test_paper_mode_places_no_real_order(self):
        t = make_trader(live=False, entry_order_type="LIMIT")
        t._client.place_order = lambda *a, **k: self.fail(
            "paper mode reached the venue")
        t._post_limit_entry(make_round(), make_signal(), 100.0, 0.5, 50.0,
                            "PAPER", make_round().end_ms)
        self.assertEqual(len(t._pending), 1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestPaperRestingOrders -v
```

Expected: FAIL, `module has no attribute 'PaperBook'`.

- [ ] **Step 3: Implement PaperBook**

In `btc_5m_predictor.py`, immediately before `class Trader`:

```python
class PaperBook:
    """
    A simulated venue for resting orders, so paper mode tests the real thing.

    Paper mode used to be trivial because a MARKET FOK order either fills at
    the quoted price or does not exist. A GTC order has a life: it rests, it
    fills in pieces as depth appears, and it has to be cancelled. Simulating
    that as an instant full fill would make paper a market order wearing a
    different name, and it would report a fill rate the live bot can never
    reach -- which is worse than not testing it, because it looks like
    evidence.

    Fills are read off the same ladders the live path prices against: a BUY
    fills against asks at or below its limit, a SELL against bids at or above
    it, and only for the depth actually shown. No queue position is modelled;
    that would be a claim about the venue's matching engine that nothing here
    can check.
    """

    def __init__(self, market_data) -> None:
        self._market_data = market_data
        self._orders: dict[str, tuple[OrderPlan, Round, bool]] = {}
        self._counter = itertools.count(1)

    def place(self, plan: OrderPlan, rnd: Round) -> str:
        order_id = f"paper-{next(self._counter)}"
        self._orders[order_id] = (plan, rnd, False)
        return order_id

    def cancel_orders(self, order_ids: list[str]
                      ) -> tuple[list[str], dict[str, str]]:
        cancelled = []
        for order_id in order_ids:
            entry = self._orders.get(order_id)
            if entry is None:
                continue
            self._orders[order_id] = (entry[0], entry[1], True)
            cancelled.append(order_id)
        return cancelled, {}

    def order_state(self, order_id: str) -> OrderState | None:
        entry = self._orders.get(order_id)
        if entry is None:
            return None
        plan, rnd, cancelled = entry
        shares, usdt, price = self._matched(plan, rnd)
        if cancelled:
            # Terminal, and still holding whatever filled before the cancel.
            return OrderState("DEAD", usdt, shares, price)
        if plan.action is Action.BUY:
            done = usdt >= plan.amount - EPS
        else:
            done = shares >= plan.amount - EPS
        if done:
            return OrderState("FILLED", usdt, shares, price)
        if usdt > 0:
            return OrderState("PARTIAL", usdt, shares, price)
        return OrderState("RESTING", 0.0, 0.0, None)

    def _matched(self, plan: OrderPlan,
                 rnd: Round) -> tuple[float, float, float | None]:
        """(shares, usdt, average price) this order would have taken by now."""
        limit = plan.price_limit
        if limit is None:
            return 0.0, 0.0, None
        if plan.action is Action.BUY:
            levels = self._market_data.asks(rnd, plan.side) or []
            crossing = [(p, s) for p, s in levels if p <= limit + EPS]
            budget, shares, spent = plan.amount, 0.0, 0.0
            for price, size in crossing:
                take = min(size, (budget - spent) / price)
                if take <= 0:
                    break
                shares += take
                spent += take * price
            return shares, spent, (spent / shares if shares > 0 else None)
        levels = self._market_data.bids(rnd, plan.side) or []
        crossing = [(p, s) for p, s in levels if p >= limit - EPS]
        shares, proceeds = 0.0, 0.0
        for price, size in crossing:
            take = min(size, plan.amount - shares)
            if take <= 0:
                break
            shares += take
            proceeds += take * price
        return shares, proceeds, (proceeds / shares if shares > 0 else None)
```

- [ ] **Step 4: Route every order call through one seam**

In `Trader.__init__`, after `self._market_data` is built:

```python
        # One seam for orders, so paper and live share the whole lifecycle
        # rather than paper taking a shortcut through it. Selected on the
        # config's mode at construction; _apply_pending_mode swaps it when a
        # deferred mode change lands.
        self._paper_book = PaperBook(self._market_data)
```

Add the property:

```python
    @property
    def _orders(self):
        """The venue in live mode, the simulator in paper mode."""
        return self._client if self._live else self._paper_book
```

In `_post_limit_entry`, replace the live-only quote-and-place block with:

```python
        if self._live:
            try:
                quote = self._client.get_quote(rnd, plan)
                order_id = self._client.place_order(rnd, quote,
                                                    sig.stake_usdt)
            except (OrderNotFilled, ApiError,
                    requests.RequestException) as exc:
                LOG.warning("%s: limit entry rejected: %s", rnd.slug, exc)
                return False
        else:
            order_id = self._paper_book.place(plan, rnd)
```

In `_reap_pending` and `_cancel_all_pending`, replace `self._client.order_state(...)` with `self._orders.order_state(...)` and `self._client.cancel_orders(...)` with `self._orders.cancel_orders(...)`.

`PaperBook.order_state` and `PaperBook.cancel_orders` are named identically to `PredictionClient`'s on purpose: it is what lets `_reap_pending` and `_cancel_all_pending` have exactly one body each, so paper cannot drift into a shortcut through the lifecycle it exists to exercise.

- [ ] **Step 5: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestPaperRestingOrders test_btc_5m.TestPendingOrderLifecycle \
  test_btc_5m.TestPartialFills test_btc_5m.TestCancelRacesFill \
  test_btc_5m.TestModeSwitching test_btc_5m.TestSimulatedSession -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Make paper mode wait like the real thing does

Paper mode was trivial while every order was fill-or-kill: the order either
filled at the quoted price or never existed. A resting order has a life --
it waits, it fills in pieces as depth appears, and it has to be retracted.

Simulating that as an instant full fill would make paper a market order
wearing a different name, and it would report a fill rate the live bot can
never reach. That is worse than not testing it, because it looks like
evidence.

Fills come off the same ladders the live path prices against, for the depth
actually shown. Queue position is deliberately not modelled: that would be a
claim about the venue's matching engine that nothing here can check.

The simulator names its methods exactly as the client does, so the reaper
has one body and paper cannot drift into a shortcut through the lifecycle it
is supposed to be exercising.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Exits

**Files:**
- Modify: `btc_5m_predictor.py` -- new methods on `Trader` after `_post_limit_entry`
- Modify: `btc_5m_predictor.py:6159-6165` (`_settle_one`'s entry) -- skip a position that has been sold
- Modify: `btc_5m_predictor.py` -- the loop body, after `_reap_pending()`
- Test: `test_btc_5m.py` -- new class

**Interfaces:**
- Consumes: `sell_reservation_price` (Task 1); `Journal.resolve_sold` (Task 8); `PendingOrder`, `_reap_pending` and `_book_fill` (Task 9); `PaperBook` and the `self._live` branch pattern (Task 10); `MarketData.bids` (Task 6); `Config.exit_order_type`, `Config.exit_trigger` (Task 7); `digital_up_probability`, `VolatilityEstimator.tail_df` and `.is_clamped` (existing).
- Produces:
  - `Trader._maybe_exit_all() -> None`
  - `Trader._post_exit(pos: Position) -> bool`
  - `_book_fill` learns to close a position when `plan.action is Action.SELL`

- [ ] **Step 1: Write the failing test**

```python
class TestLimitExits(unittest.TestCase):
    """Selling is the only way out that does not wait for the oracle."""

    def test_no_exit_is_posted_when_exits_are_off(self):
        t = make_trader(exit_order_type="NONE")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position()
        t._maybe_exit_all()
        self.assertEqual(t._pending, {})

    def test_a_resting_exit_is_posted_once_the_position_exists(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position()
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)
        pending = next(iter(t._pending.values()))
        self.assertIs(pending.plan.action, m.Action.SELL)
        self.assertIs(pending.plan.order_type, m.OrderType.LIMIT)

    def test_a_resting_exit_is_posted_only_once(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position()
        t._maybe_exit_all()
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)

    def test_a_polled_exit_waits_for_the_bid_to_cross(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="POLLED")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position()
        t._market_data.bids = lambda rnd, side: [(0.01, 100.0)]
        t._maybe_exit_all()
        self.assertEqual(t._pending, {})
        t._market_data.bids = lambda rnd, side: [(0.99, 100.0)]
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)

    def test_a_filled_sell_closes_the_row_from_its_proceeds(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        pos = make_position(committed=5.0, trade_id=7)
        t._positions[("BTCUSDT", m.Side.UP)] = pos
        t._maybe_exit_all()
        oid = next(iter(t._pending))
        t._client.order_state = lambda o: m.OrderState("FILLED", 6.0, 10.0,
                                                       0.60)
        t._reap_pending()
        self.assertEqual(t._positions, {})
        row = t._journal._conn.execute(
            "SELECT pnl, settle_source FROM trades WHERE id=7").fetchone()
        self.assertAlmostEqual(row[0], 1.0)
        self.assertEqual(row[1], "sold")

    def test_a_sold_position_is_never_settled_against_the_oracle(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position(trade_id=7)
        t._maybe_exit_all()
        t._client.order_state = lambda o: m.OrderState("FILLED", 6.0, 10.0,
                                                       0.60)
        t._reap_pending()
        t._settle_open()
        row = t._journal._conn.execute(
            "SELECT settle_source FROM trades WHERE id=7").fetchone()
        self.assertEqual(row[0], "sold")

    def test_a_partial_sell_leaves_the_remainder_to_settle(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position(committed=5.0)
        t._maybe_exit_all()
        t._client.order_state = lambda o: m.OrderState("PARTIAL", 2.0, 3.3,
                                                       0.60)
        t._reap_pending()
        pos = t._positions[("BTCUSDT", m.Side.UP)]
        self.assertAlmostEqual(pos.committed_usdt, 3.0)

    def test_selling_more_than_is_held_is_refused(self):
        t = make_trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", m.Side.UP)] = make_position(committed=5.0)
        t._maybe_exit_all()
        t._client.order_state = lambda o: m.OrderState("FILLED", 99.0, 200.0,
                                                       0.60)
        with self.assertLogs("btc5m", level="ERROR"):
            t._reap_pending()
```

`make_position(committed=5.0, trade_id=1)` is a new module-level helper beside `make_pending`:

```python
def make_position(committed=5.0, trade_id=1):
    return m.Position(trade_id, make_round(), make_signal(), committed, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest test_btc_5m.TestLimitExits -v
```

Expected: FAIL, `Trader has no attribute '_maybe_exit_all'`.

- [ ] **Step 3: Implement the exit path**

Add to `Trader`, after `_post_limit_entry`:

```python
    def _maybe_exit_all(self) -> None:
        """Consider leaving each open position before the round decides it."""
        if self._cfg.exit_order_type == "NONE":
            return
        resting = {p.rnd.topic_id for p in self._pending.values()
                   if p.plan.action is Action.SELL}
        for pos in list(self._positions.values()):
            if pos.rnd.topic_id in resting:
                continue                  # already offered; do not stack
            self._post_exit(pos)

    def _post_exit(self, pos: Position) -> bool:
        """
        Offer this position back to the market. True if an order went out.

        The bar is the sell reservation price: the market must overpay by the
        same edge the entry demanded. Selling for less than the position is
        worth to the model is not an exit, it is a loss taken voluntarily.
        """
        prob = self._model_prob(pos)
        if prob is None:
            return False
        target = sell_reservation_price(prob, self._cfg, pos.rnd.fee_bps)
        if target is None:
            return False

        if self._cfg.exit_trigger == "POLLED":
            bids = self._market_data.bids(pos.rnd, pos.signal.side)
            if not bids or bids[0][0] < target:
                return False
            # The bid is already there, so cross it rather than queue behind it.
            target = bids[0][0]

        shares = pos.committed_usdt / max(pos.signal.fill_price, EPS)
        order_type = (OrderType.LIMIT if self._cfg.exit_order_type == "LIMIT"
                      else OrderType.MARKET)
        plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                         order_type=order_type, amount=shares,
                         price_limit=target if order_type is OrderType.LIMIT
                         else None)
        if self._live:
            try:
                quote = self._client.get_quote(pos.rnd, plan)
                order_id = self._client.place_order(pos.rnd, quote)
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("%s: exit rejected: %s", pos.rnd.slug, exc)
                return False
        else:
            order_id = self._paper_book.place(plan, pos.rnd)
        self._pending[str(order_id)] = PendingOrder(
            order_id=str(order_id), rnd=pos.rnd, plan=plan,
            signal=pos.signal, posted_ms=self._client.now_ms(),
            expires_at_ms=pos.rnd.end_ms, filled_usdt=0.0,
            filled_shares=0.0, trade_id=pos.trade_id)
        LOG.info("OFFER %s %s | %.4f shares at %.4f (entry %.4f)",
                 pos.rnd.slug, pos.signal.side.value, shares, target,
                 pos.signal.fill_price)
        return True

    def _model_prob(self, pos: Position) -> float | None:
        """
        The model's current probability for the side this position holds.

        Follows _maybe_scale_in exactly, including the clamped-sigma refusal:
        an overstated sigma inflates the tail probabilities, and pricing an
        EXIT off an inflated probability holds out for a price that the model
        only believes because its volatility estimate is broken.
        """
        symbol = self._client.market_symbol(pos.rnd.feed_symbol)
        if pos.rnd.strike is None:
            return None
        spot = self._market_data.spot(symbol)
        sigma = self._vol.sigma_annual(symbol)
        if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
            return None
        tail_df = self._vol.tail_df(symbol)
        secs = pos.rnd.seconds_remaining(self._client.now_ms())
        if secs <= 0:
            return None
        p_up = digital_up_probability(spot, pos.rnd.strike, sigma, secs,
                                      tail_df)
        return p_up if pos.signal.side is Side.UP else 1.0 - p_up
```

Note the fifth argument is `tail_df`, not a `Config` -- `digital_up_probability(spot, strike, sigma_annual, seconds_left, tail_df=None)` at `btc_5m_predictor.py:1895`. `_post_exit` must therefore treat a `None` return as "cannot price an exit this pass" and return `False`:

```python
        prob = self._model_prob(pos)
        if prob is None:
            return False
```

replacing the `try/except` sketch in `_post_exit`.

- [ ] **Step 4: Teach _book_fill to close a sold position**

At the top of `_book_fill`, before the buy-side logic:

```python
        if pending.plan.action is Action.SELL:
            return self._book_sale(order_id, pending, state)
```

and add:

```python
    def _book_sale(self, order_id: str, pending: PendingOrder,
                   state: OrderState) -> PendingOrder:
        """
        Reduce or close a position that has been sold back to the market.

        A sold position never reaches settled_outcome and is never redeemed:
        there is no winning token to claim, because the shares are gone. The
        journal row therefore closes from PROCEEDS, and settle_source records
        which kind of ending it was so the calibration buckets can exclude it.
        """
        new_usdt = state.filled_usdt - pending.filled_usdt
        if new_usdt <= EPS:
            return pending
        key = (pending.rnd.symbol, pending.plan.side)
        pos = self._positions.get(key)
        if pos is None:
            LOG.error("%s: a sale filled for %.4f USDT with no position on "
                      "record; the shares are gone and nothing tracked them",
                      pending.rnd.slug, new_usdt)
            return replace(pending, filled_usdt=state.filled_usdt,
                           filled_shares=state.filled_shares)
        price = state.price or pending.plan.price_limit or 0.0
        sold_cost = state.filled_shares * pos.signal.fill_price
        if sold_cost > pos.committed_usdt + EPS:
            # Impossible: more shares came back than the position ever held.
            # Netting it silently would report a profit made from nothing.
            LOG.error("%s: sale of %.4f shares exceeds the %.4f USDT held; "
                      "refusing to net it", pending.rnd.slug,
                      state.filled_shares, pos.committed_usdt)
            return replace(pending, filled_usdt=state.filled_usdt,
                           filled_shares=state.filled_shares)
        remaining = pos.committed_usdt - sold_cost
        if remaining <= EPS:
            self._journal.resolve_sold(pos.trade_id, state.filled_usdt,
                                       price, order_id, pos.committed_usdt)
            self._positions.pop(key, None)
            LOG.info("SOLD %s %s | %.4f USDT at %.4f (entry %.4f)",
                     pending.rnd.slug, pending.plan.side.value,
                     state.filled_usdt, price, pos.signal.fill_price)
        else:
            self._positions[key] = replace(pos, committed_usdt=remaining)
            LOG.info("%s: sold %.4f of %.4f USDT at %.4f; %.4f left to settle",
                     pending.rnd.slug, sold_cost, pos.committed_usdt, price,
                     remaining)
        return replace(pending, filled_usdt=state.filled_usdt,
                       filled_shares=state.filled_shares)
```

- [ ] **Step 5: Call it from the loop**

In `run`, immediately after `self._reap_pending()`:

```python
                    self._maybe_exit_all()
```

- [ ] **Step 6: Run the tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestLimitExits test_btc_5m.TestPendingOrderLifecycle \
  test_btc_5m.TestBalanceReconciliation test_btc_5m.TestRedemption -v
```

Expected: all PASS.

- [ ] **Step 7: Run coherence and fuzz**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py \
  --source btc_5m_predictor.py --source ws_feeds.py
BINANCE_API_KEY=build BINANCE_API_SECRET=build python fuzz.py --trials 400
```

Expected: both exit 0, with no remaining dead settings.

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "$(cat <<'EOF'
Let a position leave before the oracle decides it

Every position here has been held to settlement because there was no other
way out. If the market moves to overpay for a share the account holds, that
money has simply not been available.

The bar for selling is the mirror of the bar for buying: the market must
overpay by the same edge the entry demanded. Reusing min_edge rather than
adding a second threshold matters -- two thresholds can be tuned apart, and
then the bot buys on one definition of edge and sells on another.

A sold position never reaches settled_outcome and is never redeemed, because
the shares are gone and there is no winning token to claim. Its row closes
from proceeds and records that it ended that way, so the calibration buckets
can keep it out.

A sale larger than the position held is refused rather than netted. It
cannot happen, and quietly absorbing it would report a profit made from
nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Documentation

**Files:**
- Modify: `README.md` -- new section after "6b. Feeds" (line 372-430), plus the Files table at line 58
- Test: `test_btc_5m.py` -- `TestCoherenceCorpus` (line 8121) and `TestNoBakedInValues` already read the README; check them

**Interfaces:**
- Consumes: everything.
- Produces: no code.

- [ ] **Step 1: Update the Files table line counts**

```bash
wc -l btc_5m_predictor.py ws_feeds.py test_btc_5m.py conformance.py fuzz.py coherence.py mutate.py
```

Update the `Lines` column in the README's Files table with the real numbers, and the test count if the "512 tests across 78 classes" figure has moved:

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -c "
import unittest
s = unittest.defaultTestLoader.loadTestsFromName('test_btc_5m')
print(s.countTestCases(), 'tests')"
```

- [ ] **Step 2: Add the limit-orders section**

Insert after section 6b, as `## 6c. How orders reach the book`:

```markdown
## 6c. How orders reach the book

Two order types, chosen per profile.

**MARKET** crosses the spread. It is fill-or-kill: it fills completely at
the quoted price or it is killed, so there is never a partial position at a
price the model did not approve. Every profile except `maker` uses it, and
it is what this bot did exclusively before limit orders existed.

**LIMIT** rests on the book. The venue only accepts `GTC` for a limit order
-- there is no IOC -- so it sits there until it fills, or until the bot
retracts it. Three consequences, all of which the bot has to handle and none
of which apply to a market order:

- **It may not fill.** A round can pass with a bid on the book and nothing
  bought. That is counted as a missed round like any other.
- **It may fill in pieces.** A partial fill is the ordinary outcome, not a
  fault, and it cannot be refused: the shares are already yours. So any
  non-zero fill is recorded at its real size and price. `min_fill_fraction`
  does not apply -- it is the fill-or-kill guard.
- **It has to be cancelled.** An order is retracted when the entry window
  that authorised it closes, when the round ends, or on shutdown. There is
  no configurable deadline, and the bot never re-prices a resting order.

### The price it posts at

Nothing configures the limit price. It is computed each round by inverting
the entry gates: the highest price at which `min_edge`, `min_edge_ratio` and
`min_win_return` all still clear, capped by the entry band.

That is deliberate. A price written into a profile stops agreeing with
`min_edge` the first time `min_edge` is tuned, and nothing reports the
disagreement -- the bot simply starts bidding at a price its own gates would
refuse.

It also removes a choice that would otherwise need making. The reservation
price sits below the ask when the market is priced fairly and above it when
the market is priced wrong our way, so the same formula posts a passive bid
in the first case and a marketable one in the second. There is no
"aggressive or patient" setting because there is nothing left for it to
decide.

### Selling

`exit_order_type` is `NONE` by default, which is what this bot has always
done: hold to settlement, then redeem the winning token.

Set to `LIMIT` or `MARKET`, the bot will sell a position back to the market
before the round resolves -- but only when the market overpays by the same
edge the entry demanded. `exit_trigger` picks how: `RESTING` posts the offer
when the entry fills and lets the venue wait; `POLLED` watches the bid and
sends only once it crosses. `RESTING` with `MARKET` is refused at startup,
because a market order cannot rest.

A sold position never settles and is never redeemed -- the shares are gone.
Its journal row closes from the sale proceeds and is marked `sold`, and
`--calibration-report` **excludes sold trades from its price buckets**.
Those buckets ask whether the price paid predicted the outcome, and a trade
closed before the outcome existed has no answer to give. They are reported
separately instead.

### What this costs

A resting bid fills when someone is willing to sell into it, and that
correlates with the market moving against it. The edge here is measured
against the model, not against the fill, so a maker strategy can show a good
edge and a bad P&L at once. The journal records `order_type` on every trade,
so `--calibration-report` can be asked whether limit fills calibrate
differently from market fills. Until that data exists this is an open
question, not a claim.
```

- [ ] **Step 3: Add `maker` to the profiles section**

In section 6, add a row describing `maker` alongside the existing profile descriptions, following their format.

- [ ] **Step 4: Run the doc-reading tests**

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build python -m unittest \
  test_btc_5m.TestCoherenceCorpus test_btc_5m.TestNoBakedInValues \
  test_btc_5m.TestHostingReadiness test_btc_5m.TestDeploymentManifests -v
BINANCE_API_KEY=build BINANCE_API_SECRET=build python coherence.py \
  --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: all PASS, coherence exit 0 with no stale-prose findings.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Say what a resting order does and what it costs

A limit order is not a cheaper market order. It may not fill, it may fill in
pieces, and it has to be retracted -- none of which a fill-or-kill order can
do, and all of which change what a round means.

The section also states the cost rather than only the benefit. A resting bid
fills when someone is willing to sell into it, which correlates with the
market moving the other way, so a maker strategy can show a good edge and a
bad P&L at the same time. The journal records the order type so that can be
measured rather than argued about.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

After Task 12, run the full gate the way the deploy runs it. This is the one place the full suite runs locally, and only because nothing further depends on it finishing quickly:

```bash
BINANCE_API_KEY=build BINANCE_API_SECRET=build bash verify.sh
```

Expected: byte-compile OK, unit tests OK, coherence OK, fuzz OK, schema conformance OK or SKIP. Then push, and watch the Render deploy rather than assuming it.
