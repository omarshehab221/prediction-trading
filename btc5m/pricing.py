"""What a 5m up/down contract is worth, and which prices clear a cost."""

from __future__ import annotations

import math

from btc5m.stats import norm_cdf, standardised_t_cdf

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config import Config

def buffer_sigmas(spot: float, strike: float, sigma_annual: float,
                  seconds_left: float) -> float:
    """
    Distance from the strike in standard deviations of the time remaining.

    Positive means spot is above the strike. This is the quantity a manual
    trader is estimating when they say "big buffer and not much time left":
    the buffer is the numerator, the time is the denominator, and only the
    ratio decides the bet. A 20 bps buffer is ordinary with four minutes to
    run and close to decisive with thirty seconds.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("prices must be positive")
    if sigma_annual <= 0:
        raise ValueError("volatility must be positive")
    if seconds_left <= 0:
        return math.inf if spot > strike else -math.inf

    denom = sigma_annual * math.sqrt(seconds_left / (365.0 * 24.0 * 3600.0))
    if denom <= 1e-12:
        return math.inf if spot > strike else -math.inf
    return math.log(spot / strike) / denom


def digital_up_probability(spot: float, strike: float, sigma_annual: float,
                           seconds_left: float,
                           tail_df: float | None = None) -> float:
    """
    P(spot finishes above strike), driftless, with optional fat tails.

    Drift is omitted: over <=300s it is dwarfed by diffusion, and estimating it
    would inject more variance than the term is worth.

    `tail_df` selects a variance-matched Student-t instead of a Gaussian. This
    matters enormously for cheap out-of-the-money contracts: real BTC returns
    have heavy tails, so a Gaussian materially understates the probability of
    the large move a lottery-ticket contract needs. Pricing those with a
    Gaussian makes them look like bad bets when they are not.

    Preconditions: spot>0, strike>0, sigma_annual>0, tail_df>2 if given.
    Postcondition: result in [0.0, 1.0].
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("prices must be positive")
    if sigma_annual <= 0:
        raise ValueError("volatility must be positive")
    if seconds_left <= 0:
        # Exact ties resolve DOWN here. The venue settles on its own oracle
        # and its tie rule is not published, so this only ever applies to the
        # local fallback path -- venue settlement always takes precedence.
        return 1.0 if spot > strike else 0.0

    t_years = seconds_left / (365.0 * 24.0 * 3600.0)
    denom = sigma_annual * math.sqrt(t_years)
    if denom <= 1e-12:
        return 1.0 if spot > strike else 0.0

    z = math.log(spot / strike) / denom
    return norm_cdf(z) if tail_df is None else standardised_t_cdf(z, tail_df)


def breakeven_probability(price: float, fee_bps: int) -> float:
    """
    Minimum true probability at which buying at `price` is not -EV.

    Per unit staked: win -> (1-price)/price * (1-fee) profit; lose -> -1.
    Solving EV=0 gives price / (price + (1-price)(1-fee)). Exact.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be in (0, 1)")
    f = fee_bps / 10_000.0
    return price / (price + (1.0 - price) * (1.0 - f))


def win_return(price: float, fee_bps: int) -> float:
    """
    Net profit per unit staked if a contract bought at `price` wins.

    This is the number that decides how many wins one loss costs, and it is
    NOT what the edge test measures. A trade can carry a large probability
    edge and still return 6% on a win, in which case a single loss undoes
    sixteen of them. Both questions have to be asked separately.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be in (0, 1)")
    return (1.0 - price) / price * (1.0 - fee_bps / 10_000.0)


def max_price_for_return(min_return: float, fee_bps: int) -> float:
    """
    Highest fill price whose win still pays at least `min_return` per unit.

    Inverts win_return, so the ceiling always tracks the market's own fee
    instead of being a hardcoded number that is wrong on every market whose
    fee differs from the one it was written for. Returns 1.0 when no floor is
    asked for, which is the same as no ceiling.
    """
    if min_return <= 0:
        return 1.0
    net = 1.0 - fee_bps / 10_000.0
    if net <= 0:
        return 0.0                      # fee eats the entire payout
    return net / (net + min_return)


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
    "do not trade this round". It is NOT 0.0, and a caller that treats it as
    a number posts a bid at zero.
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


def bracket_prices(fill_price: float, fee_bps: int, take_profit: float,
                   stop_loss: float) -> tuple[float, float] | None:
    """
    The two exit prices that turn a fill into +take_profit / -stop_loss.

    These are P&L targets, not price moves, and keeping that distinction is
    the whole reason this is one function rather than two expressions at the
    call sites. A stake S filled at p holds n = S/p shares; selling them at q
    nets n*q*(1-f), so

        pnl / S = q(1-f)/p - 1

    and inverting for a target r gives q = p(1+r)/(1-f). The fee is paid on
    the way out either way, so it pushes BOTH prices up -- which is what
    makes the bracket asymmetric in price even when it is symmetric in money.

    Returns None when the pair is not tradable:

    * the take-profit lands at or above 1.00, where no counterparty exists
      because the contract cannot pay more than 1;
    * the stop lands at or above the fill, which happens when the fee alone
      exceeds the loss being capped -- the position would then be cut on the
      tick it opened, at a loss larger than the one it was protecting.

    None is not an error and not a zero. It means this fill cannot carry this
    bracket, so the caller must not open the position at all.
    """
    if not 0.0 < fill_price < 1.0:
        raise ValueError("fill_price must be in (0, 1)")
    if not 0.0 < take_profit < 1.0 or not 0.0 < stop_loss < 1.0:
        raise ValueError("take_profit and stop_loss must be in (0, 1)")
    net = 1.0 - fee_bps / 10_000.0
    if net <= 0:
        return None                     # the fee eats the entire payout
    tp = fill_price * (1.0 + take_profit) / net
    stop = fill_price * (1.0 - stop_loss) / net
    if tp >= 1.0 or not 0.0 < stop < fill_price:
        return None
    return tp, stop


def signal_edge_required(fee_bps: int, take_profit: float,
                         stop_loss: float) -> float | None:
    """
    How much better than a coin flip this bracket needs, in hit-rate points.

    THE NUMBER THIS PROFILE LIVES OR DIES ON, so it is computed rather than
    assumed. Two rates matter and they are not the same:

    * the hit rate at which the bracket breaks even in money, which is
      stop/(take_profit+stop) and is 0.50 for a symmetric 5/5;
    * the hit rate a DRIFTLESS market would deliver, which is d/(u+d) where
      u and d are the price moves the two legs actually require.

    At a zero fee those coincide and "more right than wrong" is exactly the
    bar. At 200 bps the target needs a 7.14% rise while the stop fires after
    a 3.06% fall, so a market with no opinion at all hits the stop 70% of the
    time -- and the signal has to make up the whole 0.20 difference before
    the first unit of profit exists.

    Returns None when no bracket is tradable at this fee, which is the same
    answer bracket_prices gives and for the same reason.
    """
    if not 0.0 < take_profit < 1.0 or not 0.0 < stop_loss < 1.0:
        raise ValueError("take_profit and stop_loss must be in (0, 1)")
    net = 1.0 - fee_bps / 10_000.0
    if net <= 0:
        return None
    up = (1.0 + take_profit) / net - 1.0        # price rise the target needs
    down = 1.0 - (1.0 - stop_loss) / net        # price fall the stop allows
    if up <= 0 or down <= 0:
        # down <= 0 means the fee alone exceeds the loss being capped. There
        # is no bracket here to require an edge OF -- the position is stopped
        # out the moment it opens.
        return None
    breakeven = stop_loss / (take_profit + stop_loss)
    return breakeven - down / (up + down)
