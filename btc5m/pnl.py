"""What a settled trade paid."""

from __future__ import annotations

from btc5m.pricing import win_return

def wins_per_loss(price: float) -> float:
    """How many wins at this price it takes to cover one full-stake loss."""
    if not 0.0 < price < 1.0:
        raise ValueError("price must be in (0, 1)")
    return price / (1.0 - price)


def settle_pnl(stake: float, fill_price: float, won: bool,
               fee_bps: int) -> float:
    """Realised P&L for one resolved contract (paper mode)."""
    if not 0.0 < fill_price < 1.0:
        raise ValueError("fill_price must be in (0, 1)")
    if not won:
        return -stake
    return stake * win_return(fill_price, fee_bps)


def straddle_worst_case_pnl(stake_up: float, stake_down: float,
                            price_up: float, price_down: float,
                            fee_bps: int) -> float:
    """
    Worst-case P&L across the two possible outcomes of buying both sides
    of one round: `stake_up` at `price_up`, `stake_down` at `price_down`.

    Whichever side wins pays back stake/price minus the venue fee; the
    other stake is lost outright. This is the number the straddle profile
    lives or dies on -- if it is negative, the round is a guaranteed loss
    no matter which way it settles, and no amount of hoping fixes that.
    """
    if not (0.0 < price_up < 1.0 and 0.0 < price_down < 1.0):
        raise ValueError("prices must be in (0, 1)")
    if stake_up < 0 or stake_down < 0:
        raise ValueError("stakes must be non-negative")
    pnl_if_up = settle_pnl(stake_up, price_up, True, fee_bps) - stake_down
    pnl_if_down = settle_pnl(stake_down, price_down, True, fee_bps) - stake_up
    return min(pnl_if_up, pnl_if_down)
