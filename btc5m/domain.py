"""
The things the bot talks about: sides, orders, rounds, quotes,
signals, positions and the state each of them carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from btc5m.constants import EPS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config import Config

class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def other(self) -> "Side":
        return Side.DOWN if self is Side.UP else Side.UP


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


def _market_buy(side: Side, amount: float) -> OrderPlan:
    """A plain market buy -- what every call site sent before limit orders."""
    return OrderPlan(side=side, action=Action.BUY,
                     order_type=OrderType.MARKET, amount=amount)


@dataclass(frozen=True)
class Round:
    """A live BTC 5m up/down market. Only ever built by _parse_round."""

    topic_id: int
    market_id: int
    vendor: str
    slug: str
    symbol: str
    start_ms: int
    end_ms: int
    up_token_id: str
    down_token_id: str
    up_quote: float          # indicative, NOT an executable ask
    down_quote: float
    # Everything below is published by the venue per market. None of it is
    # assumed: an assumed chain id, collateral asset or price precision is a
    # silent wrong answer, whereas a missing field is a visible one.
    fee_bps: int
    chain_id: str
    collateral: str
    venue_slippage_bps: int
    decimal_precision: int
    # None means the venue did not publish it -- distinct from a real zero.
    liquidity: float | None
    strike: float | None = None      # variantData.startPrice
    feed_symbol: str | None = None   # oracle the venue resolves against

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def round_price(self, price: float) -> float:
        """Snap to the market's published precision."""
        return round(price, self.decimal_precision)

    def seconds_remaining(self, now_ms: int) -> float:
        return (self.end_ms - now_ms) / 1000.0

    def token_for(self, side: Side) -> str:
        return self.up_token_id if side is Side.UP else self.down_token_id

    def quote_for(self, side: Side) -> float:
        return self.up_quote if side is Side.UP else self.down_quote


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


@dataclass(frozen=True)
class Trend:
    """
    Whether the underlying is running, and whether it has anything left.

    THE MISTAKE THIS IS BUILT TO AVOID
    ----------------------------------
    The obvious detector counts consecutive rounds that closed the same way
    and acts once the count is high enough. That detector is guaranteed to be
    late: by the time three rounds have confirmed a trend, the move it is
    describing is three rounds old, and acting on a trend that has already
    spent itself is close to a guaranteed loss -- the price is at its worst
    exactly when the evidence is at its strongest.

    So the primary signal here is the CURRENT block: `impulse` is the move
    over the last round-length, measured in standard deviations of one such
    block. A thrust that is happening right now scores high even when it is
    the first one, which is what makes an entry at the START of a trend
    possible. The run count is kept, but as corroboration and as a brake
    (see trend_max_run), never as the trigger.

    `decay` is the other half: the current block's size relative to the one
    before it. A trend giving back momentum block over block is dying, and
    `rounds_left` turns that ratio into the only question that actually
    matters -- does this move survive the round I am about to enter?
    """

    direction: int = 0          # +1 up, -1 down, 0 flat or reversing
    impulse: float = 0.0        # current block's move, in sigmas of one block
    z: float = 0.0              # net move over the run, in sigmas of the run
    efficiency: float = 0.0     # net displacement / total distance travelled
    run: int = 0                # consecutive blocks moving in `direction`
    decay: float = 1.0          # current block magnitude / previous block's
    rounds_left: float = 0.0    # projected rounds before it decays into noise
    phase: str = "none"         # none | building | running | fading

    def confirmed(self, cfg: Config) -> bool:
        """
        Is there inertia, is it still alive, and will it outlast this round?

        Four independent ways for this to be false, because a trend fails in
        four different ways and one combined score would hide which:

        * no thrust now (`impulse`) -- whatever happened is over
        * the path wandered (`efficiency`) -- a market swinging across the
          strike covers ground and ends up nowhere
        * it is decaying (`phase`, `rounds_left`) -- entering a move with
          less than a round of life left is buying the exhaustion
        * it has run a long way already (trend_max_run) -- late is expensive
        """
        return (cfg.trend_follow
                and self.direction != 0
                and self.phase in ("building", "running")
                and self.impulse >= cfg.trend_min_impulse
                and self.z >= cfg.trend_min_z
                and self.efficiency >= cfg.trend_min_efficiency
                and cfg.trend_min_run <= self.run <= cfg.trend_max_run
                # "Will it last THIS round?" is the question an entry
                # actually asks, so it is asked in those units.
                and self.rounds_left >= cfg.trend_min_rounds_left)

    def favours(self, side: Side) -> bool:
        return ((side is Side.UP and self.direction > 0)
                or (side is Side.DOWN and self.direction < 0))

    def describe(self) -> str:
        arrow = {1: "UP", -1: "DOWN"}.get(self.direction, "flat")
        return (f"{arrow}/{self.phase} impulse={self.impulse:.2f} "
                f"run={self.run} decay={self.decay:.2f} "
                f"left~{self.rounds_left:.1f}r eff={self.efficiency:.2f}")


@dataclass(frozen=True)
class Signal:
    side: Side
    model_prob: float
    fill_price: float
    edge: float
    stake_usdt: float
    seconds_left: float
    buffer_z: float = 0.0
    # Signed trend strength at entry: positive when the trend pointed the
    # same way as the trade. Recorded so the journal can answer whether the
    # boosted trades actually earned their extra size, rather than leaving
    # that to memory.
    trend_z: float = 0.0
    # True when this entry used the trend's larger stake or earlier window.
    trend_boosted: bool = False


@dataclass(frozen=True)
class Position:
    trade_id: int
    rnd: Round
    signal: Signal
    committed_usdt: float = 0.0      # total staked on this round so far
    tranches: int = 1
    # Shares actually held, when the fill reported them. The buy's fee comes
    # out of the shares received, so committed_usdt / fill_price overstates
    # the holding -- and a sale of that many is refused by the venue as
    # exceeding the shares available.
    shares: float | None = None
    # What sales of part of this position have already made: their proceeds
    # and the cost basis they carried away. committed_usdt is only what is
    # still held, so without these a partial sale's P&L had nowhere to live
    # and vanished when the rest closed.
    sold_proceeds_usdt: float = 0.0
    sold_cost_usdt: float = 0.0

    @property
    def realized_pnl(self) -> float:
        """P&L already made by selling part of the position."""
        return self.sold_proceeds_usdt - self.sold_cost_usdt

    @property
    def held_shares(self) -> float:
        """Shares to sell: the recorded count, else the cost-implied one."""
        if self.shares is not None:
            return self.shares
        return self.committed_usdt / max(self.signal.fill_price, EPS)

    def average_price(self, extra_stake: float, extra_price: float) -> float:
        """Blended fill price after adding another tranche."""
        total = self.committed_usdt + extra_stake
        if total <= 0:
            return extra_price
        shares = (self.committed_usdt / self.signal.fill_price
                  + extra_stake / extra_price)
        return total / shares if shares > 0 else extra_price


@dataclass(frozen=True)
class PendingOrder:
    """
    A limit order the venue has accepted and not yet finished with.

    The bot has never had to hold this state. A MARKET FOK order is resolved
    by the time place_order returns -- filled or killed -- so an order and a
    position were the same thing. A GTC order can sit on the book for
    minutes, fill in pieces, and still be live when the round ends.

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


@dataclass(frozen=True)
class Bracket:
    """
    The two exits attached to one scalp position. Neither is an order.

    Both legs are prices this bot watches. The stop could never be an order:
    there is no conditional order type here, and a SELL limit posted below
    the bid is marketable, so it would close the position at once. The
    take-profit used to rest on the book, but a resting sell holds the
    position's shares, so a stop could not sell until it was cancelled --
    and batch-cancel has never succeeded on this venue.

    entry_price is carried rather than read back off the position because the
    position's own fill price blends across tranches, and this bracket was
    computed from one specific fill.
    """

    entry_price: float
    tp_price: float
    stop_price: float


@dataclass(frozen=True)
class WalletRef:
    address: str
    wallet_id: str
