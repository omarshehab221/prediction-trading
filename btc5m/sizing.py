"""
How much to stake: fractional Kelly, what the book can absorb, and
the caps that keep a blended average honest.
"""

from __future__ import annotations

import math

from btc5m.constants import EPS
from btc5m.pricing import breakeven_probability

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config import Config

def kelly_stake(bankroll: float, model_prob: float, price: float,
                cfg: Config, fee_bps: int | None = None) -> float:
    """
    Fractional-Kelly stake, with a bounded override for small accounts.

    Never exceeds cfg.max_stake_pct of bankroll -- that cap is what makes ruin
    impossible rather than merely unlikely.

    THE SMALL-ACCOUNT PROBLEM
    -------------------------
    On a small balance the Kelly stake falls below the venue's minimum order
    size: at 6.64 USDT a quarter-Kelly bet is about 0.40-0.90 USDT against a
    1.00 minimum. Returning 0 there means the bot never trades at all, silently.

    So when there is genuine positive edge but Kelly sizes below the minimum,
    the minimum is used instead -- deliberately over-betting relative to Kelly.
    That is only safe within a hard limit: expected log growth g(f) is zero at
    f = 0 and again near f = 2*full_kelly, and NEGATIVE beyond it. Past twice
    full Kelly you lose money in the long run even with a real edge. So the
    override applies only while the forced fraction stays under 2x full Kelly
    and under cfg.hard_max_stake_pct; otherwise it declines the trade.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be in (0, 1)")
    if not 0.0 <= model_prob <= 1.0:
        raise ValueError("model_prob must be a probability")
    if bankroll <= 0:
        return 0.0

    f = (cfg.fee_bps if fee_bps is None else fee_bps) / 10_000.0
    b = ((1.0 - price) / price) * (1.0 - f)
    if b <= 0:
        return 0.0

    full_kelly = (model_prob * b - (1.0 - model_prob)) / b
    if full_kelly <= 0:
        return 0.0                      # no edge: never override

    stake = bankroll * min(full_kelly * cfg.kelly_fraction, cfg.max_stake_pct)
    if stake >= cfg.min_stake_usdt:
        return stake

    if cfg.hybrid:
        # Asked for: the hybrid profile trades a balance whose stake fraction
        # sizes under the venue minimum, at the minimum, even where that is
        # past the hard cap and 2x Kelly. The caller logs the over-bet.
        return cfg.min_stake_usdt if bankroll >= cfg.min_stake_usdt else 0.0

    if not cfg.round_up_to_minimum:
        return 0.0

    forced_fraction = cfg.min_stake_usdt / bankroll
    if forced_fraction > cfg.hard_max_stake_pct:
        return 0.0                      # minimum is too big a bite
    if forced_fraction > 2.0 * full_kelly:
        return 0.0                      # past 2x Kelly: negative log growth
    return cfg.min_stake_usdt


def kelly_multiple(stake: float, bankroll: float, model_prob: float,
                   price: float, fee_bps: int) -> float | None:
    """How many times the full-Kelly fraction a stake represents. None if no edge."""
    if bankroll <= 0 or not 0.0 < price < 1.0:
        return None
    b = ((1.0 - price) / price) * (1.0 - fee_bps / 10_000.0)
    if b <= 0:
        return None
    full_kelly = (model_prob * b - (1.0 - model_prob)) / b
    if full_kelly <= 0:
        return None
    return (stake / bankroll) / full_kelly


def walk_book(asks: list[tuple[float, float]], stake_usdt: float
              ) -> float | None:
    """
    Average fill price for spending `stake_usdt` against sorted ask levels.

    Used for pre-quote screening and for paper mode. In live mode the venue's
    own quote is authoritative. Returns None if depth is insufficient.
    """
    if stake_usdt <= 0:
        raise ValueError("stake must be positive")

    spent = shares = 0.0
    for price, size in asks:
        # A share priced at or above 1 cannot profit; below 0 is nonsense.
        if not 0.0 < price < 1.0 or size <= 0:
            continue
        remaining = stake_usdt - spent
        if price * size >= remaining:
            shares += remaining / price
            spent = stake_usdt
            break
        spent += price * size
        shares += size

    if spent < stake_usdt - EPS or shares <= 0:
        return None
    return stake_usdt / shares


def boosted_stake(stake: float, bankroll: float, model_prob: float,
                  price: float, cfg: Config, fee_bps: int) -> float:
    """
    Size up while the trend is confirmed, without leaving the limits.

    The multiplier is applied to the Kelly stake and then clipped by both the
    hard stake cap and twice full Kelly. The second clip is the one that
    matters: expected log growth is negative beyond 2x full Kelly, so a
    trend-driven multiplier with no such bound would be a way of converting
    a real edge into a real loss on a long enough run.

    The ceiling wins outright, including over the incoming stake. An earlier
    version returned max(stake, min(wanted, ceiling)) so that a boost could
    never SHRINK a position -- which quietly meant an already-oversized stake
    passed through untouched. kelly_stake never produces one, so the two
    readings agree on every real input; where they disagree, the ruin bound
    is the one worth keeping.
    """
    if stake <= 0 or bankroll <= 0 or cfg.trend_stake_multiple <= 1.0:
        return stake
    ceiling = bankroll * cfg.hard_max_stake_pct
    b = ((1.0 - price) / price) * (1.0 - fee_bps / 10_000.0)
    if b > 0:
        full_kelly = (model_prob * b - (1.0 - model_prob)) / b
        if full_kelly > 0:
            ceiling = min(ceiling, bankroll * 2.0 * full_kelly)
    boosted = min(stake * cfg.trend_stake_multiple, ceiling)
    if cfg.hybrid:
        # The ceiling is the ruin bound on the BOOST. Below a floored stake
        # it would cut the order under the venue minimum, which is not a
        # smaller bet but no bet.
        return max(boosted, stake)
    return boosted


def max_topup_within_blend(committed: float, avg_price: float,
                           topup_price: float, cap: float) -> float:
    """
    Largest top-up that keeps the blended fill price at or under `cap`.

    Solving (S+t)/(S/p0 + t/p1) <= cap for t gives
        t <= S * (cap/p0 - 1) / (1 - cap/p1)

    When the top-up price is already at or below the cap the denominator is
    non-positive, meaning no amount of buying can push the blend over it, so
    the size is unbounded here and only Kelly limits it. Returns 0.0 when the
    position is already at or above the cap.
    """
    if not 0.0 < avg_price < 1.0 or not 0.0 < topup_price < 1.0:
        raise ValueError("prices must be in (0, 1)")
    if committed <= 0:
        raise ValueError("committed must be positive")
    if avg_price > cap:
        return 0.0                      # already past the ceiling
    if topup_price <= cap:
        return math.inf                 # cannot breach it by buying here

    # Here avg_price <= cap < topup_price, so both terms are positive and
    # the bound is real. (An earlier version guarded on denominator >= 0 and
    # returned "unbounded" for exactly the case that needs bounding.)
    numerator = committed * (cap / avg_price - 1.0)
    denominator = 1.0 - cap / topup_price
    if denominator <= 0:
        return math.inf
    return max(numerator / denominator, 0.0)


def straddle_split(total: float, price_up: float, price_down: float,
                   fee_bps: int) -> tuple[float, float]:
    """
    Divide `total` between the two legs so BOTH payouts come out equal.

    Splitting a straddle 50/50 is the intuitive thing and it is wrong. What
    matters is the PAYOUT each leg returns if it wins -- stake/price, net of
    fee -- not the price paid, and an equal split ties the two payouts to
    the two prices. Buy UP at 0.20 and DOWN at 0.70 with 5 USDT each and the
    UP leg returns 25 while the DOWN leg returns 7.14 against 10 staked: one
    outcome pays handsomely, the other is a guaranteed loss, so the pair is
    a coin flip rather than the hedge it was supposed to be.

    Weighting each leg by the OTHER leg's payout multiple equalises them.
    Because 1/R(p) is exactly breakeven_probability(p, fee), the weights are
    the two breakeven probabilities, and both legs then pay

        total / (be_up + be_down)

    whatever the outcome. That single expression is also the gate: the pair
    returns more than it cost precisely when be_up + be_down < 1, which is
    the fee-adjusted form of "the two prices sum to less than one". Sizing
    this way is what makes an asymmetric pair like 0.20/0.70 tradable at
    all -- 2.22 on UP and 7.78 on DOWN both return 11.11 against 10 staked,
    an 11% locked-in return that the 50/50 split threw away.

    Preconditions: total >= 0, both prices in (0, 1).
    Postcondition: the two stakes sum to `total`.
    """
    if total < 0:
        raise ValueError("total must be non-negative")
    be_up = breakeven_probability(price_up, fee_bps)
    be_down = breakeven_probability(price_down, fee_bps)
    weight = be_up + be_down
    return total * be_up / weight, total * be_down / weight


def straddle_completion_stake(stake_open: float, price_open: float,
                              price_other: float, fee_bps: int,
                              budget: float) -> tuple[float, bool]:
    """
    What to stake on the second leg, and whether it locks the round in.

    One leg is already filled: `stake_open` at `price_open`. Buying `b` of
    the other side at `price_other` makes the round cost stake_open + b, and
    the round is won either way only if BOTH payouts clear that total:

        stake_open / be_open  >  stake_open + b        (the open leg wins)
        b / be_other          >  stake_open + b        (the other leg wins)

    writing be = breakeven_probability(price, fee), which is exactly the
    reciprocal of the gross payout multiple. Those two inequalities bound b
    from above and below, and a b satisfying both exists precisely when

        be_open + be_other < 1

    -- the fee-adjusted "the two prices sum to under one". Crucially the two
    prices are from DIFFERENT moments, so this is a real condition rather
    than the near-impossibility it is within a single order book.

    Setting b = stake_open * be_other / be_open equalises the two payouts,
    which both maximises the guaranteed profit and sits strictly inside the
    feasible band whenever that band exists.

    Returns (stake, guaranteed). `guaranteed` is False when the budget
    cannot reach the band, or when the prices never allowed one -- the stake
    is still returned, because part-hedging an open leg caps a loss that
    would otherwise be the whole position.
    """
    if stake_open <= 0:
        raise ValueError("stake_open must be positive")
    if budget < 0:
        raise ValueError("budget must be non-negative")
    be_open = breakeven_probability(price_open, fee_bps)
    be_other = breakeven_probability(price_other, fee_bps)

    ideal = stake_open * be_other / be_open
    stake = min(ideal, budget)
    # Strict bounds: at either edge a payout merely equals what the round
    # cost, which is capital at risk for nothing.
    floor = (math.inf if be_other >= 1.0
             else stake_open * be_other / (1.0 - be_other))
    ceiling = stake_open * (1.0 - be_open) / be_open
    return stake, floor < stake < ceiling
