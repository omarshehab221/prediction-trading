"""Whether a round is worth entering, and at what size."""

from __future__ import annotations

from dataclasses import dataclass

from btc5m.constants import EPS
from btc5m.domain import Side, Signal, Trend
from btc5m.pricing import (
    breakeven_probability,
    buffer_sigmas,
    digital_up_probability,
    max_price_for_return,
    win_return,
)
from btc5m.sizing import boosted_stake, kelly_stake, walk_book

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config import Config
    from btc5m.domain import Round

def clears_edge(model_prob: float, price: float, cfg: Config,
                fee_bps: int | None = None) -> bool:
    """
    Both thresholds must clear: an absolute floor and a relative margin.

    Absolute alone is inconsistent across the book (0.06 is a 7% margin at
    price 0.90 and a 60% margin at 0.10). Relative alone would wave through
    trades at very low prices where a rounding error looks like an edge.
    """
    breakeven = breakeven_probability(price, cfg.fee_bps if fee_bps is None
                                      else fee_bps)
    if model_prob - breakeven < cfg.min_edge:
        return False
    return model_prob >= breakeven * (1.0 + cfg.min_edge_ratio)


def clears_return(price: float, fee_bps: int, cfg: Config) -> bool:
    """
    Would a win at this price pay enough to be worth the loss it risks?

    Separate from clears_edge on purpose. Edge asks whether the bet is
    priced wrong; this asks whether being right pays. At 0.94 a bet can be
    mispriced by three points -- a large edge -- and still return 6%, so
    sixteen of those wins are undone by one loss. Both have to hold.
    """
    if cfg.min_win_return <= 0:
        return True
    if not 0.0 < price < 1.0:
        return False
    return win_return(price, fee_bps) >= cfg.min_win_return - EPS


def blended_price_cap(cfg: Config, fee_bps: int) -> float:
    """
    Ceiling on the blended fill price across every tranche of one round.

    Two constraints, whichever binds first: the profile's own stated cap,
    and whatever price still clears the minimum return at THIS market's fee.
    Applying only the first would let a top-up drag the blend past the return
    floor that the opening tranche had to satisfy, which is the same money
    lost by a different route.
    """
    return min(cfg.max_blended_price,
               max_price_for_return(cfg.min_win_return, fee_bps))


def entry_window_start_s(cfg: Config, boosted: bool) -> float:
    """
    Earliest seconds-remaining at which an entry may be taken.

    A confirmed trend widens it: when the move has already persisted across
    rounds, the early part of a round is where the price still reflects
    uncertainty the trend has largely resolved. Never past the round length,
    since there is no such thing as entering before the round exists.
    """
    if not boosted:
        return float(cfg.entry_window_start_s)
    return min(float(cfg.round_seconds),
               float(cfg.entry_window_start_s + cfg.trend_early_entry_s))


@dataclass(frozen=True)
class Assessment:
    """
    The outcome of looking at one round: a trade, or the reason there wasn't.

    The reason matters as much as the answer. A bot that declines silently is
    indistinguishable from a bot that is broken, and with a return floor in
    force the two look identical from the outside -- both produce no trades.
    Recording WHICH gate bound turns "it isn't trading" into "it is refusing
    the price, 40 rounds running", which is a fact you can act on.
    """

    signal: Signal | None = None
    blocked_by: str = ""


# Rejection reasons, ordered by how far the round got before being turned
# away. The furthest-progressed reason is the informative one: "no edge" says
# far more about a round than "outside the window", and reporting whichever
# gate happened to fire first would bury it.
_DECLINE_ORDER = (
    "no strike published yet",
    "outside the entry window",
    "buffer too small for the time left",
    "trend does not favour an early entry",
    "price outside the entry band",
    "win pays less than the return floor",
    "edge below the floor",
    "no stake clears the sizing limits",
    "book too thin to fill",
)


def _worse(current: str, candidate: str) -> str:
    """Whichever reason represents getting further into the checks."""
    if not current:
        return candidate
    return max(current, candidate, key=_DECLINE_ORDER.index)


def assess(rnd: Round, spot: float, sigma: float, bankroll: float,
           now_ms: int, cfg: Config,
           ask_book: dict[Side, list[tuple[float, float]]] | None = None,
           tail_df: float | None = None,
           trend: Trend | None = None) -> Assessment:
    """
    Decide whether this round is worth a trade. Pure: no I/O, no mutation.

    Two-pass sizing: size off top-of-book, re-price that stake against the real
    ladder, then re-check the edge at the true average fill. Sizing off a price
    you will not actually get is how a backtest-positive strategy loses money
    live. In live mode this is a screen; the venue quote is authoritative.

    Returns None when nothing clears the edge, price and risk filters.
    """
    if rnd.strike is None:
        return Assessment(blocked_by="no strike published yet")

    trend = trend or Trend()
    confirmed = trend.confirmed(cfg)
    secs = rnd.seconds_remaining(now_ms)
    if not (cfg.entry_window_end_s <= secs
            <= entry_window_start_s(cfg, confirmed)):
        return Assessment(blocked_by="outside the entry window")

    fee_bps = rnd.fee_bps        # the market's published rate, not an assumption
    z = buffer_sigmas(spot, rnd.strike, sigma, secs)
    if cfg.min_buffer_sigmas > 0 and abs(z) < cfg.min_buffer_sigmas:
        return Assessment(blocked_by="buffer too small for the time left")
    p_up = digital_up_probability(spot, rnd.strike, sigma, secs, tail_df)

    best: Signal | None = None
    blocked = ""
    for side, model_prob in ((Side.UP, p_up), (Side.DOWN, 1.0 - p_up)):
        # With a buffer gate, only back the side the buffer actually favours;
        # betting against a large buffer is the opposite of the rule.
        if cfg.min_buffer_sigmas > 0:  # noqa: SIM102
            if (side is Side.UP and z < 0) or (side is Side.DOWN and z > 0):
                continue
        # The widened window is not a general relaxation. It exists only for
        # the side the trend actually points at; taking the other side early
        # would be using the trend as an excuse to trade against it.
        boost = confirmed and trend.favours(side)
        if secs > cfg.entry_window_start_s and not boost:
            blocked = _worse(blocked, "trend does not favour an early entry")
            continue

        levels = (ask_book or {}).get(side)
        entry = (levels[0][0] if levels
                 else min(rnd.quote_for(side) * (1.0 + cfg.assumed_spread_pct),
                          0.999))

        if not (cfg.min_entry_price <= entry <= cfg.max_entry_price):
            blocked = _worse(blocked, "price outside the entry band")
            continue
        if not clears_return(entry, fee_bps, cfg):
            blocked = _worse(blocked, "win pays less than the return floor")
            continue
        if not clears_edge(model_prob, entry, cfg, fee_bps):
            blocked = _worse(blocked, "edge below the floor")
            continue

        stake = kelly_stake(bankroll, model_prob, entry, cfg, fee_bps)
        if stake <= 0:
            blocked = _worse(blocked, "no stake clears the sizing limits")
            continue
        if boost:
            stake = boosted_stake(stake, bankroll, model_prob, entry, cfg,
                                  fee_bps)

        avg = walk_book(levels, stake) if levels else entry
        if avg is None:
            blocked = _worse(blocked, "book too thin to fill")
            continue
        # The venue quotes to its own precision, so a fill price carrying
        # more digits than that is fiction. Snap BEFORE any gate, not merely
        # before the edge is priced.
        #
        # This used to snap between the edge gate and the edge calculation,
        # so the floor was tested against a price that was never going to be
        # the fill. Rounding up raises breakeven, and a market quoting to one
        # decimal could clear a 0.02 floor at an ask of 0.753, fill at 0.80,
        # and book a trade whose real edge was 0.008. The gate said yes to a
        # price nobody was ever going to pay.
        avg = rnd.round_price(avg)
        if not 0.0 < avg < 1.0:
            blocked = _worse(blocked, "price outside the entry band")
            continue
        if not (cfg.min_entry_price <= avg <= cfg.max_entry_price):
            blocked = _worse(blocked, "price outside the entry band")
            continue

        if not clears_edge(model_prob, avg, cfg, fee_bps):
            blocked = _worse(blocked, "edge below the floor")
            continue
        # Re-check on the price actually paid, not the one at the top of the
        # book. Walking the ladder raises the average, and a return floor
        # that only ever saw the best level would let exactly the trades it
        # exists to stop through the moment the book is thin.
        if not clears_return(avg, fee_bps, cfg):
            blocked = _worse(blocked, "win pays less than the return floor")
            continue
        edge = model_prob - breakeven_probability(avg, fee_bps)

        stake = kelly_stake(bankroll, model_prob, avg, cfg, fee_bps)
        if stake <= 0:
            blocked = _worse(blocked, "no stake clears the sizing limits")
            continue
        if boost:
            stake = boosted_stake(stake, bankroll, model_prob, avg, cfg,
                                  fee_bps)

        cand = Signal(side, model_prob, avg, edge, stake, secs, z,
                      trend_z=trend.impulse * (1 if trend.favours(side)
                                               else -1),
                      trend_boosted=boost)
        if best is None or cand.edge > best.edge:
            best = cand

    if best is not None:
        return Assessment(signal=best)
    return Assessment(blocked_by=blocked or "price outside the entry band")
